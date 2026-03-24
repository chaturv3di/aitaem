# aitaem Specs

Declarative YAML specification parsing for the aitaem library. Provides strongly-typed, validated Python objects for metrics, slices, and segments.

## Overview

The specs module is a pure parsing and validation layer — no database or Ibis dependencies. It translates YAML definitions into frozen dataclasses that the query module consumes.

**Three spec types**:
- **MetricSpec**: A single measurable quantity with a SQL aggregation expression
- **SliceSpec**: A dimension breakdown — either a leaf (named WHERE conditions) or a composite (cross-product of other slices)
- **SegmentSpec**: A cohort filter applied on top of a metric

**SpecCache**: First-class cache that loads and validates all specs eagerly at construction time, then serves them by name to `MetricCompute`.

Key features:
- Validated eagerly at load time — errors surface before any query runs
- SQL expressions written directly in YAML (no custom DSL)
- Composite slices via `cross_product` for cross-dimensional breakdowns
- Independent of backend connections — spec loading never touches a database

---

## Quick Start

### 1. Define Specs in YAML

```yaml
# metrics/ctr.yaml
metric:
  name: ctr
  description: Click-through rate
  source: duckdb://analytics.db/events
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: event_ts
```

```yaml
# slices/geography.yaml
slice:
  name: geography
  description: Geographic breakdown
  values:
    - name: North America
      where: "country IN ('US', 'CA')"
    - name: Europe
      where: "country IN ('DE', 'FR', 'UK')"
```

```yaml
# segments/premium.yaml
segment:
  name: premium
  description: Premium tier users
  source: duckdb://analytics.db/events
  values:
    - name: premium_users
      where: "subscription_tier = 'premium'"
```

### 2. Load into a SpecCache

```python
from aitaem.specs import SpecCache

cache = SpecCache.from_yaml(
    metric_paths='metrics/',
    slice_paths='slices/',
    segment_paths='segments/',
)
```

### 3. Pass to MetricCompute

```python
from aitaem.connectors import ConnectionManager
from aitaem.insights import MetricCompute

conn_mgr = ConnectionManager.from_yaml('connections.yaml')
mc = MetricCompute(cache, conn_mgr)

df = mc.compute('ctr', slices='geography', segments='premium')
```

---

## YAML Schemas

### MetricSpec

```yaml
metric:
  name: homepage_ctr              # Required: unique identifier
  description: "..."              # Optional
  source: duckdb://db/table       # Required: backend URI (see URI format below)
  numerator: "SUM(clicks)"        # Required: SQL expression with an aggregate function call
  denominator: "SUM(impressions)" # Optional: when present, ratio = numerator / denominator
  timestamp_col: event_date       # Required: date/timestamp column for time_window filtering
```

The aggregation type is inferred from the SQL function in `numerator` (and `denominator`).
There is no separate `aggregation` field. Supported aggregate functions: `SUM`, `AVG`, `COUNT`,
`MIN`, `MAX`. When `denominator` is present, ratio is implied.

**Aggregation types**:

| Type | `numerator` | `denominator` |
|------|-------------|---------------|
| sum | `SUM(col)` | absent |
| count | `COUNT(*)` or `COUNT(DISTINCT col)` | absent |
| avg | `AVG(col)` | absent |
| ratio | Any SQL agg | Any SQL agg (required) |
| min | `MIN(col)` | absent |
| max | `MAX(col)` | absent |

**Source URI format**: `backend://database_identifier/table_name`
- DuckDB: `duckdb://analytics.db/events`
- BigQuery: `bigquery://my-project.dataset.table`

**Examples**:

```yaml
# Simple sum
metric:
  name: total_revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
```

```yaml
# Ratio with CASE WHEN
metric:
  name: homepage_ctr
  source: duckdb://analytics.db/events
  numerator: "SUM(CASE WHEN event_type = 'click' AND page = 'home' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' AND page = 'home' THEN 1 ELSE 0 END)"
```

---

### SliceSpec — Leaf

A leaf SliceSpec defines a set of named filter conditions. Each value maps a label to a SQL `WHERE` clause.

```yaml
slice:
  name: geography                 # Required: unique identifier
  description: "..."              # Optional
  values:                         # Required (leaf): list of named conditions
    - name: North America
      where: "country IN ('US', 'CA', 'MX')"
    - name: Europe
      where: "country IN ('DE', 'FR', 'UK', 'ES')"
    - name: Asia Pacific
      where: "country IN ('CN', 'JP', 'IN', 'AU')"
    - name: Rest of World
      where: "country NOT IN ('US', 'CA', 'MX', 'DE', 'FR', 'UK', 'ES', 'CN', 'JP', 'IN', 'AU')"
```

