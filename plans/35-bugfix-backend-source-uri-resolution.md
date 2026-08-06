# Plan 35 — Bugfix: Backend-Correct Source URI Resolution

**Scope:** Three bugs found via live testing (real BigQuery, real Supabase Postgres via `.env.backends`). Breaking change accepted directly — `list_tables`/`describe_table`'s contract changes rather than gaining a field alongside the old shape.

**Motivation:** the LLM should never assemble a `source:` URI from parts, for any backend — it should always be handed something read verbatim from a tool result. A metric's `source:` should also always be honored exactly as written, including when it names a different project/dataset than the connection's own default. Both require backend-specific resolution logic in the tools and in `aitaem`-core.

---

## 0. The gaps

### Gap A — DefinitionBot fabricates BigQuery project/dataset

Live session: DefinitionBot listed `fact_sales_normalized` via `list_tables()`, described it via `describe_table()`, then committed `MetricSpec(source='bigquery://myproject/dataset.fact_sales_normalized', ...)`. The real connection was scoped to project `project-12bab95f-3152-4c49-87a`, dataset `kaggle_retail` — neither `myproject` nor `dataset` are real; the LLM fabricated plausible-looking placeholders because it had no real values to draw from.

**Root cause:** `list_tables()`/`describe_table()` (`definition_tools.py:106-188`) return bare table names — the LLM has to construct a `source:` URI from a bare name plus project/dataset it was never given. `IbisConnector` already resolves this at connect time (`_bq_project_id`/`_bq_dataset_id`, `ibis_connector.py:122-123`) — it's just never returned to the caller.

**Compounding factor:** `validate_spec`'s Check 5 (`definition_tools.py:447-451`) catches every exception during the live column-existence check identically, degrading a definitive "this source is wrong" signal to the same non-blocking warning as "check unavailable" — so the bad spec still passed validation and committed.

### Gap B — `IbisConnector.get_table()` resolution: Postgres broken, BigQuery's scope check isn't a real boundary

`_parse_table_name_from_uri()` (`builder.py`) dot-joins Postgres schema+table into one string (`"public.specs"`) before `get_table()` passes it straight through as `self.connection.table(table_name)`. Confirmed live against Supabase:

```
conn.table("public.specs")              → TableNotFound: public.specs   (FAILS)
conn.table("specs", database="public")  → works
```

Ibis's Postgres backend requires the schema as a separate `database=` kwarg, not a dotted string. Since every Postgres `source:` carries an explicit schema, this fails for every Postgres-sourced metric/segment query against a real backend, for every schema including `public`.

The same `database=` kwarg pattern works identically for BigQuery (bare dataset, `project.dataset` string, or `(project, dataset)` tuple — all confirmed live), so `get_table()` can use one uniform call for every backend: accept the bare table name and its database/schema location as two separate parameters, passing `database=` when present.

BigQuery's scope check (`_resolve_bigquery_table_name`, `TableOutOfScopeError`) is removed. Confirmed live that `database=` enforces nothing on its own: a connection scoped to `kaggle_retail` successfully queried an unrelated public dataset in a different project when given an explicit `database=` tuple. The connection's own credentials are the actual boundary; the app-level check only blocked legitimate cross-dataset specs (e.g. `business_tables`, `application_tables` under one set of credentials).

**Verified fix, live:** unified `get_table()` resolves `specs` (`database="public"`)/`users` (`database="auth"`) on Postgres and `fact_sales_normalized` (`database="kaggle_retail"`, including a deliberately cross-project reference) on BigQuery correctly; confirmed end-to-end through `MetricCompute.compute()` on both backends.

### Gap C — `compute()` silently drops a spec's BigQuery project

`_parse_table_name_from_uri()` (`builder.py:578-585`), used by `QueryBuilder` to build the string passed into `get_table()`, discards the project for BigQuery:

```python
backend_type, schema, table = ConnectionManager.parse_source_uri(source_uri)
if backend_type == "bigquery":
    return table  # schema (the project) is discarded
if backend_type == "postgres" and schema:
    return f"{schema}.{table}"  # postgres correctly keeps it
```

Confirmed live: a `MetricSpec` with `source="bigquery://bigquery-public-data/usa_names.usa_1910_current"`, computed through a `ConnectionManager` whose BigQuery connection defaults to an unrelated project, fails with `TableNotFoundError: Table 'usa_names.usa_1910_current' not found` — the project named in the spec never reaches the query at all; `compute()` looks for the table inside the connection's own default project instead.

