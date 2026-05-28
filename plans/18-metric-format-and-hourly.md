# Plan 18: Metric Format Field and Hourly Period Type

## Overview

This plan introduces two independent enhancements:

1. **`format` field on `MetricSpec`** — a metadata-only string field that indicates how the metric
   value should be interpreted by consumers (e.g. `percentage`, `currency:USD`, `absolute`). Adds a
   `metric_format` column to the standard output. Does not affect computation.

2. **`hourly` period type** — a new value for `PeriodType` enabling sub-daily aggregation. Extends
   `time_window` to accept full ISO datetime strings (in addition to date strings) and outputs
   datetime strings in `period_start_date`/`period_end_date` for hourly periods.

The two features are independent and can be implemented in parallel or sequentially.

---

## Scope

| # | Feature |
|---|---------|
| A1 | `format` field on `MetricSpec` with validation |
| A2 | `metric_format` column injected into SQL output (both `all_time` and non-`all_time` paths) |
| A3 | Tests and docs for `format` |
| B1 | `"hourly"` added to `PeriodType` Literal |
| B2 | Hourly period boundary generation and `time_window` datetime parsing |
| B3 | Tests and docs for `hourly` |

---

## Background & Critical Observations

### Feature A: `format` field

- **Metadata only**: `format` does not change `metric_value` computation. It is a label for
  consumers to decide how to display the value (e.g. append `%`, prepend `$`).

- **Allowed values**:
  - `"percentage"` — e.g. CTR, conversion rate
  - `"absolute"` — plain count or sum with no unit
  - `"ratio"` — dimensionless ratio (not scaled to 100)
  - `"currency"` — monetary value, currency unspecified
  - `"currency:<CODE>"` — monetary value with ISO 4217 currency code (exactly 3 **uppercase**
    letters), e.g. `"currency:USD"`, `"currency:EUR"`, `"currency:GBP"`.
    Lowercase codes (e.g. `currency:usd`) and non-3-letter codes (e.g. `currency:USDX`) are invalid.

- **Optional field**: defaults to `None`. A metric without `format` outputs `NULL` in the
  `metric_format` column. No default is assumed (e.g. the library does NOT default to `"absolute"`).

- **No cross-validation** with metric type: the library does not enforce that ratio metrics use
  `format: ratio`, or that metrics with a denominator use `format: percentage`. This is the
  author's responsibility.

- **Output column placement**: `metric_format` is inserted after `metric_name` in `STANDARD_COLUMNS`.
  This is a **breaking change** for consumers relying on column position or count.

- **Injection mechanism**: the existing `lit()` helper in `_build_metric_segment_query()` already
  handles `None → NULL` and `str → 'str'`. Adding `metric_format` is a one-liner in each SELECT
  block (all_time and non-all_time paths).

### Feature B: `hourly` period type

- **Existing `_generate_period_boundaries()`** uses `date.fromisoformat()` and outputs ISO date
  strings (e.g. `"2024-01-01"`). Hourly periods require datetime strings (e.g.
  `"2024-01-01T14:00:00"`).

- **`_build_periods_cte()` already uses `CAST(... AS TIMESTAMP)`** for all period types. Passing
  datetime strings instead of date strings to the same wrapper is sufficient — no type change needed.

- **`CAST(_period_start AS VARCHAR)` already outputs datetime strings** for non-`all_time` queries
  (DuckDB formats `TIMESTAMP` as `"YYYY-MM-DD HH:MM:SS"`). For non-hourly periods, this currently
  produces `"2024-01-01 00:00:00"` (midnight). For hourly, it will produce `"2024-01-01 14:00:00"`.
  No extra formatting code is required.

- **`time_window` for hourly**: accepts either date strings (`"2024-01-01"`) or datetime strings
  (`"2024-01-01T14:00:00"`). Date strings imply midnight (`T00:00:00`). Sub-hour precision in
  the **start** is truncated to the nearest full hour. Sub-hour precision in the **end** is
  used as-is (the last period whose `period_start < end` is included).

- **Truncating the start** is consistent with how weekly/monthly round the first period boundary
  downward. However it means: if start is `"T14:30:00"`, the first period is `[14:00, 15:00)`,
  which includes 14:00–14:30 data that is before the specified start. Document this.

