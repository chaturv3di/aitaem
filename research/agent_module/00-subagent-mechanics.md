# Subagent mechanics in pydantic-ai 2.55

**Date:** 2026-10-12
**Sources:** pydantic-ai-slim 2.55.0 and pydantic-ai-harness 0.55.0 source. The DuckDB
concurrency point (§2) was carried over from the v1.1 architecture draft without a recorded source.
**Used by:** `plans/agent_module/ARCHITECTURE.md` §4.3, §7

Related notes: [00-pydantic-ai-2.55-mechanics](00-pydantic-ai-2.55-mechanics.md) ·
[00-anthropic-prompt-caching §7](00-anthropic-prompt-caching.md#7-parallel-workers) ·
[00-workflow-options](00-workflow-options.md)

---

## 1. Delegation primitives (read in source)

- Delegate from `async def` tools.
- `worker.run(…, deps=…, usage=ctx.usage)`: passing a child deps object (e.g.
  `dataclasses.replace(ctx.deps, scope=…)`) gives per-child scope; passing `usage=ctx.usage` makes
  the parent's `UsageLimits` bound the whole tree. Per-run span usage is attributed to the
  innermost run (`_usage_attribution`), so nested runs do not double count.
- Per-worker model is set on the worker `Agent`.
- `output_type` Pydantic models give typed worker results.
- A worker run without `message_history` has an isolated context.
- Cancellation: a parent's `CancellationToken` (2.26+) can be passed to children so a cancelled
  turn stops the whole tree.
- Named agents (`name=`) plus `Instrumentation` nest child runs under the tool-call span.

## 2. Concurrency

- An `asyncio.Semaphore` inside the fan-out tool bounds parallel workers.
- Model-level limiting: `ConcurrencyLimitedModel` / `limit_model_concurrency`. Since 2.53, sharing
  one limiter between a parent agent and its children's wrappers raises `UserError`, so a limiter
  must be used **only** by the children. **[verify]** against `pydantic_ai.concurrency`.
- `asyncio.gather(…, return_exceptions=True)` lets siblings finish when one fails.
- DuckDB: a single in-process connection is not safe across threads. Concurrent workers need their
  own connection (`duckdb.connect(path)` or `cursor()`), which means passing the database **path**
  rather than a connection.

## 3. Harness `SubAgents` assessment

`delegate_task` lets the LLM delegate free-form work. Fit issues: it always returns
`str(output)` (untyped), forwards the parent's deps unchanged (no per-child scope), and is 0.x with
an exact slim pin. It remains an option for free-form delegation.

## 4. Code-driven vs LLM-driven fan-out

Fan-out inside one parent tool (code calls `gather` over N worker runs) versus the LLM issuing N
tool calls:

| | Code-driven | LLM-driven |
|---|---|---|
| Determinism of fan-out | High | Depends on the model |
| History noise | One call/return pair | N pairs plus per-worker output |
| Typed results | Yes (`output_type`) | Depends on the delegation tool |
| Per-child deps | Yes | Not with harness `SubAgents` |
| Who decides *when* to fan out | The LLM (by calling the tool) | The LLM |
| Flexibility of task shapes | Fixed per tool | Free-form |