**Verified fix, live:** carrying the project as part of a separate `database` value, alongside the bare table name, rather than reconstructing a joined string — combined with Gap B's two-parameter `get_table()` — resolves and executes the cross-project spec correctly end to end.

---

## 1. Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Fix target (Gap A) | `list_tables()` returns ready-to-use `source:` URIs in place of bare table names; `describe_table()` takes that same URI as its one input, replacing `table_name`+`backend_type` | One identifier, used unchanged from listing through description through the drafted spec's `source:` field — no assembly step left for the LLM to get wrong. |
| Breaking change | Accepted directly | No released version depends on the current shape. |
| Uniform across all three backends | DuckDB and Postgres also switch to full source URIs in `list_tables()`, not just BigQuery | `describe_table(source)` needs a parseable URI regardless of backend. Neither DuckDB nor Postgres URIs are ambiguous, so this is mechanical. |
| Where DuckDB's URI component comes from | Store the `database` path used at `connect()` time on `IbisConnector` | Mirrors the existing `_bq_project_id`/`_bq_dataset_id` pattern — captured once, no new query. |
| Where Postgres's schema comes from | Query `SELECT current_schema()` once at connect time, store on `IbisConnector` | `PostgresConfig` has no schema field. Verified live against Supabase: returns `'public'`, matching `list_tables()`'s own default scope. |
| BigQuery: bare name with no default dataset configured | `build_source_uri()` returns `None` for a bare name when `dataset_id` isn't set | Defensive only — not reachable via `list_tables()` today, since a project-only-scoped BigQuery connection's `list_tables()` call already fails for the whole backend before any name is returned (see Scope, out-of-scope list). |
| `describe_table`'s new signature | `describe_table(source: str) -> DescribeTableResult`; internally calls `ConnectionManager.resolve_table_reference(source)` to resolve the table name | Same function `QueryBuilder`/`compute()`/`insights.py`'s `_run_scan` all use, so Gap C's fix applies to every caller from one place, permanently. |
| `get_table()` resolution (Gap B) | New signature `get_table(table_name: str, database: str | None = None)` — two explicit parameters, no string splitting. Uniform for all backends: `self.connection.table(table_name, database=database)` if `database` else `self.connection.table(table_name)`. | Verified live on both Postgres and BigQuery, including end-to-end through `MetricCompute.compute()`. Passing the two parts as separate parameters removes the ambiguity a Postgres quoted identifier containing a literal `.` would otherwise create. |
| BigQuery dataset-scope enforcement | Removed — `dataset_id`/`project_id` are resolution defaults only, not access restrictions | Not a real boundary (Ibis doesn't enforce `database=` against the connection's configured scope — confirmed live). The connection's own credentials are the actual boundary; the app-level check only got in the way of legitimate cross-dataset specs. |
| `TableOutOfScopeError` | Removed from the codebase — public export (`aitaem/__init__.py`, `aitaem/utils/__init__.py`), documented (`docs/api/index.md`) | Nothing raises it once scope enforcement is gone. Removed cleanly rather than left dead. |
| Check 5 blocking exception | Block on `AitaemTableNotFoundError`; every other exception type keeps today's warn-and-continue behavior | The exception a fabricated/wrong `source:` surfaces as. Also raised when the table exists but the connection's credentials can't see it — a real ambiguity; the blocking message reads as "source unresolvable," not an assertion that the source is definitely wrong. |
| `_parse_table_name_from_uri` → `ConnectionManager.resolve_table_reference` (Gap B + Gap C) | Moves off `QueryBuilder` onto `ConnectionManager`, as a public sibling to `parse_source_uri`. Returns `tuple[str, str \| None]` — `(table_name, database)` — instead of one joined string. BigQuery: `(table, f"{project}.{dataset}")`. Postgres: `(table, schema)` if `schema` else `(table, None)`. DuckDB: `(table, None)`. | It's a pure wrapper around `parse_source_uri` with no dependency on `QueryBuilder` state, called from three modules unrelated to query building (`builder.py`, `insights.py`, `definition_tools.py`) — a private method reused across module boundaries is the wrong shape for that. `ConnectionManager` already owns URI-parsing semantics and is already imported everywhere this is called. The tuple return also removes the join-then-resplit roundtrip with `get_table()`, so a literal `.` inside a table name can never be misread as a schema/table boundary. |
| `list_tables()` cross-project discovery | Out of scope | Enumerating tables in a project other than the connection's own default is a distinct capability (discovery, not resolution of an already-given URI) — bigger than this bugfix. |
| Prompt update | Layer A: `list_tables()`/`describe_table()` results are ready-to-use `source:` values — pass through and reuse verbatim; never reconstruct | Surfacing correct data doesn't guarantee the LLM copies it as-is rather than reconstructing from memory — the live failure was exactly that. |