- **DST / timezones**: the library uses naive datetimes throughout. Hourly periods are
  wall-clock/UTC-naive. No timezone handling is added.

- **Scale concern**: a 30-day hourly window generates 720 periods. With N metrics, S slice
  combos, and G segment combos the output has `720 × N × S × G` rows. Document this.

- **`timestamp_col` type requirement**: for hourly periods, `timestamp_col` must reference a
  TIMESTAMP column in the source table, not a DATE column. The library cannot validate this at
  spec-load time — it is a runtime SQL-engine concern.

---

## Sub-Features

### Part A: `format` field

#### SF-A1 — Add `format` to `MetricSpec` and validation

**Files:** `aitaem/specs/metric.py`, `aitaem/utils/validation.py`

**`MetricSpec` dataclass** — add one optional field at the end:
```python
@dataclass(frozen=True)
class MetricSpec:
    name: str
    source: str
    numerator: str
    timestamp_col: str
    description: str = ""
    denominator: str | None = None
    entities: list[str] | None = None
    format: str | None = None           # NEW
```

**Validation** — add to `validation.py` and call from `validate_metric_spec()`:
```python
import re

_FORMAT_SIMPLE: frozenset[str] = frozenset({"percentage", "absolute", "ratio", "currency"})
_FORMAT_CURRENCY_RE = re.compile(r"^currency:[A-Z]{3}$")

def _is_valid_metric_format(value: str) -> bool:
    return value in _FORMAT_SIMPLE or bool(_FORMAT_CURRENCY_RE.match(value))
```

In `validate_metric_spec()`, after existing field checks:
```python
if (fmt := spec_dict.get("format")) is not None:
    if not isinstance(fmt, str) or not _is_valid_metric_format(fmt):
        raise SpecValidationError(
            f"Invalid format '{fmt}'. Must be one of "
            f"{sorted(_FORMAT_SIMPLE)} or 'currency:<CODE>' where CODE is "
            "a 3-letter uppercase ISO 4217 currency code (e.g. 'currency:USD')."
        )
```

**Validation of SF-A1:**
- Load YAML with every valid `format` value → no error, `spec.format` set correctly
- Load YAML with `format: percentage` → `spec.format == "percentage"`
- Load YAML with `format: "currency:USD"` → `spec.format == "currency:USD"`
- Load YAML without `format` key → `spec.format is None`
- Load YAML with `format: "currency:usd"` → raises `SpecValidationError`
- Load YAML with `format: "currency:USDX"` → raises `SpecValidationError`
- Load YAML with `format: "percent"` → raises `SpecValidationError`
- Load YAML with `format: ""` → raises `SpecValidationError`

---

#### SF-A2 — Inject `metric_format` into query output

**Files:** `aitaem/query/builder.py`, `aitaem/utils/formatting.py`

**`formatting.py`** — add `"metric_format"` to `STANDARD_COLUMNS` after `"metric_name"`:
```python
STANDARD_COLUMNS: list[str] = [
    "period_type",
    "period_start_date",
    "period_end_date",
    "entity_id",
    "metric_name",
    "metric_format",      # NEW
    "slice_type",
    "slice_value",
    "segment_name",
    "segment_value",
    "metric_value",
]
```

**`builder.py`** — in `_build_metric_segment_query()`, add the `metric_format` line to both
`outer_select_cols` blocks (all_time path and non-`all_time` path), using the existing `lit()`
helper which already handles `None → NULL`:

```python
# In both outer_select_cols lists, after the metric_name line:
f"    {lit(metric.format)}                  AS metric_format",
```

Full example (all_time path after change):
```python
outer_select_cols = [
    f"    {lit(period_type)}                AS period_type",
    f"    {lit(period_start)}               AS period_start_date",
    f"    {lit(period_end)}                 AS period_end_date",
    f"    {entity_id_expr}                  AS entity_id",
    f"    '{metric.name}'                   AS metric_name",
    f"    {lit(metric.format)}              AS metric_format",   # NEW
    f"    '{slice_type_val}'                AS slice_type",
    f"    {slice_value_expr}                AS slice_value",
    f"    '{segment_name_val}'              AS segment_name",
    f"    {segment_value_expr}              AS segment_value",
    f"    {metric_value_expr}               AS metric_value",
]
```

