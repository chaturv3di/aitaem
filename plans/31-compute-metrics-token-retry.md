# Plan 31 — `compute_metrics` Restores `spec_token` on Failure

**Scope:** Bugfix to already-shipped-but-unreleased `QueryBot` v0.2 (Plan 26) behavior, plus documentation of the resulting semantics. `aitaem.agent` has never appeared in a released version (changelog's latest tag, `v0.4.0`, predates it), so this is a correction to unreleased code, not a breaking change.

---

## 0. Key Decision

**The gap, confirmed against the code:**
- `compute_metrics()` (`aitaem/agent/query_tools.py:212`) pops `spec_token` from `ctx.deps.spec_registry` *before* the `try` block that executes the compute.
- The `except Exception as exc:` handler (line 262) returns an error but never restores the popped entry.
- A transient failure anywhere inside the try — `mc.compute()`, `.to_pyarrow()`, `store_tabular()` — permanently burns the token.
- The only recovery path is a full `record_intent` → `resolve_intent` round trip to mint a new one, even though the failure had nothing to do with resolution validity.

**Why this wasn't caught by the original design:** the pop exists to solve a different problem — parallel identical tool calls in one Anthropic message double-executing the same compute (`query_tools.py`'s own comment: "prevents double warehouse execution and duplicate result_ids from one query"). That justification never considered the failure path. No doc (`02-architectural-decisions.md`, `03-component-architecture.md`, `09-querybot-v0.2-design.md`, `26-querybot-v0.2.md`, `ARCHITECTURE.md`) states single-use-including-on-failure as an intended trade-off.

**Decision — restore the token on failure; keep single-use on success.**
- In `compute_metrics()`'s `except` handler, before returning the error result, put `resolved` back into `ctx.deps.spec_registry[spec_token]`.
- `resolved` is guaranteed non-`None` inside `except` — the `resolved is None` case (already-consumed token) returns early, before the `try` block begins.
- Rejected alternative: never pop, treat the registry as read-only for the run. This reopens the exact duplicate-execution risk the pop was added to prevent, to fix a narrower problem (retry ergonomics) — the wrong trade.

**Why this is safe under real concurrency — verified, not assumed:**
- Anthropic parallel tool calls in one message are genuinely concurrent in this codebase: pydantic-ai dispatches sync tool functions (`compute_metrics` is a plain `def`, not `async def`) via `anyio.to_thread.run_sync` — real OS threads, not cooperative event-loop scheduling. Confirmed directly: two parallel calls to a sync tool with the same argument, driven through a real `Agent`/`FunctionModel`, showed overlapping execution on two distinct thread IDs.
- `dict.pop()` is atomic under the GIL. At most one concurrent caller can ever receive a non-`None` `resolved` for a given token; every other simultaneous caller gets `None` and bails before entering the `try` block.
- Restore only fires in the `except` branch, never after success. A failed attempt produced no successful compute and no result_id by definition, so restoring its token and letting a later attempt (another duplicate in the same batch, or a genuinely later retry) succeed still yields exactly one successful execution per token, never two.
- Tested adversarially, not just reasoned through: three concurrent threads reproducing the pop → try → (success | restore-on-failure) pattern, one deliberately timed to attempt its pop at the exact moment a failing thread restores the token, still produced exactly one successful execution.

**The safety argument rests on a narrower fact than "pop is atomic," and it's worth stating precisely so it doesn't silently stop holding.** The "at most one caller ever holds `resolved`" invariant requires no suspension point (`await`, or anything else that could yield control to another concurrent caller) between the pop and either a successful return or the failure-triggered restore. That holds trivially today because `compute_metrics` is fully synchronous end to end — the whole pop → compute → return-or-restore sequence runs as one uninterrupted unit on its OS thread. It stops being guaranteed for free if `compute_metrics` is ever made `async def` with a genuine `await` inserted in that span, or if `spec_registry` is ever swapped for something other than a plain in-memory dict (e.g. an async-backed store whose own `pop` has an internal yield point). SF-1 adds a code comment at the pop site recording this dependency, so a future refactor has to consciously reckon with it rather than silently invalidate it.

---

## 1. Scope

**In scope:**
- SF-1 — restore `resolved` to `spec_registry` in `compute_metrics()`'s failure path; document the atomicity dependency at the pop site.
- SF-2 — tests: retry-after-failure, parallel-call protection unaffected, and a required concurrent-race test (the first two are sequential and never exercise the concurrency the original pop exists for).
- SF-3 — document the now-defined semantics (single-use on success, retry-safe on failure) where `compute_metrics` is described.

**Out of scope:**
- Any change to `resolve_intent`/`record_intent` or the resolution flow itself.
- Also noticed in the same function, not fixed here: `03-component-architecture.md` claims `compute_metrics` "Catches `SpecNotFoundError`, `QueryBuildError`, `QueryExecutionError`, `AitaemConnectionError`" — the actual code is a bare `except Exception`, broader than those four types. Corrected as part of SF-3's doc pass since it's the same paragraph, not filed separately.

---

## 2. Sub-features

### SF-1 — Restore token on failure

**File:** `aitaem/agent/query_tools.py`

