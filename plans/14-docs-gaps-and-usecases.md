# Plan 14: Documentation Gaps, Inconsistencies, and Use-Cases Page

## Overview

Audit of all current documentation pages under `docs/` against the actual implementation and the
conceptual model. This plan catalogues every gap and inconsistency found, proposes the exact fixes,
and specifies a new **Use Cases** page to add to the site.

No documentation changes are made until this plan is approved.

---

## 1. Gaps & Inconsistencies Found

### 1.1 `docs/getting-started.md` — Installation section

**Problem:** The installation section only mentions the `bigquery` optional extra. Users have no
idea what is included in the standard install, and `postgres` is entirely undiscovered.

**Proposed fix:**

Replace the current installation section with:

```markdown
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
```

---

### 1.2 `docs/user-guide/specs.md` — `entities` field in all MetricSpec examples

**Problem:** Every MetricSpec example in this page (introductory and per-aggregation-type) uses:

```yaml
entities: [platform, campaign_type, country]
```

`platform`, `campaign_type`, and `country` are **dimensional / slice columns**, not entity
identifiers. The `entities` field exists to declare columns that identify discrete entities (e.g.
users, devices, pages) for per-entity disaggregation. Using slice/dimension columns here:

- Contradicts the field description ("List of entity column names supported for `by_entity`
  disaggregation (e.g. `[user_id, device_id]`)")
- Creates a conceptual mismatch for any reader learning both features simultaneously
- Is inconsistent with the "Entity columns" sub-section which correctly describes entities as
  `user_id`, `device_id`, etc.

**Root cause:** The ad_campaigns example dataset (`examples/data/ad_campaigns.csv`) contains
only dimensional columns (`platform`, `campaign_type`, `industry`, `country`) and no proper entity
ID columns. The `entities` field was populated with dimensional columns as a placeholder.

**Proposed fix:**

- In the **introductory MetricSpec YAML** (the first `ctr` example near the top of the page):
  Remove the `entities` field entirely, since the ad_campaigns dataset has no entity ID columns.
  Add a comment explaining that `entities` is optional and shown separately.

- In **all per-aggregation-type YAML blocks** (`sum`, `avg`, `count`, `max`, `min`, `ratio`):
  Remove `entities: [platform, campaign_type, country]` from each block.

- In the **"Entity columns" sub-section** (already correct in prose): Keep the prose unchanged.
  Update the illustrative YAML example to use a *different* source/table reference that clearly
  implies a user-centric dataset:

  ```yaml
  metric:
    name: revenue
    source: duckdb://analytics.db/transactions
    numerator: "SUM(amount)"
    timestamp_col: event_ts
    entities: [user_id, device_id]   # supports per-user or per-device disaggregation
  ```

  This is already the correct example in the sub-section — it just needs to stay decoupled from
  the ad_campaigns examples above it.

**Files affected:** `docs/user-guide/specs.md`

---

### 1.3 `examples/metrics/*.yaml` — Incorrect `entities` values

**Problem:** All six YAML files under `examples/metrics/` carry:

```yaml
entities: [platform, campaign_type, country]
```

These are referenced in the docs and used in integration tests. They have the same conceptual
problem as 1.2 above.

**Proposed fix:** Remove the `entities` field from all six example metric YAML files
(`ctr.yaml`, `total_revenue.yaml`, `avg_revenue.yaml`, `campaign_count.yaml`,
`max_revenue.yaml`, `roas.yaml`). The ad_campaigns dataset has no true entity ID columns so
the field should simply be absent. This is a valid state — a metric without `entities` can still
be fully computed; it just cannot be used with `by_entity`.

**Files affected:** `examples/metrics/*.yaml`

---

### 1.4 `docs/user-guide/computing-metrics.md` — `by_entity` example inconsistency

**Problem:** The `by_entity` section demonstrates:

```python
df = mc.compute(
    metrics="total_revenue",
    by_entity="platform",
    ...
)
```

with the comment "Total revenue disaggregated by platform". If we remove `platform` from
`entities` in the example metric specs (fix 1.3), this example becomes technically invalid when
run against the example specs. More importantly, it reinforces the incorrect conceptual model
that dimensional columns are valid entity IDs.

**Proposed fix:** Change the `by_entity` example to use a clearly entity-centric framing
(separate from the ad_campaigns dataset) — e.g. a `sessions` metric with `entities: [user_id]`
and `by_entity="user_id"`. Use a brief comment noting this is a schematic example with a
user-scoped dataset, not the ad_campaigns dataset.

```python
# Ad CTR disaggregated per user (requires metric to declare entities: ['user_id', 'page_id', 'device_id'])
df = mc.compute(
    metrics="ad_ctr",
    by_entity="user_id",
    time_window=("2024-01-01", "2024-04-01"),
    period_type="monthly",
)
```

**Files affected:** `docs/user-guide/computing-metrics.md`

---

### 1.5 `docs/index.md` — Homepage does not mention PostgreSQL

**Problem:** The homepage prose mentions "OLAP databases or local CSV files" but the supported
backends table on the Connectors page includes PostgreSQL. No mention of available backends on the
home page.

**Proposed fix:** Minor addition — either add a one-liner listing supported backends
(DuckDB, BigQuery, PostgreSQL) in the "What?" section, or leave it to the Getting Started page
where the enhanced installation table (fix 1.1) now makes this visible. Lean toward a minimal
mention so the homepage stays concise. Suggested addition:

> Supported backends: **DuckDB** (built-in), **BigQuery**, and **PostgreSQL** (optional extras).

**Files affected:** `docs/index.md` *(low priority — Getting Started fix is sufficient)*

---

### 1.6 Missing nav entry — Use Cases page

The `mkdocs.yml` nav has no Use Cases section. This will be added as part of fix 1.7 below.

---

## 2. New Page: Use Cases

### 2.1 Proposed location and nav

- **File:** `docs/use-cases.md`
- **Nav entry:** Add between "Getting Started" and "User Guide" in `mkdocs.yml`:

  ```yaml
  nav:
    - Home: index.md
    - Getting Started: getting-started.md
    - Use Cases: use-cases.md     # NEW
    - User Guide:
        ...
  ```

### 2.2 Proposed content outline

Below is the complete outline with expanded descriptions for each use case. The actual page
will follow this structure.

---

#### Use Case 1 — Reporting & Analytics

**Who:** Data analysts, business intelligence teams, product managers.

**Problem:** Dashboards answer fixed questions. Every new stakeholder question requires an analyst
to write custom SQL, leading to repeated work, inconsistent metric definitions across teams, and a
backlog that never shrinks.

**How aitaem helps:**

- Define every metric once in a YAML spec — `numerator`, `denominator`, `timestamp_col`,
  and the slices that matter.
- Any analyst or engineer can call `MetricCompute.compute()` to answer "CTR by campaign type for
  Q1" or "revenue by country and platform" without touching SQL.
- Slice and segment combinations produce the full breakdown matrix in a single call, output as a
  tidy pandas DataFrame ready for charting libraries (matplotlib, Plotly, Streamlit, etc.).
- `period_type` (monthly, weekly, quarterly) turns any metric into a time-series with no
  additional code.

**Snippet:**

```python
df = mc.compute(
    metrics=["ctr", "roas"],
    slices=["campaign_type", "geo"],
    segments="platform",
    time_window=("2024-Q1-01", "2024-04-01"),
    period_type="monthly",
)
```

---

#### Use Case 2 — Entity-Level Datasets for ML Model Training

**Who:** ML engineers and data scientists building predictive models that require per-entity
feature tables.

**Problem:** ML models for churn prediction, LTV estimation, personalisation, or recommendation
all require a "wide" feature table where each row is one entity (user, device, product) and each
column is an aggregate measure (clicks last 30 days, average revenue per session, etc.). Building
these tables by hand involves bespoke SQL per feature, repeated for every training run.

**How aitaem helps:**

- Declare metrics with `entities: [user_id]` to mark which entity column the metric can be
  disaggregated by.
- Call `compute()` with `by_entity="user_id"` to get one row per entity per metric, then
  pivot into a wide feature table.
- `period_type` with `time_window` produces rolling-window features (e.g. monthly revenue per
  user over the past year) directly.
- The output schema is fixed and tidy — easy to feed into pandas `.pivot_table()` or directly
  into a training data pipeline.

**Snippet:**

```python
# Per-user monthly revenue — feed directly into LTV model training
df = mc.compute(
    metrics="revenue",
    by_entity="user_id",
    time_window=("2023-01-01", "2024-01-01"),
    period_type="monthly",
)
# Pivot → one row per user, one column per month
feature_table = df.pivot_table(
    index="entity_id",
    columns="period_start_date",
    values="metric_value",
)
```

---

#### Use Case 3 — Authoritative Metric Service for Product Applications

**Who:** Backend engineers and platform teams building products or internal tools that need
real-time or on-demand access to business metrics.

**Problem:** Multiple services (dashboards, notification systems, ranking algorithms, pricing
engines) each re-implement their own SQL to compute "the same" KPIs. Over time, definitions
diverge, bugs multiply, and no single source of truth exists.

**How aitaem helps:**

- Host metric specs as versioned YAML files in a repository — every service references the
  same definitions.
- Wrap `MetricCompute` in a lightweight API layer (FastAPI, Flask, gRPC) to expose metrics as
  a service. Any application that needs authoritative KPIs calls the service rather than
  re-implementing SQL.
- `ConnectionManager.from_yaml()` reads connection credentials from environment variables at
  runtime — no credentials baked into application code.
- Query results are returned as pandas DataFrames and can trivially be serialized to JSON, Parquet,
  or Arrow for downstream consumers.

**Sketch:**

```
YAML specs (git repo)
       │
       ▼
MetricCompute (Python service)
       │
       ├──► Dashboard (Streamlit / Grafana)
       ├──► Notification system
       ├──► Pricing engine
       └──► Data warehouse export (Parquet/BigQuery)
```

---

#### Use Case 4 — Semantic Glue for External Data Sources

**Who:** Data engineers and analytics engineers integrating data from SaaS platforms (Salesforce,
HubSpot, Stripe, etc.) into an OLAP layer.

**Problem:** External SaaS tools expose raw APIs or exports. Teams write one-off scripts to pull
data, clean it, and load it into a warehouse. The metric logic lives in ad-hoc notebooks and is
not reusable or auditable.

**How aitaem helps:**

- aitaem's connector model is backend-agnostic (DuckDB, BigQuery, PostgreSQL). Once external
  data lands in any of these systems, aitaem can query it via a URI in the metric spec.
- A `SegmentSpec` with its own `source` field can filter on a different table than the metric —
  e.g. a Salesforce account segment can be used to slice a revenue metric sourced from a
  transactional database, joining only on entity keys.
- This "semantic glue" pattern decouples *data ingestion* (handled by ELT tools: Fivetran, dbt,
  Airbyte) from *metric definition* (handled by aitaem YAML specs). Teams can update metric
  definitions without touching the ingestion pipeline.

**Sketch:**

```
Salesforce CRM ──► Fivetran/dbt ──► BigQuery
                                         │
                                    SegmentSpec (source: bigquery://...accounts)
                                         │
Transactional DB ──────────────► MetricSpec (source: bigquery://...orders)
                                         │
                                    MetricCompute.compute()
                                         │
                              Revenue broken down by CRM account segment
```

---

#### Use Case 5 — LLM-Powered Data Analysis

**Who:** AI engineers building agents or assistants that need to answer data questions
autonomously.

**Problem:** LLMs can reason about data questions but cannot write correct, safe SQL queries
against production schemas on their own. Giving an LLM direct database access creates
correctness, security, and cost risks.

**How aitaem helps:**

- aitaem provides a **structured, bounded interface**: the LLM picks metric names, slice names,
  and a time window from a known vocabulary (the spec cache), rather than generating free-form
  SQL.
- `SpecCache` can be serialised and given to an LLM as context — the model knows what metrics
  exist and what slices are valid without introspecting raw schemas.
- The fixed 10-column output schema makes it straightforward for an LLM to interpret and narrate
  results.
- Since aitaem is explicitly designed to be LLM-friendly, it fits naturally as the "tool call"
  layer in an agentic pipeline.

**Sketch:**

```
User: "How did CTR trend by campaign type in Q1?"
          │
          ▼
     LLM Agent
          │  tool_call: compute(metrics="ctr", slices="campaign_type",
          │             time_window=("2024-01-01","2024-04-01"), period_type="monthly")
          ▼
   MetricCompute → DataFrame
          │
          ▼
     LLM narrates results
```

---

## 3. Summary of All Changes

| # | File | Change type | Priority |
|---|------|-------------|----------|
| 1.1 | `docs/getting-started.md` | Fix — expand installation section | High |
| 1.2 | `docs/user-guide/specs.md` | Fix — remove wrong entities from all MetricSpec examples | High |
| 1.3 | `examples/metrics/*.yaml` | Fix — remove entities field (6 files) | High |
| 1.4 | `docs/user-guide/computing-metrics.md` | Fix — replace by_entity example with non-ad_campaigns framing | Medium |
| 1.5 | `docs/index.md` | Minor addition — mention supported backends | Low |
| 1.6 | `mkdocs.yml` | Add Use Cases nav entry | High |
| 1.7 | `docs/use-cases.md` | New page — five use cases with code snippets | High |

---

## 4. Out of Scope

- No changes to the Python source code.
- No changes to tests (the example metrics YAML change in 1.3 is self-contained and does not
  break any tests — removing `entities` from a spec is valid, not an error).
- No API reference changes — none of the documented classes or methods are affected.
- No changelog entry required for a documentation-only fix (unless preferred).
