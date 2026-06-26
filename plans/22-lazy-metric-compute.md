# Plan 22 — Lazy Return: `MetricCompute.compute()` → `ibis.Table`

Changes `MetricCompute.compute()` to return a lazy `ibis.Table` instead of a
materialized `pd.DataFrame`, so callers with large result sets are not forced
to load all rows into process memory before inspecting the result.

---

## Scope

| Item | Description |
|------|-------------|
| EX-1 | `QueryExecutor._execute_query_group()` returns `ibis.Table` via ibis `.union()` |
| EX-2 | `QueryExecutor.execute()` returns `ibis.Table`; single-backend path stays lazy; cross-backend path materialises to pandas, reloads into in-memory DuckDB, and returns `ibis.Table` |
| FMT-1 | `ensure_standard_output()` accepts/returns `ibis.Table`; uses `.select()` |
| MC-1 | `MetricCompute.compute()` return type → `ibis.Table`; `output_format` parameter removed |
| TEST | All affected tests updated; new ibis-return-type tests added |
| DOCS | Docstrings, `docs/api/`, and `docs/changelog.md` updated |

Out of scope: Changing `Connector.execute()` abstract interface; adding new output
formats via `compute()`; any change to `QueryBuilder` or the spec layer.

---

## Background & Critical Observations

### Why ibis Table?

`QueryExecutor` currently materialises every ibis expression to a pandas DataFrame
inside `_execute_query_group()` by calling `connector.execute(ibis_expr)`.
When a result set contains hundreds of thousands of rows this forces a full
in-memory copy before the caller has had a chance to inspect, filter, or stream
the data. Returning an `ibis.Table` keeps the result as a deferred expression;
the caller calls `.to_pandas()` (or `.execute()`, `.to_polars()`, etc.) only if
and when they need to materialise.

### Current pipeline vs. new pipeline

```
# Current
compute()
  → build_queries() → [QueryGroup, ...]
  → executor.execute(query_groups)
      → _execute_query_group(group)
          → connector.connection.sql(sql)  → ibis.Table  (per SQL)
          → connector.execute(ibis_expr)   → pd.DataFrame  ← materialises HERE
          → pd.concat(dfs)                → pd.DataFrame
      → pd.concat(all_dfs)               → pd.DataFrame
  → ensure_standard_output(df)           → pd.DataFrame (reordered)
→ pd.DataFrame returned to caller

# New
compute()
  → build_queries() → [QueryGroup, ...]
  → executor.execute(query_groups)
      → _execute_query_group(group, connector)
          → connector.connection.sql(sql) → ibis.Table  (per SQL)
          → table1.union(table2)...       → ibis.Table  ← stays lazy
      → (single backend) table1.union(table2)...  → ibis.Table  ← lazy
        (multi-backend)  materialise each → pd.concat → DuckDB reload
                         → ibis.Table                 ← one materialisation
  → ensure_standard_output(table)  → table.select(STANDARD_COLUMNS)  ← lazy
→ ibis.Table returned to caller
```

### Single-backend vs. cross-backend union

Within one `QueryGroup` all SQL queries run against the same ibis backend
(`connector.connection` is a single `ibis.BaseBackend` instance). These can be
unioned lazily using `ibis.Table.union()`.

Across multiple `QueryGroup`s: if every group shares the same backend instance
(identified by `id(connector.connection)`), all group Tables can be unioned
lazily. If groups come from different backends (e.g., one DuckDB group and one
BigQuery group), ibis cannot union them natively. The fallback:
1. Materialise each group's `ibis.Table` to pandas
2. `pd.concat(dfs, ignore_index=True)`
3. Load the combined DataFrame into a fresh `ibis.duckdb.connect(":memory:")`
   table and return the resulting `ibis.Table`

The caller always receives an `ibis.Table`; the cross-backend materialisation is
an internal implementation detail. In practice, nearly all aitaem users have all
their metrics on one source (one DuckDB file) so this path will rarely execute.

