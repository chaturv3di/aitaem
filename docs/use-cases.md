# Use Cases

aitaem is a general-purpose metric computation library. Below are five patterns that show how it
fits into real workflows.

---

## 1. Reporting & Analytics

**Who:** Data analysts, business intelligence teams, product managers.

**Problem:** Dashboards answer fixed questions. Every new stakeholder question requires an analyst
to write custom SQL, leading to repeated work, inconsistent metric definitions across teams, and a
backlog that never shrinks.

**How aitaem helps:**

- Define every metric once in a YAML spec — `numerator`, `denominator`, `timestamp_col`, and the
  slices that matter.
- Any analyst or engineer can call `MetricCompute.compute()` to answer "CTR by campaign type for
  Q1" or "revenue by country and platform" without touching SQL.
- Slice and segment combinations produce the full breakdown matrix in a single call, output as a
  tidy pandas DataFrame ready for charting libraries (matplotlib, Plotly, Streamlit, etc.).
- `period_type` (monthly, weekly, quarterly) turns any metric into a time series with no
  additional code.

```python
df = mc.compute(
    metrics=["ctr", "roas"],
    slices=["campaign_type", "geo"],
    segments="platform",
    time_window=("2024-01-01", "2024-04-01"),
    period_type="monthly",
)
```

---

## 2. Entity-Level Datasets for ML Model Training

**Who:** ML engineers and data scientists building predictive models that require per-entity
feature tables.

**Problem:** ML models for churn prediction, LTV estimation, personalisation, or recommendation
all require a "wide" feature table where each row is one entity (user, device, product) and each
column is an aggregate measure (clicks last 30 days, average revenue per session, etc.). Building
these tables by hand involves bespoke SQL per feature, repeated for every training run.

**How aitaem helps:**

- Declare metrics with `entities: [user_id, page_id, device_id]` to mark which entity columns the
  metric can be disaggregated by.
- Call `compute()` with `by_entity="user_id"` to get one row per entity per metric, then pivot
  into a wide feature table.
- `period_type` with `time_window` produces rolling-window features (e.g. monthly revenue per
  user over the past year) directly.
- The output schema is fixed and tidy — easy to feed into pandas `.pivot_table()` or directly
  into a training data pipeline.

```python
# Per-user monthly ad CTR — feed directly into an ML feature pipeline
df = mc.compute(
    metrics="ad_ctr",
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

## 3. Authoritative Metric Service for Product Applications

**Who:** Backend engineers and platform teams building products or internal tools that need
on-demand access to business metrics.

**Problem:** Multiple services (dashboards, notification systems, ranking algorithms, pricing
engines) each re-implement their own SQL to compute "the same" KPIs. Over time, definitions
diverge, bugs multiply, and no single source of truth exists.

**How aitaem helps:**

- Host metric specs as versioned YAML files in a repository — every service references the same
  definitions.
- Wrap `MetricCompute` in a lightweight API layer (FastAPI, Flask, gRPC) to expose metrics as a
  service. Any application that needs authoritative KPIs calls the service rather than
  re-implementing SQL.
- `ConnectionManager.from_yaml()` reads connection credentials from environment variables at
  runtime — no credentials baked into application code.
- Query results are returned as pandas DataFrames and can trivially be serialised to JSON, Parquet,
  or Arrow for downstream consumers.

```
YAML specs (git repo)
       │
       ▼
MetricCompute (Python service)
       │
       ├──► Dashboard (Streamlit / Grafana)
       ├──► Notification system
       ├──► Pricing engine
       └──► Data warehouse export (Parquet / BigQuery)
```

---

## 4. Semantic Glue for External Data Sources

**Who:** Data engineers and analytics engineers integrating data from SaaS platforms (Salesforce,
HubSpot, Stripe, etc.) into an OLAP layer.

**Problem:** External SaaS tools expose raw APIs or data exports. Teams write one-off scripts to
pull data, clean it, and load it into a warehouse. The metric logic lives in ad-hoc notebooks and
is not reusable or auditable.

**How aitaem helps:**

- aitaem's connector model is backend-agnostic (DuckDB, BigQuery, PostgreSQL). Once external data
  lands in any of these systems, aitaem can query it via a URI in the metric spec.
- A `SegmentSpec` with its own `source` field can filter on a different table than the metric —
  e.g. a Salesforce account segment can be used to slice a revenue metric sourced from a
  transactional database, joining only on entity keys.
- This "semantic glue" pattern decouples *data ingestion* (handled by ELT tools such as Fivetran,
  dbt, or Airbyte) from *metric definition* (handled by aitaem YAML specs). Teams can update
  metric definitions without touching the ingestion pipeline.

```
Salesforce CRM ──► Fivetran / dbt ──► BigQuery
                                           │
                                      SegmentSpec (source: bigquery://…/accounts)
                                           │
Transactional DB ──────────────► MetricSpec (source: bigquery://…/orders)
                                           │
                                      MetricCompute.compute()
                                           │
                              Revenue broken down by CRM account segment
```

---

## 5. LLM-Powered Data Analysis

**Who:** AI engineers building agents or assistants that need to answer data questions
autonomously.

**Problem:** LLMs can reason about data questions but cannot reliably write correct, safe SQL
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

```
User: "How did CTR trend by campaign type in Q1?"
          │
          ▼
     LLM Agent
          │  tool_call: compute(metrics="ctr", slices="campaign_type",
          │             time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
          ▼
   MetricCompute → DataFrame
          │
          ▼
     LLM narrates results
```
