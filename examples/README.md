# aitaem Examples

This directory contains worked examples using a real ad campaign performance dataset.
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

| File                           | Metric           | Formula                              |
|--------------------------------|------------------|--------------------------------------|
| `metrics/total_revenue.yaml`   | total_revenue    | `SUM(revenue)`                       |
| `metrics/avg_revenue.yaml`     | avg_revenue      | `AVG(revenue)`                       |
| `metrics/max_revenue.yaml`     | max_revenue      | `MAX(revenue)`                       |
| `metrics/campaign_count.yaml`  | campaign_count   | `COUNT(*)`                           |
| `metrics/ctr.yaml`             | ctr              | `SUM(clicks) / SUM(impressions)`     |
| `metrics/roas.yaml`            | roas             | `SUM(revenue) / SUM(ad_spend)`       |

## Slices

| File                         | Slice          | Values                                              |
|------------------------------|----------------|-----------------------------------------------------|
| `slices/campaign_type.yaml`  | campaign_type  | Search, Display, Video, Shopping                    |
| `slices/industry.yaml`       | industry       | SaaS, E-commerce, EdTech, Fintech, Healthcare       |
| `slices/geo.yaml`            | geo            | USA, EU (UK + Germany), APAC (India + Australia), ROW |

## Segments

| File                      | Segment  | Values                               |
|---------------------------|----------|--------------------------------------|
| `segments/platform.yaml`  | platform | Google Ads, Meta Ads, TikTok Ads     |

> **Note**: The `platform` segment references a `dim_platforms` dimension table that is
> not present in the example DuckDB, so it is excluded from the QueryBot examples.

---

## Examples

| File                                  | Description                                                    |
|---------------------------------------|----------------------------------------------------------------|
| `query_bot_example.py` / `.ipynb`     | Multi-turn QueryBot conversation over the ad campaign dataset  |
| `intent_resolution_example.py` / `.ipynb` | Deep-dive into the three-step intent → resolve → compute flow and prompt-cache efficiency |
| `definition_bot_example.py` / `.ipynb` | DefinitionBot four-step workflow: direct spec parsing (Part 1) and LLM-assisted spec definition (Part 2) |

### QueryBot quick start

```python
from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import QueryBot

spec_cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
)

conn_mgr = ConnectionManager()
conn_mgr.add_connection("duckdb", path="examples/data/ad_campaigns.duckdb")

bot = QueryBot(
    model="anthropic:claude-haiku-4-5-20251001",
    spec_cache=spec_cache,
    connection_manager=conn_mgr,
)

response = await bot.chat("What was total revenue and ROAS across all campaigns?")
print(response.narrative)
```

### DefinitionBot quick start

```python
from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import DefinitionBot

spec_cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
)

conn_mgr = ConnectionManager()
conn_mgr.add_connection("duckdb", path="examples/data/ad_campaigns.duckdb")

bot = DefinitionBot(
    model="anthropic:claude-haiku-4-5-20251001",
    spec_cache=spec_cache,
    connection_manager=conn_mgr,
)

response = await bot.ask(
    "Define a metric called avg_cpc for average cost per click — "
    "total ad spend divided by total clicks."
)
print(response.narrative)
print(response.payload.yaml_string)
```

### MetricCompute (programmatic)

```python
from aitaem import MetricCompute
from aitaem.specs import SpecCache
from aitaem.connectors import ConnectionManager

cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
)
conn_mgr = ConnectionManager.from_yaml("examples/connections.yaml")

mc = MetricCompute(cache, conn_mgr)
result = mc.compute(
    metrics="ctr",
    slices="campaign_type",
    time_window=("2024-01-01", "2024-07-01"),
    period_type="monthly",
)
print(result.to_pandas())
```
