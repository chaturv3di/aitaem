# Plan 37 — DefinitionBot: Data-Grounded Thresholds via Shared Distribution Tooling

**Scope:** Give `DefinitionBot` the ability to ground spec-definition thresholds in real data
(e.g. "define a slice for high-value vs. low-value transactions, where high value is the 75th
percentile of invoices last year"), by reusing and extending Plan 36's `QueryBot` distribution
tooling (`column_distribution`, `distribution_summary`).

`distribution_summary` is part of released `v1.0.0` and is not renamed, removed, or changed.
`column_distribution` and its supporting types are still `Unreleased` and are freely reorganized.
`QueryBot`'s `compute_metrics` is also part of released `v1.0.0` and is likewise not renamed —
`DefinitionBot` gets its own tool of the same bare name (see the "Naming" key decision below).

---

## 1. Key decisions

| Decision | Choice |
|---|---|
| Raw vs. governed data access | Two non-overlapping tools: `date_range` (any raw table, temporal columns only, own bounds-only aggregate `_build_bounds_agg` and return type `DateRangeResult` — no code path to a percentile) and `column_distribution` (metric-only via `metric_name`, never a raw `source:` URI, full stats including percentiles). The percentile-capable path is only reachable through the catalog. |
| Aggregated-metric-value thresholds (e.g. p99 of per-store revenue) | New `DefinitionBot` tool `compute_metrics` (validates via `SpecResolver.resolve()`, then executes, in one call — see next row) feeding the existing, unmodified `distribution_summary(result_id)`. |
| `compute_metrics` validation (both bots) | Execution reads `metric_name`/`slices`/`segment` off the `exact_match` returned by `SpecResolver.resolve()` — documented canonical (`09-querybot-v0.2-design.md` §4.2) — not the pre-validation arguments, so a future normalization in `resolve()` (alias expansion, slice dedup/reorder) can't be silently skipped. `by_entity`/`period_type`/`time_window` still come from the arguments: `ExactMatch` doesn't carry them, and `resolve()` only validates membership/support for them, never rewrites them. Applied identically in both `DefinitionBot`'s `compute_metrics` (SF-5, single call — no persisted `spec_token`, no `record_intent`-style NL-capture step, since neither the double-execution nor NL-capture concerns that split solves for `QueryBot` apply here) and `QueryBot`'s `resolve_intent` (SF-3, the same latent gap fixed in place). |
| Naming the two `compute_metrics` tools | Same bare name, `compute_metrics`, on both bots — unchanged on `QueryBot`, new on `DefinitionBot`; no prefix, since the model never sees both in the same context and a prefix would only add taxonomy noise. Their result types do need distinguishing — `Q_ComputeMetricsResult` (renamed from `ComputeMetricsResult`) and `D_ComputeMetricsResult` (new) — safe since these are backend-only, never `__init__.py`-exported, never serialized to the model by class name. |
| Metric dependency tracking | `DefinitionDeps.dependent_metrics: list[str]` — a plain audit trail, appended by `column_distribution`/`DefinitionBot`'s `compute_metrics` on success only (never on a failed/errored call), surfaced on `DefinitionPayload`. |
| Code sharing | New `aitaem/agent/common_tools.py` holds the moved `column_distribution`/`distribution_summary` implementations, their helpers, `ToolResult`, and the distribution result types. `resolver.py` holds `MetricIntent`/`ExactMatch`/`NearMiss`/`SpecMatchResult`. `definition_tools.py`/`definition_types.py` depend on neither `query_tools.py` nor `query_types.py`. |
| Typing the shared tools' `ctx` | A `Protocol` (`SharedToolDeps`: `spec_cache: Any`, `connection_manager: Any`, `store: ResultStore`) types the shared tools' `ctx`. `RunContext`'s deps parameter is covariant in the installed `pydantic-ai`, so this type-checks correctly on either bot's toolset while still catching attribute typos and checking `store`'s `ResultStore` methods. Pinned to the installed version — SF-8 adds a dedicated regression test for this assumption. |
| Exception handling in `date_range`/`DefinitionBot`'s `compute_metrics`/moved `column_distribution`/`distribution_summary` | Two-tier: `except AitaemError` first (covers `TableNotFoundError`, `ConnectionNotFoundError`, `AitaemConnectionError`, `InvalidURIError`, `UnsupportedBackendError`, `QueryBuildError`, `QueryExecutionError`), then `except Exception` as a fallback, marked as unanticipated. Needed because `.to_pyarrow()`/`.to_pandas()` are called directly on ibis expressions, bypassing `IbisConnector.execute()`'s exception-wrapping — raw backend errors are a real, common case here and must still degrade gracefully. |
| Row ceiling on `DefinitionBot`'s `compute_metrics` | None — no SQL `LIMIT`. Truncating rows would corrupt `distribution_summary`'s percentile computation, not just shrink it. Matches `QueryBot`'s `compute_metrics`'s existing unbounded-query posture; mitigated the same way, via `date_range`/`column_distribution` grounding in Layer A. |
| Threshold-to-spec linking | None. The LLM reads a stat from a tool result and hand-writes the literal into `where:` text, same as any other predicate. |

---

## 2. Scope

**In scope:**
- SF-1 — Move `MetricIntent`/`ExactMatch`/`NearMiss`/`SpecMatchResult` to `resolver.py`; `DefinitionDeps`/`DefinitionPayload` gain `dependent_metrics` fields.
- SF-2 — New `aitaem/agent/common_tools.py`: move `ToolResult`, `ColumnDistribution`, `ColumnDistributionResult`, `DistributionSummaryResult`, `_build_distribution_agg`, `_stringify_bound`, `_row_val_or_none`, `_row_to_distribution`, `_reject_unsafe_filter`, `column_distribution`, `distribution_summary`; new `_execute_metric_compute` helper.
- SF-3 — `QueryBot`'s `compute_metrics` refactored (internal only) to call `_execute_metric_compute`; its result type renamed `ComputeMetricsResult` → `Q_ComputeMetricsResult`; `resolve_intent` fixed to read from `exact_match` instead of raw arguments (mirrors SF-5).
- SF-4 — New `date_range(source, date_column, filter=None)` tool, `DefinitionBot`-only.
- SF-5 — New `DefinitionBot` tool `compute_metrics(metric_name, slices, segment, by_entity, period_type, time_window)` + `D_ComputeMetricsResult` type.
- SF-6 — Register `date_range`, `compute_metrics`, `column_distribution`, `distribution_summary` on `DefinitionBot`; thread `dependent_metrics` into `DefinitionPayload`.
- SF-7 — Layer A prompt updates.
- SF-8 — Tests.
- SF-9 — Docs (changelog).
- SF-10 — Evals: live-model threshold-governance behavior checks.

**Out of scope:**
- `date_range` on `QueryBot`; an ungated `compute_metrics` variant on `QueryBot` (it keeps its existing `spec_token` gate).
- A `source:`-URI mode on `column_distribution`.
- A structured link between a `where:` literal and the tool result it came from.
- `group_by` on `date_range` or `column_distribution`.
- Any change to `commit_spec`/`delete_spec`/`validate_spec`.
- Any `QueryBot` prompt/tool/behavior change beyond SF-3's internal refactor.

---

## 3. Sub-features

### SF-1 — Foundation type changes

**File:** `aitaem/agent/resolver.py`
- Add `MetricIntent` (dataclass), `ExactMatch`, `NearMiss`, `SpecMatchResult`, moved from `query_types.py`.

**File:** `aitaem/agent/query_types.py`
- Re-export the four types from `resolver.py`. `aitaem/agent/__init__.py` unaffected.

**File:** `aitaem/agent/definition_types.py`
- `DefinitionDeps.dependent_metrics: list[str] = field(default_factory=list)`.
- `DefinitionPayload.dependent_metrics: list[str] = []`.

### SF-2 — Shared `common_tools.py` module

**File:** `aitaem/agent/common_tools.py` (new)
- New `SharedToolDeps(Protocol)`: `spec_cache: Any`, `connection_manager: Any`, `store: ResultStore`. Both `QueryDeps` and `DefinitionDeps` satisfy it structurally — no inheritance change needed on either dataclass.
- Move: `ToolResult`, `ColumnDistribution`, `ColumnDistributionResult`, `DistributionSummaryResult`, `_build_distribution_agg`, `_stringify_bound`, `_row_val_or_none`, `_row_to_distribution`, `_reject_unsafe_filter`, `column_distribution`, `distribution_summary`. `ctx: RunContext[QueryDeps]` → `ctx: RunContext[SharedToolDeps]` on the two tool functions.
- `column_distribution`'s docstring gains a note on why it only accepts `metric_name`, never a raw `source:` URI: a concept without a matching catalog metric can't get percentile grounding at all (SF-7 tells the LLM to define the metric first instead) — the alternative would let a threshold-setting statistic trace back to an unreviewed table instead of the catalog's approved definition. Kept as a docstring note, not a `docs/agent/*.md` edit, per Plans 33/36's convention of updating those pages only at release time (SF-9 flags it for that pass).
- `column_distribution`/`distribution_summary`'s existing `except Exception` clauses become two-tier: `except AitaemError as exc` first (`from aitaem.utils.exceptions import AitaemError`), then `except Exception as exc` as a fallback whose message is prefixed (e.g. `f"Unexpected error: {type(exc).__name__}: {exc}"`).
- New: `_execute_metric_compute(spec_cache, connection_manager, store, metric_name, slices, segment, by_entity, period_type, time_window) -> tuple[str, int, list[dict], list[str], dict[str,str]]` — the `_COMPUTE_LOCK`/`MetricCompute`/`store_tabular`/sampling logic extracted from `compute_metrics`. Raises on failure — no try/except inside it; each caller (`QueryBot`'s `compute_metrics`, `DefinitionBot`'s `compute_metrics`) applies its own two-tier catch and translates into its own result shape.
- `column_distribution`: on the successful return path only (not any of its earlier unknown-metric/unknown-column/rejected-filter/execution-error returns) — if `getattr(ctx.deps, "dependent_metrics", None)` is a list, append `metric_name` (dedup). `getattr`-based since only `DefinitionDeps` carries this field.

**File:** `aitaem/agent/query_tools.py`
- Remove moved definitions; import from `common_tools.py` and re-export for `query_bot.py`/`query_types.py`.

### SF-3 — `QueryBot` internal fixes

No tool name, signature, or observable behavior change — `QueryBot` and its prompt are untouched;
values used are functionally identical to today's since `resolve()` doesn't normalize anything yet.

**File:** `aitaem/agent/query_types.py`
- Rename `ComputeMetricsResult` → `Q_ComputeMetricsResult`. Pure Python identifier rename: not part of `aitaem/agent/__init__.py`'s exports, not part of the tool's field-level schema shown to the model — no documented contract touches a class's own name.

**File:** `aitaem/agent/query_tools.py`
- `compute_metrics`'s parameter list, `spec_token` pop/restore semantics, and (now `Q_ComputeMetricsResult`-shaped) result are otherwise unchanged. Try-body calls `_execute_metric_compute(...)`.
- `resolve_intent`: same fix as `DefinitionBot`'s `compute_metrics` (SF-5; see the "`compute_metrics` validation" key decision for rationale). Once `match_result.exact_match is None` is ruled out, the cross-metric-name check, the `ResolvedSpec` stashed in `spec_registry`, and the final `ExactMatch` returned to the LLM all read `metric_name`/`slices`/`segment` off `match_result.exact_match` instead of the raw arguments — built via `match_result.exact_match.model_copy(update={"spec_token": spec_token})`, matching `resolver.py`'s own documented contract rather than reconstructing field-by-field. `intent.by_entity`/`intent.period_type`/`intent.time_window` are unchanged.

### SF-4 — `date_range` tool

**File:** `aitaem/agent/definition_tools.py`
```python
def date_range(
    ctx: RunContext[DefinitionDeps],
    source: str,
    date_column: str,
    filter: str | None = None,
) -> DateRangeResult:
```
- Resolve `source` via `connection_manager.get_connection_for_source`/`resolve_table_reference`/`connector.get_table` (same pattern as `column_distribution`), wrapped in the same two-tier `except AitaemError` / `except Exception` pattern as SF-2.
- Unknown `date_column` → error listing available columns.
- `ibis_table[date_column].type().is_temporal()` is `False` → error naming the dtype, pointing to `column_distribution` as the alternative.
- `filter`, if given, via `_reject_unsafe_filter` + splice (reused from `common_tools.py`).
- Aggregates via a new `_build_bounds_agg(table, column) -> ibis.Table` (`definition_tools.py`, not `common_tools.py` — single consumer, no `group_by` param since `date_range` has none and grouping is out of scope). `count`, `null_count`, `min_val`, `max_val`, `distinct_count` only — no numeric branch, so no code path can ever produce a percentile regardless of input dtype. `_stringify_bound` (reused) stringifies `min_val`/`max_val`.
- Store via `store_tabular(..., metadata={"source", "column", "min_val", "max_val"})`.
- Does not touch `dependent_metrics`.

**File:** `aitaem/agent/definition_types.py`
```python
class DateRangeResult(BaseModel):
    result_id: str
    min_val: str | None = None
    max_val: str | None = None
    count: int | None = None
    null_count: int | None = None
    distinct_count: int | None = None
    error: str | None = None
```
No `mean`/`std`/`p25`/`median`/`p75` fields exist on this type — the schema itself can't carry a percentile even if a future bug fed it one.

### SF-5 — `DefinitionBot`'s `compute_metrics` tool

Validates then executes in one call — no `spec_token` indirection like `QueryBot`'s
`compute_metrics`/`resolve_intent`, since neither concern that split solves (anti-double-execution
on parallel tool calls, a preceding `record_intent` NL-capture step) applies here. Same bare tool
name as `QueryBot`'s `compute_metrics` — different file, different signature, no runtime collision
(see the "Naming" key decision).

**File:** `aitaem/agent/definition_tools.py`
```python
def compute_metrics(
    ctx: RunContext[DefinitionDeps],
    metric_name: str,
    slices: list[str] | None = None,
    segment: str | None = None,
    by_entity: str | None = None,
    period_type: str = "all_time",
    time_window: tuple[str, str] | None = None,
) -> D_ComputeMetricsResult:
```
- Build `MetricIntent(metric_concept=metric_name, scope="subset" if (slices or segment) else "overall", period_type=period_type, time_window=time_window, by_entity=by_entity)`. `by_entity` is excluded from the `scope` determination — it's a grouping, not a filter (`record_intent`'s docstring ties `"subset"` to `slice_type`/`segment_name` specifically); a `by_entity` breakdown alone stays `"overall"`.
- `SpecResolver().resolve(intent=intent, proposed_metric_name=metric_name, proposed_slices=slices or [], proposed_segment=segment, spec_cache=ctx.deps.spec_cache)`.
- `exact_match is None` → `D_ComputeMetricsResult(near_misses=..., error=...)` — **no call to `_execute_metric_compute`** in this branch; nothing is computed on a validation failure.
- Else → `_execute_metric_compute(exact_match.metric_name, exact_match.slices, exact_match.segment, by_entity, period_type, time_window)` — see the "`compute_metrics` validation" key decision for why `metric_name`/`slices`/`segment` come from `exact_match` rather than the arguments. Wrapped in the same two-tier `except AitaemError` / `except Exception` pattern as SF-2; on success append `exact_match.metric_name` to `ctx.deps.dependent_metrics` (dedup); on either exception branch → `D_ComputeMetricsResult(error=...)`.

