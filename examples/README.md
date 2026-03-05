# aitaem Examples

This directory contains a worked example using a real ad campaign performance dataset.
It demonstrates how to define metrics, slices, and segments using aitaem YAML specs,
and serves as the fixture dataset for integration tests.

---

## Dataset: Global Ads Performance

**File**: `data/ad_campaigns.csv`
**Source**: [Global Ads Performance – Google, Meta, TikTok](https://www.kaggle.com/datasets/nudratabbas/global-ads-performance-google-meta-tiktok) by Nudrat Abbas on Kaggle
**License**: CC0 1.0 Universal (Public Domain Dedication)

### Schema

| Column          | Type    | Description                                      |
|-----------------|---------|--------------------------------------------------|
| `date`          | date    | Campaign date (2024-01-01 to 2024-12-30)        |
| `platform`      | string  | Ad platform: Google Ads, Meta Ads, TikTok Ads   |
| `campaign_type` | string  | Campaign type: Search, Display, Video, Shopping  |
| `industry`      | string  | Industry vertical: SaaS, E-commerce, EdTech, Fintech, Healthcare |
| `country`       | string  | Country: USA, UK, Germany, India, Canada, Australia, UAE |
| `impressions`   | integer | Number of ad impressions served                  |
| `clicks`        | integer | Number of clicks on the ad                       |
| `ad_spend`      | float   | Total ad spend in USD                            |
| `conversions`   | integer | Number of conversions (purchases, sign-ups, etc.)|
| `revenue`       | float   | Revenue attributed to the campaign in USD        |

**Size**: 1,800 rows, 360 days, 3 platforms × 4 campaign types × 5 industries × 7 countries.

---

## Setup

### 1. Create the DuckDB database

Run this once from the project root to create `examples/data/ad_campaigns.duckdb`:

```bash
python examples/data/setup_db.py
```

### 2. Configure connections

The `connections.yaml` in this directory already points to the DuckDB file:

```yaml
duckdb:
  path: examples/data/ad_campaigns.duckdb
```

> **Note**: Run commands from the project root so that relative paths resolve correctly.

---

## Metrics

| File                      | Metric | Formula                              |
|---------------------------|--------|--------------------------------------|
| `metrics/ctr.yaml`        | CTR    | `SUM(clicks) / SUM(impressions)`     |
| `metrics/cpc.yaml`        | CPC    | `SUM(ad_spend) / SUM(clicks)`        |
| `metrics/cpa.yaml`        | CPA    | `SUM(ad_spend) / SUM(conversions)`   |
| `metrics/roas.yaml`       | ROAS   | `SUM(revenue) / SUM(ad_spend)`       |

## Slices

| File                         | Slice           | Values                                      |
|------------------------------|-----------------|---------------------------------------------|
| `slices/campaign_type.yaml`  | campaign_type   | Search, Display, Video, Shopping            |
| `slices/industry.yaml`       | industry        | SaaS, E-commerce, EdTech, Fintech, Healthcare |
| `slices/country.yaml`        | country         | USA, UK, Germany, India, Canada, Australia, UAE |

## Segments

| File                      | Segment  | Values                               |
|---------------------------|----------|--------------------------------------|
| `segments/platform.yaml`  | platform | Google Ads, Meta Ads, TikTok Ads     |

---

## Usage

```python
from aitaem import set_connections
from aitaem.insights import MetricCompute

# Load connections (run from project root)
set_connections('examples/connections.yaml')

# Load specs
mc = MetricCompute.from_yaml(
    metric_paths='examples/metrics/',
    slice_paths='examples/slices/',
    segment_paths='examples/segments/',
)

# Compute CTR sliced by campaign type, segmented by platform
df = mc.compute(
    metrics='ctr',
    slices='campaign_type',
    segments='platform',
    time_window=('2024-01-01', '2024-07-01'),
    timestamp_col='date',
)
print(df)
```
