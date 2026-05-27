# Computing Metrics

`MetricCompute` is the primary user interface. It takes a loaded `SpecCache` and a configured `ConnectionManager`, then computes metrics on demand.

## Basic Usage

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute

cache = SpecCache.from_yaml(
    metric_paths="metrics/",
    slice_paths="slices/",
    segment_paths="segments/",
)
conn = ConnectionManager.from_yaml("connections.yaml")
mc = MetricCompute(cache, conn)
```

---

## `compute()` Parameters

```python
df = mc.compute(
    metrics,              # required
    slices=None,          # optional
    segments=None,        # optional
    time_window=None,     # optional
    period_type="all_time",  # optional
    by_entity=None,       # optional
    output_format="pandas",
)
```

### `metrics`

One or more metric names defined in the spec cache.

```python
# Single metric
df = mc.compute(metrics="ctr")

# Multiple metrics
df = mc.compute(metrics=["ctr", "roas", "total_revenue"])
```

### `slices`

One or more slice names. Each slice is computed independently and stacked in the output.

```python
# Single slice
df = mc.compute(metrics="ctr", slices="campaign_type")

# Multiple slices
df = mc.compute(metrics="ctr", slices=["campaign_type", "geo"])
```

### `segments`

One or more segment names. Each segment is computed independently and stacked in the output.

```python
df = mc.compute(metrics="ctr", segments="platform")
```

### `time_window`

An `(start_date, end_date)` tuple of ISO 8601 date strings. Filters rows where `timestamp_col` falls within the window (inclusive).

!!! note
    All metrics in the call must have `timestamp_col` set in their spec when `time_window` is provided.

```python
df = mc.compute(
    metrics="ctr",
    slices="campaign_type",
    time_window=("2024-01-01", "2024-04-01"),
)
```

### `period_type`

Controls time granularity for the output. Accepted values:

| Value | Description |
|-------|-------------|
| `"all_time"` | Single row per metric/slice/segment combination, aggregated over the full `time_window` (or all data when no `time_window` is set). **Default.** |
| `"daily"` | One row per calendar day |
| `"weekly"` | One row per ISO week |
| `"monthly"` | One row per calendar month |
| `"yearly"` | One row per calendar year |

!!! note
    Any value other than `"all_time"` requires `time_window` to be set and every metric in the call to have `timestamp_col` defined in its spec. A `QueryBuildError` is raised otherwise.

```python
from aitaem import PeriodType, VALID_PERIOD_TYPES

# Inspect all valid values
print(VALID_PERIOD_TYPES)  # frozenset({'all_time', 'daily', 'weekly', 'monthly', 'yearly'})

# Monthly breakdown over Q1 2024
df = mc.compute(
    metrics="total_revenue",
    time_window=("2024-01-01", "2024-03-31"),
    period_type="monthly",
)
```

`PeriodType` is a `Literal` type alias for these values and can be used in Pydantic models or type annotations.

### `by_entity`

Group results by an entity column declared in the metric's `entities` field. Use this for
entity-level deep-dives — e.g., revenue per user, sessions per device.

```python
# Ad CTR disaggregated per user (requires metric to declare entities: ['user_id', 'page_id', 'device_id'])
df = mc.compute(
    metrics="ad_ctr",
    by_entity="user_id",
    time_window=("2024-01-01", "2024-04-01"),
    period_type="monthly",
)

# Default — aggregate over all entities (entity_id column is NULL)
df = mc.compute(metrics="ad_ctr")
```

!!! note
    All metrics in the call must list the requested `by_entity` column in their `entities`
    field. A `QueryBuildError` is raised if any metric does not declare it.

### `output_format`

Controls the return type. Currently only `"pandas"` is supported, which returns a `pandas.DataFrame`. This parameter is reserved for future output backends.

---

## Output Schema

Every `compute()` call returns a pandas DataFrame with exactly these 10 columns:

| Column | Description |
|--------|-------------|
| `period_type` | `"all_time"` when no `time_window`, otherwise the period granularity |
| `period_start_date` | Start date ISO string, or `None` |
| `period_end_date` | End date ISO string, or `None` |
| `entity_id` | Value of the entity column (e.g. a `user_id`), or `None` when `by_entity` is not set |
| `metric_name` | Name of the metric |
| `slice_type` | Slice name, or `"none"` for the all-data baseline row |
| `slice_value` | Slice value (e.g. `"Search"`), or `"all"` for the baseline |
| `segment_name` | Segment name, or `"none"` for the all-data baseline row |
| `segment_value` | Segment value (e.g. `"Google Ads"`), or `"all"` for the baseline |
| `metric_value` | Computed numeric result |

---

## Combining Slices and Segments

Slices and segments can be combined freely. Each combination is computed as a separate query group and the results are concatenated:

```python
df = mc.compute(
    metrics=["ctr", "total_revenue"],
    slices=["campaign_type", "geo"],
    segments="platform",
    time_window=("2024-01-01", "2024-07-01"),
)
```

This produces rows for:
- Each metric × all-data baseline (no slice, no segment)
- Each metric × each slice value
- Each metric × each segment value

---

## Error Handling

| Exception | Raised when |
|-----------|-------------|
| `SpecNotFoundError` | A metric, slice, or segment name is not in the cache |
| `QueryBuildError` | `time_window` is set but a metric has no `timestamp_col` |
| `QueryBuildError` | `by_entity` is set but a metric does not list it in `entities` |
| `QueryExecutionError` | All query groups fail to execute |