**File:** `aitaem/agent/definition_types.py`
```python
class D_ComputeMetricsResult(BaseModel):
    result_id: str
    row_count: int
    sample: list[dict[str, Any]]
    columns: list[str]
    near_misses: list[NearMiss] = []
    error: str | None = None
```

### SF-6 — Wire into `DefinitionBot`

**File:** `aitaem/agent/definition_bot.py`
- `_build_agent()`: register `date_range`, `compute_metrics`, `column_distribution`, `distribution_summary`.
- `ask()`/`chat()`: pass `deps.dependent_metrics` into `_assemble_payload`.
- `_assemble_payload` gains a `dependent_metrics: list[str]` parameter → `DefinitionPayload.dependent_metrics`.

### SF-7 — Prompt copy

**File:** `aitaem/agent/definition_bot.py`, `_build_layer_a_definition()`
- New section after Step 2, before Step 3: "Grounding thresholds and date ranges in real data (optional)."
  - `date_range` — temporal columns only, any discovered table; cohort/window boundaries.
  - `column_distribution` — value-based threshold on an existing metric's source table, row-level.
  - `compute_metrics` + `distribution_summary` — threshold over an aggregated metric value (e.g. `by_entity`).
  - Rule: a numeric threshold in a `where:` predicate must come from `column_distribution` or `compute_metrics`+`distribution_summary` — never invented. If no matching catalog metric exists: `status="refused"`, with `narrative`/`reason` naming the missing concept and recommending the user define a metric for it first (a separate `ask()`/`chat()` call) before retrying.

