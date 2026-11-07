"""promptfoo Python provider: replays a checkpoint and returns the agent's next move.

promptfoo calls `call_api(prompt, options, context)` once per test. The test's
vars carry the replayed conversation (`messages`, JSON). Which agent answers is
set in promptfooconfig.yaml (`config.agent`) or overridden with T2R_AGENT:

    mock-v1   deterministic agent that follows the rules (default)
    mock-v2   the same agent after a regression
    claude    the real agent (needs ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import mock_agent  # noqa: E402


def next_move(messages: list[dict[str, Any]], agent: str) -> dict[str, Any]:
    if agent.startswith("mock-"):
        return mock_agent.respond(messages, version=agent.removeprefix("mock-"))
    if agent == "claude":
        from providers import claude_agent

        return claude_agent.respond(messages)
    raise ValueError(f"unknown agent {agent!r} (expected mock-v1, mock-v2 or claude)")


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    agent = os.environ.get("T2R_AGENT") or (options.get("config") or {}).get("agent", "mock-v1")
    messages = json.loads(context["vars"]["messages"])
    try:
        move = next_move(messages, agent)
    except Exception as exc:  # surface provider failures as errors, not as failed checks
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"output": json.dumps(move, ensure_ascii=False), "metadata": {"agent": agent}}