When the cross-backend path does execute, the returned `ibis.Table` is backed by
a temporary DuckDB database managed by `MetricCompute` (a file in `tmp_dir`, or
`:memory:` when `tmp_dir=None`). Callers should be aware that this database is
not the same as any connection they configured via `ConnectionManager`, and it is
cleaned up when the `MetricCompute` instance is garbage collected. This is noted
in the `MetricCompute.__init__` and `compute()` docstrings (SF-4) so users are
not surprised if they inspect the Table's backend.

### Refactoring `_execute_query_group` signature

Currently `execute()` delegates to `_execute_query_group(group, output_format)`,
which looks up the connector itself. To support single-backend detection in
`execute()` without calling `get_connection_for_source()` twice, the connection
lookup will move entirely to `execute()` and the private helper will be renamed
to `_union_queries(queries, connector) -> ibis.Table`. This is an internal
refactor; the public `execute()` signature changes only in return type.

### `output_format` parameter removal

`MetricCompute.compute()` and `QueryExecutor.execute()` both currently accept
`output_format: str = "pandas"`. This parameter was threaded through to
`Connector.execute()`, which calls `expr.to_pandas()` or `expr.to_polars()`.
Since the executor no longer calls `Connector.execute()`, `output_format` has
no effect and is removed.

`Connector.execute()` and `IbisConnector.execute()` are **not changed** —
the abstract interface is left intact for callers who use connectors directly.

**Breaking change**: any caller passing `output_format="pandas"` to `compute()`
will get a `TypeError`. In practice this argument had no observable effect (only
`"pandas"` was ever meaningful and was the default), so the practical risk is low.

### `ensure_standard_output` signature change

The function is internal (not in `__all__`) so the signature change is not
breaking for library consumers.

Column presence check: `set(STANDARD_COLUMNS) - set(table.columns)` — ibis
`.columns` returns `list[str]`, same as `df.columns.tolist()`.

Reordering: `table.select(STANDARD_COLUMNS)` — lazy; drops any extra columns
beyond the standard set.

### Which tests need structural changes vs. `.to_pandas()` only

**Structural changes required (test logic or return-type assertions change):**
- `test_compute_returns_pandas_by_default` → rename to `test_compute_returns_ibis_table`
- `test_compute_single_metric_no_slices` → remove `isinstance(df, pd.DataFrame)` check
- All `test_executor.py` assertions on executor return values
- Column-order tests using `list(df.columns)` → can use `result.columns` on ibis Table directly

**Mechanical `.to_pandas()` addition only (no logic change):**
- Every other `mc.compute(...)` call where the result is used with pandas-style
  accessors (`.iloc`, `.isna()`, `.set_index()`, `.notna().all()`, `df["col"]`)
- `df_default.equals(df_explicit)` in `test_all_time_default_no_regression` — both
  sides get `.to_pandas()` first, then `.equals()`

No existing tests validate the *absence* of ibis Table behaviour, so no tests
need to be deleted — only updated.

**New tests to add** (in `tests/test_insights.py`):
- `test_compute_returns_ibis_table` — `isinstance(result, ibis.Table)`
- `test_compute_ibis_table_columns_match_standard` — `result.columns == STANDARD_COLUMNS`
- `test_compute_to_pandas_produces_dataframe` — `.to_pandas()` returns `pd.DataFrame`
  with the correct columns

---

## Implementation Sub-Features

Implement in this order — each SF is independently testable before moving on.

---

### SF-1: Refactor `QueryExecutor` internals — return `ibis.Table` per group

**Files changed:**
- `aitaem/query/executor.py`

**Changes:**

1. Add `import ibis` at the top; keep `import pandas as pd` (still needed for
   the cross-backend fallback in SF-2).

