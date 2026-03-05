# Query Module Plan

## Overview

The query module translates metric/slice/segment specs into executable queries and
returns results in the standard output format. It sits between the specs layer (pure
data objects) and the connectors layer (backend connections).

```
MetricSpec + SliceSpec + SegmentSpec
         ↓
    QueryBuilder        (static, no DB connection, builds SQL strings)
         ↓
    QueryGroup          (source URI + list of SQL strings)
         ↓
    QueryExecutor       (has DB connections, executes each SQL, pd.concat results)
         ↓
    Standard output DataFrame
```

NOTE (Breaking change): Each SliceSpec in the input list is now processed independently.
To get a cross-product of multiple slices, use a composite SliceSpec (cross_product field).

---

## Design Decisions

### SQL-first query building
QueryBuilder builds raw SQL strings from specs, rather than Ibis Python API expressions.
The executor wraps each SQL string with `connector.connection.sql(raw_sql)` to get a lazy
Ibis expression, then calls `.to_pandas()` / `.to_polars()` to execute.

**Why**: Specs use SQL syntax directly (numerator/denominator are SQL aggregation expressions,
WHERE clauses are SQL predicates). Translating complex `SUM(CASE WHEN ...)` into Ibis API
calls is significantly more complex than passing the SQL through. Backend portability is still
maintained via Ibis — `connection.sql()` is supported across DuckDB, ClickHouse, BigQuery, etc.

**SQL dialect**: Phase 1 uses DuckDB SQL syntax throughout (consistent with spec validation
in sqlglot). Phase 2 can add dialect transpilation via sqlglot
(`sqlglot.transpile(sql, read='duckdb', write=backend_dialect)`) in the executor.

### QueryBuilder is purely static
Builder takes specs + parameters, returns QueryGroup objects with SQL strings. No database
connection required — all work is string manipulation and iteration.

### CASE WHEN + GROUP BY for slices and segments

Each element in `slice_specs` is treated independently — one SQL query is generated per slice per segment combination, plus one no-slice/all baseline. A composite `SliceSpec` (with `cross_product`) produces one query that internally has multiple CASE WHEN columns and GROUP BY on all of them, giving the cross-product within that single spec.

**One SQL query per (slice_spec | None) × (segment_spec | None)**:
- A leaf `SliceSpec` → one CASE WHEN column in the CTE, GROUP BY that column
- A composite `SliceSpec` → one CASE WHEN column per referenced component spec, GROUP BY all of them → cross-product scoped to this spec
- `None` slot for slices → `slice_type='none'`, `slice_value='all'` (no-slice baseline)
- `None` slot for segments → `segment_name='none'`, `segment_value='all'` (no-segment baseline, already existing)

**Query count formula**: `(n_slice_specs + 1) × (n_segment_specs + 1)` per metric.
- A composite spec counts as 1 in this formula regardless of how many component specs it references.

**(Breaking change from previous design)**: Previously, all slice specs were combined into a single query (cross-product of all). Now each slice spec is independent. Users who want cross-product behavior must define a composite `SliceSpec`.

### Composite SliceSpec resolution via SpecCache

When `_build_queries_for_metric()` encounters a composite `SliceSpec` (where `spec.is_composite` is True), it resolves the component specs by calling `SpecCache.get_global().get_slice(name)` for each name in `spec.cross_product`. The resolved component specs are then passed together to `_build_metric_segment_query()` as its `slice_specs` list — the existing cross-product SQL generation logic handles the rest unchanged.

`SpecCache.get_global()` must be set before calling `QueryBuilder.build_queries()` with composite slice specs. If not set, a `RuntimeError` propagates. This is consistent with `ConnectionManager.get_global()` being required before `QueryExecutor.execute()`.

**Slice `slice_type` for composite specs**: Uses the component specs' names joined with `|` (e.g., `"industry|geo"`), not the composite spec's own name. This is consistent with how multiple leaf slices were previously formatted.

### Python-side concat instead of UNION ALL over segment specs
Each segment spec (plus the "no segment" baseline) produces a separate SQL query. The
executor runs them sequentially against one connector per source, then `pd.concat`s the
results. This eliminates UNION ALL from the generated SQL entirely and makes future
parallelization trivial.

**QueryGroup holds a list**: `sql_queries: list[str]`, one entry per
(metric_group × segment_spec | None) combination for that source.

