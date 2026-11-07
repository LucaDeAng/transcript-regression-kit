"""Deterministic checks of an agent's next move against an `expect` block.

The agent output is normalised by the provider to:
    {"text": "...", "tool_calls": [{"name": "...", "args": {...}}]}

Vocabulary (all keys optional):
    calls      tools that must be called; `args` is a subset match
               (string values: case/accent-insensitive "contains")
    no_calls   tool names that must NOT be called ("*" = no tool at all)
    say_any    reply must contain at least one of these strings
    say_all    reply must contain every one of these strings
    say_none   reply must contain none of these strings
    max_words  reply length cap (voice: long answers are a failure)
    escalate   true -> must call transfer_to_human; false -> must not
    rubric     free-text criterion for an LLM judge (not checked here)
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

HANDOFF_TOOL = "transfer_to_human"


@dataclass
class Check:
    name: str
    passed: bool
    reason: str


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def _contains(haystack: str, needle: str) -> bool:
    """Case/accent-insensitive "contains"; symbols that normalise away (e.g. "€") match raw."""
    n = _norm(needle)
    if not n:
        return str(needle).lower() in str(haystack).lower()
    return n in _norm(haystack)


def _args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, want in expected.items():
        if key not in actual:
            return False
        got = actual[key]
        if isinstance(want, str):
            if not _contains(got, want):
                return False
        elif want != got:
            return False
    return True


def parse_output(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            data = {"text": str(raw), "tool_calls": []}
    return {"text": str(data.get("text") or ""), "tool_calls": list(data.get("tool_calls") or [])}


def evaluate(expect: dict[str, Any], output: Any) -> list[Check]:
    out = parse_output(output)
    text, calls = out["text"], out["tool_calls"]
    names = [c.get("name") for c in calls]
    checks: list[Check] = []

    expect = dict(expect)
    if "escalate" in expect:
        calls_wanted = list(expect.get("calls", []))
        banned = list(expect.get("no_calls", []))
        if expect["escalate"] and not any(c["name"] == HANDOFF_TOOL for c in calls_wanted):
            expect["calls"] = calls_wanted + [{"name": HANDOFF_TOOL}]
        elif not expect["escalate"] and HANDOFF_TOOL not in banned:
            expect["no_calls"] = banned + [HANDOFF_TOOL]

    for want in expect.get("calls", []):
        hits = [c for c in calls if c.get("name") == want["name"]]
        ok = any(_args_match(want.get("args", {}), c.get("args") or {}) for c in hits)
        detail = f"called {names or 'nothing'}"
        if hits and not ok:
            detail = f"called {want['name']} with {hits[0].get('args')}, expected args ⊇ {want.get('args')}"
        checks.append(Check(f"calls:{want['name']}", ok, "ok" if ok else detail))

    for banned in expect.get("no_calls", []):
        bad = [n for n in names if banned == "*" or n == banned]
        checks.append(
            Check(f"no_calls:{banned}", not bad, "ok" if not bad else f"unexpected call(s) {bad}")
        )

    if expect.get("say_any"):
        hit = [s for s in expect["say_any"] if _contains(text, s)]
        checks.append(
            Check("say_any", bool(hit), "ok" if hit else f"none of {expect['say_any']} in reply")
        )

    for s in expect.get("say_all", []):
        found = _contains(text, s)
        checks.append(Check(f"say_all:{s}", found, "ok" if found else f"reply does not mention '{s}'"))

    for s in expect.get("say_none", []):
        found = _contains(text, s)
        checks.append(Check(f"say_none:{s}", not found, "ok" if not found else f"reply contains '{s}'"))

    if "max_words" in expect:
        n = len(text.split())
        ok = n <= int(expect["max_words"])
        checks.append(Check("max_words", ok, "ok" if ok else f"{n} words > {expect['max_words']}"))

    return checks