### SF-8 — Tests

- `tests/test_agent/test_common_tools.py` (new) — moved tests for `_build_distribution_agg`, stringification, `_reject_unsafe_filter`, `column_distribution`, `distribution_summary`; new tests for `_execute_metric_compute`; `column_distribution` — a failed call (unknown metric, unknown column, and a rejected filter, each separately) leaves `dependent_metrics` unmodified, only a successful call appends.
- `tests/test_agent/test_common_tools.py`: `test_shared_tool_ctx_typechecks_on_both_deps` — regression guard for the `RunContext` covariance assumption (see the "Typing the shared tools' `ctx`" key decision). Writes a minimal fixture to `tmp_path` (a `Protocol` matching `SharedToolDeps`, two differently-typed dummy deps dataclasses, a `RunContext[Protocol]`-typed function registered on two `FunctionToolset`s, one per deps type), shells out to `mypy` on that file, asserts a clean exit — isolated from the real code so a future `pydantic-ai` regression fails here, not as noise in the main CI job. Skipped if `mypy` isn't importable, mirroring `test_definition_bot_smoke.py`'s credential-skip pattern.
- `tests/test_agent/test_query_tools.py` — remove moved tests; update references to `Q_ComputeMetricsResult`; `compute_metrics` behavior otherwise unchanged (regression test for the `_execute_metric_compute` refactor); `resolve_intent` — a case with `SpecResolver.resolve` patched to return an `exact_match` whose `metric_name` diverges from the arguments passed in, asserting the stashed `ResolvedSpec`, the returned `ExactMatch`, and the `column_distribution_result_id` cross-metric-name check all use the `exact_match` value, not the raw arguments.
- `tests/test_agent/test_resolver.py` — import-path updates if needed.
- `tests/test_agent/test_definition_tools.py` — `date_range` (success, non-temporal rejection, unknown column/source, filter cases); `_build_bounds_agg` (asserts no `mean`/`std`/percentile keys appear regardless of input dtype); `compute_metrics` (success incl. `dependent_metrics` dedup; near-miss cases for unknown metric/slice/segment/by_entity/period_type, each asserting `dependent_metrics` is left unmodified; a case with `SpecResolver.resolve` patched to return a diverging `exact_match`, asserting execution and `dependent_metrics` follow `exact_match`, not the raw argument).
- `tests/test_agent/test_definition_bot.py` — tool-registration set; `dependent_metrics` end-to-end; prompt-content assertions.
- `tests/test_agent/test_public_api.py` — export-set regression guard.