### Segment source handling (Phase 1 simplification)
SegmentSpec has a `source` field that may differ from the metric's source. In Phase 1,
the segment CASE WHEN is applied directly on the **metric's source table**, not the
segment's source. This assumes the relevant columns exist in the metric table.

**Phase 2 note**: Proper cross-table segment filtering requires a JOIN between metric source
and segment source. This needs an `entity_col` field in SegmentSpec to identify the join key.

### Multiple segments are independent (no cross-product)
Given `segments=['user_tier', 'login_status']`, each segment spec produces its own query.
Results are:
```
segment_name=user_tier,    segment_value=premium
segment_name=user_tier,    segment_value=free
segment_name=login_status, segment_value=logged_in
segment_name=login_status, segment_value=visitor
```
Segments are not cross-producted with each other. Multiple slices ARE cross-producted (via
GROUP BY on their respective CASE WHEN columns).

### Time window via `timestamp_col` parameter
`timestamp_col` is passed as an optional parameter to `build_queries()` and ultimately
to `compute()`. If `time_window` is provided but `timestamp_col` is None, a `QueryBuildError`
is raised with a clear message. The time filter is applied inside the CTE (innermost WHERE),
so the aggregate sees only the filtered rows.

**Phase 1 time window behavior**: When time_window is provided:
- `period_type = 'all_time'`
- `period_start_date = time_window[0]`
- `period_end_date = time_window[1]`
- CTE WHERE clause: `{timestamp_col} >= '{start}' AND {timestamp_col} < '{end}'`

When no time_window: `period_type='all_time'`, `period_start_date=None`, `period_end_date=None`.
Per-period granularity (daily/weekly/monthly) is Phase 2.

### "All" sentinel values

- No slices requested OR the no-slice baseline row: `slice_type='none'`, `slice_value='all'`
- No segments requested OR the no-segment baseline row: `segment_name='none'`, `segment_value='all'`

Both the no-slice and no-segment baselines are generated automatically for every metric, regardless of whether slices/segments are provided. This mirrors the segment baseline that was already implemented and extends it symmetrically to slices.

---

## Files to Create

```
aitaem/query/
├── __init__.py          # Exports QueryBuilder, QueryExecutor, QueryGroup
├── builder.py           # QueryBuilder class + QueryGroup dataclass
└── executor.py          # QueryExecutor class
```

`optimizer.py` from the architecture is NOT created — optimization (grouping by source)
is handled inside `builder.py` as a private method.

---

## Standard Output Schema (reminder)

| Column              | Type      | Notes                                        |
|---------------------|-----------|----------------------------------------------|
| `period_type`       | str       | 'all_time' (Phase 1)                        |
| `period_start_date` | str\|None | start of time_window, or None               |
| `period_end_date`   | str\|None | end of time_window, or None                 |
| `metric_name`       | str       | MetricSpec.name                             |
| `slice_type`        | str       | pipe-delimited slice spec names, or 'none'  |
| `slice_value`       | str       | pipe-delimited slice value names, or 'all'  |
| `segment_name`      | str       | SegmentSpec.name, or 'none'                 |
| `segment_value`     | str       | SegmentValue.name, or 'all'                 |
| `metric_value`      | float     | computed metric value                        |

---

## `query/builder.py` — QueryBuilder

### `QueryGroup` dataclass

```python
@dataclass
class QueryGroup:
    source: str                # Source URI, e.g. 'duckdb://analytics.db/events'
    metrics: list[MetricSpec]
    sql_queries: list[str]     # One SQL string per (metric × segment_spec | None)
```

Changed from the architecture's `query_expr: ibis.Expr` → `sql_queries: list[str]`.
The Ibis expression is created at execution time in the executor.

### Class: `QueryBuilder` (all static methods)

