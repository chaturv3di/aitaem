# Plan 29 — Agent Phase 6: Eval Substrate Validation

**Branch:** `feature/evals-substrate` (current)
**Depends on:** Phase 2 (QueryBot), Phase 3 (DefinitionBot), Phase 5.2 (Composition — for `_toolset`/tool-registration shape referenced by test helpers). Does **not** depend on Phase 4 (SetupBot).
**Architecture references:** `plans/agent_module/ARCHITECTURE.md` §5 (Evals Substrate) and §8 (Phase 6); `plans/agent_module/05-evals.md`; `plans/agent_module/07-non-decisions.md` (ND-09).

---

## 0. Key Decisions

**D1 — Ship a reference eval harness (ND-09/OQ-2), covering both bots.**
`tests/evals/`, using `pydantic-evals`, covering both `QueryBot` and `DefinitionBot`.

**D2 — Fix `ToolCall.result_id` and `ToolCall.duration_ms` now, at the root cause.**
- Both fields are declared on `ToolCall` and documented in `05-evals.md`, but `assemble_trace()` never populates either per call.
- `result_id`: fixed at its source, not patched at the trace layer. `ValidateSpecResult` gets a real `result_id` field, honoring the `ToolResult` protocol (`03-component-architecture.md` §2). `spec_draft_token` becomes a `computed_field` derived from `result_id`, so the two values are structurally incapable of disagreeing. `assemble_trace()` then reads one canonical field.
- `duration_ms`: computed from `ModelResponse.timestamp` (call start) and `ToolReturnPart.timestamp` (call end) — both confirmed to exist on the installed `pydantic-ai-slim`.
- Both are prerequisites for SF-6/SF-7's deterministic-correctness eval cases. See SF-1.

**D3 — P6.2 uses real OTel span capture, not mocks.**
An in-memory span exporter wraps a real, `FunctionModel`-driven `agent.run()`; captured spans are asserted consistent with `RunTrace.tool_calls`. `test_trace.py` already covers `assemble_trace()`'s mock-based internal logic — this validates pydantic-ai's actual instrumentation. See SF-4.

