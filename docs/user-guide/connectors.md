# Connectors

`ConnectionManager` manages one or more backend connections and routes queries to the correct backend based on the source URI in each spec.

## Supported Backends

| Backend | URI prefix | Extra install |
|---------|-----------|---------------|
| DuckDB | `duckdb://` | Included by default |
| BigQuery | `bigquery://` | `pip install "aitaem[bigquery]"` |
| CSV (via DuckDB) | `duckdb://` | Included — use `read_csv_auto` |

---

## YAML Configuration

The recommended way to configure connections is a YAML file:

```yaml
# connections.yaml
duckdb:
  path: examples/data/ad_campaigns.duckdb
```

Load it with:

```python
from aitaem import ConnectionManager

conn = ConnectionManager.from_yaml("connections.yaml")
```

### Environment variable substitution

Sensitive values can be read from environment variables using `${VAR_NAME}` syntax:

```yaml
bigquery:
  project_id: ${GCP_PROJECT_ID}
  dataset_id: my_dataset
```

```bash
export GCP_PROJECT_ID=my-gcp-project
```

---

## Programmatic Configuration

### DuckDB — file database

```python
from aitaem import ConnectionManager

conn = ConnectionManager()
conn.add_connection("duckdb", path="analytics.db")
```

### DuckDB — in-memory with CSV

```python
from aitaem import ConnectionManager
from aitaem.connectors.ibis_connector import IbisConnector

connector = IbisConnector("duckdb")
connector.connect(":memory:")
connector.connection.raw_sql(
    "CREATE TABLE events AS SELECT * FROM read_csv_auto('data/events.csv')"
)

conn = ConnectionManager()
conn.add_connection("duckdb", connector=connector)
```

### BigQuery

```python
conn = ConnectionManager()
conn.add_connection("bigquery", project_id="my-project", dataset_id="my_dataset")
```

---

## Source URI Format

Metric and segment specs reference their source table via a URI:

```
backend://database_identifier/table_name
```

### DuckDB examples

| URI | Backend | Database | Table |
|-----|---------|----------|-------|
| `duckdb://analytics.db/events` | duckdb | analytics.db | events |
| `duckdb://:memory:/events` | duckdb | :memory: | events |
| `duckdb:///abs/path/to/db/events` | duckdb | /abs/path/to/db | events |

### BigQuery examples

| URI | Backend | Project | Table |
|-----|---------|---------|-------|
| `bigquery://my-project.dataset.table` | bigquery | my-project | dataset.table |
| `bigquery://my-project/dataset.table` | bigquery | my-project | dataset.table |

---

## Closing Connections

```python
conn.close_all()
```