```python
class QueryBuilder:

    @staticmethod
    def build_queries(
        metric_specs: list[MetricSpec],
        slice_specs: list[SliceSpec] | None,
        segment_specs: list[SegmentSpec] | None,
        time_window: tuple[str, str] | None = None,
        timestamp_col: str | None = None,
    ) -> list[QueryGroup]:
        """
        Build optimized query groups (one per unique source table).
        Each QueryGroup contains all SQL queries for that source:
        one per (metric × (each segment_spec + the no-segment baseline)).

        Raises:
            QueryBuildError: if time_window provided but timestamp_col is None
            QueryBuildError: if metric_specs is empty
        """

    @staticmethod
    def _group_by_source(
        metric_specs: list[MetricSpec],
    ) -> dict[str, list[MetricSpec]]:
        """Group metric specs by source URI."""

    @staticmethod
    def _build_queries_for_metric(
        metric: MetricSpec,
        slice_specs: list[SliceSpec] | None,
        segment_specs: list[SegmentSpec] | None,
        time_filter_sql: str | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
    ) -> list[str]:
        """
        Build all SQL queries for one metric.
        Processes each SliceSpec independently (+ a no-slice/all baseline).
        For each (slice_spec | None) × (segment_spec | None) combination, one SQL query is generated.

        For composite SliceSpecs, resolves component specs via SpecCache.get_global().

        len(result) == (len(slice_specs) + 1) × (len(segment_specs) + 1)
                       (or 1 if both are None/empty)
        """

    @staticmethod
    def _resolve_slice_components(slice_spec: SliceSpec) -> list[SliceSpec]:
        """
        For a leaf SliceSpec, return [slice_spec].
        For a composite SliceSpec, fetch each referenced SliceSpec from SpecCache.get_global()
        and return the list of component specs.

        Raises:
            RuntimeError: if composite spec is used and SpecCache.get_global() is not set
            SpecNotFoundError: if a referenced slice name is not in the cache
        """

    @staticmethod
    def _build_metric_segment_query(
        metric: MetricSpec,
        table_name: str,
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        time_filter_sql: str | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
    ) -> str:
        """
        Build a single SQL query for one (metric, segment_spec | None) combination.
        All slice specs are encoded as CASE WHEN columns in a CTE.
        GROUP BY those columns produces the slice cross-product.

        Structure:
            WITH _labeled AS (
                SELECT
                    *,
                    CASE WHEN {sv.where} THEN '{sv.name}' ... ELSE NULL END AS _slice_{name},
                    [... one CASE WHEN per slice spec ...]
                    CASE WHEN {sv.where} THEN '{sv.name}' ... ELSE NULL END AS _segment
                FROM {table_name}
                WHERE {time_filter_sql}         -- only time filter here
            )
            SELECT
                '{period_type}'                  AS period_type,
                '{period_start}'                 AS period_start_date,
                '{period_end}'                   AS period_end_date,
                '{metric_name}'                  AS metric_name,
                '{slice_type}'                   AS slice_type,
                {slice_value_concat_expr}        AS slice_value,
                '{segment_name}'                 AS segment_name,
                {segment_value_expr}             AS segment_value,
                {metric_value_expr}              AS metric_value
            FROM _labeled
            WHERE {null_filter_conditions}      -- IS NOT NULL for each labeled column
            GROUP BY {group_by_cols}            -- labeled slice/segment columns
        """

    @staticmethod
    def _build_slice_case_when_expr(
        slice_spec: SliceSpec,
        alias: str,
    ) -> str:
        """
        Build a CASE WHEN expression for a single SliceSpec.

        Returns (to be placed in the CTE SELECT):
            CASE
                WHEN {values[0].where} THEN '{values[0].name}'
                WHEN {values[1].where} THEN '{values[1].name}'
                ...
                ELSE NULL
            END AS {alias}
        """

    @staticmethod
    def _build_segment_case_when_expr(
        segment_spec: SegmentSpec,
        alias: str,
    ) -> str:
        """
        Build a CASE WHEN expression for a single SegmentSpec.
        Identical structure to _build_slice_case_when_expr.

        Returns (to be placed in the CTE SELECT):
            CASE
                WHEN {values[0].where} THEN '{values[0].name}'
                ...
                ELSE NULL
            END AS {alias}
        """

    @staticmethod
    def _build_slice_value_concat_expr(slice_aliases: list[str]) -> str:
        """
        Build the slice_value column expression by concatenating slice aliases
        with '|' separator.

        Examples:
            ['_geo']                    → '_geo'
            ['_geo', '_device']         → "_geo || '|' || _device"
            ['_geo', '_device', '_os']  → "_geo || '|' || _device || '|' || _os"
        """

    @staticmethod
    def _build_metric_value_expr(metric: MetricSpec) -> str:
        """
        Build the metric value SQL expression.

        - ratio:   {numerator} / NULLIF({denominator}, 0)
        - others:  {numerator}  (already a complete aggregation expression)

        Examples:
            ratio:  "SUM(clicks) / NULLIF(SUM(impressions), 0)"
            sum:    "SUM(amount)"
        """

    @staticmethod
    def _build_time_filter_sql(
        time_window: tuple[str, str],
        timestamp_col: str,
    ) -> str:
        """
        Build time window filter condition for the CTE WHERE clause.

        Returns:
            "{timestamp_col} >= '{start}' AND {timestamp_col} < '{end}'"
        """

    @staticmethod
    def _parse_table_name_from_uri(source_uri: str) -> str:
        """
        Extract the table name from a source URI for use in SQL FROM clause.
        Uses ConnectionManager.parse_source_uri() internally.

        Examples:
            'duckdb://analytics.db/events'       → 'events'
            'clickhouse://host/database/orders'  → 'database.orders'
            'bigquery://project/dataset/table'   → 'dataset.table'
        """
```

