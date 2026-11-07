"""Regression gate: compare a run against the baseline, per tag, and fail CI on a drop.

    python -m t2r.gate results/promptfoo.json                 # check (exit 1 on regression)
    python -m t2r.gate results/promptfoo.json --write-baseline
    python -m t2r.gate results/local.json --summary "$GITHUB_STEP_SUMMARY"

Accepts promptfoo's JSON output (`promptfoo eval -o file.json`) or the output
of `python -m t2r.run`. Why per tag: an aggregate pass rate averages a broken
safety rule with ten healthy small-talk checks. The tags are the contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rows(path: Path) -> list[dict]:
    rows = load_rows_from(json.loads(path.read_text(encoding="utf-8")))
    if not rows:
        raise SystemExit(f"{path}: no results found")
    return rows


def load_rows_from(data: dict) -> list[dict]:
    """Normalise either result format to [{test_id, tags, pass, reason}]."""
    if data.get("format") == "t2r-local/1":
        return [
            {"test_id": r["test_id"], "tags": r["tags"], "pass": r["pass"], "reason": "; ".join(r["failures"])}
            for r in data["results"]
        ]
    rows = []
    for r in data["results"]["results"]:  # promptfoo: {results: {results: [...]}}
        meta = (r.get("testCase") or {}).get("metadata") or r.get("metadata") or {}
        grading = r.get("gradingResult") or {}
        rows.append({
            "test_id": meta.get("test_id") or (r.get("vars") or {}).get("test_id"),
            "tags": list(meta.get("tags") or []),
            "pass": bool(r.get("success")),
            "reason": grading.get("reason") or r.get("error") or "",
        })
    return rows


def tag_rates(rows: list[dict]) -> dict[str, dict]:
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        for tag in r["tags"] or ["untagged"]:
            stats[tag][0] += r["pass"]
            stats[tag][1] += 1
    return {t: {"passed": p, "total": n, "rate": round(p / n, 4)} for t, (p, n) in sorted(stats.items())}


def compare(rows: list[dict], baseline: dict, tolerance: float) -> tuple[bool, str]:
    current = tag_rates(rows)
    base_tags = baseline.get("tags", {})
    base_passing = set(baseline.get("passing", []))
    regressions = []

    lines = ["## Transcript regression gate", "", "| tag | baseline | now | |", "|---|---|---|---|"]
    for tag, cur in current.items():
        base = base_tags.get(tag)
        if base is None:
            status = "new"
        elif cur["rate"] < base["rate"] - tolerance:
            status = "❌ regression"
            regressions.append(tag)
        else:
            status = "✅"
        base_txt = f"{base['rate']:.0%}" if base else "-"
        lines.append(f"| {tag} | {base_txt} | {cur['rate']:.0%} ({cur['passed']}/{cur['total']}) | {status} |")

    newly_failing = [r for r in rows if not r["pass"] and r["test_id"] in base_passing]
    total_pass = sum(r["pass"] for r in rows)
    lines += ["", f"**Overall:** {total_pass}/{len(rows)} checkpoints pass."]
    if newly_failing:
        lines += ["", "### Checkpoints that passed on the baseline and fail now", ""]
        lines += [f"- `{r['test_id']}`: {r['reason']}" for r in newly_failing]
    ok = not regressions
    lines += ["", "**Gate: PASS**" if ok else f"**Gate: FAIL**: regression in {', '.join(regressions)}"]
    return ok, "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path, default=ROOT / "baseline.json")
    parser.add_argument("--tolerance", type=float, default=0.0,
                        help="allowed drop in a tag's pass rate (0.1 = 10 points); use >0 for a live, non-deterministic agent")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--summary", type=Path, help="append the markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rows = load_rows(args.results)
    if args.write_baseline:
        baseline = {"tags": tag_rates(rows), "passing": sorted(r["test_id"] for r in rows if r["pass"])}
        args.baseline.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written: {len(baseline['passing'])}/{len(rows)} passing -> {args.baseline}")
        return

    if not args.baseline.exists():
        raise SystemExit(f"no baseline at {args.baseline}; run with --write-baseline first")
    ok, report = compare(rows, json.loads(args.baseline.read_text(encoding="utf-8")), args.tolerance)
    print(report)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(report + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
