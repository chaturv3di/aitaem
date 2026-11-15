# Section 4 — Contracts with AITAEM Core

## Purpose

This section defines what the `aitaem.agent` module imports from `aitaem` core. The boundary is **one-way**: `aitaem.agent` depends on `aitaem`; `aitaem` never imports from `aitaem.agent`. This is enforceable by package-level import rules and verified at CI time.

Two contract surfaces are documented:

1. **Current contract (AITAEM v0.4.0):** what the agent module consumes today.
2. **Cross-backend materialization considerations:** the v0.4.0 `tmp_dir` parameter, historically added to `MetricCompute` and its lifetime implications for the agent module, subsequently moved to `ConnectionManager` (Plan 25) — see §2 below for the current state.

---

## 1. Current AITAEM public surface (v0.4.0)

All imports below are from the top-level `aitaem` package. Submodule imports are explicitly out of contract — the agent module never reaches into `aitaem.specs.loader`, `aitaem.query.builder`, `aitaem.connectors.*`, etc. This rule keeps the agent module stable against AITAEM internal refactors.

### Types

| Symbol | Kind | Role in the agent module |
|---|---|---|
| `SpecCache` | class | The catalog the agent reads from to know what metrics/slices/segments exist. Constructed by the caller. |
| `MetricSpec` | class | Typed spec object available via `SpecCache.metrics[name]`. Carries `.name`, `.description`, `.entities`, `.format` (optional). |
| `SliceSpec` | class | Typed spec object via `SpecCache.slices[name]`. |
| `SegmentSpec` | class | Typed spec object via `SpecCache.segments[name]`. |
| `IbisConnector` | class | Backend-specific connector. Constructed by `ConnectionManager.add_connection()`; agent module does not instantiate directly. |
| `ConnectionManager` | class | The connection registry passed by the caller. Owns connection lifecycle. |
| `MetricCompute` | class | The compute engine. The agent module constructs one fresh per `compute_metrics` call (AD-16, revised), from `spec_cache` and `connection_manager` only — no operational kwargs exist to forward today (AD-17, dormant). `.compute(...)` returns an `ibis.Table` (lazy). |
| `PeriodType` | `Literal[...]` | Period granularity values. Used in tool input schemas to stay in sync with AITAEM. Currently: `"all_time" \| "hourly" \| "daily" \| "weekly" \| "monthly" \| "yearly"`. |
| `METRIC_FORMAT_VALUES` | `frozenset[str]` | Valid values for `MetricSpec.format`: `percentage`, `ratio`, `absolute`, `currency`, plus `currency:<ISO>` pattern. Used by tools that produce summaries to surface format metadata to the LLM. |
| `ValidationResult` | dataclass | Returned by `*Spec.from_string()` and `.validate()`. Carries `.valid`, `.errors`, `.referenced_columns: dict[str, list[str]] \| None`. |

### Exceptions

| Symbol | Raised when | Handled in agent module by |
|---|---|---|
| `SpecValidationError` | Spec YAML fails structural or SQL-identifier validation at load time | Caller's responsibility (catches before constructing `SpecCache`); agent module assumes a validated cache |
| `SpecNotFoundError` | A spec name passed to `MetricCompute` doesn't exist in the cache | Caught inside `compute_metrics` tool; converted to a tool-result error dict |
| `QueryBuildError` | AITAEM cannot construct a query from the given spec + parameter combination | Same as above |
| `QueryExecutionError` | The backend warehouse fails during execution | Same as above |
| `AitaemConnectionError` | A backend connection fails (e.g. BigQuery auth) | Same as above; may also surface during connection construction in caller code |

### Helpers

| Symbol | Role in the agent module |
|---|---|
| `aitaem.helpers.load_csvs_to_duckdb` | Not used by the agent module directly. Callers building a DuckDB-backed connection use this; the agent module sees the resulting `ConnectionManager`. |

---

## 2. What the agent module consumes — by component

### Convenience bots

All bots take three required AITAEM-derived arguments at construction (plus bot-specific extras):

```
SpecCache          — the catalog the LLM and tools read from
ConnectionManager  — the connection registry tools execute against
model              — an LLM identifier string (pydantic-ai's model string format,
                     e.g. "anthropic:claude-haiku-4-5", "openai:gpt-4o")
```

These three together are the **AITAEM-side dependency surface** for any bot. Anything beyond this (history, extra tools, prompt overrides) is agent-module concern, not AITAEM concern.

`DefinitionBot` additionally consumes:
- `IbisConnector` (via `ConnectionManager`) for schema introspection — to know what columns and types exist in the backend so it can produce valid specs.
- `MetricSpec.from_string()` / `SliceSpec.from_string()` / `SegmentSpec.from_string()` for post-generation validation. The returned `ValidationResult.referenced_columns` is the substrate for cross-table-reference checking.

`SetupBot` consumes:
- `ConnectionManager` only after a connection has been added — for validation. It does *not* construct connections itself; it produces a config dict the caller uses to call `ConnectionManager.add_connection(backend_type, **kwargs)`. This keeps credential handling outside the library.

