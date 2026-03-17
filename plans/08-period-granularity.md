# Period Granularity Plan

## Overview

Add a `period_type` parameter to `MetricCompute.compute()` and `QueryBuilder.build_queries()` that
controls how results are grouped over time. Currently, every query produces a single row per
metric×slice×segment combination with `period_type='all_time'`. With period granularity, each
combination produces one row **per time period** (day / week / month / year).

```
period_type = 'all_time'   →  1 row per metric×slice×segment (current behavior, unchanged)
period_type = 'monthly'    →  N rows per metric×slice×segment, one per calendar month
period_type = 'daily'      →  one row per calendar day
period_type = 'weekly'     →  one row per ISO week (Monday–Sunday; see note)
period_type = 'yearly'     →  one row per calendar year
```

---

## What the User Might Be Missing

1. **`time_window` is required for any non-`all_time` period type.** The period boundaries are
   generated in Python from the time window range. Without it, there is no bounded range from
   which to generate periods. Raise `QueryBuildError` if `period_type != 'all_time'` and
   `time_window` is `None`.

2. **`timestamp_col` is also required for any non-`all_time` period type**, just as it is already
   required when `time_window` is provided. Raise `QueryBuildError` if `period_type != 'all_time'`
   and any metric has `timestamp_col=None`.

3. **The JOIN with `_periods` replaces the `time_window` WHERE filter for non-`all_time` queries.**
   The period VALUES CTE covers exactly the time window; the JOIN condition
   (`CAST(t.event_ts AS TIMESTAMP) >= p.period_start AND ... < p.period_end`) simultaneously
   assigns each row to a period bucket and filters out out-of-window rows. No separate
   `WHERE time_filter_sql` is needed in the CTE for non-`all_time`.

4. **Partial-period behavior at `time_window` edges**: if `time_window=('2026-01-07', '2026-04-01')`
   and `period_type='weekly'`, the first generated period starts on Monday 2026-01-05 (the ISO
   week containing 2026-01-07). The period label is `'2026-01-05'` but only data from 2026-01-07
   onward is included via the JOIN. This is expected and consistent with how `all_time` already
   handles partial time-window edges.

5. **Output row expansion**: unlike `all_time`, non-`all_time` queries produce one row per period
   per metric×slice×segment combination. Because the period boundaries are generated in Python
   before the query runs, the total number of output rows is fully predictable:
   `len(_generate_period_boundaries(time_window, period_type)) × (n_slice_combos) × (n_segment_combos)`.
   Callers should be aware that wide time windows with fine granularity (e.g. `daily` over a year)
   will produce large result sets.

6. **`period_start_date` / `period_end_date` output type**: these come back from the database as
   TIMESTAMP objects. A `CAST(... AS VARCHAR)` in the outer SELECT ensures the output DataFrame
   always has string columns, keeping the standard output schema unchanged.

7. **`SELECT t.*` instead of `SELECT *` in the labeled CTE for non-`all_time` queries**: the
   `_labeled` CTE uses a JOIN, so `SELECT *` would be ambiguous and include columns from both
   the transactions table and `_periods`. The non-`all_time` CTE uses `SELECT t.*` explicitly
   plus the two period alias columns.

---

## Design Decisions

### Python-generated period boundaries via a VALUES CTE

Period boundaries are computed entirely in Python using the stdlib `datetime` module. They are
embedded in the SQL as a `_periods` VALUES CTE with `CAST(... AS TIMESTAMP)` literals. The
`_labeled` CTE then JOINs against `_periods` on the timestamp column.

This approach uses **no dialect-specific SQL date functions** — only `CAST`, `>=`, and `<`, which
are standard SQL across all backends.

### JOIN condition uses bilateral CAST to TIMESTAMP

The JOIN condition:

```sql
CAST(t.{timestamp_col} AS TIMESTAMP) >= p.period_start
AND CAST(t.{timestamp_col} AS TIMESTAMP) < p.period_end
```

`p.period_start` and `p.period_end` are typed as TIMESTAMP (from the CAST in the VALUES
definition). `CAST(t.{timestamp_col} AS TIMESTAMP)` normalises the event column regardless of its
storage type:

| `timestamp_col` storage type | `CAST(... AS TIMESTAMP)` result |
|---|---|
| `TIMESTAMP` | no-op |
| `DATE` | `date 00:00:00` (universally supported promotion) |
| `VARCHAR` | parsed as ISO string (works for Python-generated ISO date strings) |

Comparing TIMESTAMP to TIMESTAMP is unambiguous across all backends.

