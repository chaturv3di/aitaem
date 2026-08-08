# Plan 36 — QueryBot: Backend-Pushed-Down Distribution Tools

**Scope:** Redesign `distribution_summary` to push grouped statistics down to the backend via
ibis instead of materializing to pandas first, generalize its result type, and add a new
`column_distribution` tool for summarizing a metric's raw source-table columns (e.g. its real
timestamp range) — replacing the LLM's need to fabricate a `time_window`. Breaking change
accepted directly for `distribution_summary`'s *return* shape (`MetricDistribution` →
`ColumnDistribution` — see SF-2): only its call signature stays compatible (`group_by` is
optional, defaulting to today's `["metric_name"]` grouping), not its output type.

**Motivation:** confirmed via live testing. A user asked for "overall, weekly sales volumes"
(no explicit dates) against a BigQuery-backed metric whose real data spans 2024. `period_type`
was correctly resolved to `"weekly"`, which requires a concrete `time_window`
(`aitaem/query/builder.py:93-99`). With nothing to ground it, the LLM guessed: once too
narrow (missed the real 2024 data), then catastrophically wide
(`time_window=['1900-01-01', '2026-08-06']`), producing ~6,700 weekly period buckets
cross-joined against the fact table — a 10+ minute BigQuery query that had to be cancelled via
the BigQuery console.

**Confirmed gap:** no tool on `QueryBot` or `DefinitionBot` exposes any column statistic
(min/max/count/etc.) — `describe_table` returns only `(name, dtype)`. This was a known,
deliberately-deferred gap: `plans/08-period-granularity.md:427-428` explicitly deferred "a
pre-scan query for min/max dates" to a "Phase 2" that no later plan picked up.

**Second, related gap found along the way:** `distribution_summary` (`query_tools.py:366-406`)
already has the same class of problem in miniature — it pulls a `compute_metrics` result fully
into pandas via `.to_pandas()` before computing stats, and only ever groups by `metric_name`.
Metric results broken down by slice/segment across many time periods (weekly/daily/hourly) can
themselves be large tables; materializing them client-side before aggregating is the same
mistake at smaller scale.

---

## 1. Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Two tools, not one | Redesign `distribution_summary` (existing) for group-by + pushdown; add `column_distribution` (new) for raw source-table columns | Different inputs (a `compute_metrics` result vs. a metric's raw source table) and different capabilities (grouping makes sense post-compute; it does not pre-compute — see below). One generalized type serves both. |
| `distribution_summary` grouping | New `group_by: list[str] | None = None` param, default `["metric_name"]` — matches today's only grouping, so *call signature* stays compatible (existing `distribution_summary(result_id)` calls still work unmodified; the *return* shape does not — see Scope/SF-2). Any `STANDARD_COLUMNS` entry except `metric_value`/`metric_format` is a valid key. | Every `STANDARD_COLUMNS` field besides the measure itself and pure metadata is a legitimate grouping key (`period_type`, `period_start_date`, `entity_id`, `slice_value`, `segment_value`, …) — confirmed by reading `aitaem/utils/formatting.py:9-19`. |
| `distribution_summary` execution | `_get_ibis_table(entry).group_by(group_by).aggregate(...)` — one pushed-down query, not `.to_pandas()` first | `compute_metrics()` always stores a live `ibis_ref` (`query_tools.py:242`), so this needs no new plumbing. One `group_by().aggregate()` call compiles to one SQL round trip, confirmed across DuckDB/Postgres. |
| `distribution_summary`'s own output | Keeps its `ibis_ref` (passes the aggregated expression to `store_tabular`, doesn't null it out) — asymmetric with its four siblings; documented as such (see SF-3) so a future reader doesn't have to rediscover why. | Its output is bounded by construction — one row per requested `group_by` combination, regardless of source size — unlike its siblings: `rank_by_value`/`filter_by_threshold` can return many rows, `period_over_period`/`contribution_share` preserve full per-period/per-entity granularity. Retaining a live ref for those would risk re-exposing the expensive-re-query problem this plan exists to fix; `distribution_summary`'s result can't grow that way, so retention costs nothing. Whether anything actually chains off it (e.g. `filter_by_threshold` on the stats) is **speculative, not confirmed** — retention stands on the cost argument alone. |
| `column_distribution` grouping | **None** — filters only, no `group_by` | Slice/segment "membership" isn't a column on the raw source table — it's a *derived* classification (`QueryBuilder._build_slice_case_when_expr`'s `CASE WHEN`). Grouping by it on raw data means re-deriving that expression first, which is most of what `compute_metrics` already does. Filtering, by contrast, is a plain `WHERE` — cheap and mechanically simple. |
| `column_distribution` filtering | A single free-form SQL boolean predicate string: `filter: str | None` (e.g. `"order_value > 1000"`) | Anchoring filters to pre-defined `SliceSpec` values was considered and rejected: real questions ("orders over $1000," "the last 90 days") often don't map to any existing slice, and a slice's `where` is already just LLM-authored SQL text from an earlier conversation — reusing it adds no real safety, just narrows what's expressible. |
| Validation of `filter` — correctness | None beyond letting a compile/execution failure surface as a normal tool error | `column_distribution`'s output is ephemeral/diagnostic — never written to `SpecCache`, never presented as an authoritative metric. A malformed filter fails immediately and visibly; the LLM can retry, the same posture `draft_spec`/`validate_spec`'s correction loop already gives it. This is a deliberate, scoped exception to "the runtime LLM never authors raw SQL" — not extended to `compute_metrics`/`commit_spec`, which stay catalog-only. |
| Validation of `filter` — table scope | Parse `filter` with `sqlglot.parse_one(filter, into=sqlglot.exp.Condition, dialect=<backend>)`; reject if it contains any `Subquery`/`Select` node. Splice the **regenerated** `parsed.sql(dialect=<backend>)` string into the query, never the raw input. | Distinct from the correctness question above: the FROM table is pinned by the resolved `MetricSpec.source`, but a subquery inside `filter` (`WHERE x > (SELECT ... FROM other_table)`) can still read an arbitrary other table under the connection's credentials — and unlike a malformed filter, it *succeeds silently* rather than erroring, so the "let it fail and retry" posture doesn't cover it. Verified live: rejects real subqueries (`IN (SELECT ...)`, `EXISTS (...)`, correlated comparisons) and, as a side effect of parsing into a bounded `Condition` grammar, also rejects statement-stacking (`1=1; DROP TABLE ...`) and paren-breakout (`1=1) UNION SELECT ...`) — both fail to parse. Comment-truncation (`1=1 -- ' AND x`) parses successfully but is neutralized only if the *regenerated* SQL is spliced (sqlglot re-serializes the trailing text as a `/* ... */` block comment); splicing the raw input instead would not neutralize it. `sqlglot` is already an installed transitive dependency of `ibis`, but since `column_distribution` imports it directly, it's added to `pyproject.toml` as an explicit direct dependency rather than relied on transitively. |
| `min_val`/`max_val` stringification | Keep native column dtype through the ibis aggregate (no `.cast("string")` in SQL); stringify in Python after `.to_pandas()` — `.isoformat()` if the value has that method, else `str(value)` | Verified live: backend-side `CAST(... AS STRING/TEXT/VARCHAR)` on a timestamp is not portable — DuckDB emits `"2024-01-15 00:00:00"` (space-separated, no offset) while its own Python-side `.isoformat()` on the identical materialized value gives `"2024-01-15T00:00:00"`; BigQuery's documented default cast format adds a literal timezone suffix on top of that. Since `record_intent`'s `column_distribution_result_id` (SF-6) feeds these values straight into `QueryBuilder._parse_window_endpoint_as_datetime`'s `datetime.fromisoformat()`, an inconsistent, backend-dependent format would silently break for some backends and not others. Formatting in Python instead of SQL sidesteps the portability problem entirely — `pandas.Timestamp`/`datetime` objects always produce real ISO-8601 via `.isoformat()`, regardless of which backend they came from. |
| Cross-backend quantiles | `.approx_quantile()` everywhere, never `.quantile()` | Verified live: exact `Column.quantile()` raises `OperationNotDefinedError` on BigQuery (unimplemented in ibis 12.0.0's BigQuery compiler — confirmed via `ibis.to_sql(..., dialect="bigquery")` and ibis's own `notimpl`-marked test suite). `.approx_quantile()` compiles on DuckDB, Postgres, and BigQuery; DuckDB/Postgres resolve it to their own exact `QUANTILE_CONT`/`PERCENTILE_CONT` under the hood, so no accuracy is lost there. |
| `ibis.Table.describe()` | Not used | Verified live: fails on BigQuery entirely — both `Quantile` (used for numeric column stats) and `Mode` (used for string column stats) are unimplemented there. Also has no working grouped form (`GroupedTable.describe` is a broken attribute-delegation artifact — raises `TypeError: 'GroupedArray' object is not callable`). A small hand-rolled aggregate helper, shared by both tools, replaces it. |
| Both tools stay single-call — no `record`/`resolve`/`compute` gate | `column_distribution` validates `metric_name` inline; no `spec_token` | The 3-step gate on `compute_metrics` protects an *expensive, execution-cost-bearing* operation and guards against double-billing a costly query. Both distribution tools are architecturally closer to `describe_table`/`list_tables` (cheap, read-only inspection, resolve-and-execute in one call) than to `compute_metrics`. `column_distribution` also must work *before* `record_intent` exists (see below), so it can't depend on a prior resolve step anyway. |
| Metric-name validation reuse | Extract `SpecResolver`'s metric-name lookup (currently `resolver.py:39-50`, coupled to a full `MetricIntent` for unrelated `by_entity`/`period_type` checks) into a standalone method `column_distribution` can call directly | Avoids duplicating catalog-lookup/fuzzy-suggestion logic, without forcing `column_distribution` to fabricate a dummy `MetricIntent` just to satisfy an unrelated signature. |
| Linking a discovered range into `record_intent` | New optional `record_intent` param `column_distribution_result_id: str | None`. When set (and `time_window` is not), `time_window` is derived server-side from the referenced `column_distribution` result's stored `min_val`/`max_val` metadata. Passing both `time_window` and `column_distribution_result_id` is rejected (`RecordIntentResult.error`) rather than given a silent precedence rule. Scoped to deriving `time_window` only — no other `MetricIntent` field is currently derived from it. | Achieves the real goal — `record_intent` depending on a prior data summary — without a parallel intent/resolve/audit-trail subsystem. `column_distribution` stores `min_val`/`max_val`/`metric_name`/`column` in `TabularEntry.metadata` (`store.py:64-76` already supports this); `record_intent` just reads it back. Rejected: mirroring the full `record_intent`→`resolve_intent`→`compute_metrics` gate for data summaries too — no audit-trail requirement was identified, and the extra round trips aren't justified for what's meant to be a lightweight, exploratory tool. The field is named generally because it references a general-purpose `column_distribution` result (`mean`/`std`/percentiles, not just min/max); the derivation stays narrow because `time_window` is the only `MetricIntent` field with an actual "derive me from column stats" story today — a future field-specific need gets its own explicit handling, not an implicit extension of this one. |
| Cross-metric-name consistency for `column_distribution_result_id` | `MetricIntent` carries `column_distribution_result_id` forward (not just the derived `time_window` tuple). `resolve_intent` — after `SpecResolver.resolve()` returns a structurally valid match — additionally checks that the referenced `column_distribution` result's `metadata["metric_name"]` matches the `metric_name` now being proposed; a mismatch is appended as a `NearMiss` (`why_not="column_distribution_metric_mismatch"`), blocking `exact_match` the same way any other resolution failure does. | `column_distribution` validates its `metric_name` independently of any `record_intent`/`resolve_intent` flow (by design — it must work standalone, before an intent exists). Nothing otherwise ties the metric a `column_distribution_result_id` was computed against to the metric `resolve_intent`/`compute_metrics` eventually settles on — the LLM could derive bounds from one metric's source table (`metric_name="daily_sales"`) while `resolve_intent` resolves a *different* metric (`metric_name="sales_volume"`) for the same intent, silently applying one table's real date range to an unrelated table's query. This is checked at `resolve_intent`, the one place a canonical name is actually validated, reusing the existing near-miss/exact-match gate rather than a new mechanism. `SpecResolver` itself is not changed — it stays a pure, stateless catalog validator; the check lives in the `resolve_intent` tool function, which already has `ctx.deps.store`. |
| Data staleness (pushdown re-hits live data vs. a frozen pandas snapshot) | Not a concern to engineer around | This is OLAP analysis against a live warehouse; interim data changes are an inherent risk of any query, not something this redesign introduces or should try to prevent. |
| Segment-based filtering for `column_distribution` | Out of scope | A segment's `where` applies to a *different* table (the segment's own DIM source) and requires a join back to the fact table on `entity_id` (`_build_metric_segment_query`, `builder.py:259-269`) — meaningfully more machinery than a same-table filter. The free-form `filter` string already covers the general case; a structured segment shortcut can follow later if needed. |