### `compute_metrics` tool (in QueryBot's default tool set)

The single AITAEM-touching tool. Calls:

```
MetricCompute(spec_cache, connection_manager).compute(
    metrics=[...],
    slices=[...],
    segments=[...],
    time_window=(start_iso, end_iso) | None,
    period_type=PeriodType,
    by_entity=str | None,
)
```

The agent module's tool catches `SpecNotFoundError`, `QueryBuildError`, `QueryExecutionError`, `AitaemConnectionError`, and returns a structured tool-result dict — never raises.

### Analysis tools (in QueryBot's default tool set)

Analysis tools (`rank_by_value`, `filter_by_threshold`, `distribution_summary`, `period_over_period`, `contribution_share`) operate on the result of a prior `compute_metrics` call. They do **not** touch AITAEM directly. They consume the result store entry written by `compute_metrics` and produce new result store entries.

This is important: the analysis tools are AITAEM-shape-aware (they know STANDARD_COLUMNS) but AITAEM-API-free. They depend on the *shape* of AITAEM output, not on AITAEM types.

---

## 3. STANDARD_COLUMNS — the result shape contract

Every `MetricCompute.compute()` result, regardless of metric/slice/segment combination, carries the same 11-column schema (added in v0.2.1; unchanged in v0.4.0):

| Index | Column | Type | Notes |
|---|---|---|---|
| 0 | `period_type` | str | One of `PeriodType` values |
| 1 | `period_start_date` | date / timestamp | Per-row period boundary |
| 2 | `period_end_date` | date / timestamp | Per-row period boundary |
| 3 | `entity_id` | str / None | Populated when `by_entity` was set |
| 4 | `metric_name` | str | The spec's `name` |
| 5 | `metric_format` | str / None | Optional format hint from `MetricSpec.format` |
| 6 | `slice_type` | str / None | Slice spec name, when sliced |
| 7 | `slice_value` | str / None | Slice value, when sliced |
| 8 | `segment_name` | str / None | Segment spec name, when segmented |
| 9 | `segment_value` | str / None | Segment value, when segmented |
| 10 | `metric_value` | numeric | **The single value column.** All analysis tools reference this by name. |

**Architectural commitment:** The agent module's analysis tools reference STANDARD_COLUMNS by **name**, never by index. AITAEM has demonstrated that additive column changes can happen (the `metric_format` column added in v0.2.1 shifted subsequent column positions). Name-based access is forward-compatible.

The agent module exports a constant set (or imports it from AITAEM if/when AITAEM publishes one) listing the non-value metadata columns; downstream summary generation, chart axis derivation, and any analytic that needs to identify the value column relies on this name set.

---

## 4. Ibis-return `compute()` and cross-backend materialization

### What v0.4.0 delivers

`MetricCompute.compute()` returns an **Ibis table reference** (an unmaterialized `ibis.Table` expression) instead of a `pandas.DataFrame`. Materialization is explicit:

```
table_ref = mc.compute(...)        # lazy ibis.Table
df = table_ref.to_pandas()         # explicit materialization
arrow_table = table_ref.to_pyarrow()
```