---

### SQL generation example

**Example with independent slices (new default behavior)**

Given:
- metric: `revenue` (SUM(amount), from `duckdb://analytics.db/transactions`)
- slices: `geography=[North America, Europe]` (leaf), `device=[mobile, desktop]` (leaf)
- segments: `user_tier=[premium, free]`
- time_window: `('2026-01-01', '2026-02-01')`, timestamp_col: `event_ts`

**Number of SQL queries generated**: 1 metric × (2 slice specs + 1 no-slice) × (1 segment spec + 1 no-segment) = **6 queries**

| Query | Slice | Segment |
|---|---|---|
| 1 | geography | user_tier |
| 2 | geography | none/all |
| 3 | device | user_tier |
| 4 | device | none/all |
| 5 | none/all | user_tier |
| 6 | none/all | none/all |

**Example with composite slice**

If instead `geography` and `device` are combined into a composite slice `geo_device` (`cross_product: [geography, device]`):
- slices: `geo_device` (composite, references geography + device)

**Number of SQL queries generated**: 1 metric × (1 slice spec + 1 no-slice) × (1 segment spec + 1 no-segment) = **4 queries**

---

**Query 1 of 4: slice=geo_device, segment=user_tier**
```sql
WITH _labeled AS (
    SELECT
        *,
        CASE
            WHEN country_code IN ('US', 'CA', 'MX') THEN 'North America'
            WHEN country_code IN ('DE', 'FR', 'UK') THEN 'Europe'
            ELSE NULL
        END AS _slice_geography,
        CASE
            WHEN device_type = 'mobile'  THEN 'mobile'
            WHEN device_type = 'desktop' THEN 'desktop'
            ELSE NULL
        END AS _slice_device,
        CASE
            WHEN subscription_tier = 'premium' THEN 'premium'
            WHEN subscription_tier = 'free'    THEN 'free'
            ELSE NULL
        END AS _segment
    FROM transactions
    WHERE event_ts >= '2026-01-01' AND event_ts < '2026-02-01'
)
SELECT
    'all_time'                              AS period_type,
    '2026-01-01'                            AS period_start_date,
    '2026-02-01'                            AS period_end_date,
    'revenue'                               AS metric_name,
    'geography|device'                      AS slice_type,
    _slice_geography || '|' || _slice_device AS slice_value,
    'user_tier'                             AS segment_name,
    _segment                                AS segment_value,
    SUM(amount)                             AS metric_value
FROM _labeled
WHERE _slice_geography IS NOT NULL
  AND _slice_device IS NOT NULL
  AND _segment IS NOT NULL
GROUP BY _slice_geography, _slice_device, _segment
```

---

**Query 4 of 4 (or any baseline): no slice, no segment**
```sql
WITH _labeled AS (
    SELECT *
    FROM transactions
    WHERE event_ts >= '2026-01-01' AND event_ts < '2026-02-01'
)
SELECT
    'all_time'   AS period_type,
    '2026-01-01' AS period_start_date,
    '2026-02-01' AS period_end_date,
    'revenue'    AS metric_name,
    'none'       AS slice_type,
    'all'        AS slice_value,
    'none'       AS segment_name,
    'all'        AS segment_value,
    SUM(amount)  AS metric_value
FROM _labeled
-- no WHERE, no GROUP BY
```

---

## `query/executor.py` — QueryExecutor