**D4 — `ValidateSpecResult`'s constructor change is a breaking, public API change — ship it anyway, documented.**
- `ValidateSpecResult` is exported (`aitaem.agent.__all__`), so D2's fix changes a public constructor: `spec_draft_token=` is no longer a valid keyword argument.
- `aitaem.agent` (Phases 2/3/5.2) has never appeared in a released version — the changelog's latest tag, `v0.4.0`, predates it entirely — so the real-world blast radius is effectively zero.
- `ValidateSpecResult` gains `model_config = ConfigDict(extra="forbid")`, so the old call shape fails loudly (`ValidationError`) instead of silently constructing a `None`-valued object (pydantic's default `extra="ignore"` behavior, verified).
- Documented under `### Breaking changes` in the changelog, not folded into `### Fixed`. See SF-1, SF-9.

**Eval library (OQ-1) — already resolved, no new decision needed.**
`pydantic-evals`, resolved in Plan 23 / Phase 1. Ships via the `agent-evals` extra (`pydantic-ai-slim[evals]`) and is already present in `[dev]`.

**Confirmed against the installed environment (not assumptions):**
- `pydantic-ai-slim[evals]>=2.2.0` transitively installs `pydantic-evals` (PyPI metadata: `pydantic-evals==2.12.0; extra == "evals"`).
- `ToolReturnPart.content` on a real (non-mocked) run preserves the tool's original Python return object, not a stringified version. `model_response_str()` is a separate, on-demand stringification used only for what's sent to the LLM — so SF-1 can read `.result_id` directly off `content`.
- `opentelemetry-api` is already installed (required by `pydantic-ai-slim`); `opentelemetry-sdk` (needed for `TracerProvider`/`InMemorySpanExporter` in tests) is not — added in SF-3.
- `Agent.instrument` is a plain settable property with no side effects. It can be set on an already-constructed `bot._agent` per test, with no new constructor parameter and no global OTel state mutation.

---

## 1. Scope

**In scope:**
- SF-1/SF-2 — `ToolCall.result_id`/`duration_ms` population fix, plus tests.
- SF-3/SF-4 — P6.2: real OTel span-emission consistency validation.
- SF-5–SF-8 — P6.1: reference eval **harness** (`tests/evals/`) for both bots, wired into CI. Demonstrates the substrate is consumable by `pydantic_evals` — not a behavioral/quality evaluation of either bot (see SF-6).
- SF-9 — changelog entry.
- SF-10 — mark Phase 6 complete; resolve ND-09/OQ-2 everywhere it's referenced.

**Out of scope (deferred, not silently dropped):**
- **P7.1's "Evaluating your agent" docs guide** — Architecture §8 places this in Phase 7, after the harness exists to document. `tests/evals/` itself is the interim reference.
- **SetupBot eval coverage** — SetupBot (Phase 4) doesn't exist yet.
- **RAG / deepeval integration** — no RAG flows exist in the codebase yet.
- **Any change to `RunTrace`/`ToolCall`'s field set** — SF-1 populates existing fields only; AD-08's semver-stability guarantee on `RunTrace`/`BotResponse` shapes is preserved.
- **Behavioral/quality evaluation against a live model.** Every `tests/evals/` case runs a scripted `FunctionModel` built to produce the outcome its evaluator checks for (CI correctly forbids live LLM calls). This proves the substrate — `RunTrace`, `ResultStore`, `BotResponse` — is wired for `pydantic_evals.Evaluator`s to consume. It does not measure whether an LLM selects tools correctly, refuses appropriately, or reasons to a correct answer. The harness makes live-model evaluation *possible* (swap the `FunctionModel` outside CI) but doesn't deliver it.

---

## 2. Sub-features

### SF-1 — `ValidateSpecResult.result_id`, and `ToolCall.result_id`/`duration_ms` population

**Root cause, not the symptom:**
- `03-component-architecture.md` §2 defines the `ToolResult` protocol as exposing `result_id`. Every `query_types.py` `ToolResult` subclass honors this; `ValidateSpecResult` doesn't — it exposes `spec_draft_token` instead, even though `validate_spec()` already mints that value via `ctx.deps.store.store_text(...)`. It's a `ResultStore` ID under a different name.
- Fix the contract at its source: `ValidateSpecResult` gets a real `result_id` field. `spec_draft_token` becomes a `@computed_field` derived from it (`result_id or None`) — not a second, independently-settable field.
- Two fields carrying "the same value by convention" is the same bug class as the original gap, one level down. Deriving one from the other makes divergence structurally impossible, not just untested — no `model_validator` equality check needed, because there's nothing left to validate.
- `assemble_trace()` then reads one canonical field (`content.result_id`) — no fallback list to maintain as new tools are added.

**Why `spec_draft_token` stays LLM-facing and `result_id` doesn't:**
- `spec_draft_token` exists so the LLM has one unambiguous token to copy verbatim into `DefinitionOutput.spec_draft_token`. A second, differently-named field carrying the same value would reintroduce that exact ambiguity.
- `result_id: str | None = Field(default=None, exclude=True)` — `exclude=True` keeps it out of `model_dump()`/`model_dump_json()` (the serialization pydantic-ai sends the LLM), while leaving plain Python attribute access intact for `assemble_trace()` and `tests/evals/`'s `ResultStore` lookups.
- Verified directly: `model_dump()` on this shape returns `{"spec_draft_token": ...}` only — `result_id` doesn't appear.

**Breaking-change note — confirmed, not assumed:**
- `ValidateSpecResult` is exported from `aitaem.agent` (`definition_types.py` import + `__all__`), so external code can construct it directly.
- Before this fix, `ValidateSpecResult(spec_draft_token="x")` was the correct call. After, `spec_draft_token` isn't a constructor argument.
- Verified against pydantic v2's actual default behavior: without further changes, the old call would silently succeed via `extra="ignore"`, dropping `spec_draft_token` and landing both fields on `None` — a silent behavior change, not an error.
- Fix: add `model_config = ConfigDict(extra="forbid")`. The old call now raises `ValidationError` naming `spec_draft_token` as an extra input.
- Deliberate, acknowledged breaking change to a public constructor. See §0 (D4) for the decision, SF-9 for the changelog entry.

**Files:**
- `aitaem/agent/definition_types.py` — on `ValidateSpecResult`:
  - `model_config = ConfigDict(extra="forbid")`.
  - `result_id: str | None = Field(default=None, exclude=True)` — the real, stored field, set only when all five `validate_spec` checks pass.
  - `spec_draft_token` — `@computed_field` `@property` returning `self.result_id or None`.
  - No other `definition_types.py` result types change — `RecordDefinitionIntentResult`, `ListTablesResult`, `DescribeTableResult`, `DraftSpecResult` mint no `ResultStore` entries.
- `aitaem/agent/definition_tools.py` — `validate_spec()`'s success path constructs `ValidateSpecResult(result_id=..., warnings=..., referenced_columns=...)`; drops the now-nonexistent `spec_draft_token=` kwarg; renames the local variable to `result_id`.
- `aitaem/agent/trace.py` — in `assemble_trace()`'s `ToolReturnPart` loop, alongside the existing `llm_summary`/`success` extraction: read `content.result_id`; capture a per-call start time when building `pending` (`ToolCallPart` loop) and an end time from the matching `ToolReturnPart`; set `ToolCall.duration_ms` from the difference.

**`duration_ms` — what it measures, and why it's a safe over-approximation:**
- `assemble_trace()` never populates `ToolCall.duration_ms` per call today — only the whole-turn `RunTrace.duration_ms` aggregate is set.
- Formula: `duration_ms = (ToolReturnPart.timestamp - ModelResponse.timestamp).total_seconds() * 1000`, per call.
- Proven, not just likely, to always be `>=` true execution time, never `<`:
  - `ModelResponse.timestamp` (`field(default_factory=_now_utc)`) is set when the response carrying the tool-call request is received — before that tool executes.
  - `ToolReturnPart.timestamp` (same default factory) is set at construction, immediately after `execute_tool_call()` returns — after execution finishes.
- For parallel tool calls in one `ModelResponse`, every call shares the start timestamp; each still gets its own accurate end timestamp.
- This mechanism has to work without OpenTelemetry: `assemble_trace()` runs on every turn regardless of whether `bot._agent.instrument` is set, and `opentelemetry-sdk` is `[dev]`-only (SF-3), not a production dependency. Message timestamps are the only signal available unconditionally.
- SF-4's OTel spans measure the true execution window directly, but only when instrumentation is enabled. SF-4 adds a test asserting `duration_ms` is never less than the corresponding span's true duration — documenting this relationship as a checked invariant.

**Behavior:**
```python
def _extract_result_id(content: Any) -> str | None:
    """Pull a ResultStore pointer off a tool's structured return value, if present.

    Reads the single canonical `result_id` attribute (per the ToolResult
    protocol, 03-component-architecture.md §2) — every tool result type that
    mints a ResultStore entry exposes it under this name, including
    ValidateSpecResult (this SF adds it there as the source of spec_draft_token).
    Returns None if the attribute doesn't exist (tools that mint no store
    entry, e.g. record_intent/list_tables) or is falsy (covers the
    `result_id=""` failure sentinel used by query_types.ToolResult subclasses).
    """
```
- Called only when `content` isn't a `str` (matches the existing `isinstance(content, str)` branch used for `llm_summary`).
- `tc["result_id"]` set unconditionally — `None` is a valid, expected value for tools that don't mint store entries.
- `tc["duration_ms"]` computed unconditionally from the two timestamps described above.
- No change to `assemble_trace(result, run_start)`'s signature or the `RunTrace`/`ToolCall` shapes — both fields already exist (Phase 1 foundations); this SF only fixes their population, and gives `ValidateSpecResult` the field it should have had from the start.

### SF-2 — Tests for SF-1

**`ValidateSpecResult` unit tests** (`tests/test_agent/test_definition_bot.py`, or a new `test_definition_types.py`):
- `ValidateSpecResult(result_id="abc").spec_draft_token == "abc"`
- `ValidateSpecResult(result_id=None).spec_draft_token is None`
- `ValidateSpecResult(result_id="").spec_draft_token is None` (the `or None` sentinel-folding case)
- `ValidateSpecResult(result_id="abc").model_dump()` contains `spec_draft_token` only — `"result_id"` absent from the dumped keys (locks in the LLM-facing contract)
- `pytest.raises(ValidationError)` around `ValidateSpecResult(spec_draft_token="x")` (the pre-fix call), asserting the error names `spec_draft_token` as extra/forbidden (locks in the breaking-change guard)
- No "set one, check the other diverges" case is needed — that bug class is structurally impossible now, which is what the cases above verify by construction.

**`assemble_trace()` unit tests** (`tests/test_agent/test_trace.py`), using `MagicMock(spec=ToolReturnPart)` stand-ins:
- `content.result_id = "abc123"` → `ToolCall.result_id == "abc123"`
- `content.result_id = ""` → `None`
- no `result_id` attribute at all → `None`
- `duration_ms`: mock `ModelResponse.timestamp`/`ToolReturnPart.timestamp` at a known, fixed offset (e.g. 250ms) → `ToolCall.duration_ms` equals that offset
- parallel calls: two `ToolCallPart`s in one `ModelResponse`, two `ToolReturnPart`s with different timestamps → each `duration_ms` computed from its own return timestamp against the shared start

**Regression tests against real (non-mocked) runs:**
- `tests/test_agent/test_query_bot.py` — append to the existing "3-step flow" section (`_make_bot_with_model(_make_three_step_model())`): the `"compute_metrics"` trace entry has non-`None` `result_id` equal to `response.payload.primary_result_id`, and non-`None` `duration_ms >= 0`.
- `tests/test_agent/test_definition_bot.py` — append near `test_full_flow_returns_status_ok`: the `"validate_spec"` trace entry has `result_id == response.payload.spec_draft_token`, and non-`None` `duration_ms`.
- These two are what would have caught the original gap during Phase 2/3 — they exercise real, non-mocked `ToolReturnPart.content`, which `test_trace.py`'s mocks alone can't guarantee.

**A structural contract test, closing the recurrence gap the tests above don't cover:**
- The tests above verify known tools (`compute_metrics`, `validate_spec`) only. A *future* tool that writes to `ResultStore` under a field name other than `result_id` produces no test failure anywhere — `_extract_result_id` returns `None` silently by design, so there's no way to distinguish "this tool legitimately writes nothing" from "this tool writes something, named wrong." A per-tool regression test can't close this gap by construction.
- **New file:** `tests/test_agent/test_result_id_contract.py` — discovers tools rather than testing them one by one, so it automatically covers tools that don't exist yet.

```python
def _tool_functions_that_write_to_store() -> Iterator[tuple[str, Callable[..., Any]]]:
    """AST-scan every aitaem/agent/*_tools.py module for top-level function
    definitions whose body contains a call matching `<expr>.store_tabular(...)`
    or `<expr>.store_text(...)` (matched syntactically, by attribute-call name
    — not by resolving the receiver to a specific ResultStore instance, since
    that would require executing the tool). Yields (qualified_name, function)
    for each match, resolving the function object from the corresponding
    module's namespace via getattr/importlib.

    Glob-based file discovery (aitaem/agent/*_tools.py), not a fixed list of
    tool modules, so a future tools module (e.g. setup_tools.py once SetupBot
    ships) is covered with no change to this function or the test using it.
    """

def test_every_store_writing_tool_exposes_result_id():
    """For every (name, fn) yielded by _tool_functions_that_write_to_store(),
    resolve fn's declared return type (typing.get_type_hints(fn)["return"])
    and assert "result_id" appears in ReturnType.model_fields or
    ReturnType.model_computed_fields. Failure message names the offending
    tool and its return type directly, so a future violation fails at CI
    time, at the exact tool that introduced it — not as a silent None
    discovered later, if ever, by a human reading a trace.
    """
```

This mirrors SF-1's own fix, one level up: enforce the contract at its source (test-suite scope) rather than special-case each known violation.

### SF-3 — Add `opentelemetry-sdk` to `[dev]`

**File:** `pyproject.toml`

**What:** Add `"opentelemetry-sdk>=1.28.0"` (matches the existing `opentelemetry-api>=1.28.0` floor pulled in transitively by `pydantic-ai-slim`) to the `dev` extra.

**Why `[dev]`, not `[agent-evals]` or a new extra:**
- Needed only to *test* the OTel-compatibility claim (SF-4) — not something `aitaem.agent` imports in production code.
- Downstream users evaluating their own deployments bring their own OTel SDK/exporter (Logfire, Datadog, etc.).
- Mirrors how `pytest`/`mypy`/`ruff` are `[dev]`-only despite being essential to CI.

**CI impact:** None beyond the dependency flowing through the existing `.[agent-anthropic,dev]` install step already used by the `test-agent` job. SF-4's new test file needs no new CI job.

### SF-4 — OTel span-emission consistency tests (P6.2)

**New file:** `tests/test_agent/test_otel_spans.py`

**What:** Validates that `RunTrace` is faithful to what pydantic-ai actually emits as OpenTelemetry spans — not just that `assemble_trace()`'s mock-based logic is internally consistent (already covered by `test_trace.py`).

**Why the cross-check carries real signal, not tautological agreement:**
Worth writing only if `RunTrace` and the captured spans come from genuinely independent code paths. Confirmed by reading the installed `pydantic-ai-slim` source:
- `assemble_trace()` builds `RunTrace` entirely from `result.new_messages()` — the final, immutable message-part list, assembled *after* the run completes. `ToolCall.success` comes from `ToolReturnPart.outcome`, parsed post-hoc.
- OTel spans are emitted *live*, during execution, by `wrap_tool_execute` → `_run_tool_span`, which wraps the actual async tool call in `tracer.start_as_current_span(...)`. Span attributes: `gen_ai.tool.name = call.tool_name`, `gen_ai.tool.call.id = call.tool_call_id` — the same `tool_call_id` string `assemble_trace()` keys its `pending` dict by, so spans can be matched to `ToolCall` entries exactly, not just by name-set. Span failure is set directly inside a live `try/except` around the call, not re-derived from `ToolReturnPart.outcome` afterward.

Identifying data (name, `tool_call_id`) necessarily traces to the same underlying call — there's only one. But *count*, *order*, and *success/failure* are computed via two independent mechanisms. A bug in one wouldn't automatically reproduce in the other, so agreement is real evidence of internal consistency, not a tautology.

**Helper:**
```python
@contextlib.contextmanager
def _captured_spans(bot: Bot) -> Iterator[list[ReadableSpan]]:
    """Instrument `bot._agent` with a local, in-memory OTel span exporter for
    the duration of the `with` block, then yield the list of spans captured.

    Builds a fresh opentelemetry.sdk.trace.TracerProvider + InMemorySpanExporter
    + SimpleSpanProcessor per call (no global `set_tracer_provider()` call —
    the provider is passed directly to InstrumentationSettings, and
    bot._agent.instrument is restored to its prior value on exit). This keeps
    span capture scoped to a single test with no cross-test leakage.
    """
```

**Test cases** (parametrized or duplicated across both bots, reusing each test file's existing FunctionModel fixtures — `_make_three_step_model()` from `test_query_bot.py`, the full-flow model from `test_definition_bot.py`).

Captured spans are filtered to `gen_ai.operation.name == "execute_tool"` first:
- `wrap_tool_execute` shares span machinery with output-processing spans (`_run_tool_span` also backs `wrap_output_process`), so in general an unfiltered span list isn't directly comparable to `tool_calls` count.
- In this specific case the risk doesn't materialize: `QueryBot`/`DefinitionBot` both construct their `Agent` with a plain `output_type` (`QueryOutput`/`DefinitionOutput`), not a function-based output validator, so `wrap_output_process`'s `output_context.has_function` guard is always `False` and no output-process span is ever emitted for these bots. Filtering by `gen_ai.operation.name` is correct regardless and keeps the test from silently depending on that fact.

- `test_span_count_matches_tool_call_count` — number of `execute_tool` spans equals `len(response.trace.tool_calls)`.
- `test_span_tool_call_ids_match_trace` — `{(span.attributes["gen_ai.tool.name"], span.attributes["gen_ai.tool.call.id"])}` equals `{(tc.name, tc.tool_call_id)}` exactly — an ID-level match, stronger than name-only, since `gen_ai.tool.call.id` is confirmed to equal `ToolCallPart.tool_call_id` verbatim.
- `test_span_order_matches_trace_order` — captured span start-time ordering matches `response.trace.tool_calls`'s order.
- `test_duration_ms_covers_span_duration` — for each `(span, tool_call)` pair matched by `tool_call_id`, compute `span_duration_ms = (span.end_time - span.start_time) / 1e6` (OTel `ReadableSpan` timestamps are nanoseconds); assert `tool_call.duration_ms >= span_duration_ms`. Checks the invariant documented in SF-1: the message-timestamp window strictly contains the span's true-execution window, so the aggregate can never under-report — only over-report by the surrounding agent-loop overhead.

**Confirmed against the installed `pydantic-ai-slim` version (not assumed):**
- `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id` span attributes, and the `wrap_tool_execute`/`_run_tool_span` code path in `pydantic_ai/capabilities/instrumentation.py`. These are internal pydantic-ai attribute names, not part of `aitaem`'s own contract — if a future upgrade renames them, this test (not `aitaem/agent/trace.py`) is what breaks, which is the intended blast radius.
- `ModelResponse.timestamp` and `ToolReturnPart.timestamp` are both `field(default_factory=_now_utc)` (`pydantic_ai/messages.py`); `ToolReturnPart` construction in `_tool_execution.py` happens strictly after `execute_tool_call()` returns — making the `>=` assertion above a provable invariant, not an empirical tendency that could flake.

### SF-5 — `tests/evals/` package scaffold

**New files:** `tests/evals/__init__.py` (empty), `tests/evals/_fixtures.py`.

**Design — self-contained, not reusing `tests/test_agent/*` private helpers:**
- `tests/evals/` doubles as the reference example ND-09/OQ-2 ships (blueprint philosophy, G2) — a user should read and copy it without also understanding `tests/test_agent/`'s internal fixtures.
- `_fixtures.py` defines its own minimal spec-cache/connection-manager/ground-truth fixtures, duplicating a small amount of structure already present in `test_query_bot.py`/`test_definition_bot.py`. Deliberate, bounded exception to "don't duplicate" — legibility in isolation is the point.

**Drift risk from the duplication, and how it's bounded:**
- SF-6's locally-redefined refusal-triggering `FunctionModel` mirrors `test_query_bot.py`'s `_make_refused_model` shape. If the canonical helper's shape changes (e.g. the refusal-tool-call sequence changes with `QueryBot`'s resolution flow), the local copy can silently go stale and mislead a reader into believing it still reflects the real refusal path.
- Self-containment is still the right call — a copyable reference shouldn't require cross-referencing `tests/test_agent/`. Mitigation: a one-line comment at the local copy's definition site pointing back to the canonical helper by name and file (`# Mirrors _make_refused_model() in tests/test_agent/test_query_bot.py — keep in sync in spirit, not by import`) — no shared code, just a discoverability trail.

**Design — `tests/evals/` is mypy-covered; the rest of `tests/` is not:**
- mypy's default scope is `aitaem/` only (§4); this plan carves in `tests/evals/` specifically (SF-8), leaving `tests/test_agent/*` and the rest of `tests/` outside it (heavy `MagicMock(spec=...)` usage there isn't worth fighting the type checker over).
- The carve-in matters because `tests/evals/`'s entire value as a blueprint sits in the `pydantic_evals.Evaluator[InputsT, OutputT, MetadataT]` generic and `EvaluatorContext[InputsT, OutputT, MetadataT]` parameterization (SF-6/SF-7) — exactly where a copying user hits type errors first.
- `tests/evals/` is written to pass `mypy` cleanly at the same default, non-strict settings already configured for `aitaem/` — no new strictness, only broadened coverage.