---

## 2. Scope

**In scope:**
- SF-1 — Shared internal aggregate-builder helper (`query_tools.py`), dtype-aware, used by both tools.
- SF-2 — Generalized `ColumnDistribution` type replacing `MetricDistribution`; updated `DistributionSummaryResult`; new `ColumnDistributionResult`.
- SF-3 — `distribution_summary` redesign: `group_by` param, ibis pushdown, retained `ibis_ref`.
- SF-4 — New `column_distribution` tool: `metric_name`, `column` (defaults to `timestamp_col`), `filter` (subquery-rejecting via `sqlglot`).
- SF-5 — Extract reusable metric-name validation from `SpecResolver`.
- SF-6 — `record_intent`: new `column_distribution_result_id` param, server-side `time_window` derivation; `resolve_intent`: validates the derivation wasn't sourced from a different metric.
- SF-7 — Register `column_distribution` on `QueryBot`; prompt updates (Layer A).
- SF-8 — Tests.
- SF-9 — Docs (changelog).

**Out of scope:**
- Adding either tool to `DefinitionBot` — not the reported need.
- `group_by` support for `column_distribution` — would require re-deriving slice/segment classification expressions against the raw table, i.e. most of `compute_metrics`.
- Segment-based filtering for `column_distribution` (needs DIM-table join machinery) — the free-form `filter` string covers the general case for now.
- Any *correctness* validation layer on `column_distribution`'s `filter` beyond execution-time compile/run errors (the `sqlglot`-based check added is narrowly scoped to table-scope safety — rejecting subqueries — not general SQL correctness).
- Pushing `period_over_period`/`contribution_share` to ibis — both legitimately need pandas today for period-ordering/window logic (their own stated design rationale, `plans/24-agent-phase2.md:113`); a separate, harder effort if ever pursued.
- Guaranteeing consistency between a `column_distribution` filter (or lack thereof) and whatever slice/segment eventually scopes the real metric computation — bridged by the LLM's own judgment, not enforced in code. This is narrower than it may sound: the *metric identity* itself is enforced (see the "Cross-metric-name consistency" key decision — `resolve_intent` blocks if `column_distribution_result_id` points at a different metric than the one being resolved); only the *filter/slice/segment scope within the same metric* is left to the LLM.

