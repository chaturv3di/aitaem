# by_entity Metrics Plan

## Overview

Add an optional `entities` field to `MetricSpec` that declares which entity columns the metric
supports for disaggregation. At compute time, the caller passes `by_entity` to
`MetricCompute.compute()` to select which entity column to group by.

When `by_entity` is set at compute time, a new `entity_id` column appears in the output
(positioned right after `period_end_date`, before `metric_name`). When omitted, `entity_id`
is `NULL`.

---

## Motivation

Until now, all metrics aggregate over the full population: every row in the source table
contributes to a single value per period×slice×segment bucket. Analysts doing deep-dives
into metric distributions or investigating individual-level trends need entity-level
breakdowns — e.g., revenue per `user_id`, sessions per `device_id`, engagement per `page_id`.

A source table commonly records multiple entity identifiers (e.g. `user_id`, `device_id`,
`page_id` alongside event data). An earlier design put a single `by_entity` field directly on
`MetricSpec`, but this would force analysts to create one copy of the spec per entity type.
Those copies diverge over time as the primary metric definition evolves.

The revised design separates concerns cleanly:

- `MetricSpec.entities` declares *what is possible* for that metric (spec-time).
- `by_entity` in `compute()` selects *what to compute* (call-time).

This mirrors how `period_type` works: the same metric spec is valid for `all_time`, `monthly`,
or `daily` without duplication.

---

## Design Decisions

### `entities` on `MetricSpec`; `by_entity` on `compute()`

`entities` is metadata about the source table's entity columns — it belongs in the spec.
`by_entity` is a runtime choice about how to aggregate — it belongs at call time, alongside
`period_type`, `slices`, and `segments`.

### `entities` is an optional list; `None` means "no entity breakdown declared"

```python
entities: list[str] | None = None
```

`None` (or absent from YAML) means the spec author has not declared any entity columns.
An empty list `[]` is rejected by validation (consistent with `values` in SliceSpec/SegmentSpec,
which must be non-empty if present).

### Strict cross-metric validation at compute time

If `by_entity='user_id'` is passed to `compute()`, every metric in the call must list
`'user_id'` in its `entities`. If any metric omits it (or has `entities=None`), a
`QueryBuildError` is raised with a clear message — the same pattern used for `timestamp_col`
validation across metrics when `period_type != 'all_time'`.

### No `supported_entities()` method

`.entities` is already a public field on the frozen dataclass. A thin wrapper method adds no
value over `metric.entities or []`. Callers can read the field directly.

### `entity_id` is the canonical output column name

The output column is always `entity_id` regardless of the source column name (`user_id`,
`device_id`, etc.). This keeps the standard output schema stable and column-name–agnostic for
downstream consumers.

### `NULL` when `by_entity` is not passed

Rows without entity-level grouping emit `NULL` as `entity_id`. This is consistent with how
`slice_type='none'` / `segment_name='none'` signal "not disaggregated."

### SQL changes are minimal and symmetric across `all_time` / non-`all_time`

Both paths already do `SELECT *` / `SELECT t.*` in the `_labeled` CTE, so the entity column
is available in the outer query without any CTE changes. The only SQL differences are:

1. Outer SELECT: `<by_entity_col> AS entity_id` (or `NULL AS entity_id`)
2. GROUP BY: prepend `<by_entity_col>` when `by_entity` is set

### `by_entity` propagates down the call chain like `period_type`

```
MetricCompute.compute(by_entity=...)
  → QueryBuilder.build_queries(by_entity=...)
      → _build_queries_for_metric(by_entity=...)
          → _build_metric_segment_query(by_entity=...)
```

### Column position: right after `period_end_date`, before `metric_name`

This groups all temporal/entity context columns together before the metric identity columns.

---

## API Changes

### `MetricSpec` — `specs/metric.py`

New field:

```python
@dataclass(frozen=True)
class MetricSpec:
    ...
    entities: list[str] | None = None   # NEW
```

YAML key: `entities`. Optional list of column name strings. Example:

```yaml
metric:
  name: revenue
  source: duckdb://data.db/transactions
  aggregation: sum
  numerator: amount
  timestamp_col: event_ts
  entities: [user_id, device_id]        # NEW — optional
```