**Validation of SF-A2:**
- `compute()` with a spec having `format: percentage` → `df["metric_format"]` is `"percentage"` for all rows
- `compute()` with a spec having `format: "currency:USD"` → `df["metric_format"]` is `"currency:USD"`
- `compute()` with a spec having no `format` → `df["metric_format"]` is `None` (pandas `NaN`/`None`)
- `compute()` mixing two metrics (one with format, one without) → correct per-row values
- `df.columns.tolist()` matches `STANDARD_COLUMNS` exactly (11 columns, `metric_format` at index 5)

---

#### SF-A3 — Tests and documentation for `format`

**Files:**
- `tests/test_specs/` — add format-specific cases to metric spec tests (or new file)
- `tests/test_insights*.py` — integration tests covering SF-A2 scenarios
- `docs/user-guide/specs.md` — add `format` to YAML spec reference
- `docs/api/specs.md` — document `MetricSpec.format` field
- `docs/changelog.md` — add entry under `## Unreleased`
- `aitaem/__init__.py` — export `METRIC_FORMAT_VALUES` constant

**Public export** — add to `aitaem/__init__.py`:
```python
from aitaem.utils.validation import _FORMAT_SIMPLE as METRIC_FORMAT_VALUES
```
Or define a new named public constant in `validation.py`:
```python
METRIC_FORMAT_VALUES: frozenset[str] = frozenset({"percentage", "absolute", "ratio", "currency"})
```
And export it from `aitaem/__init__.py`. Note in docs that `"currency:<CODE>"` is also valid.

---

### Part B: `hourly` period type

#### SF-B1 — Add `"hourly"` to `PeriodType` Literal

**Files:** `aitaem/query/builder.py`

Change line 20:
```python
# Before
PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly"]

# After
PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly", "hourly"]
```

`VALID_PERIOD_TYPES` is derived via `get_args(PeriodType)` and updates automatically.

**Also update the docstring** of `build_queries()` (line 53) to include `'hourly'` in the list.

**Validation of SF-B1:**
- `"hourly" in VALID_PERIOD_TYPES` → `True`
- `build_queries(..., period_type="hourly", time_window=None)` → raises `QueryBuildError`
- `build_queries(..., period_type="hourly", time_window=(...))` with a metric that has no
  `timestamp_col` → raises `QueryBuildError`

---

#### SF-B2 — Hourly period boundary generation and `time_window` datetime parsing

**Files:** `aitaem/query/builder.py`

Add a private helper function:
```python
from datetime import datetime, date, time as time_

@staticmethod
def _parse_window_endpoint_as_datetime(s: str) -> datetime:
    """Parse a date-only or datetime string to a datetime.

    "YYYY-MM-DD" → midnight (T00:00:00).
    Accepts T or space as date/time separator.
    """
    if len(s) == 10:  # "YYYY-MM-DD" only
        return datetime.combine(date.fromisoformat(s), time_(0, 0, 0))
    return datetime.fromisoformat(s)
```

Extend `_generate_period_boundaries()` to handle `"hourly"`:

```python
elif period_type == "hourly":
    start_dt = QueryBuilder._parse_window_endpoint_as_datetime(time_window[0])
    end_dt = QueryBuilder._parse_window_endpoint_as_datetime(time_window[1])
    # Truncate start to nearest full hour; end is used as-is
    start_dt = start_dt.replace(minute=0, second=0, microsecond=0)
    current = start_dt
    while current < end_dt:
        next_period = current + timedelta(hours=1)
        boundaries.append((
            current.strftime("%Y-%m-%dT%H:%M:%S"),
            next_period.strftime("%Y-%m-%dT%H:%M:%S"),
        ))
        current = next_period
```

Remove `"hourly"` from the `else: raise QueryBuildError(...)` branch (the `else` now only
fires for truly unknown values, but since `VALID_PERIOD_TYPES` validation already rejects
unknown values before this function is reached, the branch is unreachable).