---

## 3. Sub-features

### SF-1 — Shared distribution aggregate helper

**File:** `aitaem/agent/query_tools.py`
- `_build_distribution_agg(table: ibis.Table, value_column: str, group_by: list[str] | None) -> ibis.Table`.
- Dtype-branches on `table[value_column].type().is_numeric()`:
  - Numeric: `count`, `null_count` (`col.isnull().sum()`), `mean`, `std`, `min_val`/`max_val` (native dtype, **not** cast to string here), `p25`/`median`/`p75` via `col.approx_quantile(0.25|0.5|0.75)`.
  - Non-numeric: `count`, `null_count`, `min_val`/`max_val` (native dtype), `distinct_count` (`col.nunique()`).
- `table.group_by(group_by).aggregate(**aggs)` if `group_by` else `table.aggregate(**aggs)` — one query either way.
- `min_val`/`max_val` are stringified by the *caller*, in Python, after `.to_pandas()` — never
  in the ibis expression itself (see the "`min_val`/`max_val` stringification" key decision
  above). Both `distribution_summary` (SF-3) and `column_distribution` (SF-4) apply the same
  rule when converting a result row into a `ColumnDistribution`: `value.isoformat()` if the
  value has that method, else `str(value)`, else `None`.

### SF-2 — Generalized result types