---

## 2. Scope

**In scope:**
- SF-1 — `IbisConnector`: capture DuckDB's `database` path and Postgres's `current_schema()` at connect time; add `build_source_uri(table_name: str) -> str | None` covering all three backends.
- SF-2 — `IbisConnector.get_table()`: new two-parameter signature (`table_name`, `database`), no string splitting; remove `_resolve_bigquery_table_name` and BigQuery scope enforcement.
- SF-3 — `list_tables()`: return `build_source_uri()` output in place of bare names.
- SF-4 — `describe_table(source: str)`: replaces `table_name`+`backend_type` params; `DescribeTableResult` gains `source: str`, drops `table_name`/`backend_type`; resolves via `ConnectionManager.resolve_table_reference`.
- SF-5 — `DefinitionBot` prompt: use the provided URIs verbatim throughout.
- SF-6 — `validate_spec` Check 5: `AitaemTableNotFoundError` blocks (via `column_errors`) instead of warning.
- SF-7 — `TableOutOfScopeError`: remove entirely (exception class, exports, docs page entry).
- SF-8 — Move `QueryBuilder._parse_table_name_from_uri` to `ConnectionManager.resolve_table_reference` (`aitaem/connectors/connection.py`), public, returning `tuple[str, str | None]`; fixes the BigQuery project-drop (Gap C) and the join-then-resplit ambiguity (Gap B) in the same change; update all call sites.
- SF-9 — Tests: update every existing `describe_table`/`DescribeTableResult`/`list_tables`/`TableOutOfScopeError` caller and fixture; regression tests for Gap B and Gap C; coverage for the new URI-building and resolution logic.
- SF-10 — Docs: changelog entries for all three gaps; `docs/api/index.md` update for the removed exception.

**Out of scope:**
- Any change to `MetricSpec`/`SegmentSpec`/`SliceSpec` — `source:` was already a plain string.
- Enumerating tables on a project-only-scoped BigQuery connection (no default `dataset_id`) — confirmed live: Ibis's `list_tables()` raises `ValueError: Unable to determine BigQuery dataset` in that configuration today, independent of this plan.
- `list_tables()` discovering/enumerating tables across multiple BigQuery projects — a distinct, bigger capability than correctly resolving a `source:` you already have.

---

## 3. Sub-features

### SF-1 — `IbisConnector`: capture connect-time location, unify URI construction