2. Rename `_execute_query_group(group, output_format)` →
   `_union_queries(sql_queries, connector) -> ibis.Table | None`.

   ```python
   def _union_queries(
       self,
       sql_queries: list[str],
       connector: Connector,
   ) -> ibis.Table | None:
       assert connector.connection is not None
       tables: list[ibis.Table] = []
       for sql in sql_queries:
           tables.append(connector.connection.sql(sql))
       if not tables:
           return None
       result = tables[0]
       for t in tables[1:]:
           result = result.union(t)
       return result
   ```

3. Remove the `output_format` parameter from the private helper entirely.

**Edge cases:**
- `ibis.Table.union()` requires matching schemas. All SQL queries in one
  `QueryGroup` produce the same output schema (period_type, metric_name,
  metric_value, etc.), so this is always safe.
- `tables[0]` is safe because `QueryGroup.sql_queries` is never empty
  (verified by `QueryBuilder`). The `if not tables` guard is a defensive
  fallback only.

**Validation:**
- Add `TestUnionQueries` class in `tests/test_query/test_executor.py`:
  - `test_union_queries_returns_ibis_table` — single SQL query → `ibis.Table`
  - `test_union_queries_multiple_sqls_returns_ibis_table` — two queries → `ibis.Table`
  - `test_union_queries_result_materialises_correctly` — `.to_pandas()` has
    expected columns and row count

---

### SF-2: Update `QueryExecutor.execute()` to return `ibis.Table`

**Files changed:**
- `aitaem/query/executor.py`

**Changes:**

```python
def execute(
    self,
    query_groups: list[QueryGroup],
    cross_backend_conn: ibis.BaseBackend | None = None,
) -> ibis.Table:
    """Execute all query groups and combine results as a lazy ibis.Table.

    ...
    """
    tables: list[ibis.Table] = []
    backends: list[ibis.BaseBackend] = []

    for group in query_groups:
        try:
            connector = self.connection_manager.get_connection_for_source(group.source)
        except (ConnectionNotFoundError, RuntimeError) as e:
            logger.warning("Skipping query group for source '%s': %s", group.source, e)
            continue

        table = self._union_queries(group.sql_queries, connector)
        if table is not None:
            tables.append(table)
            assert connector.connection is not None
            backends.append(connector.connection)

    if not tables:
        raise QueryExecutionError(
            "All query groups failed to produce results. "
            "Check connection configuration and query specs."
        )

    if len(tables) == 1:
        return tables[0]

    # Multiple groups — check if they share the same backend instance
    if len(set(id(b) for b in backends)) == 1:
        # Same backend: lazy union
        combined = tables[0]
        for t in tables[1:]:
            combined = combined.union(t)
        return combined
    else:
        # Cross-backend fallback: materialise to pandas, reload into DuckDB.
        # cross_backend_conn is owned by MetricCompute and passed in by caller.
        if cross_backend_conn is None:
            raise QueryExecutionError(
                "Cross-backend query requires a cross_backend_conn argument."
            )
        dfs = [t.to_pandas() for t in tables]
        df = pd.concat(dfs, ignore_index=True)
        table_name = f"__combined_{uuid.uuid4().hex[:8]}__"
        return cross_backend_conn.create_table(table_name, obj=df)
```

**Edge cases:**
- `output_format` parameter is removed — no default needed.
- The cross-backend path uses a persistent in-memory DuckDB connection owned
  by `MetricCompute` (see SF-4), not a fresh one per `execute()` call. Each
  result is stored as a uniquely-named table (UUID suffix) so multiple results
  from the same `MetricCompute` instance coexist without conflict. This
  eliminates any ambiguity about connection lifetime: the backend lives exactly
  as long as the `MetricCompute` instance, matching the user's natural notion
  of a session.
- `QueryExecutor` itself does not own or create the cross-backend connection;
  `MetricCompute` passes it in (see SF-4 for the interface change).
- When `len(tables) == 1` we return directly, skipping even a no-op
  `union()` call, to keep the expression maximally simple.

**Validation:**

Update `tests/test_query/test_executor.py`:

