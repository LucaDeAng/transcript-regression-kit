"""Regression gate: compare a run against the baseline, per tag, and fail CI on a drop.

    python -m t2r.gate results/promptfoo.json                 # check (exit 1 on regression)
    T2R_AGENT=claude python -m t2r.gate results/promptfoo.json --tolerance 1
    python -m t2r.gate results/promptfoo.json --write-baseline
    python -m t2r.gate results/local.json --summary "$GITHUB_STEP_SUMMARY"

Accepts promptfoo's JSON output (`promptfoo eval -o file.json`) or the output
of `python -m t2r.run`. Why per tag: an aggregate pass rate averages a broken
safety rule with ten healthy small-talk checks. The tags are the contract.
"""

from __future__ import annotations

import argparse
import json
import os
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


STRICT_TAGS = frozenset({"safety", "security", "privacy"})


def compare(rows: list[dict], baseline: dict, tolerance: int = 0,
            strict_tags: frozenset[str] = STRICT_TAGS) -> tuple[bool, str]:
    """A tag regresses when more than `tolerance` of its checkpoints passed on the
    baseline and fail now. Counting checkpoints, not points of pass rate, keeps the
    rule meaningful for small tags (one miss in a 1-checkpoint tag is -100 points).
    Strict tags (safety, security, privacy by default) get zero tolerance whatever
    `tolerance` says: otherwise a one-checkpoint safety tag could never fail live.
    Checkpoints that passed on the baseline and are gone from the run fail the gate:
    deleting a transcript must come with a deliberate baseline refresh."""
    current = tag_rates(rows)
    base_tags = baseline.get("tags", {})
    base_passing = set(baseline.get("passing", []))
    newly_failing = [r for r in rows if not r["pass"] and r["test_id"] in base_passing]
    missing = sorted(base_passing - {r["test_id"] for r in rows})

    per_tag: dict[str, int] = defaultdict(int)
    for r in newly_failing:
        for tag in r["tags"] or ["untagged"]:
            per_tag[tag] += 1
    regressions = [t for t, n in sorted(per_tag.items()) if n > (0 if t in strict_tags else tolerance)]

    lines = ["## Transcript regression gate", "",
             "| tag | baseline | now | newly failing | |", "|---|---|---|---|---|"]
    for tag, cur in current.items():
        base = base_tags.get(tag)
        status = "new" if base is None else ("❌ regression" if tag in regressions else "✅")
        base_txt = f"{base['rate']:.0%}" if base else "-"
        lines.append(
            f"| {tag} | {base_txt} | {cur['rate']:.0%} ({cur['passed']}/{cur['total']}) | {per_tag.get(tag, 0)} | {status} |"
        )

    total_pass = sum(r["pass"] for r in rows)
    strict = ", ".join(sorted(strict_tags))
    lines += ["", f"**Overall:** {total_pass}/{len(rows)} checkpoints pass (tolerance: {tolerance} per tag; 0 for {strict})."]
    if newly_failing:
        lines += ["", "### Checkpoints that passed on the baseline and fail now", ""]
        lines += [f"- `{r['test_id']}`: {r['reason']}" for r in newly_failing]
    if missing:
        lines += ["", "### Baseline checkpoints missing from this run", ""]
        lines += [f"- `{m}`" for m in missing]
        lines += ["", "Removed on purpose? Refresh the baseline in the same pull request."]

    ok = not regressions and not missing
    reasons = [f"regression in {', '.join(regressions)}"] if regressions else []
    if missing:
        reasons.append(f"{len(missing)} baseline checkpoint(s) missing")
    lines += ["", "**Gate: PASS**" if ok else f"**Gate: FAIL**: {'; '.join(reasons)}"]
    return ok, "\n".join(lines)


def default_baseline() -> Path:
    """One baseline per agent family: every mock version is judged against mock-v1
    (so the regressed mock-v2 stays red), a live model against its own baseline."""
    agent = os.environ.get("T2R_AGENT") or "mock-v1"
    return ROOT / ("baseline.mock-v1.json" if agent.startswith("mock-") else f"baseline.{agent}.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", type=Path, default=None,
                        help="default: baseline.<T2R_AGENT>.json (baseline.mock-v1.json when unset)")
    parser.add_argument("--tolerance", type=int, default=0,
                        help="newly failing checkpoints allowed per tag; use 1 for a live, non-deterministic agent")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--summary", type=Path, help="append the markdown report here (e.g. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    baseline_path = args.baseline or default_baseline()

    if not args.results.exists():
        raise SystemExit(f"no results at {args.results}: run `python -m t2r.run` or `promptfoo eval -o {args.results}` first")
    rows = load_rows(args.results)
    if args.write_baseline:
        baseline = {"tags": tag_rates(rows), "passing": sorted(r["test_id"] for r in rows if r["pass"])}
        baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written: {len(baseline['passing'])}/{len(rows)} passing -> {baseline_path}")
        return

    if not baseline_path.exists():
        raise SystemExit(f"no baseline at {baseline_path}; run with --write-baseline first")
    ok, report = compare(rows, json.loads(baseline_path.read_text(encoding="utf-8")), args.tolerance)
    print(report)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(report + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
