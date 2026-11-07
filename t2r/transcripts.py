"""Load annotated transcripts and turn them into replayable checkpoints.

A transcript is a YAML file: a recorded conversation where some user turns
carry an `expect` block. Each of those turns becomes one regression test:
replay the conversation up to that turn, ask the agent for its next move,
check the move against the expectation.

    id: T-003
    title: Bill amount needs identity first
    tags: [billing, auth]
    turns:
      - user: "Quanto devo pagare questo mese?"
        expect:
          no_calls: [get_bill]
          say_any: ["codice cliente"]
      - assistant: "Mi dice il codice cliente e la data di nascita?"
      - user: "C-1042, nato il 12 aprile 1985"
        expect:
          calls: [{name: verify_customer, args: {customer_id: C-1042}}]
      - calls:
          - {name: verify_customer, args: {...}, result: {verified: true}}
        expect:                      # checked right after the tool result
          calls: [{name: get_bill}]

Recorded assistant turns may include tool calls with their results; they
are replayed as `tool_use` / `tool_result` blocks so the agent sees the same
state it saw in production. An `expect` on such a turn tests the agent's
move right after those results come back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EXPECT_KEYS = {"calls", "no_calls", "say_any", "say_all", "say_none", "max_words", "escalate", "rubric"}


class TranscriptError(ValueError):
    """A transcript file that does not follow the annotation format."""


@dataclass
class Checkpoint:
    transcript_id: str
    title: str
    step: str  # "u2" = after the 2nd user turn, "r3" = after the 3rd batch of tool results
    tags: list[str]
    messages: list[dict[str, Any]]  # Anthropic Messages API format, ends with the user turn
    expect: dict[str, Any]
    source: str = "synthetic"

    @property
    def test_id(self) -> str:
        return f"{self.transcript_id}#{self.step}"


@dataclass
class Transcript:
    id: str
    title: str
    tags: list[str]
    turns: list[dict[str, Any]]
    source: str = "synthetic"
    path: Path | None = field(default=None, repr=False)

    def checkpoints(self) -> list[Checkpoint]:
        out: list[Checkpoint] = []
        messages: list[dict[str, Any]] = []
        user_turn = 0
        result_turn = 0
        call_counter = 0
        for turn in self.turns:
            if "user" in turn:
                user_turn += 1
                messages.append({"role": "user", "content": turn["user"]})
                if "expect" in turn:
                    out.append(
                        Checkpoint(
                            transcript_id=self.id,
                            title=self.title,
                            step=f"u{user_turn}",
                            tags=list(self.tags),
                            messages=json.loads(json.dumps(messages)),
                            expect=turn["expect"],
                            source=self.source,
                        )
                    )
            else:  # recorded assistant turn
                blocks: list[dict[str, Any]] = []
                if turn.get("assistant"):
                    blocks.append({"type": "text", "text": turn["assistant"]})
                results: list[dict[str, Any]] = []
                for call in turn.get("calls", []):
                    call_counter += 1
                    tool_id = f"toolu_{self.id.replace('-', '').lower()}_{call_counter:02d}"
                    blocks.append(
                        {"type": "tool_use", "id": tool_id, "name": call["name"], "input": call.get("args", {})}
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps(call.get("result", {}), ensure_ascii=False),
                        }
                    )
                messages.append({"role": "assistant", "content": blocks})
                if results:
                    # Tool results go back as one user message, before the next user turn.
                    messages.append({"role": "user", "content": results})
                    result_turn += 1
                    if "expect" in turn:
                        out.append(
                            Checkpoint(
                                transcript_id=self.id,
                                title=self.title,
                                step=f"r{result_turn}",
                                tags=list(self.tags),
                                messages=json.loads(json.dumps(messages)),
                                expect=turn["expect"],
                                source=self.source,
                            )
                        )
        return out


def _merge_consecutive_users(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tool_result message followed by a user turn -> one user message (API needs alternation)."""
    merged: list[dict[str, Any]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"] == "user":
            prev = merged[-1]["content"]
            prev_blocks = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
            cur = m["content"]
            cur_blocks = cur if isinstance(cur, list) else [{"type": "text", "text": cur}]
            merged[-1] = {"role": "user", "content": prev_blocks + cur_blocks}
        else:
            merged.append(m)
    return merged


def load_transcript(path: Path) -> Transcript:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TranscriptError(f"{path.name}: invalid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise TranscriptError(f"{path.name}: top level must be a mapping")
    for key in ("id", "title", "turns"):
        if key not in data:
            raise TranscriptError(f"{path.name}: missing '{key}'")
    turns = data["turns"]
    if not turns or "user" not in turns[0]:
        raise TranscriptError(f"{path.name}: the first turn must be a user turn")
    for i, turn in enumerate(turns, start=1):
        is_user = "user" in turn
        is_assistant = "assistant" in turn or "calls" in turn
        if is_user == is_assistant:
            raise TranscriptError(f"{path.name}: turn {i} must be either a user or an assistant turn")
        unknown = set(turn.get("expect", {})) - EXPECT_KEYS
        if unknown:
            raise TranscriptError(f"{path.name}: turn {i} has unknown expect keys {sorted(unknown)}")
        if "expect" in turn and not is_user and not turn.get("calls"):
            raise TranscriptError(
                f"{path.name}: turn {i}: 'expect' goes on user turns or on assistant turns with tool calls"
            )
    return Transcript(
        id=str(data["id"]),
        title=str(data["title"]),
        tags=[str(t) for t in data.get("tags", [])],
        turns=turns,
        source=str(data.get("source", "synthetic")),
        path=path,
    )


def load_all(directory: Path) -> list[Transcript]:
    transcripts = [load_transcript(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [t.id for t in transcripts]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise TranscriptError(f"duplicate transcript ids: {sorted(dupes)}")
    return transcripts


def all_checkpoints(directory: Path) -> list[Checkpoint]:
    cps = [cp for t in load_all(directory) for cp in t.checkpoints()]
    for cp in cps:
        cp.messages = _merge_consecutive_users(cp.messages)
    return cps