### SF-9 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Added`: `date_range`, `DefinitionBot`'s `compute_metrics`, `DefinitionBot` reusing `column_distribution`/`distribution_summary`, `DefinitionPayload.dependent_metrics`.
- No `docs/api/*.md` or `docs/agent/*.md` edits now. When those pages get their release-time pass, `docs/agent/getting-started.md`'s `column_distribution` coverage should carry the same metric-gating rationale as its docstring (see SF-2).

### SF-10 — Evals: threshold-governance behavior

Neither SF-8 nor the existing scripted-model `tests/evals/test_definition_bot_evals.py` dataset
exercises whether a real LLM follows SF-7's governance instructions — that's prompt compliance,
not mechanics, and needs a live-model check.

**File:** `tests/evals/test_definition_bot_evals.py`
- Add one `Case` to the existing scripted-model dataset: a `FunctionModel` that calls
  `column_distribution` then `draft_spec`/`validate_spec`, asserting `dependent_metrics` and the
  new tools' result types flow through `RunTrace`/`DefinitionPayload` correctly. CI-safe, no
  credentials needed.

**File:** `tests/evals/test_definition_bot_threshold_evals.py` (new)
- Module-level `pytestmark = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), ...)` —
  same pattern as `test_definition_bot_smoke.py`. Real model (e.g.
  `anthropic:claude-haiku-4-5-20251001`), not a scripted `FunctionModel`. Not part of the default
  CI gate; a failure here means "check Layer A wording," not necessarily a code bug.