**Constraints**:
- `values` must be non-empty
- Each `name` within a spec must be unique
- `where` must be a valid SQL boolean expression

---

### SliceSpec — Composite

A composite SliceSpec references two or more leaf specs by name and produces their cross-product. The result has pipe-delimited `slice_type` and `slice_value` columns.

```yaml
slice:
  name: geo_device                # Required: unique identifier
  description: "..."              # Optional
  cross_product:                  # Required (composite): ≥2 leaf slice names
    - geography
    - device_type
```

**Constraints**:
- `cross_product` must name at least 2 other SliceSpecs
- All referenced specs must be leaf specs (nesting composites is not supported in Phase 1)
- `values` and `cross_product` are mutually exclusive

**Output**: A composite `geo_device` slice produces rows with `slice_type='geography|device_type'` and `slice_value='North America|mobile'`, etc.

---

### SegmentSpec

A SegmentSpec defines one or more named cohort filters. Each value maps a label to a SQL `WHERE` clause.

```yaml
segment:
  name: platform                  # Required: unique identifier
  description: "..."              # Optional
  source: duckdb://db/table       # Required: backend URI
  values:                         # Required: list of named filters
    - name: Google Ads
      where: "platform = 'Google Ads'"
    - name: Meta Ads
      where: "platform = 'Meta Ads'"
```

**Note**: In Phase 1, `source` is recorded but the segment's WHERE conditions are applied to the metric's own table — both must share the same columns.

---

## SpecCache

`SpecCache` is the primary entry point for loading specs. It validates all specs eagerly when constructed, so errors surface before any query runs.

### Loading from Files

```python
from aitaem.specs import SpecCache

# From directories (loads all *.yaml / *.yml files)
cache = SpecCache.from_yaml(
    metric_paths='metrics/',
    slice_paths='slices/',
    segment_paths='segments/',
)

# Multiple paths per type (merged, later paths win on name collision)
cache = SpecCache.from_yaml(
    metric_paths=['base/metrics/', 'custom/metrics/'],
    slice_paths='slices/',
)

# Single files
cache = SpecCache.from_yaml(
    metric_paths='metrics/ctr.yaml',
    slice_paths=['slices/geo.yaml', 'slices/device.yaml'],
)
```

### Loading from Strings

Useful for tests or programmatic spec construction:

```python
cache = SpecCache.from_string(
    metric_yaml="""
metric:
  name: ctr
  source: duckdb://analytics.db/events
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
""",
    slice_yaml="""
slice:
  name: geography
  values:
    - name: US
      where: "country = 'US'"
""",
)
```

`from_string` also accepts a list of YAML strings to load multiple specs of the same type:

```python
cache = SpecCache.from_string(
    metric_yaml=[metric_yaml_1, metric_yaml_2],
)
```

### Adding Specs Programmatically

```python
from aitaem.specs import SpecCache, MetricSpec

cache = SpecCache.from_yaml(metric_paths='metrics/')

# Add a spec at runtime — validated immediately
extra_metric = MetricSpec.from_yaml('custom/my_metric.yaml')
cache.add(extra_metric)
```

### Retrieving Specs

```python
metric  = cache.get_metric('ctr')
slice_  = cache.get_slice('geography')
segment = cache.get_segment('platform')
```

Raises `SpecNotFoundError` if the name is not found.

---

## API Reference

### MetricSpec

```
MetricSpec(
    name: str,
    source: str,
    numerator: str,
    timestamp_col: str,
    description: str = "",
    denominator: str | None = None,
    entities: list[str] | None = None,
)
```

Frozen dataclass. All fields are immutable after construction.

**`MetricSpec.from_yaml(yaml_input: str | Path) -> MetricSpec`**

Load from a file path or YAML string. Raises `SpecValidationError` on invalid input.

**`metric.validate() -> ValidationResult`**

Re-validate fields without raising. Returns a `ValidationResult` with any errors.

---

### SliceSpec

```
SliceSpec(
    name: str,
    values: tuple[SliceValue, ...] = (),
    cross_product: tuple[str, ...] = (),
    description: str = "",
)
```

**`SliceSpec.from_yaml(yaml_input: str | Path) -> SliceSpec`**

**`slice_spec.is_composite -> bool`**

`True` if `cross_product` is non-empty; `False` for leaf specs.

**`SliceValue(name: str, where: str)`**

Frozen dataclass representing a single named filter condition within a leaf SliceSpec.

---

### SegmentSpec

```
SegmentSpec(
    name: str,
    source: str,
    values: tuple[SegmentValue, ...],
    description: str = "",
)
```

**`SegmentSpec.from_yaml(yaml_input: str | Path) -> SegmentSpec`**

**`SegmentValue(name: str, where: str)`**