| Test | Change |
|------|--------|
| `TestExecuteQueryGroup` class | Remove class; its coverage is now in `TestUnionQueries` (SF-1) |
| `TestExecute.test_full_integration_no_slice_no_segment` | Assert `ibis.Table` return; add `df = result.to_pandas()` before DataFrame assertions |
| `TestExecute.test_full_integration_with_slices_and_segment` | Same pattern |
| `TestExecute.test_raises_when_all_groups_fail` | No change needed (exception is still raised) |
| `TestExecute.test_partial_failure_returns_partial_result` | Assert `ibis.Table`; add `.to_pandas()` before len/column checks |
| `TestExecute.test_multiple_metrics_combined` | Add `.to_pandas()` |
| `TestExecute.test_time_window_filters_data` | Add `.to_pandas()` |
| `TestEndToEndIntegration.*` | Add `.to_pandas()` before DataFrame assertions |

Add one new test:
- `test_execute_returns_ibis_table` — confirms `isinstance(executor.execute(groups), ibis.Table)`

---

### SF-3: Update `ensure_standard_output()` to accept `ibis.Table`

**Files changed:**
- `aitaem/utils/formatting.py`

**Changes:**

```python
import ibis

STANDARD_COLUMNS: list[str] = [...]  # unchanged


def ensure_standard_output(table: ibis.Table) -> ibis.Table:
    """Select and reorder columns to match the standard output schema.

    Raises:
        ValueError: if any required column is missing from the Table.
    """
    missing = set(STANDARD_COLUMNS) - set(table.columns)
    if missing:
        raise ValueError(f"Table missing expected columns: {missing}")
    return table.select(STANDARD_COLUMNS)
```

- Remove `import pandas as pd`.
- `table.select(STANDARD_COLUMNS)` preserves column order exactly, drops any
  extra columns, and is itself lazy.

**Edge cases:**
- `ibis.Table.columns` returns `list[str]` in the order the columns appear in
  the schema. `set(table.columns)` for membership checks works correctly.
- If any column in `STANDARD_COLUMNS` is missing, `ibis` would itself raise an
  opaque error on `.select()`; the explicit `missing` check before the select
  provides a clear error message.

**Validation:**
- No dedicated unit tests for `ensure_standard_output` exist; it is covered
  indirectly by every integration test that checks `result.columns == STANDARD_COLUMNS`.
- After SF-4, integration tests pass, confirming this change is correct.

---

### SF-4: Update `MetricCompute.compute()` return type

**Files changed:**
- `aitaem/insights.py`

**Changes:**

1. Remove `import pandas as pd`.
2. Add `import ibis`, `import uuid`, `import tempfile`, `import os`.
3. Remove `output_format` parameter.
4. Change return type annotation: `-> pd.DataFrame` → `-> ibis.Table`.
5. Add `tmp_dir` parameter to `__init__` and supporting private state:

   ```python
   def __init__(
       self,
       spec_cache: SpecCache,
       connection_manager: ConnectionManager,
       tmp_dir: str | None = "/tmp",
   ) -> None:
       """
       Args:
           spec_cache: Loaded and validated metric, slice, and segment specs.
           connection_manager: Backend connections for query execution.
           tmp_dir: Directory for the temporary DuckDB file used when a compute()
               call spans multiple source backends. Defaults to '/tmp', which
               prevents large cross-backend result sets from bloating process
               memory. Set to None to force an in-memory DuckDB instead (safe
               when result sets are known to be small). The file is deleted
               automatically when this MetricCompute instance is garbage
               collected; the OS reclaims it on reboot as a final backstop.
       """
       self.spec_cache = spec_cache
       self.connection_manager = connection_manager
       self._tmp_dir = tmp_dir
       self._cross_backend_conn: ibis.BaseBackend | None = None
       self._cross_backend_db_path: str | None = None
   ```

   Lazily initialise the cross-backend connection on first use:

   ```python
   def _get_cross_backend_conn(self) -> ibis.BaseBackend:
       if self._cross_backend_conn is None:
           if self._tmp_dir is not None:
               fd, path = tempfile.mkstemp(suffix=".duckdb", dir=self._tmp_dir)
               os.close(fd)
               self._cross_backend_db_path = path
               self._cross_backend_conn = ibis.duckdb.connect(path)
           else:
               self._cross_backend_conn = ibis.duckdb.connect(":memory:")
       return self._cross_backend_conn
   ```

   Add `__del__` for temp file cleanup:

   ```python
   def __del__(self) -> None:
       if self._cross_backend_conn is not None:
           self._cross_backend_conn = None  # release the connection
       if self._cross_backend_db_path is not None:
           try:
               os.unlink(self._cross_backend_db_path)
           except OSError:
               pass
   ```

   Pass the connection into `QueryExecutor.execute()` so the executor does not
   own or create any cross-backend connections itself.