The `_build_periods_cte()` method requires no change — it already wraps boundaries in
`CAST('...' AS TIMESTAMP)`, and DuckDB accepts both date strings and ISO datetime strings in that cast.

The `CAST(_period_start AS VARCHAR)` in the outer SELECT also requires no change — DuckDB
formats any TIMESTAMP as `"YYYY-MM-DD HH:MM:SS"`. For hourly, this produces
`"2024-01-01 14:00:00"` (note: space separator, not `T`). This is parseable by
`datetime.fromisoformat()` in all Python ≥ 3.7.

**Validation of SF-B2:**
- `_generate_period_boundaries(("2024-01-01", "2024-01-01T03:00:00"), "hourly")` →
  `[("2024-01-01T00:00:00", "2024-01-01T01:00:00"), ("2024-01-01T01:00:00", "2024-01-01T02:00:00"), ("2024-01-01T02:00:00", "2024-01-01T03:00:00")]`
- `_generate_period_boundaries(("2024-01-01T14:30:00", "2024-01-01T16:30:00"), "hourly")` →
  3 periods: `[14:00,15:00)`, `[15:00,16:00)`, `[16:00,17:00)` (start truncated to 14:00; end 16:30 includes the 16:00 period)
- `_generate_period_boundaries(("2024-01-01T14:00:00", "2024-01-01T16:00:00"), "hourly")` →
  2 periods: `[14:00,15:00)`, `[15:00,16:00)` (end 16:00 is NOT < 16:00 so excluded)
- `_parse_window_endpoint_as_datetime("2024-01-01")` → `datetime(2024, 1, 1, 0, 0, 0)`
- `_parse_window_endpoint_as_datetime("2024-01-01T14:30:45")` → `datetime(2024, 1, 1, 14, 30, 45)`
- Existing non-hourly boundary generation: run full regression to confirm no change

---

#### SF-B3 — Integration test and documentation for `hourly`

**Files:**
- `tests/test_query/test_builder.py` — unit tests for hourly boundaries and datetime parsing
- `tests/test_insights*.py` — integration test: `compute()` with `period_type="hourly"` and
  datetime `time_window` on test data; verify period count, `period_start_date` format, and
  `period_type` column value
- `docs/user-guide/computing-metrics.md` — add `hourly` to the period_type reference table;
  note the `time_window` datetime string support; add scale warning
- `docs/api/insights.md` — update `PeriodType` documentation
- `docs/changelog.md` — add entry under `## Unreleased`

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| `format` defaults to `None`, not `"absolute"` | Defaulting to `"absolute"` would mislabel ratio metrics that don't set a format. `None` is honest. |
| `currency:<CODE>` overloading instead of a separate `currency_code` field | Keeps the spec to one field. The pattern is unambiguous and discoverable. Plain `"currency"` is still valid for cases where the code is irrelevant. |
| `metric_format` placed after `metric_name` in `STANDARD_COLUMNS` | Groups metric metadata (name, format) together before dimensional columns (slice, segment). |
| Use existing `lit()` helper to inject `metric_format` | The helper already handles `None → NULL` and `str → 'str'` correctly. Zero new abstractions needed. |
| `hourly` start truncated to nearest hour | Consistent with weekly/monthly rounding the first period boundary downward to the containing period. |
| `hourly` end used as-is (no truncation) | Consistent with all other period types — the loop runs `while current < end`. Truncating the end could silently drop the user's last requested period. |
| No rename of `period_start_date`/`period_end_date` columns | The columns already contain datetime strings for all non-`all_time` period types (DuckDB `CAST(TIMESTAMP AS VARCHAR)` produces `"YYYY-MM-DD HH:MM:SS"`). The format is richer for hourly periods but the column semantics are unchanged. |
| No guardrail on hourly time-window size | Adds complexity; document the scale concern instead. |
| No timezone support | Consistent with existing implementation. Hourly periods are naive/UTC. |

---

## API Changes

### MetricSpec YAML