### Period boundaries generated with stdlib `datetime` only — no new dependency

For `monthly` and `yearly`, month/year increments are handled with explicit `date` arithmetic
(incrementing month/year fields directly) rather than `dateutil.relativedelta`, so no new Python
package is required.

### `all_time` behavior is completely unchanged

When `period_type='all_time'`, the SQL is identical to the current implementation — single
`_labeled` CTE, optional WHERE time filter, no `_periods` CTE, no JOIN.

### `period_type` is a compute-time decision, not a spec-level field

`period_type` is passed at call time to `MetricCompute.compute()`, not baked into `MetricSpec`.
The same metric YAML can be computed at any granularity.

### Validation in `build_queries()`

Raise `QueryBuildError` with a clear message if:
- `period_type` is not one of the valid values
- `period_type != 'all_time'` and `time_window` is `None`
- `period_type != 'all_time'` and any metric in `metric_specs` has `timestamp_col=None`

The existing `time_window` + `timestamp_col` validation for `all_time` (currently raises on
`time_window` without `timestamp_col`) is unchanged.

### Weekly period start: ISO Monday

`_generate_period_boundaries()` rounds the start of the first weekly period down to the Monday of
the ISO week containing `time_window[0]`. This is consistent with ISO 8601. `week_start_day`
configuration is Phase 2.

### `period_type` propagates down the same call chain as today

```
MetricCompute.compute(period_type=...)
  → QueryBuilder.build_queries(period_type=...)
      → _build_queries_for_metric(period_type=...)
          → _build_metric_segment_query(period_type=...)
```

Two new private helpers are added to `builder.py`. No new public methods.

---

## API Changes

### `MetricCompute.compute()` — `insights.py`

```python
def compute(
    self,
    metrics: str | list[str],
    slices: str | list[str] | None = None,
    segments: str | list[str] | None = None,
    time_window: tuple[str, str] | None = None,
    period_type: str = 'all_time',           # NEW
    output_format: str = 'pandas',
) -> pd.DataFrame:
```

### `QueryBuilder.build_queries()` — `query/builder.py`

```python
@staticmethod
def build_queries(
    metric_specs: list[MetricSpec],
    slice_specs: list[SliceSpec] | None,
    segment_specs: list[SegmentSpec] | None,
    time_window: tuple[str, str] | None = None,
    spec_cache: SpecCache | None = None,
    period_type: str = 'all_time',           # NEW
) -> list[QueryGroup]:
```

---

## Standard Output Schema (unchanged)

`period_start_date` and `period_end_date` remain `str | None`. For non-`all_time` they are CAST
to VARCHAR in the SQL; for `all_time` they remain static string literals (or NULL).

| Column              | Type      | Notes                                                         |
|---------------------|-----------|---------------------------------------------------------------|
| `period_type`       | str       | `'all_time'`, `'daily'`, `'weekly'`, `'monthly'`, `'yearly'` |
| `period_start_date` | str\|None | ISO date string; `None` only for `all_time` with no time_window |
| `period_end_date`   | str\|None | ISO date string (exclusive end); same caveat as above         |
| `metric_name`       | str       | MetricSpec.name                                               |
| `slice_type`        | str       | pipe-delimited slice spec names, or `'none'`                  |
| `slice_value`       | str       | pipe-delimited slice value names, or `'all'`                  |
| `segment_name`      | str       | SegmentSpec.name, or `'none'`                                 |
| `segment_value`     | str       | SegmentValue.name, or `'all'`                                 |
| `metric_value`      | float     | computed metric value                                         |

---

## SQL Structure

### `all_time` (unchanged)

```sql
WITH _labeled AS (
    SELECT
        *,
        [CASE WHEN ... END AS _slice_geo, ...]
        [CASE WHEN ... END AS _segment]
    FROM transactions
    [WHERE event_ts >= '2026-01-01' AND event_ts < '2026-02-01']
)
SELECT
    'all_time'   AS period_type,
    '2026-01-01' AS period_start_date,
    '2026-02-01' AS period_end_date,
    ...
FROM _labeled
[WHERE ...]
[GROUP BY _slice_geo, _segment]
```

### Non-`all_time` (e.g., `monthly`)