```python
class QueryExecutor:
    """
    Execute QueryGroups using the global ConnectionManager.
    Gets one connector per source, executes each SQL string, pd.concat results.
    """

    def execute(
        self,
        query_groups: list[QueryGroup],
        output_format: str = 'pandas',
    ) -> DataFrame:
        """
        Execute all query groups sequentially (Phase 1).
        Combine all results into a single DataFrame via pd.concat.

        If a connection is missing for a group, log a warning and skip that group.
        Returns partial results if any groups succeed.

        Raises:
            QueryExecutionError: if ALL groups fail to produce results
        """

    def _execute_query_group(
        self,
        query_group: QueryGroup,
        output_format: str,
    ) -> DataFrame | None:
        """
        Execute all SQL queries in a single QueryGroup.
        Gets one connector for the group's source, runs each sql in sql_queries,
        and returns pd.concat of all results.

        Steps:
        1. Get IbisConnector via ConnectionManager.get_global().get_connection_for_source()
        2. For each sql in query_group.sql_queries:
               ibis_expr = connector.connection.sql(sql)
               df = connector.execute(ibis_expr, output_format)
        3. Return pd.concat(dfs, ignore_index=True)

        Returns None (with warning) if connection is unavailable.
        """
```

### Dialect handling (Phase 1)
SQL is built in DuckDB dialect. The executor passes it as-is to the backend.
Most simple SQL constructs (IN, CASE WHEN, SUM, COUNT, IS NULL) are standard and work
across DuckDB, ClickHouse, and BigQuery without modification.
Phase 2 can add `sqlglot.transpile(sql, read='duckdb', write=backend_type)` in
`_execute_query_group()` before calling `connection.sql()`.

---

## `query/__init__.py`

```python
from aitaem.query.builder import QueryBuilder, QueryGroup
from aitaem.query.executor import QueryExecutor

__all__ = ["QueryBuilder", "QueryGroup", "QueryExecutor"]
```

---

## New Exception

Add to `aitaem/utils/exceptions.py`:

```python
class QueryBuildError(AitaemError):
    """Raised when query construction fails due to invalid or incompatible specs."""
```

---

## Sub-Feature Implementation Order

Each sub-feature is independently testable. Implement and test in this order:

### 1. `QueryGroup` dataclass
- Define `QueryGroup` with `source: str`, `metrics: list[MetricSpec]`, `sql_queries: list[str]`
- **Test**: Instantiate; verify attributes

### 2. `QueryBuildError`
- Add to `aitaem/utils/exceptions.py`
- **Test**: Can be raised and caught as `AitaemError`

### 3. `_group_by_source()`
- Group a list of MetricSpecs by their `source` field
- **Test**: 3 metrics across 2 unique sources → dict with 2 keys and correct lengths

### 4. `_parse_table_name_from_uri()`
- Use `ConnectionManager.parse_source_uri()` to extract table name
- **Test**: `duckdb://analytics.db/events` → `'events'`; `bigquery://proj/dataset/tbl` → `'dataset.tbl'`

### 5. `_build_time_filter_sql()`
- Build `{col} >= '{start}' AND {col} < '{end}'`
- **Test**: Correct output for a given time window and column name

### 6. `_build_metric_value_expr()`
- `ratio` → `{numerator} / NULLIF({denominator}, 0)`; others → numerator as-is
- **Test**: ratio with denominator; sum without denominator

### 7. `_build_slice_case_when_expr()`
- Build `CASE WHEN {where} THEN '{name}' ... ELSE NULL END AS {alias}`
- **Test**: Output string contains all values from the SliceSpec; ends with correct alias

### 8. `_build_segment_case_when_expr()`
- Same structure as slice; verify independently
- **Test**: Same pattern as slice CASE WHEN test

### 9. `_build_slice_value_concat_expr()`
- Single alias → just the alias; multiple → `a || '|' || b || ...`
- **Test**: 1 alias → no `||`; 3 aliases → two `||` with `'|'` separators

### 10. `_build_metric_segment_query()`
- Assemble the full CTE + SELECT statement for one (metric, segment_spec | None)
- Cover all four cases: slices+segment, slices only, segment only, neither
- **Test**:
  - Slices + segment: CTE has CASE WHEN for each slice and for segment; outer has GROUP BY all; WHERE has IS NOT NULL for all
  - Slices only: CTE has slice CASE WHEN; segment columns absent; GROUP BY slices only; segment_name='none', segment_value='all'
  - Segment only: CTE has segment CASE WHEN; slice columns absent; slice_type='none', slice_value='all'
  - Neither: minimal CTE, no GROUP BY, slice_type='none', segment_name='none'
  - Execute generated SQL against in-memory DuckDB to confirm it is valid SQL

### 11. `_build_queries_for_metric()`
- Returns one query per segment spec + one no-segment query
- **Test**: 2 segment specs → list of 3 SQL strings; 0 segment specs → list of 1 SQL string