```yaml
# Example: percentage metric
metric:
  name: homepage_ctr
  source: duckdb://analytics.db/events
  numerator: "SUM(CASE WHEN event_type = 'click' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' THEN 1 ELSE 0 END)"
  timestamp_col: event_ts
  format: percentage        # NEW — optional

# Example: currency metric with code
metric:
  name: total_revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: transaction_date
  format: "currency:USD"    # NEW — optional

# Example: no format (existing behavior unchanged)
metric:
  name: active_users
  source: duckdb://analytics.db/sessions
  numerator: "COUNT(DISTINCT user_id)"
  timestamp_col: session_start
```

### `MetricCompute.compute()` — signature unchanged

`period_type="hourly"` is now a valid value. `time_window` accepts datetime strings when
`period_type="hourly"`.

```python
# New valid usage:
df = mc.compute(
    metrics="ad_impressions",
    time_window=("2024-01-15T08:00:00", "2024-01-15T18:00:00"),
    period_type="hourly",
)
```

---

## Standard Output Schema (updated)

| Column | Type | Notes |
|--------|------|-------|
| `period_type` | `str` | `"all_time"`, `"daily"`, `"weekly"`, `"monthly"`, `"yearly"`, **`"hourly"`** |
| `period_start_date` | `str \| None` | `"YYYY-MM-DD HH:MM:SS"` for non-`all_time`; `None` for `all_time` without `time_window`. For `all_time` with `time_window`: literal date string from `time_window[0]`. |
| `period_end_date` | `str \| None` | Same format rules as `period_start_date` |
| `entity_id` | `str \| None` | Entity grouping value, or `None` |
| `metric_name` | `str` | `MetricSpec.name` |
| `metric_format` | `str \| None` | **NEW** — one of `"percentage"`, `"absolute"`, `"ratio"`, `"currency"`, `"currency:<CODE>"`, or `None` |
| `slice_type` | `str` | Pipe-delimited slice spec names, or `"none"` |
| `slice_value` | `str` | Pipe-delimited slice value names, or `"all"` |
| `segment_name` | `str` | SegmentSpec name, or `"none"` |
| `segment_value` | `str` | SegmentValue name, or `"all"` |
| `metric_value` | `float` | Computed metric value (unchanged) |

**Total columns: 11** (was 10 before this plan).

---

## SQL Structure

### all_time path (after `metric_format` change)

```sql
WITH _labeled AS (
    SELECT
        *
    FROM ad_campaigns
    WHERE campaign_date >= '2024-01-01' AND campaign_date < '2024-04-01'
)
SELECT
    'all_time'          AS period_type,
    '2024-01-01'        AS period_start_date,
    '2024-04-01'        AS period_end_date,
    NULL                AS entity_id,
    'total_revenue'     AS metric_name,
    'currency:USD'      AS metric_format,     -- NEW (or NULL if not set)
    'none'              AS slice_type,
    'all'               AS slice_value,
    'none'              AS segment_name,
    'all'               AS segment_value,
    SUM(amount)         AS metric_value
FROM _labeled
```

### hourly path (non-`all_time` with `period_type="hourly"`)