### `MetricCompute.compute()` — `insights.py`

```python
def compute(
    self,
    metrics: str | list[str],
    slices: str | list[str] | None = None,
    segments: str | list[str] | None = None,
    time_window: tuple[str, str] | None = None,
    period_type: str = "all_time",
    by_entity: str | None = None,        # NEW
    output_format: str = "pandas",
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
    period_type: str = "all_time",
    by_entity: str | None = None,        # NEW
) -> list[QueryGroup]:
```

Validation added: if `by_entity` is not `None`, every metric in `metric_specs` must include
`by_entity` in its `entities` list; otherwise raise `QueryBuildError`.

### Standard output schema — `utils/formatting.py`

New column inserted between `period_end_date` and `metric_name`:

| Column              | Type       | Notes                                              |
|---------------------|------------|----------------------------------------------------|
| `period_type`       | str        | unchanged                                          |
| `period_start_date` | str\|None  | unchanged                                          |
| `period_end_date`   | str\|None  | unchanged                                          |
| **`entity_id`**     | str\|None  | **NEW** — entity column value, or `NULL`           |
| `metric_name`       | str        | unchanged                                          |
| `slice_type`        | str        | unchanged                                          |
| `slice_value`       | str        | unchanged                                          |
| `segment_name`      | str        | unchanged                                          |
| `segment_value`     | str        | unchanged                                          |
| `metric_value`      | float      | unchanged                                          |

---

## SQL Structure

### `all_time`, `by_entity` not passed (unchanged aggregation)

```sql
WITH _labeled AS (
    SELECT
        *,
        [slice/segment CASE WHEN exprs]
    FROM transactions
    [WHERE event_ts >= '...' AND event_ts < '...']
)
SELECT
    'all_time'    AS period_type,
    '2026-01-01'  AS period_start_date,
    '2026-02-01'  AS period_end_date,
    NULL          AS entity_id,           -- sentinel
    'revenue'     AS metric_name,
    ...
FROM _labeled
[WHERE ...]
[GROUP BY _slice_geo, _segment]
```

### `all_time`, `by_entity='user_id'`

```sql
WITH _labeled AS (
    SELECT
        *,
        [slice/segment CASE WHEN exprs]
    FROM transactions
    [WHERE event_ts >= '...' AND event_ts < '...']
)
SELECT
    'all_time'    AS period_type,
    '2026-01-01'  AS period_start_date,
    '2026-02-01'  AS period_end_date,
    user_id       AS entity_id,
    'revenue'     AS metric_name,
    ...
FROM _labeled
[WHERE ...]
GROUP BY user_id [, _slice_geo, _segment]
```

### Non-`all_time` (e.g., `monthly`), `by_entity='user_id'`

```sql
WITH _periods(...) AS (...),
_labeled AS (
    SELECT
        t.*,
        p.period_start AS _period_start,
        p.period_end   AS _period_end,
        [slice/segment CASE WHEN exprs]
    FROM transactions t
    JOIN _periods p ON ...
)
SELECT
    'monthly'                       AS period_type,
    CAST(_period_start AS VARCHAR)  AS period_start_date,
    CAST(_period_end   AS VARCHAR)  AS period_end_date,
    user_id                         AS entity_id,
    'revenue'                       AS metric_name,
    ...
FROM _labeled
[WHERE ...]
GROUP BY _period_start, _period_end, user_id [, _slice_geo, _segment]
```

---

## Files to Modify

```
aitaem/specs/metric.py                      # Add entities field; parse + validate
aitaem/utils/validation.py                  # Add optional entities validation in validate_metric_spec()
aitaem/query/builder.py                     # Accept by_entity; add entity_id to SELECT and GROUP BY;
                                            #   validate by_entity against metric.entities
aitaem/utils/formatting.py                  # Add entity_id to STANDARD_COLUMNS
aitaem/insights.py                          # Add by_entity param; update docstring
docs/user-guide/specs.md                    # Add entities field to MetricSpec field table + example
docs/user-guide/computing-metrics.md        # Add by_entity section; update output schema table;
                                            #   update error handling table
docs/changelog.md                           # Add Unreleased entry
```

