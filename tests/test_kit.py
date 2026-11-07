"""Tests for the kit itself: parsing, checks, mock agents, gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import mock_agent  # noqa: E402
from t2r import gate  # noqa: E402
from t2r.expectations import evaluate  # noqa: E402
from t2r.run import run  # noqa: E402
from t2r.transcripts import TranscriptError, all_checkpoints, load_transcript  # noqa: E402

TRANSCRIPTS = ROOT / "transcripts"


# --- transcripts -------------------------------------------------------------

def test_every_transcript_loads_and_yields_checkpoints():
    cps = all_checkpoints(TRANSCRIPTS)
    assert len({cp.transcript_id for cp in cps}) == len(list(TRANSCRIPTS.glob("*.yaml")))
    assert len({cp.test_id for cp in cps}) == len(cps), "test ids must be unique"


def test_replayed_history_is_valid_for_the_messages_api():
    for cp in all_checkpoints(TRANSCRIPTS):
        roles = [m["role"] for m in cp.messages]
        assert roles[0] == "user" and roles[-1] == "user", cp.test_id
        assert all(a != b for a, b in zip(roles, roles[1:])), f"{cp.test_id}: roles must alternate"
        used = {b["id"] for m in cp.messages if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_use"}
        answered = {b["tool_use_id"] for m in cp.messages if isinstance(m["content"], list)
                    for b in m["content"] if b.get("type") == "tool_result"}
        assert used == answered, f"{cp.test_id}: every tool_use needs its tool_result"


def test_tool_result_checkpoint_ends_on_the_results(tmp_path):
    cp = next(c for c in all_checkpoints(TRANSCRIPTS) if c.test_id == "T-001#r1")
    last = cp.messages[-1]["content"]
    assert [b["type"] for b in last] == ["tool_result"]


@pytest.mark.parametrize("body, message", [
    ("id: X\ntitle: t\nturns:\n  - assistant: hi\n", "first turn must be a user turn"),
    ("id: X\ntitle: t\nturns:\n  - user: hi\n    expect: {say_maybe: [x]}\n", "unknown expect keys"),
    ("id: X\ntitle: t\nturns:\n  - user: hi\n  - assistant: ok\n    expect: {say_any: [x]}\n", "'expect' goes on"),
    ("id: X\ntitle: t\nturns:\n  - user: \"unterminated\n", "invalid YAML"),
])
def test_malformed_transcripts_fail_loudly(tmp_path, body, message):
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(TranscriptError, match=message):
        load_transcript(path)


# --- expectations ------------------------------------------------------------

def _move(text="", *calls):
    return {"text": text, "tool_calls": list(calls)}


def test_args_are_a_case_and_accent_insensitive_subset():
    out = _move("", {"name": "report_outage", "args": {"address": "Via Roma 12, Torino", "kind": "electricity"}})
    assert all(c.passed for c in evaluate({"calls": [{"name": "report_outage", "args": {"address": "via roma 12"}}]}, out))


def test_symbols_that_normalise_away_still_match():
    # "€" vanishes under ASCII folding; it must not turn into an always-true empty needle.
    assert all(c.passed for c in evaluate({"say_none": ["€"]}, _move("Mi dice il codice cliente?")))
    assert not all(c.passed for c in evaluate({"say_none": ["€"]}, _move("Sono 84,30 €.")))


def test_escalate_does_not_duplicate_an_explicit_handoff_check():
    expect = {"escalate": True, "calls": [{"name": "transfer_to_human", "args": {"reason": "auth_failed"}}]}
    checks = evaluate(expect, _move(""))
    assert [c.name for c in checks] == ["calls:transfer_to_human"]


def test_no_calls_star_means_no_tool_at_all():
    checks = evaluate({"no_calls": ["*"]}, _move("ok", {"name": "get_bill", "args": {}}))
    assert not checks[0].passed


def test_output_may_arrive_as_a_json_string():
    raw = json.dumps(_move("Buongiorno", {"name": "get_bill", "args": {"customer_id": "C-1042"}}))
    assert all(c.passed for c in evaluate({"calls": [{"name": "get_bill"}], "say_any": ["buongiorno"]}, raw))


# --- the mock agents: the suite must separate them ----------------------------

def test_mock_v1_passes_every_checkpoint():
    report = run("mock-v1", TRANSCRIPTS)
    failing = [(r["test_id"], r["failures"]) for r in report["results"] if not r["pass"]]
    assert not failing


def test_mock_v2_regresses_on_auth_and_escalation_only():
    report = run("mock-v2", TRANSCRIPTS)
    rates = gate.tag_rates(gate.load_rows_from(report))
    for tag in ("auth", "escalation"):
        assert rates[tag]["rate"] < 1.0, tag
    for tag in ("outage", "safety", "security", "scope"):
        assert rates[tag]["rate"] == 1.0, tag


def test_mock_rejects_unknown_version():
    with pytest.raises(ValueError):
        mock_agent.respond([{"role": "user", "content": "ciao"}], version="v3")


# --- gate --------------------------------------------------------------------

def _baseline_from(report):
    rows = gate.load_rows_from(report)
    return {"tags": gate.tag_rates(rows), "passing": [r["test_id"] for r in rows if r["pass"]]}


def test_gate_passes_v1_and_fails_v2_against_a_v1_baseline():
    baseline = _baseline_from(run("mock-v1", TRANSCRIPTS))
    ok_v1, _ = gate.compare(gate.load_rows_from(run("mock-v1", TRANSCRIPTS)), baseline, tolerance=0.0)
    ok_v2, report = gate.compare(gate.load_rows_from(run("mock-v2", TRANSCRIPTS)), baseline, tolerance=0.0)
    assert ok_v1
    assert not ok_v2
    assert "T-005#u1" in report and "regression in" in report


def test_gate_tolerance_absorbs_small_drops():
    rows = [{"test_id": f"t{i}", "tags": ["a"], "pass": i != 0, "reason": ""} for i in range(10)]
    baseline = {"tags": {"a": {"passed": 10, "total": 10, "rate": 1.0}}, "passing": [f"t{i}" for i in range(10)]}
    assert not gate.compare(rows, baseline, tolerance=0.0)[0]
    assert gate.compare(rows, baseline, tolerance=0.1)[0]


def test_gate_reads_promptfoo_output_shape(tmp_path):
    promptfoo = {"results": {"results": [
        {"success": True, "testCase": {"metadata": {"test_id": "T-1#u1", "tags": ["auth"]}}, "gradingResult": {"reason": "ok"}},
        {"success": False, "testCase": {"metadata": {"test_id": "T-2#u1", "tags": ["auth"]}}, "gradingResult": {"reason": "no"}},
    ]}}
    path = tmp_path / "pf.json"
    path.write_text(json.dumps(promptfoo), encoding="utf-8")
    rows = gate.load_rows(path)
    assert [(r["test_id"], r["pass"]) for r in rows] == [("T-1#u1", True), ("T-2#u1", False)]
