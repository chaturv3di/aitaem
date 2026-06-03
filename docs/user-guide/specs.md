# Writing Specs

Specs are YAML files that declaratively describe *what* you want to measure and *how* to slice the data. There are three spec types: **MetricSpec**, **SliceSpec**, and **SegmentSpec**.

## MetricSpec

A metric defines a single measurable quantity from a source table.

```yaml
metric:
  name: ctr
  description: Click-through rate — ratio of clicks to impressions
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier used in `MetricCompute.compute()` |
| `source` | Yes | Source URI — see [Connectors](connectors.md) for format |
| `numerator` | Yes | SQL expression containing an aggregate function call (`SUM`, `AVG`, `COUNT`, `MIN`, `MAX`) |
| `timestamp_col` | Yes | Column used for `time_window` filtering |
| `denominator` | No | SQL expression containing an aggregate function call. When present, the metric is computed as `numerator / denominator` (ratio). |
| `entities` | No | List of entity column names supported for `by_entity` disaggregation (e.g. `[user_id, device_id]`). Must be non-empty if provided. |
| `format` | No | Value interpretation hint for consumers. See [Format field](#format-field) below. |
| `description` | No | Human-readable description |

The aggregation type is inferred from the SQL function in `numerator` (and `denominator`). There is no separate `aggregation` field — write the aggregate directly in the expression.

### Aggregation types

=== "ratio"

    Ratio is implied by the presence of a `denominator`. Both `numerator` and `denominator` must
    contain an aggregate function.

    ```yaml
    metric:
      name: ctr
      description: Click-through rate — ratio of clicks to impressions
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      numerator: "SUM(clicks)"
      denominator: "SUM(impressions)"
      timestamp_col: date
    ```

=== "sum"

    ```yaml
    metric:
      name: total_revenue
      description: Total revenue generated across all campaigns
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      numerator: "SUM(revenue)"
      timestamp_col: date
    ```

=== "avg"

    ```yaml
    metric:
      name: avg_revenue
      description: Average revenue per campaign row
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      numerator: "AVG(revenue)"
      timestamp_col: date
    ```

=== "count"

    ```yaml
    metric:
      name: campaign_count
      description: Number of campaign rows
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      numerator: "COUNT(*)"
      timestamp_col: date
    ```

=== "max"

    ```yaml
    metric:
      name: max_revenue
      description: Peak revenue from a single campaign row
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      numerator: "MAX(revenue)"
      timestamp_col: date
    ```

=== "min"

    ```yaml
    metric:
      name: min_ad_spend
      description: Lowest ad spend entry
      source: duckdb://ad_campaigns.duckdb/ad_campaigns
      numerator: "MIN(ad_spend)"
      timestamp_col: date
    ```

### Format field

The optional `format` field is a metadata hint that tells consumers how to interpret the
`metric_value`. It does **not** affect computation — the value is stored as-is.

| Value | Meaning |
|-------|---------|
| `percentage` | A proportion expressed as a decimal (e.g. 0.42 for 42%) |
| `absolute` | A plain count or sum with no unit |
| `ratio` | A dimensionless ratio that is not a percentage |
| `currency` | A monetary value where the currency is unspecified or mixed across rows |
| `currency:<CODE>` | A monetary value in a specific ISO 4217 currency. `<CODE>` must be 3 uppercase letters (e.g. `currency:USD`, `currency:EUR`). |

```yaml
metric:
  name: ctr
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
  format: percentage

metric:
  name: total_revenue
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "SUM(revenue)"
  timestamp_col: date
  format: "currency:USD"
```

When `format` is set, the `metric_format` column in the output DataFrame carries the value.
When omitted, `metric_format` is `None`.

Use `METRIC_FORMAT_VALUES` to enumerate the simple (non-currency-code) allowed values:

```python
from aitaem import METRIC_FORMAT_VALUES
print(METRIC_FORMAT_VALUES)  # frozenset({'percentage', 'absolute', 'ratio', 'currency'})
```

### Entity columns

Use `entities` to declare which columns in the source table identify entities that the metric
can be disaggregated by. At compute time, pass `by_entity` to `MetricCompute.compute()` to
select which entity column to group by.

```yaml
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: event_ts
  entities: [user_id, device_id]   # supports per-user or per-device disaggregation
```

A metric without `entities` can still be computed normally — it simply cannot be disaggregated
by entity. See [Computing Metrics](computing-metrics.md#by_entity) for usage.

---

## SliceSpec

A slice defines a breakdown dimension — a set of mutually exclusive (or overlapping) filters applied to the metric query.

### Leaf slice

A leaf slice defines the filter values for a single column dimension. There are two variants:

**Value-list** — enumerate explicit filter predicates:

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

**Wildcard** — auto-discover distinct values from a column at query time by setting `where` at
the spec level to a bare column name:

```yaml
slice:
  name: industry
  where: industry
```

Dot-qualified column names are also accepted:

```yaml
slice:
  name: country
  where: public.campaigns.country
```

!!! note
    `where` at the spec level must be a bare column name — no spaces, no SQL expressions.
    Use the value-list form when you need explicit `WHERE` predicates.

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
| `values` | Leaf (value-list) | List of `{name, where}` filter definitions |
| `where` | Leaf (wildcard) | Bare column name whose distinct values are auto-discovered at query time. Dot-qualified names (e.g. `schema.table.column`) are accepted. |
| `cross_product` | Composite only | List of leaf slice names to cross |
| `description` | No | Human-readable description |

---

## SegmentSpec

A segment classifies fact rows by joining to a **DIM table** (dimension table). Unlike slices, which apply filter predicates directly to the fact table, a segment resolves entity attributes from a separate DIM table — the standard star-schema pattern where classification attributes (e.g., subscription tier, region, plan type) live in their own table.

```yaml
segment:
  name: platform
  description: Breakdown by advertising platform
  source: duckdb://ad_campaigns.duckdb/dim_platforms
  entity_id: platform
  values:
    - name: Google Ads
      where: "platform = 'Google Ads'"
    - name: Meta Ads
      where: "platform = 'Meta Ads'"
    - name: TikTok Ads
      where: "platform = 'TikTok Ads'"
