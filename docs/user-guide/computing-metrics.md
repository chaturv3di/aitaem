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
    metrics,           # required
    slices=None,       # optional
    segments=None,     # optional
    time_window=None,  # optional
    output_format="pandas",
)
```

### `metrics`

One or more metric names defined in the spec cache.

```python
# Single metric
df = mc.compute(metrics="ctr")

# Multiple metrics
df = mc.compute(metrics=["ctr", "cpa", "roas"])
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

---

## Output Schema

Every `compute()` call returns a pandas DataFrame with exactly these 9 columns:

| Column | Description |
|--------|-------------|
| `period_type` | `"all_time"` when no `time_window`, otherwise `"custom"` |
| `period_start_date` | Start date ISO string, or `None` |
| `period_end_date` | End date ISO string, or `None` |
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
    metrics=["ctr", "cpa"],
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
| `QueryExecutionError` | All query groups fail to execute |