**File:** `aitaem/connectors/ibis_connector.py`
- `_connect_duckdb`: store `self._duckdb_database = database`.
- `_connect_postgres`: after connecting, run `SELECT current_schema()` once, store `self._pg_schema`.
- New method `build_source_uri(self, table_name: str) -> str | None`:
  - DuckDB: `f"duckdb://{self._duckdb_database}/{table_name}"`.
  - Postgres: `f"postgres://{self._pg_schema}/{table_name}"` (`None` if `_pg_schema` wasn't captured).
  - BigQuery: `None` if not connected; `None` if `self._bq_dataset_id` isn't set (§1, "BigQuery: bare name with no default dataset configured"); else `f"bigquery://{self._bq_project_id}/{self._bq_dataset_id}.{table_name}"`.
  - `None` if `self.connection` is `None`.

### SF-2 — `get_table()`: unified resolution, no scope enforcement

**File:** `aitaem/connectors/ibis_connector.py`
- New signature: `get_table(self, table_name: str, database: str | None = None) -> ibis.expr.types.Table`.
- `self.connection.table(table_name, database=database)` if `database` is not `None`, else `self.connection.table(table_name)`. No string splitting, no backend-specific branch.
- Delete `_resolve_bigquery_table_name` and its `TableOutOfScopeError`/`InvalidURIError` raises.
- Existing not-found exception handling (`IbisError`/generic exception → `AitaemTableNotFoundError`) is unchanged.

**File:** `aitaem/connectors/README.md`
- Update all `get_table(...)` examples to the two-parameter form (e.g. `get_table('orders', database='public')` instead of `get_table('public.orders')`).

### SF-3 — `list_tables()` returns ready-to-use URIs

**File:** `aitaem/agent/definition_tools.py`
- After `tables[bt] = connector.list_tables()`, replace with `[uri for name in raw_names if (uri := connector.build_source_uri(name)) is not None]`.

### SF-4 — `describe_table(source: str)`

**File:** `aitaem/agent/definition_tools.py`
- New signature: `describe_table(ctx, source: str) -> DescribeTableResult`.
- `backend_type, _, _ = ctx.deps.connection_manager.parse_source_uri(source)` to get the backend; on parse failure, return `DescribeTableResult(source=source, columns=[], error=...)`.
- `table_name, database = ConnectionManager.resolve_table_reference(source)` (SF-8's fixed version) — not a separate inline reconstruction.
- Otherwise: resolve the connector, `connector.get_table(table_name, database=database)`, build `columns` as today.

**File:** `aitaem/agent/definition_types.py`
- `DescribeTableResult`: `source: str`, `columns: list[ColumnInfo] = []`, `error: str | None = None` — drops `table_name`/`backend_type`.

### SF-5 — Prompt: pass URIs straight through

**File:** `aitaem/agent/definition_bot.py`
- Layer A: `list_tables()` returns ready-to-use `source:` values — pass one directly to `describe_table(source=...)`, reuse the same string verbatim as `source:` in the drafted spec.

### SF-6 — `validate_spec`: block on `AitaemTableNotFoundError`

**File:** `aitaem/agent/definition_tools.py`
- In Check 5's column-existence try/except (`definition_tools.py:447-451`): catch `AitaemTableNotFoundError` separately, append to `column_errors` (field `"source"`, message: table not found or not accessible with the current credentials — not asserting the source is necessarily wrong) instead of `warnings`. All other exception types unchanged.

### SF-7 — Remove `TableOutOfScopeError`

**Files:** `aitaem/utils/exceptions.py`, `aitaem/utils/__init__.py`, `aitaem/__init__.py`
- Delete the class and its exports.

### SF-8 — `ConnectionManager.resolve_table_reference`: moved off `QueryBuilder`, structured return, fixes BigQuery project drop

**File:** `aitaem/connectors/connection.py`
- New public static method on `ConnectionManager`, alongside `parse_source_uri`: `resolve_table_reference(source_uri: str) -> tuple[str, str | None]` — `(table_name, database)`.
  - BigQuery: `(table, f"{project}.{dataset}")` — project/dataset carried as `database`, never joined into `table_name` (fixes Gap C).
  - Postgres: `(table, schema)` if `schema` else `(table, None)`.
  - DuckDB: `(table, None)`.
- Remove `QueryBuilder._parse_table_name_from_uri` (`aitaem/query/builder.py`) — no longer a `QueryBuilder`-owned method.

**File:** `aitaem/query/builder.py`
- Update the two internal call sites to `ConnectionManager.resolve_table_reference`, unpack the pair, pass `database=` to `get_table()`:
  - `aitaem/query/builder.py:250-251`
  - `aitaem/query/builder.py:260-261`

**File:** `aitaem/insights.py`
- Update `_run_scan`'s call site (`insights.py:35-36`) the same way.

### SF-9 — Tests

**Files:** `tests/test_connectors/test_ibis_connector.py`, `tests/test_connectors/test_connection_manager.py`, `tests/test_connectors/test_list_tables.py`, `tests/test_agent/test_definition_tools.py`, `tests/test_agent/test_definition_types.py`, `tests/test_agent/test_otel_spans.py`, `tests/test_agent/test_composition.py`, `tests/evals/_fixtures.py`, `tests/test_query/test_builder.py`
- `get_table()`: regression test for Gap B — a mocked Postgres connector's `.table()` call receives `table_name` and `database` as separate arguments, not a dotted string. A cross-project/cross-dataset BigQuery reference now succeeds (previously raised `TableOutOfScopeError`) — flip the two existing tests asserting that exception.
- `ConnectionManager.resolve_table_reference` (`TestURIParsing` in `test_connection_manager.py`): regression test for Gap C — a BigQuery `source:` naming a project returns `(table, "project.dataset")`, not `(table, "dataset")`.
- `ConnectionManager.resolve_table_reference` / `get_table()`: regression test for a Postgres table name containing a literal `.` (a quoted identifier, e.g. `source="postgres://public/my.weird.table"`) — the table name is passed to `get_table()` intact as `table_name="my.weird.table"`, `database="public"`, never split on the embedded dot.
- `tests/test_query/test_builder.py`: update the two `QueryBuilder` call sites that used to call `_parse_table_name_from_uri` directly to reflect the move to `ConnectionManager.resolve_table_reference`.
- `build_source_uri`: correct URI per backend (DuckDB, Postgres, BigQuery with/without default dataset); `None` for an ambiguous bare BigQuery name; `None` before connect.
- `list_tables()`: entries are full URIs per backend.
- `describe_table(source)`: correct resolution per backend, including a cross-project BigQuery source; parse-failure and unknown-table-name error paths.
- `DescribeTableResult`/`ListTablesResult`: updated field-shape tests.
- Update every mock/fixture across the listed files to the new `source`-based shape.
- `validate_spec`: `AitaemTableNotFoundError` during Check 5 populates `column_errors`, no token minted; every other exception type still degrades to a warning.
- Manual live re-verification against `.env.backends`' real BigQuery and Supabase connections after implementation, mirroring how all three gaps were found.

### SF-10 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Fixed`:
  - `list_tables`/`describe_table` now return/accept a ready-to-use `source:` URI instead of a bare table name; a source that doesn't resolve now fails `validate_spec` instead of committing with a warning. Breaking change: `describe_table`'s signature changes from `(table_name, backend_type)` to `(source)`; `DescribeTableResult` drops `table_name`/`backend_type` in favor of `source`; `ListTablesResult.tables`' entries are now full `source:` URIs. `TableOutOfScopeError` is removed — BigQuery `dataset_id`/`project_id` are resolution defaults only; scope is governed by the connection's own credentials.
  - `IbisConnector.get_table()` now resolves correctly against a real Postgres backend — previously failed for every schema, including `public`.
  - `compute()` now honors a BigQuery metric/segment source's own project instead of silently substituting the connection's default project.
- `docs/api/index.md`: remove the `TableOutOfScopeError` row.

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/connectors/ibis_connector.py` | SF-1: connect-time `database`/`current_schema()` capture, `build_source_uri()`; SF-2: `get_table(table_name, database=None)` two-parameter signature, `_resolve_bigquery_table_name` removed |
| `aitaem/connectors/README.md` | SF-2: `get_table()` examples updated to two-parameter form |
| `aitaem/agent/definition_tools.py` | SF-3: `list_tables()` returns URIs; SF-4: `describe_table(source)`; SF-6: `AitaemTableNotFoundError` blocks in Check 5 |
| `aitaem/agent/definition_types.py` | SF-4: `DescribeTableResult` field change |
| `aitaem/agent/definition_bot.py` | SF-5: prompt — pass URIs straight through |
| `aitaem/utils/exceptions.py` | SF-7: `TableOutOfScopeError` removed |
| `aitaem/utils/__init__.py` | SF-7: export removed |
| `aitaem/__init__.py` | SF-7: export removed |
| `aitaem/connectors/connection.py` | SF-8: new `ConnectionManager.resolve_table_reference()`, returning `tuple[str, str \| None]`; fixes the BigQuery project drop and the join-then-resplit ambiguity |
| `aitaem/query/builder.py` | SF-8: `_parse_table_name_from_uri` removed; its two internal call sites now use `ConnectionManager.resolve_table_reference()` |
| `aitaem/insights.py` | SF-8: `_run_scan()` call site updated to `ConnectionManager.resolve_table_reference()`, unpacking `(table_name, database)` and passing `database=` to `get_table()` |
| `tests/test_connectors/test_ibis_connector.py` | SF-9: `build_source_uri` tests; `get_table()` regression tests; cross-scope tests flipped to succeed |
| `tests/test_connectors/test_list_tables.py` | SF-9: updated for URI-shaped entries |
| `tests/test_agent/test_definition_tools.py` | SF-9: `describe_table(source)` tests; Check 5 blocking test |
| `tests/test_agent/test_definition_types.py` | SF-9: `DescribeTableResult`/`ListTablesResult` field tests |
| `tests/test_agent/test_otel_spans.py` | SF-9: mock/fixture updates for new signature |
| `tests/test_agent/test_composition.py` | SF-9: mock/fixture updates for new signature |
| `tests/evals/_fixtures.py` | SF-9: mock/fixture updates for new signature |
| `tests/test_connectors/test_connection_manager.py` | SF-9: `resolve_table_reference` tuple-return, BigQuery project, and quoted-identifier regression tests (`TestURIParsing`) |
| `tests/test_query/test_builder.py` | SF-9: `QueryBuilder` call sites updated to `ConnectionManager.resolve_table_reference()` |
| `docs/changelog.md` | SF-10: Unreleased entries for all three gaps |
| `docs/api/index.md` | SF-10: `TableOutOfScopeError` row removed |