This aligns AITAEM with how Ibis is generally consumed (see [ibis-project.org](https://ibis-project.org/)) and matches the agent module's dual-representation result store design (AD-12).

### Cross-backend `tmp_dir`

v0.4.0 added a `tmp_dir` parameter to `MetricCompute`, used for a scratch DuckDB file when a `compute()` call spans multiple source backends. Plan 25 subsequently moved this parameter onto `ConnectionManager` — **`tmp_dir` is a `ConnectionManager` constructor parameter today, not a `MetricCompute` one.** The v0.4.0-era shape is kept below as historical context; the current shape follows.

**v0.4.0-era shape (superseded):**

```
MetricCompute(
    spec_cache: SpecCache,
    connection_manager: ConnectionManager,
    tmp_dir: str | None = "/tmp",
)
```

**Current shape (post-Plan-25):**

```
ConnectionManager(
    ...,
    tmp_dir: str | None = "/tmp",
)
MetricCompute(spec_cache: SpecCache, connection_manager: ConnectionManager)
```

When a `compute()` call spans multiple source backends (e.g. a metric whose numerator and segment data live in different connections), AITAEM cannot push the entire computation to one warehouse. It pulls intermediate results into a scratch DuckDB file at `tmp_dir`; the file is reclaimed when the `ConnectionManager` instance is garbage-collected (or `close_all()` is called), with the OS reclaiming on reboot as a final backstop. `tmp_dir=None` forces in-memory DuckDB — safe only when result sets are known small.

### What this means for the agent module

Two architectural commitments (a third, from the v0.4.0-era shape, no longer applies):

- **`MetricCompute` is constructed fresh per `compute_metrics` call (AD-16, revised).** It's a stateless wrapper around `spec_cache`/`connection_manager` — no resource-lifecycle reason to hold one. Live Ibis refs in the result store (AD-12 dual representation) reference into `ConnectionManager`'s scratch DuckDB, not `MetricCompute`'s — tearing down a per-call `MetricCompute` instance doesn't invalidate them, since `ConnectionManager` is bot-held for the bot's lifetime regardless.
- **Filesystem footprint is the caller's responsibility**, now via `ConnectionManager(tmp_dir=...)` rather than a bot-side passthrough. Multi-tenant deployments may want per-tenant tmp directories (e.g. `/tmp/aitaem/{workspace_id}/`). Resource-constrained environments (Lambda's 512MB /tmp, container tmpfs limits) may need specific paths or `None` for in-memory. None of this is the agent module's concern; the caller constructs `ConnectionManager` directly with the `tmp_dir` it wants.
- **No AITAEM operational-parameter passthrough exists today (AD-17, dormant).** `MetricCompute` takes only `spec_cache`/`connection_manager` — nothing left for a `compute_kwargs` dict to forward. If AITAEM reintroduces an operational `MetricCompute` parameter, AD-17's opaque-dict design is the reactivation guidance.

### Tool contract under v0.4.0

`compute_metrics` tool:
- Constructs `MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)` fresh, then calls `.compute(...)` (AD-16, revised) — not held on the bot.
- To the LLM: a minimal summary (row count, column names, sample of ≤5 rows materialized via `to_pyarrow().slice(0, 5).to_pylist()` or equivalent, plus format/spec metadata).
- To the result store: both the Ibis ref and a materialized Arrow artifact.

Analysis tools accept an existing result store entry as input (by ID) and produce a new entry. Each may operate in **lazy mode** (chain Ibis operations, materialize only at the end) or **eager mode** (operate on the already-materialized artifact). Lazy mode is preferred when the ref is live and the operation has good Ibis support; eager mode is the fallback. This choice is internal to each tool.

---

## 5. Spec catalog — how the agent reads it

The agent's system prompt is built from the `SpecCache` via typed attributes:

- `cache.metrics: dict[str, MetricSpec]`
- `cache.slices: dict[str, SliceSpec]`
- `cache.segments: dict[str, SegmentSpec]`

Each spec exposes `.name`, `.description`. `MetricSpec` additionally exposes `.entities: list[str]` and `.format: str | None`.

**Architectural commitment:** the agent module never parses raw YAML. Spec metadata access is via typed attributes only. Once AITAEM exposes typed attributes for a piece of metadata, that is the contract — falling back to YAML re-parsing is brittle against AITAEM internal changes. Any future spec metadata the agent needs to surface to the LLM should be requested as an AITAEM API addition, not parsed from YAML in the agent module.

---

## 6. The one-way dependency rule

Enforcement:

- `aitaem.agent.__init__` may import from `aitaem` freely (top-level only — never submodules).
- `aitaem.*` (excluding `aitaem.agent`) must not import from `aitaem.agent` under any circumstance.
- CI check: a simple import-graph linter (e.g., `import-linter` with a `forbidden` contract) catches accidental violations.

Why this matters: `aitaem.agent` is an optional install (`pip install aitaem[agent]`). Users who don't install the `agent` extra must still be able to import `aitaem` and use core functionality. Any reverse dependency breaks the optional-install promise.

---

## 7. Versioning expectations between AITAEM and `aitaem.agent`

The two ship together — `aitaem.agent` is a subpackage of `aitaem`, not a separate distribution. There is no version skew possible between the agent module and its host AITAEM version.

That said, the agent module's *external* signatures (bot constructors, response types, tool input schemas) should follow semver discipline: breaking changes go in major version bumps. AITAEM-internal refactors that don't touch the agent module's external API can ship in minors and patches freely.

---

## 8. Summary — the contract in one block

```
aitaem.agent imports (top-level only):
    SpecCache, MetricSpec, SliceSpec, SegmentSpec
    ConnectionManager, IbisConnector
    MetricCompute
    PeriodType, METRIC_FORMAT_VALUES
    ValidationResult
    SpecValidationError, SpecNotFoundError,
    QueryBuildError, QueryExecutionError, AitaemConnectionError

aitaem.agent assumes (AITAEM v0.4.0):
    MetricCompute(spec_cache, connection_manager)
        — constructed fresh per compute_metrics call (AD-16, revised)
    MetricCompute.compute(...) -> ibis.Table (lazy)
    ibis.Table.to_pandas() / .to_pyarrow()  (explicit materialization)
    STANDARD_COLUMNS schema, accessed by name
    ConnectionManager(tmp_dir=...) for cross-backend scratch DuckDB
        — no operational-parameter passthrough exists today (AD-17, dormant)

aitaem.agent never:
    imports from aitaem submodules
    parses raw spec YAML
    handles credentials directly
    instantiates IbisConnector or backend-specific connections
    persists anything to disk or network
    picks defaults for AITAEM operational parameters
```