```sql
WITH _periods(period_start, period_end) AS (
    VALUES
        (CAST('2024-01-15T08:00:00' AS TIMESTAMP), CAST('2024-01-15T09:00:00' AS TIMESTAMP)),
        (CAST('2024-01-15T09:00:00' AS TIMESTAMP), CAST('2024-01-15T10:00:00' AS TIMESTAMP)),
        -- ... up to 10 rows for an 8–18h window
),
_labeled AS (
    SELECT
        t.*,
        p.period_start AS _period_start,
        p.period_end   AS _period_end
    FROM ad_campaigns t
    JOIN _periods p
      ON CAST(t.event_ts AS TIMESTAMP) >= p.period_start
     AND CAST(t.event_ts AS TIMESTAMP) <  p.period_end
)
SELECT
    'hourly'                            AS period_type,
    CAST(_period_start AS VARCHAR)      AS period_start_date,  -- "2024-01-15 08:00:00"
    CAST(_period_end   AS VARCHAR)      AS period_end_date,
    NULL                                AS entity_id,
    'ad_impressions'                    AS metric_name,
    NULL                                AS metric_format,       -- if no format set
    'none'                              AS slice_type,
    'all'                               AS slice_value,
    'none'                              AS segment_name,
    'all'                               AS segment_value,
    SUM(impressions)                    AS metric_value
FROM _labeled
GROUP BY _period_start, _period_end
```

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/specs/metric.py` | Add `format: str \| None = None` field |
| `aitaem/utils/validation.py` | Add `_FORMAT_SIMPLE`, `_FORMAT_CURRENCY_RE`, `_is_valid_metric_format()`, call from `validate_metric_spec()` |
| `aitaem/utils/formatting.py` | Add `"metric_format"` to `STANDARD_COLUMNS` (after `"metric_name"`) |
| `aitaem/query/builder.py` | Add `metric_format` line to both `outer_select_cols` blocks; add `"hourly"` to `PeriodType`; add `_parse_window_endpoint_as_datetime()`; add `hourly` branch in `_generate_period_boundaries()` |
| `aitaem/__init__.py` | Export `METRIC_FORMAT_VALUES` constant |
| `docs/user-guide/specs.md` | Add `format` field to YAML spec reference |
| `docs/user-guide/computing-metrics.md` | Add `"hourly"` to period_type docs; datetime `time_window` note; scale warning |
| `docs/api/specs.md` | Document `MetricSpec.format` field |
| `docs/api/insights.md` | Update `PeriodType` to include `"hourly"` |
| `docs/changelog.md` | Add entries under `## Unreleased` |
| `tests/test_specs/` | New/updated tests for `format` field validation |
| `tests/test_query/test_builder.py` | Tests for hourly boundary generation and `_parse_window_endpoint_as_datetime()` |
| `tests/test_insights*.py` | Integration tests for `metric_format` in output; integration tests for `period_type="hourly"` |

---

## Testing Strategy

### SF-A1 (format validation)

| Input | Expected |
|-------|----------|
| `format: percentage` | `spec.format == "percentage"` |
| `format: absolute` | `spec.format == "absolute"` |
| `format: ratio` | `spec.format == "ratio"` |
| `format: currency` | `spec.format == "currency"` — valid; means monetary/mixed, no code required |
| `format: "currency:USD"` | `spec.format == "currency:USD"` |
| `format: "currency:EUR"` | `spec.format == "currency:EUR"` |
| No `format` key | `spec.format is None` |
| `format: "currency:usd"` | `SpecValidationError` |
| `format: "currency:USDX"` | `SpecValidationError` |
| `format: "percent"` | `SpecValidationError` |
| `format: ""` | `SpecValidationError` |
| `format: 123` | `SpecValidationError` |

### SF-A2 (metric_format column in output)

- `compute()` with `format: percentage` → `df["metric_format"].eq("percentage").all()`
- `compute()` with no `format` → `df["metric_format"].isna().all()`
- `compute()` with two metrics (one with format, one without) → correct per-metric rows
- `df.columns.tolist() == STANDARD_COLUMNS` (11 columns)

### SF-B1 (`"hourly"` in PeriodType)

- `"hourly" in VALID_PERIOD_TYPES` → `True`
- `build_queries(..., period_type="hourly", time_window=None)` → `QueryBuildError`
- `build_queries(..., period_type="hourly", ...)` with metric missing `timestamp_col` → `QueryBuildError`

### SF-B2 (hourly boundaries)

- `("2024-01-01", "2024-01-01T03:00:00")` → 3 periods starting at midnight
- `("2024-01-01T14:30:00", "2024-01-01T16:30:00")` → 3 periods `[14:00,15:00)`, `[15:00,16:00)`, `[16:00,17:00)`
- `("2024-01-01T14:00:00", "2024-01-01T16:00:00")` → 2 periods (16:00 end excludes the 16:00 start period)
- Single-hour window: `("2024-01-01T10:00:00", "2024-01-01T11:00:00")` → 1 period
- `_parse_window_endpoint_as_datetime("2024-01-01")` → `datetime(2024, 1, 1, 0, 0, 0)`
- `_parse_window_endpoint_as_datetime("2024-01-01T14:30:45")` → `datetime(2024, 1, 1, 14, 30, 45)` (no truncation in the parser; truncation is done at the call site for the start endpoint)

### SF-B3 (integration)