```sql
WITH _periods(period_start, period_end) AS (
    VALUES
        (CAST('2026-01-01' AS TIMESTAMP), CAST('2026-02-01' AS TIMESTAMP)),
        (CAST('2026-02-01' AS TIMESTAMP), CAST('2026-03-01' AS TIMESTAMP)),
        (CAST('2026-03-01' AS TIMESTAMP), CAST('2026-04-01' AS TIMESTAMP))
),
_labeled AS (
    SELECT
        t.*,
        p.period_start                   AS _period_start,
        p.period_end                     AS _period_end,
        [CASE WHEN ... END AS _slice_geo, ...]
        [CASE WHEN ... END AS _segment]
    FROM transactions t
    JOIN _periods p
      ON CAST(t.event_ts AS TIMESTAMP) >= p.period_start
     AND CAST(t.event_ts AS TIMESTAMP) <  p.period_end
)
SELECT
    'monthly'                          AS period_type,
    CAST(_period_start AS VARCHAR)     AS period_start_date,
    CAST(_period_end   AS VARCHAR)     AS period_end_date,
    'revenue'                          AS metric_name,
    ...
FROM _labeled
[WHERE _slice_geo IS NOT NULL AND _segment IS NOT NULL]
GROUP BY _period_start, _period_end [, _slice_geo, _segment]
```

Key differences from `all_time`:
1. A `_periods` VALUES CTE is prepended with Python-generated boundaries, cast to TIMESTAMP.
2. `_labeled` JOINs against `_periods` (no separate WHERE time filter).
3. `SELECT t.*` instead of `SELECT *` to avoid column name conflicts from the JOIN.
4. `_period_start` and `_period_end` are always in `GROUP BY` (even for no-slice/no-segment).
5. `period_start_date` / `period_end_date` in the outer SELECT are dynamic CAST expressions.

---

## New Private Helpers in `builder.py`

### `_generate_period_boundaries(time_window: tuple[str, str], period_type: str) -> list[tuple[str, str]]`

```python
@staticmethod
def _generate_period_boundaries(
    time_window: tuple[str, str],
    period_type: str,
) -> list[tuple[str, str]]:
    """
    Generate (period_start, period_end) ISO date string pairs covering time_window.

    Each pair is a half-open interval [period_start, period_end).
    The first period_start is rounded down to the period boundary containing
    time_window[0] (e.g., the Monday of the containing ISO week).

    Uses stdlib datetime only — no external dependencies.

    Examples:
        time_window=('2026-01-01', '2026-04-01'), period_type='monthly'
        → [('2026-01-01', '2026-02-01'),
           ('2026-02-01', '2026-03-01'),
           ('2026-03-01', '2026-04-01')]

        time_window=('2026-01-07', '2026-01-22'), period_type='weekly'
        → [('2026-01-05', '2026-01-12'),   # Monday of week containing Jan 7
           ('2026-01-12', '2026-01-19'),
           ('2026-01-19', '2026-01-26')]
    """
```

### `_build_periods_cte(boundaries: list[tuple[str, str]]) -> str`

```python
@staticmethod
def _build_periods_cte(boundaries: list[tuple[str, str]]) -> str:
    """
    Build the _periods VALUES CTE SQL string.

    Returns:
        "_periods(period_start, period_end) AS (\n    VALUES\n        (...),\n        ...\n)"

    Each boundary value is wrapped in CAST('{date}' AS TIMESTAMP).
    """
```

### Module-level constant (replaces `_PERIOD_TRUNC` / `_PERIOD_INTERVAL`)

```python
_VALID_PERIOD_TYPES = frozenset({"all_time", "daily", "weekly", "monthly", "yearly"})
```

---

## Files to Modify

```
aitaem/query/builder.py     # New helpers, updated _build_metric_segment_query + build_queries
aitaem/insights.py          # Add period_type param to compute()
```

No new files. No new dependencies (stdlib `datetime` only).

---

## Sub-Feature Implementation Order

### 1. `_VALID_PERIOD_TYPES` constant in `builder.py`

- Add module-level `_VALID_PERIOD_TYPES = frozenset({...})`
- **Test**: Verify all five expected values are present

### 2. `_generate_period_boundaries()`

- Implement daily / weekly / monthly / yearly boundary generation using stdlib `datetime`
- Weekly: round start down to Monday via `start - timedelta(days=start.weekday())`
- Monthly: increment by 1 month using explicit `date` field arithmetic (no `dateutil`)
- Yearly: increment year field directly
- **Test**:
  - `monthly`, aligned window → 3 clean month pairs; no partial first period
  - `weekly`, window starting mid-week → first period_start is the preceding Monday
  - `daily`, 3-day window → 3 pairs, each 1 day apart
  - `yearly`, 2-year window → 2 pairs
  - Window starting on a non-period-boundary → first period_start is before `time_window[0]`

### 3. `_build_periods_cte()`