- Fixture catalog: one metric with both a numeric and a temporal column; a second concept with no
  matching metric.
- `pydantic_evals` `Dataset`/`Case`/`Evaluator`s, built on `RunTrace.tool_calls` (name/args),
  matching `05-evals.md`'s committed substrate:
  - Threshold request against the existing metric ("75th percentile of ...") →
    `column_distribution`/`compute_metrics` appears in `trace.tool_calls`; `date_range` is
    not called on the metric's value column.
  - Threshold request with no matching metric → no `spec_draft_token` minted from a fabricated
    literal: either the governed tool was attempted and returned an error, or `status` is
    `refused`; `date_range` is not called as a workaround. An `LLMJudge` evaluator additionally
    asserts `narrative`/`reason` names the missing concept and recommends defining a metric for
    it first — the regression case for SF-7's refusal-guidance rule.
  - Pure date-grounding request ("cohort of signups since March") → `date_range` is called on the
    temporal column; `column_distribution`/`compute_metrics` is not (unnecessary here).

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/agent/resolver.py` | SF-1: `MetricIntent`/`ExactMatch`/`NearMiss`/`SpecMatchResult` moved in |
| `aitaem/agent/query_types.py` | SF-1, SF-2: re-exports; SF-3: `ComputeMetricsResult` → `Q_ComputeMetricsResult` rename |
| `aitaem/agent/definition_types.py` | SF-1: `dependent_metrics` fields; SF-4: `DateRangeResult`; SF-5: `D_ComputeMetricsResult` |
| `aitaem/agent/common_tools.py` | SF-2: new shared module |
| `aitaem/agent/query_tools.py` | SF-2: re-import from `common_tools.py`; SF-3: internal refactor (`compute_metrics` calls `_execute_metric_compute`; `resolve_intent` reads from `exact_match`) |
| `aitaem/agent/definition_tools.py` | SF-4: `date_range`, `_build_bounds_agg`; SF-5: `compute_metrics` |
| `aitaem/agent/definition_bot.py` | SF-6: tool registration, payload threading; SF-7: prompt section |
| `tests/test_agent/test_common_tools.py` | SF-8: new |
| `tests/test_agent/test_query_tools.py` | SF-8: trim + `Q_ComputeMetricsResult`/`resolve_intent` regression |
| `tests/test_agent/test_resolver.py` | SF-8: import updates |
| `tests/test_agent/test_definition_tools.py` | SF-8: new tool tests |
| `tests/test_agent/test_definition_bot.py` | SF-8: registration/payload/prompt tests |
| `tests/test_agent/test_public_api.py` | SF-8: export-set guard |
| `docs/changelog.md` | SF-9: `Unreleased` entry |
| `tests/evals/test_definition_bot_evals.py` | SF-10: one scripted-model wiring case for the new tools |
| `tests/evals/test_definition_bot_threshold_evals.py` | SF-10: new — live-model threshold-governance behavior evals |
