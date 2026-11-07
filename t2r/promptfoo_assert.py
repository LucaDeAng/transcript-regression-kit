"""promptfoo Python assertion: runs the deterministic checks for one checkpoint.

Attached to every test via `defaultTest` in promptfooconfig.yaml. Each check
becomes a component result, so the promptfoo UI shows *which* rule broke.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from t2r.expectations import evaluate  # noqa: E402


def get_assert(output: Any, context: dict[str, Any]) -> dict[str, Any]:
    expect = json.loads(context["vars"]["expect"])
    checks = evaluate(expect, output)
    failed = [c for c in checks if not c.passed]
    return {
        "pass": not failed,
        "score": (len(checks) - len(failed)) / len(checks) if checks else 1.0,
        "reason": "; ".join(f"{c.name}: {c.reason}" for c in failed) or f"{len(checks)} checks passed",
        "componentResults": [
            {"pass": c.passed, "score": 1.0 if c.passed else 0.0, "reason": f"{c.name}: {c.reason}"}
            for c in checks
        ],
    }
