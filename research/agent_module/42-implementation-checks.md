# Plan 42 implementation checks

**Date:** 2026-11-10
**Sources:** executed in the project environment (Python 3.12.10, pydantic-ai-slim 2.55.0,
hatchling via `python -m build`)
**Used by:** `plans/42-aitaem-agent-v3.md` SF-9, SF-10, SF-14

Plan 42 marks claims as **[verify]** where a sub-feature depends on them. This note records the
result of each check executed during implementation. (SF-4 is recorded in
[42-pydantic-ai-2.55-mechanics](42-pydantic-ai-2.55-mechanics.md), SF-7 in
[42-spec-subcache-scan](42-spec-subcache-scan.md).)

## SF-9: `instructions.md` in the wheel

`python -m build --wheel` with a probe package `aitaem/agent/agent_roles/_probe/` holding
`__init__.py` and `instructions.md` produced a wheel listing
`aitaem/agent/agent_roles/_probe/instructions.md`. Hatchling's default wheel target includes
non-Python files inside the package, so no build configuration is needed.

## SF-10: passing the coordinator's `CancellationToken` to executors

- `Agent.run(..., cancellation_token=...)` takes a token, but `RunContext` exposes none: the run's
  tokens sit in the private `ctx._cancellation._tokens`. The public `ctx.cancel()` cancels the
  current run only.
- Executed: a parent run started with a token, whose `async` tool awaits
  `asyncio.gather(child.run(...) × 3, return_exceptions=True)`. Cancelling the token while the
  children were waiting on their model ended the parent with `RunCancelled`, and all three
  children received `CancelledError` (3 started, 3 cancelled, 0 finished). Cancellation reaches
  the executors through asyncio task cancellation of the fan-out tool's `gather`, without passing
  the token.
