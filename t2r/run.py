"""Run the suite without promptfoo (no Node needed). Same checks, same output for the gate.

    python -m t2r.run --agent mock-v1
    python -m t2r.run --agent mock-v2 --out results/local.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from providers.agent_provider import next_move
from t2r.expectations import evaluate
from t2r.transcripts import all_checkpoints

ROOT = Path(__file__).resolve().parent.parent


def run(agent: str, transcripts_dir: Path, trials: int = 1) -> dict:
    """Replay every checkpoint `trials` times. A checkpoint passes only if every
    trial passes (pass^k, as in tau-bench): for a live model, "usually right" is
    not good enough for an identity or safety rule."""
    rows = []
    for cp in all_checkpoints(transcripts_dir):
        outcomes = []
        for _ in range(trials):
            move = next_move(cp.messages, agent)
            checks = evaluate(cp.expect, move)
            outcomes.append((all(c.passed for c in checks), [f"{c.name}: {c.reason}" for c in checks if not c.passed], move))
        passed = sum(ok for ok, _, _ in outcomes)
        first_failure = next(((f, m) for ok, f, m in outcomes if not ok), None)
        rows.append({
            "test_id": cp.test_id,
            "transcript": cp.transcript_id,
            "tags": cp.tags,
            "pass": passed == trials,
            "trials_passed": f"{passed}/{trials}",
            "failures": first_failure[0] if first_failure else [],
            "output": first_failure[1] if first_failure else outcomes[0][2],
        })
    return {"format": "t2r-local/1", "agent": agent, "trials": trials, "results": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", default="mock-v1", help="mock-v1 | mock-v2 | claude")
    parser.add_argument("--trials", type=int, default=1, help="replays per checkpoint; pass^k = all k must pass")
    parser.add_argument("--transcripts", type=Path, default=ROOT / "transcripts")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "local.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    report = run(args.agent, args.transcripts, trials=args.trials)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in report["results"]:
        mark = "PASS" if r["pass"] else "FAIL"
        flaky = f" [{r['trials_passed']}]" if args.trials > 1 else ""
        print(f"{mark}  {r['test_id']:<10}{flaky} {'; '.join(r['failures'])}")
    passed = sum(r["pass"] for r in report["results"])
    metric = f"pass^{args.trials}" if args.trials > 1 else "passed"
    print(f"\n{passed}/{len(report['results'])} checkpoints {metric} ({args.agent}) -> {args.out}")


if __name__ == "__main__":
    main()