- `compute()` with `period_type="hourly"` and a 4-hour `time_window` → 4 rows (no slices/segments)
- `df["period_type"].eq("hourly").all()` → `True`
- `df["period_start_date"]` values match pattern `\d{4}-\d{2}-\d{2} \d{2}:00:00`
- Regression: all existing tests pass (no column-count regressions beyond `metric_format`)

### Final regression

```bash
python -m pytest --cov=aitaem -v
```

All pre-existing tests must pass. Any test asserting `len(STANDARD_COLUMNS) == 10` must be
updated to `11`. Any test asserting exact column order must be updated to include `metric_format`.

---

## Edge Cases

| Edge Case | Handling |
|-----------|----------|
| `format: "currency:usd"` (lowercase code) | `SpecValidationError` at load time |
| `format: "currency:USDX"` (4-letter code) | `SpecValidationError` at load time |
| `format: "currency:"` (empty code) | `SpecValidationError` — fails regex `[A-Z]{3}` |
| `format` not present | `spec.format = None`; output `metric_format` column = `None` |
| `period_type="hourly"` without `time_window` | `QueryBuildError` (existing validation path) |
| `time_window` start equals end for hourly | Zero periods generated — empty result (same as other types) |
| `time_window` start > end for hourly | Zero periods generated (no iterations; `current < end` is false immediately) |
| Hourly `time_window` start has sub-hour precision | Start truncated to nearest full hour; first period may include data before the specified start |
| Hourly `time_window` end has sub-hour precision | End used as-is; last period included if `period_start < end` |
| `timestamp_col` is a DATE column for hourly | Runtime SQL error from the engine — not validated by the library |
| Very large hourly window (e.g. 90 days = 2160 periods) | Supported; user is responsible for size |
| `by_entity` combined with `hourly` | Supported — no special case needed |
| Two metrics with different `format` values in one `compute()` | Correct per-row values; output correctly interleaves the rows |
| `all_time` period type with datetime strings in `time_window` | Supported — the `lit()` injection in the all_time path emits them as-is as string literals in `period_start_date`/`period_end_date` |

---

## Out of Scope

- Currency display logic (symbol, rounding, locale) — consumer responsibility
- Timezone-aware hourly periods — deferred
- `minute` or `second` period types — deferred
- Cross-validation between `format` and metric type (e.g. ratio metric should use `format: ratio`) — deferred
- `currency_code` as a separate spec field — superseded by `currency:<CODE>` syntax
- Max-period guardrail for hourly — deferred
- `format` field on `SliceSpec` or `SegmentSpec` — not applicable

---

## Documentation Changes

### `docs/changelog.md` — under `## Unreleased`

```markdown
### Added
- `MetricSpec.format` — optional metadata field for metric value interpretation.
  Allowed values: `percentage`, `absolute`, `ratio`, `currency`, `currency:<CODE>`.
  Adds `metric_format` column to the standard output.
- `hourly` period type. `time_window` now accepts full ISO datetime strings
  (e.g. `"2024-01-15T08:00:00"`) for sub-daily granularity.
  `period_start_date` / `period_end_date` contain datetime strings
  (format `"YYYY-MM-DD HH:MM:SS"`) when `period_type="hourly"`.
- `METRIC_FORMAT_VALUES` exported from `aitaem` — frozenset of the simple format values.

### Changed
- `STANDARD_COLUMNS` now has 11 entries. `metric_format` is inserted after `metric_name`.
  **Breaking change** for consumers relying on column position or count.
```

### `docs/api/specs.md`

Add a `format` field entry under `MetricSpec` with:
- Allowed values table (simple values + `currency:<CODE>` pattern)
- Example YAML snippets
- Note: metadata only, no effect on `metric_value`

### `docs/api/insights.md`

Update the `PeriodType` / `period_type` parameter documentation to include `"hourly"` with a note:
- `time_window` accepts full datetime strings for hourly
- Output `period_start_date`/`period_end_date` format for hourly
- Scale warning (720 periods per 30-day window)

### `docs/user-guide/specs.md`

Add `format` to the YAML spec reference with example and allowed values.

### `docs/user-guide/computing-metrics.md`

Add `"hourly"` to the period type comparison table. Add a section on datetime `time_window`
strings and a callout box on scale.
