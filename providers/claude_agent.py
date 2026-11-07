"""The real agent: Claude with the system prompt and tools in agent/.

Point T2R_AGENT_DIR at another folder holding system_prompt.md and tools.json
to regress a different agent with the same transcripts machinery (for example
the exports of deployment-in-a-box).

One call = one agent move. The replayed history already contains every earlier
tool_use / tool_result pair, so we never execute tools here: we only record
what the agent *decided* to do next, which is what the expectations check.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("T2R_MODEL", "claude-opus-5-5")
AGENT_DIR = Path(os.environ.get("T2R_AGENT_DIR") or ROOT / "agent")


@lru_cache(maxsize=1)
def _client():
    import anthropic  # imported lazily: the mock path needs no SDK

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


@lru_cache(maxsize=1)
def _agent_config() -> tuple[str, list[dict[str, Any]]]:
    system = (AGENT_DIR / "system_prompt.md").read_text(encoding="utf-8")
    tools = json.loads((AGENT_DIR / "tools.json").read_text(encoding="utf-8"))
    return system, tools


def fingerprint() -> dict[str, str]:
    """What a result file needs to say which agent it measured."""
    system, tools = _agent_config()
    blob = system + json.dumps(tools, sort_keys=True, ensure_ascii=False)
    return {"model": MODEL, "prompt_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]}


def respond(messages: list[dict[str, Any]]) -> dict[str, Any]:
    system, tools = _agent_config()
    response = _client().beta.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        tools=tools,
        messages=messages,
        # On a safety decline, re-run on Anthropic's recommended fallback model
        # instead of returning an empty refusal to the test.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        # Raised, not returned: the provider reports it as an error, so a refusal
        # is never mistaken for an agent that simply said nothing.
        raise RuntimeError("model refused (stop_reason=refusal) even after fallback")
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    calls = [{"name": b.name, "args": dict(b.input)} for b in response.content if b.type == "tool_use"]
    # response.model is the model that answered: the fallback, if the request fell back.
    return {"text": text, "tool_calls": calls, "model": response.model}
