# Getting Started

## Installation

Install from PyPI:

```bash
pip install aitaem
```

The standard install includes:

- **DuckDB** backend (`ibis-framework[duckdb]`)
- `pandas`, `pyarrow`, `pyyaml`

Optional extras add support for additional backends:

| Extra | Install command | What it adds |
|-------|----------------|--------------|
| `bigquery` | `pip install "aitaem[bigquery]"` | Google BigQuery backend |
| `postgres` | `pip install "aitaem[postgres]"` | PostgreSQL backend |
| `all` | `pip install "aitaem[all]"` | All backends + dev + docs tools |

## Three-Step API

Every aitaem workflow has three steps:

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute

# 1. Load metric, slice, and segment specs from YAML files or directories
cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
    segment_paths="examples/segments/",
)

# 2. Configure the data backend (DuckDB, BigQuery, etc.)
conn = ConnectionManager.from_yaml("examples/connections.yaml")

# 3. Compute metrics
mc = MetricCompute(cache, conn)
df = mc.compute(
    metrics="ctr",
    slices="campaign_type",
    segments="platform",
    time_window=("2024-01-01", "2024-04-01"),
)
print(df)
```

## Standard Output Format

Every `compute()` call returns a pandas DataFrame with exactly these 10 columns:

| Column | Description |
|--------|-------------|
| `period_type` | `"all_time"` or a named period |
| `period_start_date` | ISO date string or `None` |
| `period_end_date` | ISO date string or `None` |
| `entity_id` | Entity column value when `by_entity` is set; `None` otherwise |
| `metric_name` | Name of the metric (e.g. `"ctr"`) |
| `slice_type` | Slice name or `"none"` for the all-data baseline |
| `slice_value` | Slice value (e.g. `"Search"`) or `"all"` |
| `segment_name` | Segment name or `"none"` for the all-data baseline |
| `segment_value` | Segment value (e.g. `"Google Ads"`) or `"all"` |
| `metric_value` | Computed numeric result |

## Example: Ad Campaigns Dataset

The `examples/` directory contains sample YAML specs and a DuckDB dataset for an ad campaigns use case.

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute
from aitaem.helpers import load_csvs_to_duckdb

# Load the sample CSV into a DuckDB database
connector = load_csvs_to_duckdb(
    csv_path="examples/data/ad_campaigns.csv",
    db_path="/tmp/ad_campaigns.duckdb",
)

conn = ConnectionManager()
conn.add_connection("duckdb", connector=connector)

cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
    segment_paths="examples/segments/",
)

mc = MetricCompute(cache, conn)

# CTR by campaign type for Q1 2024
df = mc.compute(
    metrics="ctr",
    slices="campaign_type",
    time_window=("2024-01-01", "2024-04-01"),
)
print(df[["slice_value", "metric_value"]])
```

## Next Steps

- [Writing Specs](user-guide/specs.md) — learn the YAML spec format
- [Connectors](user-guide/connectors.md) — connect to DuckDB, BigQuery, or CSV
- [Computing Metrics](user-guide/computing-metrics.md) — full `MetricCompute` reference
- [API Reference](api/index.md) — complete class and method documentation