- In `compute_metrics()`'s `except Exception as exc:` block, restore the popped entry before constructing and returning the error `ComputeMetricsResult`: `ctx.deps.spec_registry[spec_token] = resolved`. No signature change to `compute_metrics()`, `ResolvedSpec`, or `QueryDeps`.
- One-line comment at the `ctx.deps.spec_registry.pop(spec_token, None)` call site, recording the atomicity dependency (see §0): the "at most one caller ever holds `resolved`" guarantee requires no suspension point between this pop and the eventual restore-on-failure. True today because the function is fully synchronous — not automatically true if it's ever made `async def` with an `await` in that span, or if `spec_registry` stops being a plain in-memory dict. This comment is what makes that assumption visible to whoever changes it next.

### SF-2 — Tests

**File:** `tests/test_agent/test_query_bot.py`

- **Retry-after-failure (sequential).** A `FunctionModel`-driven flow where the compute step (mocked `MetricCompute.compute()` or the `.to_pyarrow()`/`store_tabular()` call) raises on the first `compute_metrics(spec_token=X)` call and succeeds on a second call using the same token. Assert: first call returns an error `ComputeMetricsResult` (`result_id == ""`, `error` set); second call succeeds with a real `result_id`.
- **Parallel-call protection unaffected (sequential).** Two sequential `compute_metrics(spec_token=X)` calls where the first succeeds — assert the second still returns the existing "already consumed" error, confirming restore-on-failure doesn't weaken the pop's protection on the success path.
- **Concurrent race case — required, not optional.** The two cases above never exercise the concurrency the pop exists for.
  - Invoke `compute_metrics(ctx, spec_token=X)` directly, bypassing the full `Agent`/`FunctionModel` stack — the race lives entirely in `ctx.deps.spec_registry`, so exercising the tool function directly against a shared `QueryDeps` fixture is higher-signal than routing through the agent.
  - Drive it from two or three genuine OS threads (`threading.Thread`) — matching how `anyio.to_thread.run_sync` actually dispatches parallel sync tool calls in production. `asyncio.gather` alone would not reproduce real thread interleaving for a sync function.
  - Script `MetricCompute.compute()` (mocked) so exactly one concurrent caller raises and the other(s) succeed. Time the failing call's restore (via a `threading.Event`) to land as close as possible to another thread's own pop attempt — this maximizes the chance of provoking a violation, but is not itself the assertion.
  - **Primary, durable assertion — a call-boundary spy, not a success/timing count.** Wrap the mocked `MetricCompute.compute()` with a counter/lock pair tracking maximum concurrent depth. Assert observed max concurrent depth never exceeds 1, for the whole test. This is the assertion that has to survive: "at most one successful `result_id`" can pass by luck even if two threads briefly both entered `compute()` for the same token, and an `Event`-timed assertion window shrinks or vanishes if a future change shifts execution timing — silently turning the test green while the invariant is broken. A concurrency-depth spy at the call boundary catches a violation regardless of how the timing lands.
  - Duplicate-`result_id` / duplicate-success checks stay as secondary assertions — informative, not what this test is graded on.

### SF-3 — Documentation

- `plans/agent_module/09-querybot-v0.2-design.md` §4.4 — add a line: tokens are single-use only on success; a failed `compute_metrics` call restores the token, so the LLM can retry with the same token without a `record_intent`/`resolve_intent` round trip.
- `plans/agent_module/03-component-architecture.md`'s `compute_metrics` description — correct "Catches `SpecNotFoundError`, `QueryBuildError`, `QueryExecutionError`, `AitaemConnectionError`; returns error dicts. Never raises." to reflect the actual bare `except Exception` (broader, not four named types); add the retry-safe-on-failure note above.
- `docs/changelog.md`, `## Unreleased` → `### Fixed`: brief entry noting `compute_metrics` no longer permanently consumes a `spec_token` on a failed compute — matches the audit-trail convention used for other pre-release fixes in this cycle (e.g. Plan 29's `result_id`/`duration_ms` entries).

---

## 3. Files changed summary

| File | Change |
|---|---|
| `aitaem/agent/query_tools.py` | SF-1: restore `spec_token` to `spec_registry` on failure; add atomicity-dependency comment at the pop site |
| `tests/test_agent/test_query_bot.py` | SF-2: retry-after-failure test; parallel-protection regression test; real-thread concurrent-race test with call-boundary spy |
| `plans/agent_module/09-querybot-v0.2-design.md` | SF-3: document single-use-on-success / retry-safe-on-failure semantics |
| `plans/agent_module/03-component-architecture.md` | SF-3: correct exception-handling claim; add retry-safe note |
| `docs/changelog.md` | SF-3: `### Fixed` entry |

---

## 4. Success criteria

- [ ] A `compute_metrics(spec_token=X)` call that fails leaves `spec_token` usable for a subsequent retry with the same token.
- [ ] A `compute_metrics(spec_token=X)` call that succeeds still leaves the token consumed — a second call with the same token still returns "already consumed."
- [ ] The concurrent-race test's call-boundary spy on `MetricCompute.compute()` never observes concurrent depth greater than 1 for a given token, across real OS threads — the assertion that has to hold, independent of the `threading.Event` timing coordination used to provoke the race.
- [ ] `aitaem/agent/query_tools.py`'s pop site carries a comment stating the no-suspension-point dependency this fix relies on.
- [ ] `03-component-architecture.md`'s exception-handling claim matches the actual `except Exception` scope.
- [ ] The single-use-on-success / retry-safe-on-failure semantics — and why the fix is concurrency-safe (pop's GIL-level atomicity, restore only after failure, no suspension point in between) — are stated explicitly in at least one design doc, not left implicit in code comments only.
