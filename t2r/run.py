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


def run(agent: str, transcripts_dir: Path) -> dict:
    rows = []
    for cp in all_checkpoints(transcripts_dir):
        move = next_move(cp.messages, agent)
        checks = evaluate(cp.expect, move)
        rows.append({
            "test_id": cp.test_id,
            "transcript": cp.transcript_id,
            "tags": cp.tags,
            "pass": all(c.passed for c in checks),
            "failures": [f"{c.name}: {c.reason}" for c in checks if not c.passed],
            "output": move,
        })
    return {"format": "t2r-local/1", "agent": agent, "results": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", default="mock-v1", help="mock-v1 | mock-v2 | claude")
    parser.add_argument("--transcripts", type=Path, default=ROOT / "transcripts")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "local.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    report = run(args.agent, args.transcripts)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in report["results"]:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{mark}  {r['test_id']:<10} {'; '.join(r['failures'])}")
    passed = sum(r["pass"] for r in report["results"])
    print(f"\n{passed}/{len(report['results'])} checkpoints passed ({args.agent}) -> {args.out}")


if __name__ == "__main__":
    main()