6. Update the `executor.execute()` call — remove `output_format` argument; add
   `cross_backend_conn=self._get_cross_backend_conn()` argument.

7. Update docstring "Returns" section (capture this wording exactly):

   ```
   Returns:
       Lazy ibis.Table with columns: period_type, period_start_date,
       period_end_date, entity_id, metric_name, metric_format, slice_type,
       slice_value, segment_name, segment_value, metric_value.
       Call .to_pandas() to materialise.

       When all metrics share the same source backend the returned Table is a
       deferred expression on that backend and no data is transferred until
       .to_pandas() (or any other materialising call) is invoked.

       When metrics span multiple source backends the results are materialised
       internally and re-exposed as a Table backed by a temporary DuckDB
       database (file in tmp_dir, or in-memory when tmp_dir=None). This
       database is not accessible via ConnectionManager and is cleaned up
       when this MetricCompute instance is garbage collected.
   ```

```python
def compute(
    self,
    metrics: str | list[str],
    slices: str | list[str] | None = None,
    segments: dict[str, str] | str | None = None,
    time_window: tuple[str, str] | None = None,
    period_type: PeriodType = "all_time",
    by_entity: str | None = None,
) -> ibis.Table:
```

**Breaking changes:**
- `output_format` parameter removed. Any caller who explicitly passed
  `output_format="pandas"` gets a `TypeError`. Since `"pandas"` was the
  default and the only supported value, no caller would observe different
  *results* by removing it — only callers who pass the argument by name
  will break.
- Return type is now `ibis.Table`, not `pd.DataFrame`. Callers that
  immediately assigned the result to a variable typed as `pd.DataFrame`
  or called pandas-only methods without `.to_pandas()` will fail. This is
  the intentional breaking change this plan introduces.

**Validation:**
- Run `tests/test_insights*.py` after SF-4 (before SF-5); tests will fail
  because they expect pandas — confirms the change is in effect before the
  test updates begin.

---

### SF-5: Update all tests

**Files changed:**
- `tests/test_insights.py`
- `tests/test_insights_period_granularity.py`
- `tests/test_insights_by_entity.py`
- `tests/test_insights_metric_format.py`
- `tests/test_query/test_executor.py` *(covered partially in SF-1 and SF-2)*

**Standard migration pattern:**

```python
# Before
df = mc.compute("ctr")
assert isinstance(df, pd.DataFrame)
assert list(df.columns) == STANDARD_COLUMNS
assert len(df) == 1
assert df["metric_name"].iloc[0] == "ctr"

# After
result = mc.compute("ctr")
assert isinstance(result, ibis.Table)
assert result.columns == STANDARD_COLUMNS
df = result.to_pandas()
assert len(df) == 1
assert df["metric_name"].iloc[0] == "ctr"
```

Column-ordering checks do not require `.to_pandas()` — `ibis.Table.columns`
returns `list[str]`, so `result.columns == STANDARD_COLUMNS` works directly.

**Detailed change table for `tests/test_insights.py`:**

