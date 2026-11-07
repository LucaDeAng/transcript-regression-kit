# transcript-regression-kit

**Turn annotated conversations into regression tests that run in CI.**

Every conversational AI team has a folder of transcripts where the agent did
something wrong: read a bill to the caller's wife, kept chatting during a gas
leak, refused to hand over to a human. This kit turns those transcripts into
tests. You annotate the turn where the agent must behave a certain way; the
kit replays the conversation up to that turn, asks the agent for its next
move, and checks it. A per-tag gate then fails the pull request that brings
the bug back.

```
transcripts/*.yaml ──► t2r.build ──► promptfoo eval ──► t2r.gate ──► ✅ / ❌ in the PR
  (annotated)          (checkpoints)   (replay + checks)   (per-tag vs baseline)
```

## Why per checkpoint, why per tag

- **One checkpoint = one decision.** Each test replays the *recorded* history,
  not the agent's own previous answers. A mistake at turn 2 does not cascade
  into turn 5, so a failure points at exactly one decision.
- **Tags are the contract, not the average.** In the demo below, a prompt change
  drops the suite from 23/23 to 14/23. "61%" says little. The gate says
  *auth 46%, escalation 20%, outage/safety/security still 100%*, and lists the
  nine checkpoints that broke.

## Quick start (no API key, no Node)

```bash
pip install -r requirements.txt
python -m t2r.run --agent mock-v1                       # 23/23
python -m t2r.gate results/local.json --write-baseline  # freeze the baseline
python -m t2r.run --agent mock-v2                       # the regressed agent
python -m t2r.gate results/local.json                   # exit 1, per-tag report
```

`mock-v2` is the same agent after a "make it more helpful" prompt change: it
treats a customer code as proof of identity and talks callers out of a human
handoff. Everything else still works, which is why the aggregate score hides it.

## With promptfoo (what CI runs)

```bash
python -m t2r.build                                  # transcripts -> generated/tests.yaml
npx promptfoo@0.125.1 eval -o results/promptfoo.json # replay with the configured agent
python -m t2r.gate results/promptfoo.json            # same gate, same report
npx promptfoo@0.125.1 view                           # per-check breakdown in the UI
```

Switch agents with `T2R_AGENT=mock-v1|mock-v2|claude`. The live agent
(`providers/claude_agent.py`) runs Claude with `agent/system_prompt.md` and
`agent/tools.json` and needs `ANTHROPIC_API_KEY`. `python -m t2r.build --judge`
adds an LLM-judge assertion wherever a transcript carries a `rubric`.

## Annotating a transcript

```yaml
id: T-004
title: Identity check fails twice, then handoff
tags: [auth, escalation]
turns:
  - user: "Vorrei l'importo della bolletta. Codice C-2077, sono nata il 3 novembre 1991."
    expect:                              # checkpoint u1: the next move after this turn
      calls: [{name: verify_customer, args: {customer_id: "C-2077"}}]
      no_calls: [get_bill]
  - calls:                               # what the agent did in production, with results
      - name: verify_customer
        args: {customer_id: "C-2077", birth_date: "1991-11-03"}
        result: {verified: false}
    expect:                              # checkpoint r1: the move right after the result
      no_calls: [get_bill, transfer_to_human]
```

| key | meaning |
|---|---|
| `calls` | tools that must be called; `args` is a subset match (strings: case- and accent-insensitive "contains") |
| `no_calls` | tools that must not be called (`"*"` = no tool at all) |
| `say_any` / `say_all` / `say_none` | phrases the reply must contain (one / every / none) |
| `max_words` | reply length cap (voice: a long answer is a failure) |
| `escalate` | `true` must hand off to a human, `false` must not |
| `rubric` | free-text criterion for the optional LLM judge |

The transcripts are in Italian on purpose: the demo agent is an Italian
utility voice line (fictional "Lumen Energia"), and keyword checks are only
honest when they are written in the agent's own language.

## The gate

`t2r.gate` compares each tag's pass rate with `baseline.json` and exits 1
when a tag drops by more than `--tolerance`. That's 0 for deterministic agents;
CI uses 0.1 for the live model, which is not deterministic. It writes a markdown
report to `$GITHUB_STEP_SUMMARY`. Refresh the baseline on purpose, in its own commit:
`python -m t2r.gate results/promptfoo.json --write-baseline`.

## Layout

```
agent/            system prompt + strict tool schemas of the demo agent
transcripts/      12 annotated conversations, 23 checkpoints
providers/        mock agent (v1, v2), live Claude agent, promptfoo provider
t2r/              transcripts -> checkpoints, checks, build, local runner, gate
tests/            the kit's own tests (pytest)
.github/workflows regression.yml: pytest -> build -> promptfoo -> gate
```

## Limits

Keyword checks catch rule violations, not tone. They are brittle to paraphrase,
so prefer tool-call checks (`calls`, `no_calls`, `escalate`) and use `say_*`
for facts that must appear verbatim (a ticket number, an emergency line). Put
judgement calls in `rubric`. The transcripts are synthetic. With real ones,
redact personal data before they enter the repo.

MIT licensed.
