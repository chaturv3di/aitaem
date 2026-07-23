# Agent: Evaluating Your Agent

`aitaem.agent`'s trace and result-store types are designed to be consumed by
[`pydantic_evals`](https://ai.pydantic.dev/evals/): `RunTrace.tool_calls` exposes which
tools ran, in what order, with what arguments; `ToolCall.result_id` links a tool call to
its full `ResultStore` entry; `BotResponse.status` exposes refusal/error outcomes
directly. This page covers two distinct things you can do with that substrate.

## Install

```bash
pip install "aitaem[agent-evals]"
```

## Track 1 — substrate validation (CI-safe, no API key)

[`tests/evals/`](https://github.com/chaturv3di/aitaem/tree/main/tests/evals) in this
repo is the reference harness: `Case`/`Dataset`/`Evaluator` from `pydantic_evals`, driven
by hand-scripted `pydantic_ai.models.function.FunctionModel`s that already know which
tool to call and in what order. It runs in CI with no live LLM calls and no API key.

Read it as proof that `RunTrace`, `ResultStore`, and `BotResponse` are consumable by
`pydantic_evals.Evaluator`s end-to-end — **not** as a measurement of whether an LLM would
select the right tool, refuse appropriately, or produce a correct answer. Every
`FunctionModel` in that harness is scripted to produce the outcome being asserted, so its
assertions are near-tautological by construction. That's still a legitimate and valuable
thing to have proven on its own — point the same harness at a live model outside CI and
the wiring already works — but don't mistake "the harness runs and passes" for "the agent
was evaluated."

## Track 2 — behavioral evaluation against a live model

For an actual measurement of agent behavior, you need real questions, a real spec
catalog, and a real model. [`examples/04_evaluating_agents_example.py`](https://github.com/chaturv3di/aitaem/blob/main/examples/04_evaluating_agents_example.py)
(and its notebook counterpart) is the pattern to copy: a `query_task()` function that
builds a fresh `QueryBot` per case and calls `bot.ask()`, `Evaluator` subclasses that
inspect `response.status` and `response.trace.tool_calls`, a `Dataset` of `Case`s against
a real spec catalog, and a `pass_rate()` helper that repeats the dataset `n` times and
reports a per-case pass fraction — because a single run against a live, non-deterministic
model tells you much less than a distribution does.

That example also demonstrates a deliberate trade-off worth internalizing for your own
evaluations: an evaluator that checks an *exact* tool-call sequence (e.g. to confirm a
gating rule was honored before a computation ran) is stricter than one that just checks a
tool was called at all, and will occasionally fail a run that self-corrected but still
behaved correctly. Read exact-sequence assertions through `pass_rate()`, not a single
run's pass/fail.

## Which track should I use?

- Changing tool wiring, trace assembly, or the result-store contract? Extend
  `tests/evals/` — it must keep passing in CI with no API key.
  See [Building Your Own Bot](building-your-own-bot.md) if you're introducing new tools.
- Measuring whether your prompt, catalog, or model choice actually produces correct
  behavior? Write a `04_evaluating_agents_example.py`-style dataset against a live model.

## Next steps

- [Stability & Limitations](stability.md) — note that default prompt content isn't
  semver-stable, which matters if you're tracking eval pass rates over time
- [Agent API Reference](../api/agent.md) — `RunTrace`, `ToolCall`, `ResultStore` field
  reference