| Test | Change |
|------|--------|
| `test_compute_single_metric_no_slices` | `isinstance` check → ibis; add `df = result.to_pandas()` |
| `test_compute_single_metric_with_slice` | Add `.to_pandas()` |
| `test_compute_single_metric_with_segment` | Add `.to_pandas()` |
| `test_compute_with_time_window` | Add `.to_pandas()` to both `compute()` calls |
| `test_compute_multiple_metrics` | Add `.to_pandas()` |
| `test_compute_multiple_slices` | Add `.to_pandas()` |
| `test_compute_metric_not_found` | No change (exception path, no result used) |
| `test_compute_slice_not_found` | No change |
| `test_output_column_order` | `list(df.columns)` → `result.columns` (no `.to_pandas()` needed) |
| `test_compute_returns_pandas_by_default` | Rename → `test_compute_returns_ibis_table`; assert `ibis.Table` |

**New tests to add in `tests/test_insights.py`:**

```python
import ibis

def test_compute_returns_ibis_table(mc):
    result = mc.compute("ctr")
    assert isinstance(result, ibis.Table)


def test_compute_ibis_table_columns_match_standard(mc):
    result = mc.compute("ctr")
    assert result.columns == STANDARD_COLUMNS


def test_compute_to_pandas_produces_dataframe(mc):
    result = mc.compute("ctr")
    df = result.to_pandas()
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == STANDARD_COLUMNS
```

**`tests/test_insights_period_granularity.py`:**
- All `mc.compute(...)` calls: assign result, then call `.to_pandas()` before
  pandas-style assertions.
- `test_all_time_default_no_regression`: both `df_default` and `df_explicit`
  get `.to_pandas()` before `.equals()`.
- Column-order tests (`assert list(df.columns) == STANDARD_COLUMNS`):
  change to `assert result.columns == STANDARD_COLUMNS`.

**`tests/test_insights_by_entity.py`:**
- All `mc.compute(...)` calls: add `.to_pandas()` before pandas assertions.
- `test_default_no_by_entity_column_order` and `test_by_entity_user_id_column_order`:
  change to `result.columns == STANDARD_COLUMNS`.

**`tests/test_insights_metric_format.py`:**
- All `mc_format.compute(...)` calls: add `.to_pandas()` before pandas assertions.
- Column-order tests: use `result.columns` directly.

**`tests/test_query/test_executor.py`:**
- Updates for `TestUnionQueries` (new class, SF-1) and `TestExecute` (SF-2)
  are already described in those sub-features above.

---

### SF-6: Documentation updates

**Files changed:**
- `docs/api/index.md`
- `docs/changelog.md`

**`docs/api/index.md`:**
- In the `MetricCompute` section, update the `compute()` return type from
  `pd.DataFrame` to `ibis.Table`.
- Add a note that callers call `.to_pandas()` to materialise.
- Note removal of the `output_format` parameter (breaking change).

**`docs/changelog.md`:**
- Add an entry under `## Unreleased`:
  ```
  ### Breaking changes
  - `MetricCompute.compute()` now returns a lazy `ibis.Table` instead of
    `pd.DataFrame`. Call `.to_pandas()` on the result to get the previous
    behaviour.
  - `MetricCompute.compute()` `output_format` parameter removed.
  - `QueryExecutor.execute()` `output_format` parameter removed.
  ```

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/query/executor.py` | `_execute_query_group` → `_union_queries(sql_queries, connector)` returning `ibis.Table`; `execute()` returns `ibis.Table`, removes `output_format`, handles single/multi-backend |
| `aitaem/utils/formatting.py` | `ensure_standard_output` accepts/returns `ibis.Table`; uses `.select()`; removes `pd` import |
| `aitaem/insights.py` | `compute()` returns `ibis.Table`; `output_format` removed; `pd` import replaced with `ibis` |
| `tests/test_insights.py` | `.to_pandas()` added; `test_compute_returns_ibis_table` updated; 3 new tests |
| `tests/test_insights_period_granularity.py` | `.to_pandas()` added throughout; column-order checks updated |
| `tests/test_insights_by_entity.py` | `.to_pandas()` added throughout; column-order checks updated |
| `tests/test_insights_metric_format.py` | `.to_pandas()` added throughout; column-order checks updated |
| `tests/test_query/test_executor.py` | `TestExecuteQueryGroup` replaced by `TestUnionQueries`; `TestExecute` assertions updated; 1 new test |
| `docs/api/index.md` | `compute()` return type and parameter notes updated |
| `docs/changelog.md` | Breaking change entry under `## Unreleased` |