**Constraint — relative imports only, confirmed against this exact repo layout, not assumed:**
- `tests/` has no `__init__.py` (root-level files like `test_insights.py` sit directly under it); `tests/evals/` will, matching the existing convention in `tests/test_agent/`, `tests/test_connectors/`, etc.
- Reproduced `mypy aitaem/ tests/evals/` (SF-8's exact CI command) against a scratch copy of the planned layout: with `test_query_bot_evals.py` importing `_fixtures` via `from tests.evals._fixtures import ...` (absolute), mypy fails with `Source file found twice under different module names: "evals._fixtures" and "tests.evals._fixtures"` — because a bare `tests/evals/` CLI root (no `__init__.py` above it) resolves as top-level package `evals`, while the absolute import resolves the same file as `tests.evals._fixtures`.
- Fix: relative imports (`from ._fixtures import ...`). Verified clean under both `mypy aitaem/ tests/evals/` and `pytest tests/evals/ --collect-only` against the same reproduction.
- `test_query_bot_evals.py`/`test_definition_bot_evals.py` must import `_fixtures` as `from ._fixtures import ...`, never `from tests.evals._fixtures import ...`.

**Contents:**
```python
GROUND_TRUTH_REVENUE_TABLE: pa.Table
"""Fixed ground-truth Arrow table for the deterministic-correctness eval case.
Small, hand-constructed (a handful of rows) — not derived from any real backend."""

def make_query_bot_fixture(model: Any) -> QueryBot:
    """Build a QueryBot against a minimal one-metric SpecCache stand-in and a
    MagicMock ConnectionManager, with aitaem.agent.query_tools.MetricCompute
    patched so compute_metrics() deterministically returns
    GROUND_TRUTH_REVENUE_TABLE regardless of the (fake) backend."""

def make_definition_bot_fixture(model: Any) -> DefinitionBot:
    """Build a DefinitionBot against a MagicMock ConnectionManager exposing one
    `transactions` table with a fixed two-column schema (amount, transaction_date)."""
```

### SF-6 — `tests/evals/test_query_bot_evals.py`

**Framing — harness demonstration, not a behavioral eval:**
- CI correctly forbids live LLM calls, so every `Case` is driven by a hand-scripted `FunctionModel` that already knows which tool to call and in what order — the assertions are near-tautological by construction: the tool-selection case scripts `compute_metrics` and asserts `CalledTool("compute_metrics")`; the refusal case scripts a refusal and asserts `Status.refused`; `ResultMatchesGroundTruth` patches `MetricCompute` to return `GROUND_TRUTH_REVENUE_TABLE` and then asserts the stored result equals it.
- None of the three cases measure whether an LLM *would* select the right tool, refuse appropriately, or produce a correct answer. They measure that `RunTrace`, `ResultStore`, and `BotResponse` are consumable by `pydantic_evals.Evaluator`s — i.e. that Architecture §5's substrate is wired correctly end to end.
- That's a legitimate, valuable thing to ship on its own (the G2 blueprint promise — point this same harness at a live model outside CI and the wiring already works), but it's substrate validation, not agent-quality evaluation. Every place this harness is described (docstrings, changelog, SF-9/SF-10) must say so, so a reader copying `tests/evals/` doesn't mistake "the harness runs and passes" for "the agent was evaluated."

**What:** A `pydantic_evals.Dataset` of three `Case`s against `task(inputs: QueryEvalInput) -> QueryEvalOutput`, which builds a fresh `QueryBot` per case (SF-5's fixture) with a case-specific `FunctionModel`, runs `bot.ask(inputs.question)`, and returns both the response and a handle back to the bot (so evaluators can call `bot.get_result(...)`).

**Signatures:**
```python
@dataclass
class QueryEvalInput:
    question: str
    model: FunctionModel  # drives the fake LLM's tool-calling behavior for this case

@dataclass
class QueryEvalOutput:
    response: BotResponse  # QueryResponse
    bot: QueryBot          # for get_result() lookups in evaluators

async def query_bot_task(inputs: QueryEvalInput) -> QueryEvalOutput:
    ...

class CalledTool(Evaluator[QueryEvalInput, QueryEvalOutput, None]):
    """Asserts a named tool appears in output.response.trace.tool_calls."""
    tool_name: str
    def evaluate(self, ctx: EvaluatorContext[QueryEvalInput, QueryEvalOutput, None]) -> bool: ...

class StatusIs(Evaluator[QueryEvalInput, QueryEvalOutput, None]):
    """Asserts output.response.status == the configured Status."""
    expected: Status
    def evaluate(self, ctx: EvaluatorContext[QueryEvalInput, QueryEvalOutput, None]) -> bool: ...

class ResultMatchesGroundTruth(Evaluator[QueryEvalInput, QueryEvalOutput, None]):
    """Finds the compute_metrics ToolCall, asserts its result_id is non-None,
    calls bot.get_result(result_id) (returns the ResultEntry union — TabularEntry
    | TextEntry, per aitaem/agent/store.py), narrows with isinstance(entry,
    TabularEntry) (compute_metrics always writes a TabularEntry; this satisfies
    both a real runtime guard and mypy's union-attribute check under SF-8), then
    asserts entry.arrow.equals(GROUND_TRUTH_REVENUE_TABLE). This case only became
    writable once SF-1 populated ToolCall.result_id."""
    def evaluate(self, ctx: EvaluatorContext[QueryEvalInput, QueryEvalOutput, None]) -> bool: ...
```

**Confirmed facts behind the signatures above:**
- `TabularEntry.arrow: pa.Table | None` is the field name — not `.table` or a `.to_arrow()` method. `02-architectural-decisions.md`'s "dual-rep (artifact + optional ibis ref)" phrasing doesn't pin an accessor name, so this was checked directly against `aitaem/agent/store.py`.
- `Bot.get_result(result_id) -> ResultEntry` returns the `TabularEntry | TextEntry` union, not `TabularEntry` directly — the `isinstance` narrow above is required, not optional, for correctness (`TextEntry` has no `.arrow`) and for mypy.
- `Evaluator`, `EvaluatorContext`, `Case`, and `Dataset` are all `Generic[InputsT, OutputT, MetadataT]` — three type parameters, not two — checked against the installed `pydantic_evals` package. None of these cases use per-case `metadata=`, so `MetadataT = None` throughout. Getting this arity wrong is exactly the error a copying user would hit, and only `mypy tests/evals/` (SF-8) catches it — `ruff` doesn't flag a missing type parameter.

**Cases** (exercising the wiring for the three eval-dimension *shapes* named in Architecture §5 — scripted, not measured; see the framing note above):
1. **Tool-selection wiring** — "What was total revenue?"; `FunctionModel` drives `record_intent → resolve_intent → compute_metrics`; evaluator: `CalledTool("compute_metrics")`. Proves the evaluator can read tool-selection from `RunTrace`; does not test whether an LLM selects correctly.
2. **Refusal wiring** — an out-of-catalog metric question (e.g. "What was sales velocity?", mirroring `_make_refused_model`'s shape from `test_query_bot.py`, redefined locally per SF-5's self-containment rule); evaluator: `StatusIs(Status.refused)`. Proves the evaluator can read status; does not test whether an LLM refuses appropriately.
3. **Deterministic-correctness wiring** — same question as case 1; evaluator: `ResultMatchesGroundTruth()`. Proves `bot.get_result(result_id)` round-trips through `ResultStore` correctly against a fixed ground-truth table; does not test correctness of an LLM-driven computation, since `MetricCompute` is patched to return the ground-truth table regardless of input.

**Test entrypoint:**
```python
def test_query_bot_eval_dataset_passes():
    """Builds the Dataset from the three cases above, runs
    dataset.evaluate_sync(query_bot_task), and asserts report.cases[i].assertions
    all evaluate True (equivalently: no failing assertions across the report).
    Runs entirely against FunctionModel — no live LLM call, no API key needed,
    consistent with every other test in tests/test_agent/. This asserts the
    eval SUBSTRATE is consumable end-to-end (trace/store/evaluator wiring),
    not that the AGENT behaves correctly — every FunctionModel here is scripted
    to produce the outcome being asserted. See the framing note at the top of
    this SF for why that distinction matters for a directory meant to be copied."""
```

### SF-7 — `tests/evals/test_definition_bot_evals.py`

**Framing:** same caveat as SF-6, in full. Both cases below use a scripted `FunctionModel` that already knows which tool calls to make. They prove the harness can read `validate_spec` gate outcomes and `spec_draft_token`/`result_id` off `RunTrace`/`BotResponse` — not that an LLM drafts specs correctly or recognizes ambiguous schemas on its own.

**What:** Mirrors SF-6's structure for `DefinitionBot`, using `make_definition_bot_fixture()` from SF-5.

**Cases:**
1. **`validate_spec` gate wiring** — a straightforward, single-pass request to define a valid metric on the fixture's `transactions` table (no name-conflict retry loop — deliberately simpler than `test_definition_bot.py`'s `test_full_flow_returns_status_ok` model, since this demonstrates wiring, not the retry path, which is covered elsewhere). Evaluators: `StatusIs(Status.ok)`, and `MintedSpecDraftToken` asserting `payload.spec_draft_token is not None` **and** the `validate_spec` trace entry has a matching non-`None` `result_id`. Proves the evaluator can read a successful gate outcome; the `FunctionModel` is scripted to reach it, not discovering it.
2. **Refusal-on-ambiguous-schema wiring** — a request referencing a column absent from the fixture's `describe_table` result, driving to a `validate_spec` call that fails the column-existence check. Evaluator asserts non-`ok` status and `payload.spec_draft_token is None`. Proves the evaluator can read a failed gate outcome; the `FunctionModel` is scripted to trigger that specific check, not detecting ambiguity itself.

**Test entrypoint:** `test_definition_bot_eval_dataset_passes()`, same shape as SF-6's — asserts substrate wiring, not agent behavior.

### SF-8 — CI: new `evals` job, and extend `type-check` to cover `tests/evals/`

**File:** `.github/workflows/ci.yml`

**Part A — new `evals` job.** Single Python version (3.12) — `tests/evals/` is `FunctionModel`-driven and deterministic, not a cross-version compatibility surface; `test-agent` already covers agent-module compatibility across 3.10/3.11/3.12.

No `agent-anthropic` extra — confirmed unneeded, not assumed:
- Grepped `aitaem/agent/*.py` for anthropic-specific imports (`pydantic_ai.models.anthropic`, `AnthropicModel`, `AnthropicProvider`, `from anthropic`/`import anthropic`) — none found. The only "anthropic" occurrences are the string-literal default model (`"anthropic:claude-sonnet-4-6"`) and plain `provider == "anthropic"` string comparisons for cache-control kwargs.
- `tests/evals/` always constructs bots with an explicit `model=FunctionModel(...)` instance, never the string default, so pydantic-ai never parses an `"anthropic:..."` model string and never touches the anthropic provider adapter.
- The `evals` job installs `.[agent-evals,dev]` only — narrower than `test-agent`'s `.[agent-anthropic,dev]`, which needs `agent-anthropic` for a different reason (out of scope to re-verify here).

```yaml
evals:
  name: evals
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - name: Install uv
      run: pip install uv
    - name: Install eval dependencies
      run: uv pip install --system -e ".[agent-evals,dev]"
    - name: Run reference eval harness
      run: python -m pytest tests/evals/
```

**Part B — extend the existing `type-check` job to also check `tests/evals/`**, so the reference harness's `Evaluator`/`EvaluatorContext`/`Dataset` generic parameterization is verified, not just linted:
- Install step: `.[dev]` → `.[agent-evals,dev]` — same reasoning as Part A; `agent-anthropic` isn't needed.
- Run step: `mypy aitaem/` → `mypy aitaem/ tests/evals/`. This exact command was run against a scratch reproduction of the planned file layout before being written into this plan (SF-5) — it requires `tests/evals/`'s own files to import each other with relative imports, or mypy fails with a module-identity error given `tests/` has no `__init__.py` above `tests/evals/`. See SF-5 for the reproduction and the exact error.
- `tests/test_agent/*` and the rest of `tests/` remain outside mypy's scope, unchanged — a deliberate, narrow carve-in for `tests/evals/` only, not a blanket policy change.

`test_otel_spans.py` (SF-4) needs no CI change — it lives under `tests/test_agent/` and is already covered by the existing `test-agent` job once `opentelemetry-sdk` lands in `[dev]` (SF-3); it is not part of the mypy carve-in.

**Also:** update `CLAUDE.md`'s "Common Commands" section (project root, not `docs/`): "Type checking: `mypy aitaem/`" becomes "Type checking: `mypy aitaem/ tests/evals/`", so the documented local command matches what CI gates on.

### SF-9 — Changelog entry

**File:** `docs/changelog.md`, under `## Unreleased`. `aitaem.agent` (Phases 2/3/5.2) hasn't appeared in any released version — the changelog's latest tagged section is `v0.4.0`, which predates the agent module entirely — so this entry adds to, rather than amends, an already-unreleased surface.

**`### Breaking changes`** (append to the existing subsection, which already carries the `tmp_dir` entry from Phase 5.2's cycle — match its before/after style):
- `ValidateSpecResult.spec_draft_token` is no longer a constructor argument — it's a read-only property derived from the new `result_id` field.

  ```python
  # Before
  ValidateSpecResult(spec_draft_token="dd_abc123")

  # After
  ValidateSpecResult(result_id="dd_abc123")
  ```

- `ValidateSpecResult` now also rejects unknown constructor arguments (`extra="forbid"`), so the old call raises `ValidationError` rather than silently constructing an object with `spec_draft_token=None`.
- Only affects direct construction of `ValidateSpecResult` (custom tooling, or tests standing in for `validate_spec()`'s return value). Callers going through `DefinitionBot`/`validate_spec()` see no change — `spec_draft_token`'s value and meaning to the LLM are unchanged.

**`### Fixed`:**
- `RunTrace.tool_calls[i].result_id` is now populated for tool calls that mint a new `ResultStore` entry (previously always `None` regardless of what the tool returned).
- `RunTrace.tool_calls[i].duration_ms` is now populated per tool call (previously always `None` — only the whole-turn `RunTrace.duration_ms` aggregate was set).
- No fields were added or removed — both already existed; only their population was fixed.

**`### Added`:**
- `tests/evals/` — a runnable reference harness (`pydantic-evals`) demonstrating how to wire tool-selection, refusal, and deterministic-correctness evaluators against `QueryBot`/`DefinitionBot`'s `RunTrace`/`ResultStore`/`BotResponse` substrate, resolving ND-09/OQ-2. Runs in CI via the new `evals` job against scripted `FunctionModel`s (no live LLM calls or API keys required) — validates that the substrate is consumable by `pydantic_evals.Evaluator`s, not agent behavior; point it at a live model outside CI to evaluate actual quality.

### SF-10 — Mark Phase 6 complete; resolve ND-09/OQ-2; fix a pre-existing `05-evals.md` field-name defect

**Files:** `plans/agent_module/08-implementation-order.md`, `plans/agent_module/ARCHITECTURE.md`, `plans/agent_module/07-non-decisions.md`, `plans/agent_module/05-evals.md`.

**Phase-completion status:**
- `08-implementation-order.md` — P6.1/P6.2 headings gain "— ✅ Shipped" / status lines (matching the existing P5.2 entry's style), referencing this plan. Effort/risk table row for "Phase 6 — Eval validation" updated to "✅ Done".
- `ARCHITECTURE.md` §8 — summary table and the "Phase order summary" mermaid diagram gain the same "done" annotation used for P5.2 (`classDef done`).

**Resolving ND-09/OQ-2, everywhere it's stated as open (four locations, three files) — not just `ARCHITECTURE.md` §7:**
- `07-non-decisions.md` — ND-09's **canonical** text (`ARCHITECTURE.md` §7 is a summary table that points here). Heading gains "— ✅ Resolved (Plan 29)"; a **Resolution** line is added ahead of the existing "What's deferred" / "Why this is non-deciding-able" / "Escape valve" text, which stays as historical record.
- `ARCHITECTURE.md` §7 non-decisions table — ND-09 row's "Escape valve" column updated to reflect resolution, pointing to `07-non-decisions.md`'s ND-09 and this plan.
- `ARCHITECTURE.md` Executive Summary's "Open questions for user decision" list — item 2 ("Ship a reference eval harness...?") removed (no longer open). OQ-1 and OQ-3 are untouched — resolving those is out of scope here.
- `ARCHITECTURE.md` "Open Questions Awaiting User Decision" section, `### OQ-2` subsection — retitled `### OQ-2: Reference eval harness in the repository? — closed`, following the exact pattern already established by `### OQ-4: AITAEM-side coordination — closed` immediately below it. (a)/(b) framing kept as historical context, with a resolution sentence appended pointing to Plan 29.

**Fixing a pre-existing contract-doc defect in `05-evals.md`**, on the same file this SF already touches for ND-09/OQ-2:
- §1's shape description currently reads: `` `tools_called: list[ToolCall]` where each entry carries `name`, `args: dict`, `result_id: str | None`, `summary_returned_to_llm: dict`, `success: bool`, `duration_ms: int` ``. This predates Phase 1's implementation and doesn't match the shipped `aitaem/agent/trace.py` shape:
  - the attribute is `RunTrace.tool_calls` (doc says `tools_called`)
  - the per-call summary field is `ToolCall.llm_summary: str | None` (doc says `summary_returned_to_llm: dict` — wrong name *and* wrong type)
  - `tool_call_id: str` exists on every `ToolCall` and isn't mentioned at all (and is now the exact field SF-4 uses to match spans to trace entries by ID)
  - `06-extensibility.md` §85 declares this shape "public, semver-stable — the eval substrate is contract," so this is a real defect on the documented contract surface, not a cosmetic wording issue.
- **Fix direction: update the doc to match the shipped code, not the reverse.** `llm_summary` already shipped in Phase 1 and is exercised throughout this plan; renaming it now would itself be the breaking change §85 exists to prevent, for no functional benefit — the doc predates the implementation and is simply stale.
- New paragraph text: `` `RunTrace.tool_calls: list[ToolCall]` where each entry carries `tool_call_id: str`, `name: str`, `args: dict[str, Any]`, `result_id: str | None`, `llm_summary: str | None`, `success: bool`, `duration_ms: float | None`. This shape is consumable directly by any eval framework's "tool-use" scorer. ``
- §6 "Open question for the user" — retitled "§6 Resolved: reference eval harness ships in the repo"; (a)/(b) framing kept as historical context, with a closing line: "Resolved: (a). See `plans/29-agent-phase6-evals.md` and `07-non-decisions.md` ND-09."

---

## 3. Ordering

```
SF-1 (result_id + duration_ms fix)
  └─> SF-2 (tests for SF-1)
        └─> SF-6, SF-7 need SF-1 for their deterministic-correctness cases

SF-3 (opentelemetry-sdk dependency)
  └─> SF-4 (span capture tests)

SF-5 (tests/evals/ fixtures)
  └─> SF-6 (QueryBot evals)
  └─> SF-7 (DefinitionBot evals)
        └─> SF-8 (CI job — needs SF-6/SF-7 files to exist to be meaningful)

SF-9, SF-10 (changelog, architecture docs) — last, after everything above is verified green.
```

SF-1/SF-2 and SF-3/SF-4 are independent of each other and of SF-5 through SF-8; can be done in either order or in parallel.

---

## 4. Testing strategy

Per CLAUDE.md's Common Instructions:

1. Existing tests checked first — `test_trace.py`, `test_query_bot.py`, `test_definition_bot.py` already have the right shape (`FunctionModel`-driven, no live LLM) to extend rather than replace.
2. New test files only where no existing one fits: `tests/test_agent/test_otel_spans.py` (new concern — real span capture), `tests/evals/*` (new concern — `pydantic-evals` wiring, deliberately not folded into `tests/test_agent/`).
3. Run via `pytest` with `pytest-cov`: `python -m pytest tests/test_agent/ --cov=aitaem/agent` (existing job) and `python -m pytest tests/evals/` (new job, SF-8) — the latter is a demonstration harness, not a coverage target, so no `--cov` gate on it.
4. `ruff check aitaem/ tests/` and `mypy aitaem/ tests/evals/` — both CI gates, run before considering any sub-feature done. `tests/test_agent/*` and the rest of `tests/` stay outside mypy's scope (type-correctness rides on `ruff` only, as before); `tests/evals/` is a deliberate, narrow exception because it's the reference blueprint and its value is largely in `Evaluator`/`EvaluatorContext` generic parameterization.
5. Commit only after all of the above are green.

---

## 5. Files changed summary

| File | Change |
|---|---|
| `aitaem/agent/definition_types.py` | SF-1: `ValidateSpecResult` gets `result_id` (stored, excluded from serialization) with `spec_draft_token` derived from it, plus `extra="forbid"` — a breaking change to this public type's constructor (see SF-9) |
| `aitaem/agent/definition_tools.py` | SF-1: set `result_id` in `validate_spec()`'s success return |
| `aitaem/agent/trace.py` | SF-1: populate `ToolCall.result_id` (single-field read, no fallback list) and `ToolCall.duration_ms` (from `ModelResponse.timestamp` / `ToolReturnPart.timestamp`) |
| `tests/test_agent/test_trace.py` | SF-2: unit tests for SF-1 |
| `tests/test_agent/test_query_bot.py` | SF-2: `result_id`/`duration_ms` regression test |
| `tests/test_agent/test_definition_bot.py` | SF-2: `result_id`/`duration_ms` regression test |
| `tests/test_agent/test_result_id_contract.py` | SF-2: new — structural contract test, catches future tools that mint a `ResultStore` entry without exposing `result_id` |
| `pyproject.toml` | SF-3: add `opentelemetry-sdk` to `[dev]` |
| `tests/test_agent/test_otel_spans.py` | SF-4: new — span-emission consistency tests |
| `tests/evals/__init__.py` | SF-5: new (empty) |
| `tests/evals/_fixtures.py` | SF-5: new — self-contained eval fixtures |
| `tests/evals/test_query_bot_evals.py` | SF-6: new — QueryBot reference eval dataset |
| `tests/evals/test_definition_bot_evals.py` | SF-7: new — DefinitionBot reference eval dataset |
| `.github/workflows/ci.yml` | SF-8: new `evals` job (`.[agent-evals,dev]`); `type-check` job extended to install `.[agent-evals,dev]` and run `mypy aitaem/ tests/evals/` |
| `docs/changelog.md` | SF-9: Unreleased entries (`### Breaking changes`, `### Fixed`, `### Added`) |
| `plans/agent_module/08-implementation-order.md` | SF-10: P6.1/P6.2 marked shipped |
| `plans/agent_module/ARCHITECTURE.md` | SF-10: §8 done-annotated; §7 table, Executive Summary, and `OQ-2` all updated to reflect ND-09 resolved |
| `plans/agent_module/07-non-decisions.md` | SF-10: ND-09 (canonical text) marked resolved with pointer to this plan |
| `plans/agent_module/05-evals.md` | SF-10: §1 field-name fix (`tools_called`/`summary_returned_to_llm` → `tool_calls`/`llm_summary`, matching shipped `trace.py`); §6 open question marked resolved |
| `CLAUDE.md` | SF-8: "Common Commands" type-checking line updated to `mypy aitaem/ tests/evals/` |

No changes to `aitaem/agent/__init__.py`'s export *list* — the Documentation Instructions' "new/removed public export" trigger does not fire, so no `docs/api/` page additions are needed (consistent with deferring the "Evaluating your agent" guide itself to Phase 7). `ValidateSpecResult` itself is already exported and its constructor changes — that's a contract change to an existing export, not an export-surface change, and is called out separately in SF-9's changelog entry.

---

## 6. Success criteria

- [ ] `ToolCall.result_id` is non-`None` for `compute_metrics` and `validate_spec` tool calls that succeed, and stays `None` for tools that don't mint store entries or that fail.
- [ ] `ValidateSpecResult`'s LLM-facing serialization (`model_dump()`/`model_dump_json()`) contains `spec_draft_token` only — `result_id` is excluded, so the LLM never sees two differently-named fields carrying the same value.
- [ ] `ValidateSpecResult(spec_draft_token="x")` (the pre-fix constructor call) raises `ValidationError` rather than silently constructing a `None`-valued object, and this breaking change is documented under `### Breaking changes` in `docs/changelog.md`.
- [ ] `test_result_id_contract.py` passes today (every current store-writing tool exposes `result_id`) and is structured so a future tool violating the convention fails this test, not just returns a silent `None` from `assemble_trace()`.
- [ ] `ToolCall.duration_ms` is non-`None` and reflects real elapsed time (not the whole-turn aggregate) for every tool call in `RunTrace.tool_calls`.
- [ ] `tests/test_agent/test_otel_spans.py` demonstrates, against real (in-memory) captured OTel spans, that span count/names/order are consistent with `RunTrace.tool_calls` for both bots, and that `ToolCall.duration_ms` is never less than the corresponding span's true execution duration.
- [ ] `tests/evals/` runs standalone (`python -m pytest tests/evals/`) with no live LLM calls or API keys, and demonstrates that the substrate (`RunTrace`, `ResultStore`, `BotResponse`) is wired correctly for each of the three eval-dimension *shapes* from Architecture §5 (tool-selection, refusal, deterministic correctness) for `QueryBot`, plus the `DefinitionBot`-specific equivalents — **not** that any agent behavior was measured. Every case is driven by a scripted `FunctionModel`, so this criterion is satisfied by "the harness runs, reads the right fields, and reports correctly," not by any claim about LLM decision quality. This scoping must be legible in the harness's own docstrings (SF-6/SF-7), not just in this plan.
- [ ] New `evals` CI job is green.
- [ ] `ruff check aitaem/ tests/` and `mypy aitaem/ tests/evals/` clean — `tests/evals/`'s `Evaluator`/`EvaluatorContext`/`Dataset` generic parameterization is type-checked, not just linted.
- [ ] No regressions in the existing `test` / `test-agent` / `import-graph` CI jobs.
- [ ] ND-09/OQ-2 is no longer stated as open anywhere it currently is: `07-non-decisions.md` (canonical), `ARCHITECTURE.md` §7 table, Executive Summary, and the `OQ-2` subsection, and `05-evals.md` §6.
- [ ] `05-evals.md` §1's `RunTrace.tool_calls`/`ToolCall` field-name description matches `aitaem/agent/trace.py` exactly (`tool_calls`, `tool_call_id`, `llm_summary`) — no drift between the documented "contract" (`06-extensibility.md` §85) and the shipped shape.

---

## 7. Known deviations from the original architecture doc

- Architecture §5/§8 (written before Phase 3 shipped) only mentions `QueryBot` in the reference-harness description. This plan covers `DefinitionBot` too (§0, D1) — DefinitionBot exists now, and the harness would be a weaker blueprint without it.
- P6.2 was described only as "tests that RunTrace and the underlying spans... are consistent" without specifying depth. This plan commits to real OTel span capture, not mock-based (§0, D3) — SF-4 documents why the two derivations are genuinely independent, which is what gives that depth of test real signal.
- `ToolCall.result_id` (SF-1) was always `None` in the shipped code — not named anywhere in the architecture docs, but a real prerequisite for the deterministic-correctness eval case (§0, D2), so it's fixed here rather than filed as a separate bugfix plan. The fix goes to the root cause named in `03-component-architecture.md` §2's `ToolResult` protocol: `ValidateSpecResult` gets a real `result_id` field, with `spec_draft_token` as a `computed_field` derived from it, so the two values are structurally incapable of disagreeing.
- `ToolCall.duration_ms` (also SF-1, §0 D2) has the same defect: declared on the model and in `05-evals.md`'s field list, but never populated per call — only the whole-turn `RunTrace.duration_ms` aggregate was set. Fixed in the same pass as `result_id`, using `ModelResponse.timestamp`/`ToolReturnPart.timestamp`.
- `05-evals.md` §1's field-name description (`tools_called`, `summary_returned_to_llm: dict`) was already wrong against the shipped `trace.py` shape before this plan — a pre-existing doc/code mismatch on a surface `06-extensibility.md` §85 declares "contract" (SF-10). Fixed by correcting the doc to match the shipped, semver-stable field names (`tool_calls`, `llm_summary`), not by renaming shipped code.
