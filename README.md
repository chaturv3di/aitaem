# AITÆM: All Interesting Things Are Essentially Metrics

[![CI](https://github.com/chaturv3di/aitaem/actions/workflows/ci.yml/badge.svg)](https://github.com/chaturv3di/aitaem/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/aitaem)](https://pypi.org/project/aitaem/)
[![Python versions](https://img.shields.io/pypi/pyversions/aitaem)](https://pypi.org/project/aitaem/)
[![Docs](https://img.shields.io/badge/docs-github.io-blue)](https://chaturv3di.github.io/aitaem)

This is the `aitaem` library, pronounced "i-tame".

## Why?
> **TL;DR:** Point this Python library toward your OLAP database or a local CSV file and start generating insights, i.e. metrics, slices, segments, and time series, in no time. This library is LLM friendly (more on that later).

Business leaders, PMs, EMs, and even individual contributors constantly require deep understanding of their businesses and products. Another term for this understanding is "data insights". The most common way to obtain these insights is to rely on a data scientist or a business analyst to dive into the data and compute the insights. Why is this a bad idea?

1. Practically: Dashboards are limited in how much they can hold apriori. There's always a new question which existing dashboards cannot answer. There are always either too many dashboards or too few
2. Operationally: It is a waste of time if a DS or a BA has to dive into the source tables and (re)write SQL queries to compute customized metrics, slices, or segments repeatedly
3. Scientifically: The accuracy of ad-hoc analysis depends upon the individual; the same analysis done by different individuals can yield different results
4. Organisationally: Inter-org trust should be built upon _processes/toolings_ rather than on _individuals_

## What?
This library provides powerful functionality in a compact API. The core consists of two componenents.

1. Specifications: A simple declarative structure to modularly define metric specs, slice/breakdown specs, and segment specs
2. Computation: A small collection of Python classes with compact signatures which compute the metrics

Additionally, there are utilities to connect to various data backends (simultaneously) and helpers to visualize/render charts and trends.

## Quick Start

### Installation

```bash
pip install aitaem
```

### Three-Step API

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute

# 1. Load metric, slice, and segment specs from YAML files or directories
cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
    segment_paths="examples/segments/",
)

# 2. Configure the data backend (DuckDB, BigQuery, etc.)
conn = ConnectionManager()
conn.add_connection("duckdb", database=":memory:")  # or path to .duckdb file

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

### Standard Output Format

Every `compute()` call returns a pandas DataFrame with exactly these 9 columns:

| Column | Description |
|--------|-------------|
| `period_type` | `"all_time"` or a named period |
| `period_start_date` | ISO date string or `None` |
| `period_end_date` | ISO date string or `None` |
| `metric_name` | Name of the metric (e.g. `"ctr"`) |
| `slice_type` | Slice name or `"none"` for the all-data baseline |
| `slice_value` | Slice value (e.g. `"Search"`) or `"all"` |
| `segment_name` | Segment name or `"none"` for the all-data baseline |
| `segment_value` | Segment value (e.g. `"Google Ads"`) or `"all"` |
| `metric_value` | Computed numeric result |

### Example: Ad Campaigns Dataset

The `examples/` directory contains sample YAML specs and a CSV dataset for an ad campaigns use case. You can run the following end-to-end:

```python
from aitaem import SpecCache, ConnectionManager, MetricCompute
from aitaem.helpers import load_csvs_to_duckdb

# Load the sample CSV into a DuckDB file and get back a connector
connector = load_csvs_to_duckdb("examples/data/ad_campaigns.csv", "examples/data/ad_campaigns.duckdb")

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