No new files. No new dependencies.

---

## Sub-Feature Implementation Order

### 1. Add `entities` to `MetricSpec` and its validation

**Files**: `aitaem/specs/metric.py`, `aitaem/utils/validation.py`

Changes:
- Add `entities: list[str] | None = None` field to `MetricSpec` dataclass
- In `from_yaml()`: extract `entities = spec_dict.get("entities") or None` and pass to
  `cls(...)` constructor
- In `validate()`: include `entities` in `spec_dict` when present
- In `validate_metric_spec()`: if `entities` is present, validate it is a non-empty list of
  non-blank strings; emit `ValidationError` if the list is empty or any entry is blank

**Test**:
- YAML without `entities` → `MetricSpec.entities is None`
- YAML with `entities: [user_id, device_id]` → `MetricSpec.entities == ['user_id', 'device_id']`
- YAML with `entities: []` (empty list) → `SpecValidationError`
- YAML with `entities: [""]` (blank entry) → `SpecValidationError`
- `MetricSpec.validate()` returns valid result with and without `entities`

---

### 2. Add `by_entity` to `QueryBuilder.build_queries()` and propagate to SQL generation

**File**: `aitaem/query/builder.py`

Changes in `build_queries()`:
- Accept `by_entity: str | None = None`
- If `by_entity` is not `None`, validate that every metric in `metric_specs` has `by_entity`
  in its `entities` list (or `entities` is not `None`); raise `QueryBuildError` with a clear
  message if any metric fails this check
- Pass `by_entity` down through `_build_queries_for_metric()` to
  `_build_metric_segment_query()`

Changes in `_build_metric_segment_query()`:
- Accept `by_entity: str | None`
- Compute `entity_id_expr`:
  - `by_entity` is set → `f"{by_entity} AS entity_id"`
  - `by_entity` is `None` → `"NULL AS entity_id"`
- Insert `entity_id_expr` into `outer_select_cols` right after `period_end_date` and before
  `metric_name` (both `all_time` and non-`all_time` paths)
- If `by_entity` is set, prepend it to `group_by_cols` (after `_period_start`/`_period_end`
  for non-`all_time`, before slice/segment aliases)

**Test** (SQL string inspection + DuckDB execution):
- `by_entity=None`: `entity_id` column is `NULL`; GROUP BY unchanged
- `by_entity='user_id'`, metric has `entities=['user_id']`: `entity_id` column contains
  `user_id` values; `user_id` in GROUP BY
- `by_entity='device_id'`, metric has `entities=['user_id']` → `QueryBuildError`
- `by_entity='user_id'`, metric has `entities=None` → `QueryBuildError`
- `all_time` + `by_entity`: one row per `user_id`; metric value is per-user aggregate
- Non-`all_time` + `by_entity`: one row per `(period, user_id)`; GROUP BY includes
  `_period_start, _period_end, user_id`
- With slices + `by_entity`: GROUP BY contains all of `user_id, _slice_*, _segment`

---

### 3. Update `STANDARD_COLUMNS` in `formatting.py`

**File**: `aitaem/utils/formatting.py`

- Insert `"entity_id"` into `STANDARD_COLUMNS` between `"period_end_date"` and `"metric_name"`
- `ensure_standard_output()` already raises `ValueError` on missing columns — no other change
  needed

**Test**:
- `ensure_standard_output()` on a DataFrame that includes `entity_id` → succeeds, correct
  column order
- `ensure_standard_output()` on a DataFrame missing `entity_id` → raises `ValueError`

---

### 4. Add `by_entity` to `MetricCompute.compute()` and update docstring

**File**: `aitaem/insights.py`

- Add `by_entity: str | None = None` parameter; pass to `QueryBuilder.build_queries()`
- Update `Returns:` docstring to list `entity_id` column (noting `NULL` when not set)
- Update `Raises:` docstring to mention `QueryBuildError` for unsupported `by_entity`