Frozen dataclass representing a single named cohort filter.

---

### SpecCache

**`SpecCache.from_yaml(metric_paths=None, slice_paths=None, segment_paths=None) -> SpecCache`**

Load and validate specs from YAML files or directories. Each argument accepts a single path (str/Path) or a list. Raises `SpecValidationError` on the first invalid spec found.

**`SpecCache.from_string(metric_yaml=None, slice_yaml=None, segment_yaml=None) -> SpecCache`**

Load specs from YAML strings. Each argument accepts a single string or a list of strings.

**`cache.add(spec: MetricSpec | SliceSpec | SegmentSpec) -> None`**

Add a spec programmatically. Validated immediately on addition.

**`cache.get_metric(name: str) -> MetricSpec`**

**`cache.get_slice(name: str) -> SliceSpec`**

**`cache.get_segment(name: str) -> SegmentSpec`**

All `get_*` methods raise `SpecNotFoundError` if the name is not in the cache.

**`cache.clear() -> None`**

Evict all cached specs.

---

### Loader Functions

Lower-level utilities for loading individual specs or scanning directories directly:

**`load_spec_from_file(path, spec_type) -> AnySpec`**

**`load_spec_from_string(yaml_string, spec_type) -> AnySpec`**

**`load_specs_from_directory(directory, spec_type) -> dict[str, AnySpec]`**

Returns a dict of `name → spec`. Files that fail validation are skipped with a warning log.

---

## Error Handling

All errors inherit from `AitaemError`:

**`SpecValidationError`**: Raised when a YAML spec fails validation.
```
SpecValidationError: Invalid metric spec 'homepage_ctr':
  - field 'numerator': 'revenue' must contain an aggregate function (SUM, AVG, COUNT, MIN, MAX)
  - field 'denominator': 'impressions' must contain an aggregate function (SUM, AVG, COUNT, MIN, MAX)
```

**`SpecNotFoundError`**: Raised when a spec name cannot be found in the cache.
```
SpecNotFoundError: No metric named 'revenue' found.
Searched paths: ['metrics/']
```

**`FileNotFoundError`**: Raised when a specified file or directory path does not exist.

```python
from aitaem.utils.exceptions import AitaemError

try:
    cache = SpecCache.from_yaml(metric_paths='metrics/')
except AitaemError as e:
    print(f"Spec loading failed: {e}")
```

---

## Examples

### Load from the Ad Campaigns Example Dataset

```python
from aitaem.specs import SpecCache

cache = SpecCache.from_yaml(
    metric_paths='examples/metrics/',
    slice_paths='examples/slices/',
    segment_paths='examples/segments/',
)

# Inspect what was loaded
ctr    = cache.get_metric('ctr')
geo    = cache.get_slice('geo')
plat   = cache.get_segment('platform')

print(ctr.numerator)        # 'SUM(clicks)'
print(ctr.denominator)      # 'SUM(impressions)'
print(geo.values[0].name)   # 'USA'
print(geo.is_composite)     # False
```

### Define a Composite Slice

```python
from aitaem.specs import SpecCache

cache = SpecCache.from_string(
    slice_yaml=[
        """
slice:
  name: geo
  values:
    - name: US
      where: "country = 'US'"
    - name: EU
      where: "country IN ('DE', 'FR', 'UK')"
""",
        """
slice:
  name: device
  values:
    - name: mobile
      where: "device_type = 'mobile'"
    - name: desktop
      where: "device_type = 'desktop'"
""",
        """
slice:
  name: geo_device
  cross_product:
    - geo
    - device
""",
    ]
)

geo_device = cache.get_slice('geo_device')
assert geo_device.is_composite          # True
assert geo_device.cross_product == ('geo', 'device')
```

### Build a Cache Programmatically

```python
from aitaem.specs import SpecCache, MetricSpec, SliceSpec

cache = SpecCache()

cache.add(MetricSpec.from_yaml("""
metric:
  name: ctr
  source: duckdb://analytics.db/events
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
"""))

cache.add(SliceSpec.from_yaml("""
slice:
  name: region
  values:
    - name: EMEA
      where: "region = 'EMEA'"
    - name: AMER
      where: "region = 'AMER'"
"""))
```

---

## Testing

```bash
# All spec tests
pytest tests/test_specs/ -v

# With coverage
pytest tests/test_specs/ --cov=aitaem.specs --cov-report=term-missing
```

---

## Future Enhancements

Planned for Phase 2:
- Database-backed spec storage (load specs from a table, not just YAML files)
- HAVING clauses and subquery-based segment definitions
- Nested composite slices (composites referencing other composites)
- Remote YAML loading (HTTP/S3)
- Thread-safe SpecCache