- Build the VALUES SQL string with `CAST('...' AS TIMESTAMP)` for each boundary
- **Test**: output string contains `_periods(period_start, period_end) AS`; correct number of
  VALUES rows; each value wrapped in `CAST(... AS TIMESTAMP)`

### 4. Update `_build_metric_segment_query()` to accept `period_type` and `timestamp_col`

- For `all_time`: current behavior unchanged
- For non-`all_time`:
  - Generate period boundaries via `_generate_period_boundaries()` and build `_periods` CTE via
    `_build_periods_cte()`
  - Prepend `_periods` CTE before `_labeled`; change `_labeled` FROM to a JOIN
  - Switch `SELECT *` → `SELECT t.*` in the `_labeled` CTE
  - Add `p.period_start AS _period_start, p.period_end AS _period_end` to `_labeled` SELECT
  - Remove `time_filter_sql` WHERE clause (JOIN replaces it)
  - Replace static `period_start_date` / `period_end_date` literals with
    `CAST(_period_start AS VARCHAR)` / `CAST(_period_end AS VARCHAR)`
  - Prepend `_period_start, _period_end` to `group_by_cols`

Note: `timestamp_col` must be passed in (from `metric.timestamp_col`) for the JOIN condition.

- **Test** (execute generated SQL against in-memory DuckDB):
  - `all_time`: SQL and output are identical to current behavior
  - `monthly`: one row per month; `period_start_date` is `'YYYY-MM-01'`;
    `period_end_date` is first day of the following month
  - `monthly` with slices + segment: GROUP BY includes `_period_start, _period_end` alongside
    slice/segment aliases
  - `weekly`: `period_start_date` is always a Monday
  - No-slice, no-segment, non-`all_time`: GROUP BY still contains `_period_start, _period_end`

### 5. Update `build_queries()` to accept, validate, and propagate `period_type`

- Accept `period_type: str = 'all_time'`
- Validate `period_type in _VALID_PERIOD_TYPES`; raise `QueryBuildError` on unknown value
- Validate that if `period_type != 'all_time'`, `time_window` is not `None`; raise
  `QueryBuildError` if missing
- Validate that if `period_type != 'all_time'`, every metric has `timestamp_col`; raise
  `QueryBuildError` if any are missing
- Pass `period_type` (and `time_window` for boundary generation) down to
  `_build_queries_for_metric()` → `_build_metric_segment_query()`

- **Test**:
  - Unknown `period_type` string → `QueryBuildError`
  - `period_type='monthly'` with `time_window=None` → `QueryBuildError`
  - `period_type='monthly'` with a metric missing `timestamp_col` → `QueryBuildError`
  - `period_type='monthly'` with valid inputs → correct SQL (DuckDB execution)

### 6. Update `MetricCompute.compute()` in `insights.py`

- Accept `period_type: str = 'all_time'`; pass to `QueryBuilder.build_queries()`
- **Test** (integration with in-memory DuckDB via `MetricCompute`):
  - `period_type='all_time'` (default): no regression vs. current behavior
  - `period_type='monthly'`: result has one row per month per metric×slice×segment combination

---

## Integration Test Scenario

```python
# Table: transactions
# Columns: amount, country_code, subscription_tier, event_ts (TIMESTAMP)
# Data: rows spanning 2026-01 through 2026-03, across all slice/segment combinations
# Metric:   revenue (SUM(amount), timestamp_col='event_ts')
# Slices:   geography=[North America, Europe]
# Segments: user_tier=[premium, free]
# time_window: ('2026-01-01', '2026-04-01')
# period_type: 'monthly'
```

Assertions:

1. `period_type` column is `'monthly'` on every row
2. `period_start_date` values are exactly `{'2026-01-01', '2026-02-01', '2026-03-01'}`
3. `period_end_date` for January is `'2026-02-01'`; for March is `'2026-04-01'`
4. `metric_value` is non-null and matches per-month SUM computed independently
5. `slice_value='all'` and `slice_type='none'` on no-slice rows
6. All standard output columns present with correct types
7. **Backward compatibility**: omitting `period_type` produces identical output to the
   current `all_time` behavior (same SQL, same DataFrame)

---

## Out of Scope

- Custom `week_start_day` parameter (Sunday start, fiscal weeks) — Phase 2
- Custom fiscal calendar tables — Phase 2
- Per-metric `period_type` (different granularities for different metrics in one call) — Phase 2
- `period_type != 'all_time'` without a `time_window` (would require a pre-scan query for
  min/max dates) — Phase 2
