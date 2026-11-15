# Agent: Evaluating Your Agent

`AitaemAgent`'s responses, traces, and artifacts are built to be read by
[`pydantic_evals`](https://ai.pydantic.dev/evals/) evaluators:

- `Response.status` exposes refusals and errors directly.
- `agent.trace(response.trace_id)` returns the turn's `RunTrace`. `tool_calls` lists every tool
  call of every run in the turn (main session, coordinators, executors), and `runs` gives each
  run's tool calls and usage, cache reads and writes included.
- `ToolCall.artifact_id` links a tool call to its artifact; `agent.store.read(artifact_id)`
  returns the result.

## Install

```bash
pip install "aitaem[agent-evals]"
```

## Track 1 — substrate validation (CI-safe, no API key)

[`tests/evals/`](https://github.com/chaturv3di/aitaem/tree/main/tests/evals) is the reference
harness: `Case`, `Dataset`, and `Evaluator` from `pydantic_evals`, with every agent role driven by
a scripted `FunctionModel` that already knows which tools to call. It covers analysis (status,
refusals, attachments against a direct `MetricCompute` result), spec design (validate, then
commit), threshold governance, fan-out correctness (executor verdicts against
`MetricCompute.scan()`), and artifact completeness (every tool call names an artifact in the
manifest).

These cases prove that traces, responses, and artifacts are consumable end to end. They do not
measure whether a model would choose the right tools: each scripted model produces the outcome
being asserted.

## Track 2 — behavioral evaluation against a live model

The same harness runs against a live model by passing a model name instead of a scripted model.
`tests/evals/` includes live cases, skipped without `ANTHROPIC_API_KEY`: threshold governance
(grounding thresholds with `column_distribution` and `date_range` instead of inventing them),
answer fidelity after history stubbing, and prompt-cache reads on the second turn. A single run
against a non-deterministic model tells you little; repeat each dataset and read the pass rate.

## Which track should I use?

- Changing tool wiring, traces, or the artifact store? Extend `tests/evals/`; it must pass in CI
  with no API key.
- Measuring whether instructions, catalog, or model choice produce correct behavior? Write live
  cases and track their pass rates.

## Next steps

- [Stability & Limitations](stability.md) — role instructions are not semver-stable, which matters
  when you track pass rates over time
- [Agent API Reference](../api/agent.md)
