# aitaem Architecture

## Design Principles

- **Import depth ≤ 2** — all public types importable from `aitaem` or `aitaem.<module>`
- **LLM-friendly** — standardised output schema, familiar technologies, no custom DSLs
- **Lazy evaluation** — `compute()` returns an `ibis.Table`; data moves only when materialised
- **SQL-native** — metric expressions are plain SQL aggregates; no query DSL
- **Loosely coupled** — specs (metrics, slices, segments) are independent of each other and of the backends

---

## Technology Stack

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Query abstraction | [ibis](https://ibis-project.org/) | Backend-portable; DuckDB, BigQuery, PostgreSQL all supported |
| Default backend | DuckDB | Zero-install local analytics; reads CSV/Parquet natively |
| Output | `ibis.Table` (lazy) | Caller decides when and how to materialise (pandas, polars, Arrow, …) |
| Spec format | YAML | Human-readable; easy to version-control and generate from LLMs |

---

## Module Structure

```
aitaem/
├── __init__.py              # Depth-1 re-exports (MetricCompute, ConnectionManager, …)
├── insights.py              # MetricCompute — primary user interface
├── specs/                   # YAML spec parsing and validation
│   ├── loader.py            # SpecCache — load/cache metric, slice, segment specs
│   ├── metric.py            # MetricSpec
│   ├── slice.py             # SliceSpec, SliceValue
│   ├── segment.py           # SegmentSpec, SegmentValue
│   └── compatibility.py     # ScanResult, CompatibilityResult (pre-flight scan)
├── query/                   # Query construction and execution
│   ├── builder.py           # QueryBuilder — specs → SQL query groups
│   └── executor.py          # QueryExecutor — execute groups, union results lazily
├── connectors/              # Backend connection management
│   ├── connection.py        # ConnectionManager — route queries by source URI
│   ├── ibis_connector.py    # IbisConnector — thin ibis wrapper (DuckDB/BigQuery/Postgres)
│   └── backend_specs.py     # DuckDBConfig, BigQueryConfig, PostgresConfig dataclasses
├── helpers/                 # User-facing convenience functions
│   └── csv_to_duckdb.py     # load_csvs_to_duckdb
├── agent/                   # LLM agent utilities (QueryBot, tool definitions)
└── utils/                   # Internal utilities
    ├── validation.py        # YAML field validation
    ├── exceptions.py        # AitaemError hierarchy
    └── formatting.py        # ensure_standard_output, STANDARD_COLUMNS
```

---

## Data Flow

```
SpecCache  +  ConnectionManager
       ↓              ↓
   MetricCompute.compute()
       ↓
   QueryBuilder.build_queries()   →  [QueryGroup, …]  (SQL strings, grouped by source)
       ↓
   QueryExecutor.execute()
       ├── single backend  →  ibis.Table.union()  (fully lazy)
       └── cross backend   →  materialise each → DuckDB reload  →  ibis.Table
       ↓
   ensure_standard_output()       →  ibis.Table  (columns reordered)
       ↓
   caller: .to_pandas() / .to_polars() / ibis filters / …
```

---

## Standard Output Schema

Every `compute()` call returns an `ibis.Table` with exactly these 11 columns:

| Column | Description |
|--------|-------------|
| `period_type` | `"all_time"`, `"daily"`, `"weekly"`, `"monthly"`, `"yearly"`, or `"hourly"` |
| `period_start_date` | ISO date string or `None` (for `all_time`) |
| `period_end_date` | ISO date string or `None` |
| `entity_id` | Entity column value when `by_entity` is set; `None` otherwise |
| `metric_name` | Name of the metric |
| `metric_format` | Format hint from spec (e.g. `"percentage"`, `"currency:USD"`) or `None` |
| `slice_type` | Slice name, pipe-delimited for composite slices, or `"none"` |
| `slice_value` | Slice value, pipe-delimited for composite slices, or `"all"` |
| `segment_name` | Segment name or `"none"` |
| `segment_value` | Segment value or `"all"` |
| `metric_value` | Computed numeric result (`float`) |

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `compute()` returns `ibis.Table` | Caller controls materialisation; large result sets never forced into memory |
| `ConnectionManager` owns cross-backend DuckDB | Single class manages all connections; `close_all()` is the one teardown point |
| No `output_format` on `compute()` | ibis Table is format-agnostic; callers use `.to_pandas()`, `.to_polars()`, etc. |
| One ibis backend per backend type | Fully-qualified source URIs handle multi-table scenarios without multiple connections |
| `tmp_dir` on `ConnectionManager`, not `MetricCompute` | Temporary storage is a connection concern, not a computation concern |
| `QueryExecutor` receives a cross-backend factory | Factory is called lazily — no temp DuckDB created for single-backend queries |
| No abstract `Connector` base class | `IbisConnector` is the sole implementation; the abstraction added friction without benefit |
| Static `QueryBuilder` methods | Ibis expressions are lazy; no connection or instance state needed at build time |
| Partial computation in `execute()` | Missing connections log a warning and are skipped; other metrics still return results |
| `SpecCache` eager validation | Spec errors surface at load time, not at query time |
