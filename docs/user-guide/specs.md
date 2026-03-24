# Writing Specs

Specs are YAML files that declaratively describe *what* you want to measure and *how* to slice the data. There are three spec types: **MetricSpec**, **SliceSpec**, and **SegmentSpec**.

## MetricSpec

A metric defines a single measurable quantity from a source table.

```yaml
metric:
  name: ctr
  description: Click-through rate — ratio of clicks to impressions
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  aggregation: ratio
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier used in `MetricCompute.compute()` |
| `source` | Yes | Source URI — see [Connectors](connectors.md) for format |
| `aggregation` | Yes | Either `"sum"` or `"ratio"` |
| `numerator` | Yes | SQL aggregate expression for the numerator (or sole value for `sum`) |
| `timestamp_col` | Yes | Column used for `time_window` filtering |
| `denominator` | Only for `ratio` | SQL aggregate expression for the denominator |
| `entities` | No | List of entity column names supported for `by_entity` disaggregation (e.g. `[user_id, device_id]`). Must be non-empty if provided. |
| `description` | No | Human-readable description |

### Aggregation types

=== "sum"

    ```yaml
    metric:
      name: total_revenue
      source: duckdb://analytics.db/orders
      aggregation: sum
      numerator: "SUM(revenue)"
      timestamp_col: created_at
    ```

=== "ratio"

    ```yaml
    metric:
      name: cpa
      description: Cost per acquisition
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      aggregation: ratio
      numerator: "SUM(ad_spend)"
      denominator: "SUM(conversions)"
      timestamp_col: date
    ```

### Entity columns

Use `entities` to declare which columns in the source table identify entities that the metric
can be disaggregated by. At compute time, pass `by_entity` to `MetricCompute.compute()` to
select which entity column to group by.

```yaml
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  aggregation: sum
  numerator: "SUM(amount)"
  timestamp_col: event_ts
  entities: [user_id, device_id]   # supports per-user or per-device breakdown
```

A metric without `entities` can still be computed normally — it simply cannot be disaggregated
by entity. See [Computing Metrics](computing-metrics.md#by_entity) for usage.

---

## SliceSpec

A slice defines a breakdown dimension — a set of mutually exclusive (or overlapping) filters applied to the metric query.

### Leaf slice

A leaf slice defines the filter values directly:

```yaml
slice:
  name: campaign_type
  description: Breakdown by ad campaign type
  values:
    - name: Search
      where: "campaign_type = 'Search'"
    - name: Display
      where: "campaign_type = 'Display'"
    - name: Video
      where: "campaign_type = 'Video'"
    - name: Shopping
      where: "campaign_type = 'Shopping'"
```

### Composite slice (cross-product)

A composite slice computes the cross-product of two or more leaf slices:

```yaml
slice:
  name: campaign_type_x_geo
  description: Campaign type broken down by geography
  cross_product:
    - campaign_type
    - geo
```

!!! note
    Composite slices cannot reference other composite slices (no nesting).
    All referenced slices must be loaded into the same `SpecCache`.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `values` | Leaf only | List of `{name, where}` filter definitions |
| `cross_product` | Composite only | List of leaf slice names to cross |
| `description` | No | Human-readable description |

---

## SegmentSpec

A segment is similar to a slice but includes a `source` field — it can filter on a different table than the metric. This is useful when the breakdown dimension lives in a separate table.

```yaml
segment:
  name: platform
  description: Breakdown by advertising platform
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  values:
    - name: Google Ads
      where: "platform = 'Google Ads'"
    - name: Meta Ads
      where: "platform = 'Meta Ads'"
    - name: TikTok Ads
      where: "platform = 'TikTok Ads'"
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `source` | Yes | Source URI for this segment's table |
| `values` | Yes | List of `{name, where}` filter definitions |
| `description` | No | Human-readable description |

---

## Loading specs

Use `SpecCache` to load all specs before computing:

```python
from aitaem import SpecCache

# From directories (loads all *.yaml / *.yml files)
cache = SpecCache.from_yaml(
    metric_paths="metrics/",
    slice_paths="slices/",
    segment_paths="segments/",
)

# From individual files
cache = SpecCache.from_yaml(
    metric_paths=["metrics/ctr.yaml", "metrics/cpa.yaml"],
    slice_paths="slices/campaign_type.yaml",
)

# From YAML strings (useful for testing)
cache = SpecCache.from_string(
    metric_yaml="""
metric:
  name: ctr
  source: duckdb://:memory:/events
  aggregation: ratio
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
""",
)
```
