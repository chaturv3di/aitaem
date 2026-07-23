# Section 8 — Implementation Order

## Purpose

This section sequences the implementation work in dependency order. Each phase is described at the architectural level — what gets built, why it must come before what follows, and what's blocked until it lands. Detailed implementation plans (file lists, public method signatures, test cases) are downstream of this document — likely produced by Claude Code consuming each phase as a unit.

---

## 0. Hard prerequisite (in AITAEM core, not the agent module) — ✅ COMPLETE

### P0 — AITAEM Ibis-return migration ✅ Shipped in AITAEM v0.4.0

**What:** `MetricCompute.compute()` returns `ibis.Table` instead of `pandas.DataFrame`. Materialization is explicit via `.to_pandas()` / `.to_pyarrow()`. Also delivered in v0.4.0: `tmp_dir` parameter for cross-backend scratch DuckDB, originally on `MetricCompute` itself, later moved onto `ConnectionManager` by Plan 25.

**Status:** Done. The agent module can now build directly against the Ibis-return shape. AD-12, AD-16, AD-17 in Section 2 originally reflected the v0.4.0 reality; AD-16/AD-17 (and AD-12's restatement) were subsequently revised to match Plan 25's `tmp_dir` migration — see Plan 30.

**What this unblocks:** All of Phase 2 onward. Phase 1 (foundations) was already independent of this prerequisite and can start immediately or in parallel with Phase 2 prep.

---

## Phase 1 — Foundations

The minimum scaffolding for everything that follows. Can be developed in parallel with P0.

### P1.1 — Package structure and optional install plumbing

**What:**
- `aitaem.agent` subpackage with `__init__.py` and empty top-level files.
- `pyproject.toml` updated: `agent` extra includes `pydantic-ai[anthropic,openai,...]` and `pydantic-evals` (if shipping reference evals).
- Import-graph CI check enforcing `aitaem.* → aitaem.agent` is forbidden.

**Why first:** Settling the install contract before any internal code so structure can't drift.

**Blocks:** Every subsequent phase imports through this structure.

### P1.2 — Primitives skeleton

**What:**
- `Bot` abstract base class with method signatures and docstrings — no logic.
- `BotResponse[PayloadT]`, `Status` enum, `RunTrace`, `ToolCall`, `Usage` Pydantic models.
- `ResultStore` class with the dual-representation entry shape.
- History I/O surface (`dump_history()` / `load_history()` signatures).

**Why now:** This is the public surface of the primitives layer. Stubbing it first lets P2 / P3 / P4 import and reference real types.

**Blocks:** Convenience bots, default tools.

### P1.3 — Trace assembly from pydantic-ai

**What:** Convert pydantic-ai's internal trace (or OTel spans) into the `RunTrace` shape returned on responses. This is the substrate the eval framework choice depends on, so getting the shape right early matters.

**Why now:** Trace shape is the eval contract (Section 5). Errors here would propagate.

**Blocks:** Anything that returns a real response (every subsequent phase).

---

## Phase 2 — Core QueryBot (post-P0)

The first convenience bot, and the most architecturally load-bearing.

### P2.1 — `compute_metrics` tool against AITAEM v0.4.0

**What:** The tool that constructs `MetricCompute` fresh per call and calls `.compute(...)` (AD-16, revised). Writes (Arrow artifact, Ibis ref) to result store. Returns minimal LLM-facing summary. Catches AITAEM exceptions; returns error dicts.

**Why first inside Phase 2:** Every QueryBot turn that does anything useful starts with this tool. Analysis tools depend on result store entries it creates. `MetricCompute` construction happens inside `compute_metrics` itself, from the `spec_cache`/`connection_manager` the `QueryBot` constructor (P2.4) already holds — no `compute_kwargs` parameter exists (AD-17, dormant).

**Tool summary contract (applies to all Phase 2+ tools):** A tool's return value — the string that becomes `ToolReturnPart.content` and is stored in `ToolCall.llm_summary` — must be a compact, human/LLM-readable snippet. It must never contain raw result data. Metric tables can be thousands of rows; putting that in the summary would overflow context and pollute traces/logs. The full result lives in `ResultStore` only, referenced by `ToolCall.result_id`. A good summary for `compute_metrics` looks like: `"Computed 3 metrics across 2 slices. result_id=abc123"`. Each tool is responsible for producing its own summary string — there is no shared truncation utility.

**Blocks:** Analysis tools, QueryBot integration.

### P2.2 — Analysis tools (lazy-mode-aware)

**What:** `rank_by_value`, `filter_by_threshold`, `distribution_summary`, `period_over_period`, `contribution_share`. Each:
- Reads a prior result store entry by ID.
- Prefers Ibis ref (lazy) over materialized artifact (eager) when both are available.
- Writes a new result store entry.
- Returns minimal LLM-facing summary.

**Why now:** These are part of `QueryBot`'s default identity and need to ship in v1.0.

**Blocks:** QueryBot integration.

### P2.3 — Default system prompt and Metric Precision Rule

**What:** Build the QueryBot default system prompt including:
- Spec catalog assembly from `SpecCache.metrics`/`.slices`/`.segments` (typed attributes; no YAML re-parsing).
- The Metric Precision Rule guardrail (refuse rather than substitute approximate metrics).
- Tool-use guidance.
- Format-aware narration (metrics with `format` set should be narrated as percentages/currency/etc.).

**Why now:** The prompt drives every turn. Building it early lets it iterate against real queries during the rest of Phase 2.

**Blocks:** QueryBot integration testing.

### P2.4 — QueryBot integration

**What:** The `QueryBot` class itself — constructor, default tool set, default prompt, payload type (`QueryPayload`), `ask()` and `chat()` methods.

**Why last in Phase 2:** Requires P2.1, P2.2, P2.3, plus the primitives from Phase 1.

**Blocks:** All real validation of the architecture. First end-to-end test runs here.

### P2.5 — `ask()` / `chat()` parity tests; history dump/load round-trip

**What:** End-to-end tests that:
- Run multi-turn conversations.
- Dump history, instantiate a fresh bot with `history=`, verify `get_result()` on prior turn's result IDs still works.
- Verify trace shape matches `RunTrace`.

**Why now:** Validates AD-04 (bot-as-session), AD-05 (result store + history serialization), AD-08 (trace shape).

**Blocks:** Anything depending on multi-turn working correctly.

---

## Phase 3 — DefinitionBot

The second convenience bot. Depends on Phase 1 + Phase 2 patterns.

> **Phase 3 (Plan 27) note:** DefinitionBot is primarily a **single-turn bot** — `ask()` is
> the primary entry point. Within one `agent.run()` call, the LLM loops through schema
> exploration, drafting, and token-gated validation. `chat()` is provided for cross-turn
> context but multi-turn interactive spec refinement is deferred to v1.x (ND-10).
>
> Detailed implementation plan: `plans/27-agent-phase3.md`.

### P3.0 — Prerequisite: `list_tables()` in aitaem core

**What:** Add `IbisConnector.list_tables(pattern=None) → list[str]` and
`ConnectionManager.list_tables(backend_type=None, pattern=None) → dict[str, list[str]]`.
Small additive change to `aitaem` core (not the agent module).

**Why first:** DefinitionBot's `list_tables` tool calls `ConnectionManager.list_tables()`.
This must exist in core before the tool layer is written.

**Blocks:** SF-3 and SF-4 (list_tables and describe_table tools).

### P3.1 — Type models (`definition_types.py`)

**What:** `DefinitionDeps`, `DefinitionOutput`, `DefinitionPayload`, `DefinitionIntent`,
`SpecDraft`, and all tool result models (`RecordDefinitionIntentResult`, `ListTablesResult`,
`DescribeTableResult`, `DraftSpecResult`, `ValidateSpecResult`, `ValidationIssue`,
`ColumnInfo`).

### P3.2 — Tools (`definition_tools.py`)

**What:** Five tools in 4-step gate order:
1. `record_definition_intent` — capture spec type + description + optional existing YAML
2. `list_tables` — delegate to `ConnectionManager.list_tables()`
3. `describe_table` — schema for one table via `IbisConnector.get_table()`
4. `draft_spec` — store LLM-written YAML in `DefinitionDeps.draft_registry`; return `draft_id`
5. `validate_spec` — 5-check gate: structural + SQL, name conflict, composite cross-ref,
   column existence (live), ResultStore store + spec_draft_token mint

**Why this order:** `draft_spec` / `validate_spec` are the anti-hallucination gate and
require the type models from P3.1. Schema tools (P3.0 prerequisite) unlock P3.2.

### P3.3 — Bot class (`definition_bot.py`)

**What:** `DefinitionBot(Bot)` constructor, `_build_layer_a_definition()`,
`_build_layer_b_definition(spec_cache)`, `_build_agent()`, `chat()`, `ask()`,
`_assemble_payload(output, store)`, `_error_response()`. `DefinitionResponse` typed alias.

**Why last in Phase 3:** Requires all type models and tools from P3.1–P3.2.

### P3.4 — `__init__.py` update and tests

**What:** Add all Phase 3 exports to `aitaem/agent/__init__.py`. Full FunctionModel
integration tests verifying the 4-step flow end-to-end without a real LLM.

**Why this phase:** DefinitionBot has narrower scope than QueryBot but exercises a different
part of the AITAEM API (schema, validation). Sequencing after QueryBot means QueryBot's
patterns are battle-tested before DefinitionBot inherits them.

---

## Phase 4 — SetupBot

The third convenience bot. Lightest of the three.

### P4.1 — Connection-validation tool

**What:** Internal tool that attempts `ConnectionManager.add_connection(...)` in a sandboxed scope and returns success/failure plus diagnostic. Does not retain the connection.

### P4.2 — Default prompt and SetupBot integration

**What:** The `SetupBot` class, default prompt covering supported backend types and credential-input phrasing, and `SetupPayload` (config dict + validation result, never credentials in plaintext on the response).

---

## Phase 5 — Composition primitives

Runtime tool-composition primitives. Lightweight in code, important for the blueprint promise (G2).

> **Reordering note:** Phase 5.2 is intentionally prioritized ahead of Phase 4
> (SetupBot) and Phase 5.1. It depends only on Phase 1 (foundations) and
> Phase 2/3 (QueryBot, DefinitionBot) — not on Phase 4 — so it can proceed
> before SetupBot exists. Detailed implementation plan:
> `plans/28-agent-phase5.2-composition.md`.

### P5.1 — `Bot.as_tool()` — **Status: Deferred, see ND-11**

**What:** Was scoped to return a pydantic-ai-compatible Tool whose JSON schema is derived from the wrapped bot's `ask()` signature, invoking the wrapped bot's `ask()` and returning a structured result the outer agent can consume. Deferred pending a third convenience bot to design against; see ND-11 in [`07-non-decisions.md`](./07-non-decisions.md). Escape valve in the interim: wrap `ask()` as a plain function and register via `add_tool()` (P5.2).

### P5.2 — `Bot(tools=[...])` / `add_tool()` / per-call `extra_tools` — ✅ Shipped

**Status:** Done. Implemented per `plans/28-agent-phase5.2-composition.md`, ahead of Phase 4 and Phase 5.1 per the reordering note above. `Bot.__init__` now enforces a `self._toolset` contract (raises `TypeError` at construction if a subclass's `_build_agent()` doesn't set it); tool-name collisions raise `pydantic_ai.exceptions.UserError` (fail loud, no auto-namespacing); `load_history()` warns if a reloaded bundle references `add_tool()`-added tools not present after reload. Test coverage: `tests/test_agent/test_primitives.py`, `tests/test_agent/test_history.py`, `tests/test_agent/test_query_bot.py`, `tests/test_agent/test_definition_bot.py`, and the cross-bot parametrized suite in `tests/test_agent/test_composition.py`.

**What:** The runtime tool addition surface (AD-11), minus `add_bot()` (sugar over the deferred `as_tool()` — see ND-11). Three surfaces, all against `QueryBot` and `DefinitionBot` (the only convenience bots that exist today):
- `Bot(tools=[...])` — construction-time tools, folded into the bot's default `FunctionToolset` alongside its built-in tools.
- `add_tool()` — persistent runtime addition; mutates the bot's held `FunctionToolset`.
- `extra_tools` on `chat()` / `ask()` — per-call ephemeral tools, passed through to pydantic-ai's per-run `toolsets=...` (additive to construction-time toolsets, not a replacement).

**Why this phase:** These three surfaces are declared in both bots' constructor/method signatures already (Phase 2/3) but are inert — `tools=` and `extra_tools=` are accepted and silently dropped, and `add_tool()` raises `NotImplementedError`. Closing this gap doesn't require SetupBot to exist, so it's pulled ahead of Phase 4.

---

## Phase 6 — Eval substrate validation

The eval substrate is committed by architecture (Section 5); this phase validates it.

### P6.1 — Reference eval harness — ✅ Shipped

**Status:** Done. Implemented per `plans/29-agent-phase6-evals.md`. `tests/evals/` (using pydantic-evals) covers both `QueryBot` and `DefinitionBot` — the original scope was `QueryBot`-only; `DefinitionBot` exists now, so the harness covers it too. Runs in CI via the `evals` job against scripted `FunctionModel`s (no live LLM calls or API keys). Demonstrates that the substrate is wired for evaluators to consume — not a behavioral/quality evaluation of either bot; see `07-non-decisions.md` ND-09.

**What:** A small, opinionated `tests/evals/` directory using pydantic-evals to evaluate `QueryBot`/`DefinitionBot` against a fixture spec catalog and a set of canned questions. Covers:
- Tool-selection correctness (did the agent call `compute_metrics` with the right spec?).
- Refusal correctness (did the agent refuse out-of-scope questions with `status=refused`?).
- Deterministic correctness (does the dereferenced result match the known ground truth?).

Doubles as the canonical "how to evaluate your AITAEM agent" example.

### P6.2 — OTel span emission validation — ✅ Shipped

**Status:** Done. Implemented per `plans/29-agent-phase6-evals.md`. `tests/test_agent/test_otel_spans.py` instruments a real, `FunctionModel`-driven `agent.run()` with an in-memory OTel span exporter and asserts span count, tool-call IDs, order, and duration all agree with `RunTrace` — real span capture, not mocks, since `assemble_trace()`'s mock-based logic was already covered by `test_trace.py`.

**What:** Tests that `RunTrace` and the underlying spans pydantic-ai emits are consistent — that an eval framework consuming spans sees the same tool calls and arguments as the `RunTrace` does.

**Why this phase:** Confirms the eval substrate works *before* v1.0 ships. After ship, any drift is a breaking change.

---

## Phase 7 — Documentation and v1.0 release

### P7.1 — Public API docs

Auto-generated from docstrings for all public classes / methods. Manually authored:
- Getting-started example for each convenience bot.
- "Building your own bot" guide using the primitives.
- "Evaluating your agent" guide referencing P6.1.

### P7.2 — v1.0 release

`pip install aitaem[agent]==1.0.0`.

---

## Phase order summary

```mermaid
flowchart LR
    P0["P0: AITAEM Ibis migration<br/>(✅ done — v0.4.0)"]:::done --> P2
    P1[Phase 1: Foundations] --> P2
    P1 --> P3
    P1 --> P4
    P2[Phase 2: QueryBot] --> P3[Phase 3: DefinitionBot]
    P2 --> P4[Phase 4: SetupBot]
    P2 --> P5[Phase 5.2: Composition]:::done
    P3 --> P5
    P2 --> P6[Phase 6: Eval validation]:::done
    P3 --> P6
    P4 --> P6
    P5 --> P6
    P6 --> P7[Phase 7: Docs + v1.0]
    classDef done fill:#dfd,stroke:#393
```

Phase 1 is independent of P0 and can start immediately. Phase 2 can start as soon as Phase 1's primitives skeleton is in place. Phase 5.2 depends only on Phase 2 and Phase 3 (QueryBot, DefinitionBot) — not Phase 4 — and is reordered ahead of it; Phase 5.1 (`Bot.as_tool()`) is deferred (ND-11) and dropped from this graph. Phase 6 validates everything before ship.

---

## Estimated relative effort

Architectural estimates, not commitments. For Claude Code's downstream planning:

| Phase | Relative effort | Risk |
|---|---|---|
| P0 (AITAEM v0.4.0) | ✅ Done | — |
| Phase 1 — Foundations | ✅ Done | — |
| Phase 2 — QueryBot | ✅ Done | — |
| Phase 3 — DefinitionBot | ✅ Done | — |
| Phase 4 — SetupBot | Small | Low |
| Phase 5.2 — Composition | ✅ Done | — |
| Phase 6 — Eval validation | ✅ Done | — |
| Phase 7 — Docs + ship | Medium | Low |

The bulk of architectural risk concentrates in Phase 2. Specifically:
- Trace assembly faithfulness (does the RunTrace actually contain what the eval substrate promises?).
- Tool-summary-vs-result-store split (is the LLM-facing summary always sufficient?).
- Metric Precision Rule effectiveness (does the agent actually refuse rather than substitute?).

Each of these has been worked through against concrete scenarios in the design process, which is why I'm calling risk medium and not high.

---

## What's NOT in the implementation order

- Streaming surface (ND-01).
- Event observability hooks (ND-02).
- Error taxonomy refinement (ND-03).
- Prompt-fragment-override API (ND-04).
- Hot-reload of SpecCache (ND-07).
- Generic `Bot.as_tool()` / `add_bot()` bot-as-tool composition (ND-11).

These are tracked in Section 7 with escape valves. They're v1.x or v2.0 candidates, not v1.0 implementation work.

---

## Appendix: Open Questions

### OQ-A1 — Context-window management via `ProcessHistory`

Tracked in Phase 1 foundations. Built into the `Bot` docstring as a reference pattern; no implementation shipped in v1.0.

### OQ-A2: `compute_metrics` segment join-key override

**Problem:** `MetricCompute.compute()` accepts `segments` as either `str` (segment name,
uses spec's default join key) or `dict[str, str]` (name → custom join key). The Phase 2
`compute_metrics` tool only exposes the string form.

**Impact:** Users who need to override the join key via the LLM interface cannot do so
with the default QueryBot. They would need to call `MetricCompute.compute()` directly
or build a custom tool.

**Decision trigger:** When a user reports needing non-default join key selection
through the LLM interface (likely rare; most specs have a single natural join key).

**Implementation path when triggered:** Add an optional `segment_join_key: str | None`
parameter to the `compute_metrics` tool. Construct `segments={segment: segment_join_key}`
when both are provided.

---