**Test** (integration with in-memory DuckDB via `MetricCompute`):
- `by_entity=None` (default): `entity_id` column is `NULL` on all rows; no regression
- `by_entity='user_id'`: output has one row per `(user_id, period×slice×segment)`;
  `entity_id` values match input `user_id` values

---

### 5. Update documentation

#### `docs/user-guide/specs.md`

- Add `entities` row to the MetricSpec **Fields** table:

  | Field | Required | Description |
  |-------|----------|-------------|
  | `entities` | No | List of entity column names this metric supports for `by_entity` disaggregation (e.g. `[user_id, device_id]`). Must be non-empty if provided. |

- Add a new **Entity columns** sub-section under MetricSpec (after Aggregation types) showing
  a YAML example with `entities` and a brief explanation of when to use it.

#### `docs/user-guide/computing-metrics.md`

- Add a **`by_entity`** parameter sub-section (after `time_window`, before the Output Schema):

  ```python
  # Disaggregate revenue by user
  df = mc.compute(metrics="revenue", by_entity="user_id",
                  time_window=("2026-01-01", "2026-04-01"), period_type="monthly")

  # Default — aggregate over all entities
  df = mc.compute(metrics="revenue")
  ```

  Include a note:
  > All metrics in the call must list `by_entity` in their `entities` field. A `QueryBuildError`
  > is raised if any metric does not declare the requested entity column.

- Update the **Output Schema** table: change the column count from 9 to 10; insert the
  `entity_id` row between `period_end_date` and `metric_name`:

  | Column | Description |
  |--------|-------------|
  | `entity_id` | Value of the entity column (e.g. a `user_id`), or `None` when `by_entity` is not set |

- Update the **Error Handling** table to add:

  | `QueryBuildError` | `by_entity` is set but a metric does not list it in `entities` |

#### `docs/changelog.md`

Add to the `## Unreleased` section:

```
- `MetricSpec`: new optional `entities` field — declares which entity columns the metric
  supports for disaggregation (e.g. `entities: [user_id, device_id]`)
- `MetricCompute.compute()`: new `by_entity` parameter — groups results by an entity column
  declared in each metric's `entities` list; raises `QueryBuildError` if any metric does not
  support the requested entity
- Standard output schema gains an `entity_id` column (position 4, between `period_end_date`
  and `metric_name`); `NULL` when `by_entity` is not set
```

**Note on API reference docs** (`docs/api/specs.md`, `docs/api/insights.md`): these are
auto-rendered from docstrings via `mkdocstrings`. No manual edits are required — they will
pick up the updated `MetricSpec` and `MetricCompute.compute()` docstrings automatically once
the code changes (sub-features 1 and 4) are implemented.

---

## Integration Test Scenario

```python
# Table: transactions
# Columns: user_id (str), device_id (str), amount (float), event_ts (TIMESTAMP)
# Data: 3 users × 2 devices × 3 months (Jan–Mar 2026), 2 rows per combination per month
# Metric: revenue (SUM(amount), timestamp_col='event_ts', entities=['user_id', 'device_id'])
# No slices, no segments
# time_window: ('2026-01-01', '2026-04-01')
# period_type: 'monthly'
```

Assertions — `by_entity='user_id'`:

1. Output has `entity_id` column positioned between `period_end_date` and `metric_name`
2. `entity_id` values are exactly the 3 user IDs; no `NULL`
3. One row per `(user_id, month)` — 3 users × 3 months = 9 rows
4. `metric_value` for each `(user_id, month)` equals the independently computed per-user
   per-month sum of `amount`
5. `slice_type='none'`, `slice_value='all'`, `segment_name='none'`, `segment_value='all'`

Assertions — `by_entity=None` (default):

6. `entity_id` column is `NULL` on all rows
7. 3 rows total (one per month); `metric_value` is sum across all users and devices

Error case:

8. `by_entity='page_id'` (not in `entities`) → `QueryBuildError`

---

## Out of Scope

- Multiple simultaneous entity groupings (e.g. GROUP BY both `user_id` and `device_id`) — Phase 2
- Filtering to a specific entity or set of entities at compute time — Phase 2
- Entity-level metrics combined with segment specs that use a different join key — Phase 2