```

At compute time, aitaem generates a JOIN from the fact table to the DIM table on the entity key:

```sql
FROM fact_table t
JOIN dim_platforms _dim ON t.<join_key> = _dim.platform
```

The `<join_key>` is resolved at `compute()` time — see [`segments` parameter](computing-metrics.md#segments).

!!! note
    `values[].where` expressions are evaluated against the DIM table (aliased as `_dim`).
    Unqualified column references are automatically qualified — `"platform = 'Google Ads'"` becomes
    `_dim.platform = 'Google Ads'` in the generated SQL. You do **not** need to write the prefix yourself.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `source` | Yes | URI of the DIM table to join |
| `entity_id` | Yes | Primary key column on the DIM table. Used as the right-hand side of the JOIN ON condition (`_dim.<entity_id>`). |
| `values` | Yes | List of `{name, where}` filter definitions against DIM table columns |
| `join_keys` | No | Whitelist of fact-table FK columns accepted as join keys. When non-empty, the join key supplied in `compute()` must be in this list. When empty (default), any column name is accepted. |
| `description` | No | Human-readable description |

### Example — customer value tier

```yaml
segment:
  name: customer_value
  description: Customer segmentation by lifetime value tier
  source: duckdb://analytics.db/dim_customers
  entity_id: customer_id
  join_keys: [buyer_id, seller_id]
  values:
    - name: high_value
      where: "lifetime_value > 1000"
    - name: low_value
      where: "lifetime_value <= 1000"
```

Here `customer_id` is the PK on `dim_customers`, and the `join_keys` whitelist restricts the caller to `buyer_id` or `seller_id` as the fact-table FK. At compute time you specify which one applies to the query (see [`segments` parameter](computing-metrics.md#segments)).

---

## Column introspection

After loading a spec you can inspect every column it references without needing a warehouse
connection. This is useful for downstream validation — for example, verifying that every
referenced column exists in the source table before running `compute()`.

```python
result = metric_spec.validate()
if result.valid:
    print(result.referenced_columns)
    # {
    #   'numerator':     ['revenue'],
    #   'denominator':   ['impressions'],
    #   'timestamp_col': ['created_at'],
    #   'entities':      ['user_id'],
    # }
```

For slice specs:

```python
result = slice_spec.validate()
if result.valid:
    print(result.referenced_columns)
    # {'values[0].where': ['region'], 'values[1].where': ['region', 'country']}
```

For segment specs:

```python
result = segment_spec.validate()
if result.valid:
    print(result.referenced_columns)
    # {
    #   'entity_id':       ['customer_id'],
    #   'join_keys':       ['buyer_id', 'seller_id'],   # omitted when join_keys is empty
    #   'values[0].where': ['lifetime_value'],
    #   'values[1].where': ['lifetime_value'],
    # }
```

!!! note
    `referenced_columns` is `None` when the spec is invalid — always check `result.valid` first.

    Column names are unqualified: `SUM(t.revenue)` yields `"revenue"`. For wildcard slices,
    the bare column name is returned directly. For composite slices, an empty dict `{}` is
    returned (composite specs contain no SQL expressions).

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
    metric_paths=["metrics/ctr.yaml", "metrics/total_revenue.yaml"],
    slice_paths="slices/campaign_type.yaml",
)

# From YAML strings (useful for testing)
cache = SpecCache.from_string(
    metric_yaml="""
metric:
  name: ctr
  source: duckdb://:memory:/events
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
""",
)
```

---

## Compatibility Scanning

Before calling `compute()`, use `MetricCompute.scan()` to check which slices and segments are
usable with each metric. The scan introspects source table schemas and returns a
`ScanResult` with one `CompatibilityResult` per metric × spec pair.

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute

cache = SpecCache.from_yaml(
    metric_paths="metrics/",
    slice_paths="slices/",
    segment_paths="segments/",
)
conn = ConnectionManager.from_yaml("connections.yaml")
mc = MetricCompute(cache, conn)

result = mc.scan()

# Slices compatible with the "ctr" metric
compatible_slices = result.compatible_slices("ctr")
# e.g. ["campaign_type", "country"]

# Segments compatible with the "ctr" metric
compatible_segs = result.compatible_segments("ctr")
# e.g. ["platform"]

# Pass scan results directly into compute()
df = mc.compute(
    metrics="ctr",
    slices=compatible_slices,
    segments=compatible_segs[0] if compatible_segs else None,
)
```

For slices, the scan checks that every column referenced in `values[].where` (or the bare
`column` field for wildcard slices) exists in the metric's source table.

For segments, only the **fact-table side** is checked: the scan verifies that at least one join
key candidate (`join_keys`, or `entity_id` when `join_keys` is empty) exists in the metric's
source table. The DIM-table columns (used in `values[].where`) are on the segment's own source
and are not validated here.

```python
# Inspect a specific result
r = result.for_spec("platform")[0]
print(r.compatible)        # True
print(r.valid_join_keys)   # ["platform"]
print(r.missing_columns)   # []

# Find which metrics an incompatible slice is blocking
r = result.for_spec("bad_slice")[0]
print(r.compatible)        # False
print(r.missing_columns)   # ["nonexistent_column"]
print(r.reason)            # "columns not found in source table: ['nonexistent_column']"
```