### 11b. `_resolve_slice_components()`
- For leaf spec: return `[spec]`
- For composite spec: call `SpecCache.get_global().get_slice(name)` for each name; return list
- **Test**: leaf spec → list of 1; composite spec with mocked SpecCache → list of component specs; missing name raises SpecNotFoundError

### 12. `QueryBuilder.build_queries()` (integration)
- Group by source; for each metric, call `_build_queries_for_metric()`
- Total sql_queries count per group = num_metrics_in_group × (num_slice_specs + 1) × (num_segment_specs + 1)
- Validate time_window + timestamp_col consistency
- **Test**:
  - 0 slices, 0 segments → 1 query per metric
  - 2 slices, 1 segment → (2+1)×(1+1)=6 queries per metric
  - 1 composite slice (2 components), 1 segment → (1+1)×(1+1)=4 queries per metric
  - `QueryBuildError` raised when time_window provided without timestamp_col

### 13. `QueryExecutor._execute_query_group()`
- Fetch connector; execute each SQL via `connector.connection.sql()`; pd.concat
- Return None on missing connection
- **Test**: Integration test with in-memory DuckDB; verify output DataFrame has all expected columns and correct schema

### 14. `QueryExecutor.execute()`
- Iterate over QueryGroups; call `_execute_query_group()`; pd.concat all results
- Skip failed groups with warning; raise `QueryExecutionError` only if all fail
- **Test**: Multiple groups → combined DataFrame; all groups fail → exception; partial failure → warning + partial result

---

## Integration Test Scenario

Use DuckDB in-memory for integration testing:

```python
# Sample data: transactions table
# columns: country_code, device_type, subscription_tier, is_logged_in, amount, event_ts
#
# Rows cover: US/mobile/premium, US/desktop/free, EU/mobile/premium, EU/desktop/free
```

End-to-end assertions (1 metric, 2 leaf slices=[geo, device], 1 segment=[user_tier]):
1. **Query count**: (2 slices + 1 no-slice) × (1 segment + 1 no-segment) = 6 queries → 6 result batches before concat
2. `metric_value` is non-null and numerically correct
3. `slice_type` values: `'geography'`, `'device'`, `'none'`
4. `slice_value='all'` on all no-slice rows; `'|'`-separated values for composite slices
5. `segment_value='all'` on all no-segment rows
6. All standard output columns present with correct dtypes

---

## Dependencies

No new Python packages needed:
- `itertools` (stdlib) — not needed anymore (GROUP BY replaces cross-product iteration)
- `ibis-framework` — already a dependency
- `pandas` — already a dependency

---

## Out of Scope for This Plan

- `insights.py` (`MetricCompute` class) — separate plan
- Time-series granularity (daily/weekly/monthly) — Phase 2
- Parallel query execution (concurrent.futures) — Phase 2; current sequential structure
  makes this a trivial future change: replace the loop in `execute()` with a thread pool
- SQL dialect transpilation (sqlglot) — Phase 2
- Cross-table segment filtering with JOIN — Phase 2
- SliceSpec `column:` field for open-ended column-value slicing — Phase 2

---

## Open Questions / Phase 2 Notes

1. **Segment JOIN**: Phase 2 SegmentSpec should add `entity_col: str` field (the join key
   between segment source and metric source table).

2. **SliceSpec column extension**: Phase 2 SliceSpec should support an optional `column: str`
   field as an alternative to `values`. When `column` is set, the CTE uses `{column} AS _slice_{name}`
   directly (no CASE WHEN), and GROUP BY on that column produces all observed values.
   This is a natural extension of the CASE WHEN + GROUP BY structure.

3. **Period granularity**: Phase 2 should add `period_type` parameter to `build_queries()`
   supporting 'daily'/'weekly'/'monthly' via `GROUP BY date_trunc('{granularity}', {timestamp_col})`
   added to the outer SELECT and GROUP BY clause.

4. **Parallel execution**: Phase 2 `QueryExecutor.execute()` can use `concurrent.futures`
   to execute multiple QueryGroups (or sql_queries within a group) in parallel.

5. **SQL dialect**: Phase 2 can add sqlglot transpilation in `_execute_query_group()` based
   on the backend type extracted from the source URI.

6. **Metric value SQL injection**: In Phase 1, metric names, slice values, and segment values
   are embedded in SQL string literals. These come from trusted YAML specs (not user input),
   so SQL injection risk is low. Phase 2 should evaluate using parameterized queries.