---

## Testing Strategy

1. Run `python -m pytest` before starting to confirm a green baseline.
2. After SF-1: run `python -m pytest tests/test_query/test_executor.py` — new
   `TestUnionQueries` tests should pass; `TestExecute` still passes because
   `execute()` has not changed yet.
3. After SF-2: run `python -m pytest tests/test_query/test_executor.py` — all
   executor tests pass with ibis Table assertions.
4. After SF-3: no isolated test run needed; `ensure_standard_output` is
   validated end-to-end in SF-5.
5. After SF-4: run `python -m pytest tests/test_insights*.py` — tests will
   **fail** (confirms the API change is live before test updates).
6. After SF-5: run `python -m pytest --cov=aitaem --cov-report=term-missing`
   — full suite passes; confirm no coverage regression.
7. Commit after all SFs pass.

---

## Appendix: Remove abstract `Connector` base class

Discovered during implementation: `aitaem/connectors/base.py` defines an
abstract `Connector(ABC)` class that was designed for a world where each
warehouse would have its own concrete subclass. That world never arrived —
`IbisConnector` wraps ibis and handles DuckDB, BigQuery, and Postgres through
ibis's own backend dispatch. There is exactly one concrete subclass, and there
will never be a second. The abstract class currently causes friction: it does
not declare `connection: ibis.BaseBackend | None`, so any code that needs to
call `.sql()` on the underlying backend gets mypy errors and has to work around
the gap.

### Changes required

**Delete**

- `aitaem/connectors/base.py` — the entire file

**Update `aitaem/connectors/ibis_connector.py`**

- Remove `from aitaem.connectors.base import Connector`
- Remove `(Connector)` from the class declaration; `IbisConnector` becomes a
  plain class (no ABC inheritance)

**Update `aitaem/connectors/__init__.py`**

- Remove `from aitaem.connectors.base import Connector`
- Remove `"Connector"` from `__all__`

**Update `aitaem/query/executor.py`**

- Already imports `IbisConnector` directly (done in this plan's mypy fix); no
  further change needed

**No changes required**

- `aitaem/connectors/connection.py` — already typed against `IbisConnector`
  throughout; no reference to `Connector`
- `aitaem/__init__.py` — `Connector` is not re-exported at the top level
- All test files — no test references `Connector` directly or asserts
  `IbisConnector` is a subclass of it

### Impact on public API

`Connector` is exported from `aitaem.connectors` (via `__init__.py`) but is
**not** exported from the top-level `aitaem` package. Users who import it
directly (`from aitaem.connectors import Connector`) would break. Given that
`Connector` has no user-facing methods beyond what `IbisConnector` exposes, the
practical user population is expected to be zero, but this should be noted as a
breaking change in `docs/changelog.md`.

### Checklist

- [ ] Delete `aitaem/connectors/base.py`
- [ ] Remove `(Connector)` from `IbisConnector` class declaration; remove its import
- [ ] Remove `Connector` from `aitaem/connectors/__init__.py` and `__all__`
- [ ] Add breaking-change entry to `docs/changelog.md` under `## Unreleased`
- [ ] Run `python -m mypy aitaem/` — should pass with no errors
- [ ] Run `python -m pytest` — full suite passes
- [ ] Commit