**File:** `aitaem/agent/query_types.py`
- Replace `MetricDistribution` with:
  ```python
  class ColumnDistribution(BaseModel):
      group_key: dict[str, str]
      count: int
      null_count: int | None = None
      mean: float | None = None
      std: float | None = None
      min_val: str | None = None
      p25: float | None = None
      median: float | None = None
      p75: float | None = None
      max_val: str | None = None
      distinct_count: int | None = None
  ```
  Breaking, not additive: `MetricDistribution.metric_name: str` is gone — replaced by
  `group_key: dict[str, str]` (holding `{"metric_name": ...}` for `distribution_summary`'s
  default grouping, or any other requested `group_by` combination). `min_val`/`max_val` change
  type from `float | None` to `str | None` (see the `min_val`/`max_val` stringification key
  decision — needed for `column_distribution`'s non-numeric columns, applied uniformly to both
  tools' results for one consistent type). Any existing caller reading
  `distribution.metric_name`, or treating `distribution.min_val`/`max_val` as numeric, breaks —
  regardless of whether it passes the new `group_by` param.
- `DistributionSummaryResult`: `result_id: str`, `group_by: list[str]`, `distributions: list[ColumnDistribution]`.
- New `ColumnDistributionResult`: `result_id: str`, `distribution: ColumnDistribution | None = None`, `error: str | None = None`.
- `RecordIntentResult` gains `error: str | None = None` (for an invalid `column_distribution_result_id`).
- `NearMiss.why_not` (`query_types.py:77-82`) is a closed `Literal` of seven values today —
  gains an eighth: `"column_distribution_metric_mismatch"` (needed by SF-6's cross-metric-name
  check). `NearMiss.suggestions: list[str] = []` already exists and needs no field change, but
  its docstring (`query_types.py:84-86`) currently states "Non-empty only when
  why_not='unknown_metric' ... Empty for all other why_not reasons" — SF-6 populates it for
  `column_distribution_metric_mismatch` too (with the metric the `column_distribution` result
  was actually computed against, not a fuzzy-match suggestion — a different purpose reusing the
  same field). Update the docstring to document both populated cases, not just the one.

### SF-3 — `distribution_summary` redesign

**File:** `aitaem/agent/query_tools.py`
- New signature: `distribution_summary(ctx, result_id: str, group_by: list[str] | None = None) -> DistributionSummaryResult`. `group_by` defaults to `["metric_name"]`.
- `table = _get_ibis_table(entry)`; `agg = _build_distribution_agg(table, "metric_value", group_by)`; `df = agg.to_pandas()` (now cheap — the aggregate, not the raw table).
- One `ColumnDistribution` per row: `group_key = {k: str(row[k]) for k in group_by}`, remaining fields from the row.
- `store.store_tabular(pa.Table.from_pandas(df), agg)` — passes `agg` (the lazy expression) as `ibis_ref`, not `None`. Note in the docstring/comment that this is deliberately asymmetric with the other four analysis tools (see the key-decisions row) so a future reader touching a sibling doesn't have to rediscover why.

### SF-4 — `column_distribution` tool

**File:** `aitaem/agent/query_tools.py`
```python
def column_distribution(
    ctx: RunContext[QueryDeps],
    metric_name: str,
    column: str | None = None,
    filter: str | None = None,
) -> ColumnDistributionResult:
```
- Resolve `metric_name` via SF-5's extracted validator; unknown name → `error` set, `distribution=None`.
- `column` defaults to `spec.timestamp_col`; if still `None`, error asking for an explicit `column`.
- Resolve connector/table via `connection_manager.get_connection_for_source(spec.source)` +
  `resolve_table_reference(spec.source)` + `connector.get_table(...)` (same pattern
  `compute_metrics`/`validate_spec` already use).
- `column` not in the table's schema → error listing available columns.
- If `filter` is set: first pass it through `_reject_unsafe_filter(filter, dialect) -> str`
  (new helper — see below); a rejection returns a normal `error`, not raised. The helper's
  return value (the sqlglot-regenerated SQL, not the raw input) is then spliced the same way
  `QueryBuilder` splices other user-authored SQL fragments — alias the table,
  `t_src.sql(f"SELECT * FROM {alias} WHERE {safe_filter}")`. A compile/execution failure at
  this point (valid `Condition` syntax, but references a column that doesn't exist, etc.) is
  also caught and returned as a normal `error`.
- `agg = _build_distribution_agg(filtered_table, column, group_by=None)`; execute; build one
  `ColumnDistribution` with `group_key={"metric_name": metric_name, "column": column}`.
- `store.store_tabular(single_row_arrow, agg, metadata={"metric_name": metric_name, "column": column, "min_val": ..., "max_val": ...})` — metadata carries `min_val`/`max_val` so SF-6 can read them without re-parsing Arrow.

**New helper, same file:** `_reject_unsafe_filter(filter_sql: str, dialect: str) -> str`.
- Parses `filter_sql` via `sqlglot.parse_one(filter_sql, into=sqlglot.exp.Condition, dialect=dialect)`.
- A parse failure, or a parsed tree containing any `Subquery`/`Select` node
  (`parsed.find_all(...)`), raises `ValueError` with a message identifying which — caught by
  the caller and surfaced as a normal tool `error`, never propagated as an exception.
- On success, returns `parsed.sql(dialect=dialect)` — the sqlglot-*regenerated* SQL, not the
  raw input — which callers must splice, so a trailing SQL comment in `filter_sql` is
  re-serialized as an inert block comment rather than passed through unmodified.
- `dialect` is the metric's own backend type (`duckdb`/`bigquery`/`postgres`), already known
  from resolving `spec.source` earlier in the function.
- `sqlglot` is added as an explicit direct dependency in `pyproject.toml` — it's already
  installed transitively via `ibis`, but `column_distribution` now imports it directly, so it
  shouldn't be relied on as a transitive dependency going forward.

### SF-5 — Reusable metric-name validation

**File:** `aitaem/agent/resolver.py`
- Extract `SpecResolver`'s metric-lookup + fuzzy-suggestion block (`resolver.py:39-50`) into a
  standalone method: `SpecResolver.resolve_metric_name(name: str, spec_cache: Any) -> tuple[MetricSpec | None, list[str]]`
  — `(spec, [])` on success, `(None, suggestions)` on failure — callable without a
  `MetricIntent`. A plain tuple, not a `MetricSpec | list[str]` union: a union return would
  force `isinstance()`-branching at every call site, whereas `if spec is None: ...` reads
  directly. Matches the precedent already set by `ConnectionManager.resolve_table_reference()`
  (Plan 35) — a plain tuple over a bespoke type for a simple two-part return. `resolve()` calls
  it internally; behavior unchanged for existing callers.

### SF-6 — `record_intent` + `resolve_intent`: derive `time_window` from a prior summary, and validate it wasn't derived from a different metric

**File:** `aitaem/agent/query_tools.py`, `aitaem/agent/query_types.py`

**`record_intent` (deriving):**
- Gains `column_distribution_result_id: str | None = None`.
- Both `time_window` and `column_distribution_result_id` set → `RecordIntentResult(error=...)`, no
  intent recorded — ambiguous input, rejected rather than given a silent precedence rule.
- `column_distribution_result_id` set, `time_window` not: look up
  `ctx.deps.store.get_tabular(column_distribution_result_id)`, read `entry.metadata["min_val"]`/`["max_val"]`;
  missing/wrong-shaped metadata → `RecordIntentResult(error=...)`, no intent recorded.
  Otherwise `MetricIntent.time_window = (min_val, max_val)` **and**
  `MetricIntent.column_distribution_result_id = column_distribution_result_id` (the reference itself is kept,
  not just the derived tuple — needed by the `resolve_intent` check below).
- `MetricIntent` (`query_types.py:14-29`) gains the same `column_distribution_result_id: str | None = None` field.

**`resolve_intent` (validating):**
- After `SpecResolver.resolve()` returns a match with `exact_match` set (i.e. metric
  name/slices/segment/by_entity/period_type all already passed): if
  `intent.column_distribution_result_id is not None`, look up that entry again and compare
  `entry.metadata["metric_name"]` against the `metric_name` being proposed to `resolve_intent`
  in *this* call. Mismatch → override the result to `exact_match=None`, append
  `NearMiss(name=metric_name, why_not="column_distribution_metric_mismatch", suggestions=[entry.metadata["metric_name"]])`
  — no `spec_token` minted, same as any other resolution failure.
- This check lives in the `resolve_intent` tool function itself, not inside `SpecResolver` —
  `SpecResolver.resolve()`'s signature and body are unchanged; it stays a pure `spec_cache`
  lookup with no `ResultStore` dependency, preserving its documented "stable interface, v1 swaps
  only the body" contract (`resolver.py:9-14`).

**Lifetime caveat:** `ResultStore` is not per-run — `history.py`'s
`dump_store`/`load_store` (`history.py:26-49,52-74`) explicitly round-trip `TabularEntry.metadata`
through `load_history()`; only `ibis_ref` is dropped on reload (`ibis_ref=None`,
`history.py:70`). So a `column_distribution_result_id`'s `min_val`/`max_val`/`metric_name`
survive a reload intact. What genuinely *is* per-run is `QueryDeps.intents`
(`query_bot.py:372-376,419-423` construct a fresh `QueryDeps()`, `intents`/`spec_registry`
defaulting empty, on every `ask()`/`chat()` call) — so `record_intent` and `resolve_intent` for
a given intent must still happen within the same turn. This is a pre-existing v0.2 constraint
that applies to every intent regardless of `column_distribution_result_id` — not something this
sub-feature introduces or needs to work around. SF-6's existing "missing/wrong-shaped metadata
→ error" handling already covers the one case that *would* still matter: a
`column_distribution_result_id` that's genuinely invalid or unresolvable for any reason
degrades gracefully into a normal tool error, no special-casing needed.

### SF-7 — Wire into `QueryBot` + prompt

**File:** `aitaem/agent/query_bot.py`
- Register `column_distribution` in `_build_agent()`.
- Layer A: document `column_distribution` (when to call it — period_type isn't `all_time` but
  the user implies "all data"; that it must be called *before* Step 1 (`record_intent`), not
  after — `record_intent`'s `column_distribution_result_id` needs the result to already exist,
  so calling `record_intent` first leaves nothing to reference; how `filter` works — a plain SQL
  predicate, not a slice name; that a temporal column lands in the non-numeric stat branch —
  `count`/`min`/`max`/`distinct_count` only, no `mean`/`std`/percentiles — so don't ask it for
  the median of a date column); document
  `column_distribution_result_id` on `record_intent`, including that `resolve_intent` will
  reject it (`why_not="column_distribution_metric_mismatch"`) if the metric being resolved doesn't
  match the metric `column_distribution` was called against — call `column_distribution` again
  for the correct metric rather than reusing an unrelated `result_id`; document
  `distribution_summary`'s new `group_by` param; keep the existing "never fabricate a
  placeholder date" instruction.

### SF-8 — Tests

**Files:** `tests/test_agent/test_query_tools.py`, `tests/test_agent/test_query_types.py`, `tests/test_agent/test_query_bot.py`, `tests/test_agent/test_resolver.py`
- `_build_distribution_agg`: numeric vs. non-numeric branch, grouped vs. ungrouped, `approx_quantile` used (not `quantile`) — assert via `ibis.to_sql(..., dialect="bigquery")` compiling cleanly (regression test for the `describe()`/`quantile()` BigQuery failure this design avoids).
- `min_val`/`max_val` stringification (DuckDB-backed): for a timestamp column, assert the
  stored `ColumnDistribution.min_val` is `datetime.fromisoformat()`-parseable and specifically
  equals the Python-side `.isoformat()` form (`"2024-01-15T00:00:00"`), not the backend's own
  `CAST(... AS STRING)` form (`"2024-01-15 00:00:00"`) — regression test for the format
  mismatch this design avoids.
- `distribution_summary`: default `group_by=["metric_name"]` groups identically to today's
  hardcoded behavior (same rows, same values) but returns `ColumnDistribution`/`group_key`, not
  `MetricDistribution`/`metric_name` — assert the new shape explicitly, don't just assert
  row-equivalence with the old test's expectations; custom `group_by` (e.g.
  `["metric_name", "slice_value"]`) produces one row per combination; output retains `ibis_ref`.
- `column_distribution`: success (explicit `column`, default-to-`timestamp_col`); unknown `metric_name`; unknown `column`; `filter` narrows results (mocked or DuckDB-backed); malformed `filter` → `error` set, not raised.
- `_reject_unsafe_filter`: rejects `IN (SELECT ...)`, `EXISTS (...)`, and a correlated
  comparison subquery, across all three dialects; rejects statement-stacking and paren-breakout
  (both fail to parse); accepts legitimate multi-condition filters (`AND`/`OR`/parenthesized
  groups) unchanged; comment-truncation input (`1=1 -- ' AND x`) parses but the *returned* SQL
  has the trailing text safely inside a block comment — assert on the returned string, not just
  that it didn't raise.
- `SpecResolver.resolve_metric_name`: extracted method covers the same unknown-name/fuzzy-suggestion cases as today's `resolve()` tests.
- `record_intent` with `column_distribution_result_id`: derives `time_window` correctly and stores
  `column_distribution_result_id` on the `MetricIntent`; unknown/wrong-shaped `result_id` → `error` set;
  both `time_window` and `column_distribution_result_id` given → `error` set, no intent recorded.
- `resolve_intent` cross-metric-name check: `column_distribution_result_id` from `column_distribution(metric_name="daily_sales")`,
  then `resolve_intent(..., metric_name="daily_sales")` → `exact_match` set as normal;
  `resolve_intent(..., metric_name="sales_volume")` (different metric) → `exact_match=None`,
  a `NearMiss` with `why_not="column_distribution_metric_mismatch"` present, no `spec_token` minted —
  the regression test for the gap this sub-feature closes.
- `ColumnDistribution`/`DistributionSummaryResult`/`ColumnDistributionResult` field-shape tests.

### SF-9 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Added`: new `column_distribution` tool and
  `record_intent`'s `column_distribution_result_id`.
- `docs/changelog.md` → `## Unreleased` → `### Breaking changes` (or equivalent section, per
  this repo's convention for a breaking release — see Plan 35's changelog entries): call
  signature stays compatible, but `DistributionSummaryResult.distributions` is now
  `list[ColumnDistribution]`, not `list[MetricDistribution]` — `metric_name: str` is replaced by
  `group_key: dict[str, str]`, and `min_val`/`max_val` change from `float | None` to
  `str | None`. `distribution_summary` also now pushes aggregation down to the backend instead
  of pandas.

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/agent/query_tools.py` | SF-1: `_build_distribution_agg`; SF-3: `distribution_summary` redesign; SF-4: new `column_distribution`, `_reject_unsafe_filter`; SF-6: `record_intent` param + `resolve_intent` cross-metric-name check |
| `pyproject.toml` | SF-4: `sqlglot` added as an explicit direct dependency |
| `aitaem/agent/query_types.py` | SF-2: `ColumnDistribution`, updated `DistributionSummaryResult`, new `ColumnDistributionResult`, `RecordIntentResult.error`, `NearMiss.why_not` new literal; SF-6: `MetricIntent.column_distribution_result_id` |
| `aitaem/agent/resolver.py` | SF-5: extracted `resolve_metric_name` |
| `aitaem/agent/query_bot.py` | SF-7: tool registration; Layer A prompt updates |
| `tests/test_agent/test_query_tools.py` | SF-8: `_build_distribution_agg`, `distribution_summary`, `column_distribution` tests |
| `tests/test_agent/test_query_types.py` | SF-8: field-shape tests |
| `tests/test_agent/test_query_bot.py` | SF-8: tool-registration set update, prompt-content assertions |
| `tests/test_agent/test_resolver.py` | SF-8: `resolve_metric_name` tests |
| `docs/changelog.md` | SF-9: Unreleased entries |
