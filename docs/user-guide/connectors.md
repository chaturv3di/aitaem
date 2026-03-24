# Connectors

`ConnectionManager` manages one or more backend connections and routes queries to the correct backend based on the source URI in each spec.

## Supported Backends

| Backend | URI prefix | Extra install |
|---------|-----------|---------------|
| DuckDB | `duckdb://` | Included by default |
| BigQuery | `bigquery://` | `pip install "aitaem[bigquery]"` |
| PostgreSQL | `postgres://` | `pip install "aitaem[postgres]"` |
| CSV (via DuckDB) | `duckdb://` | Included — use `read_csv_auto` |

---

## YAML Configuration

The recommended way to configure connections is a `connections.yaml` file. Load it with:

```python
from aitaem import ConnectionManager

conn = ConnectionManager.from_yaml("connections.yaml")
```

### DuckDB

```yaml
duckdb:
  path: data/analytics.db   # required
  read_only: false           # optional, default: false
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `path` | Yes | — | File path to the DuckDB database, or `':memory:'` for an in-process ephemeral database |
| `read_only` | No | `false` | Open the database in read-only mode |

### BigQuery

```yaml
bigquery:
  project_id: my-gcp-project   # required
  dataset_id: my_dataset        # optional
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `project_id` | Yes | — | GCP project ID that owns the BigQuery datasets |
| `dataset_id` | No | `null` | Default dataset used when a table name is not fully-qualified |

**Authentication** uses Application Default Credentials (ADC). Before connecting, run:

```bash
gcloud auth application-default login
```

Or point to a service account key file:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
```

### PostgreSQL

```yaml
postgres:
  host: localhost             # optional, default: localhost
  port: 5432                  # optional, default: 5432
  database: mydb              # required
  user: myuser                # required
  password: ${POSTGRES_PASSWORD}  # required
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `database` | Yes | — | Name of the PostgreSQL database to connect to |
| `user` | Yes | — | PostgreSQL username |
| `password` | Yes | — | Password for the given user |
| `host` | No | `localhost` | Hostname or IP address of the PostgreSQL server |
| `port` | No | `5432` | TCP port the PostgreSQL server listens on |

### Environment variable substitution

Any field value can reference an environment variable using `${VAR_NAME}` syntax. aitaem substitutes the variable at load time and raises a `ConfigurationError` if it is not set.

```yaml
duckdb:
  path: ${DUCKDB_PATH}

bigquery:
  project_id: ${GCP_PROJECT_ID}

postgres:
  host: ${POSTGRES_HOST}
  database: ${POSTGRES_DB}
  user: ${POSTGRES_USER}
  password: ${POSTGRES_PASSWORD}
```

---

## Programmatic Configuration

Use `add_connection()` when you want to configure connections in code rather than a YAML file.

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

### PostgreSQL

```python
import os
from aitaem import ConnectionManager

conn = ConnectionManager()
conn.add_connection(
    "postgres",
    host="db.example.com",
    database="mydb",
    user="myuser",
    password=os.environ["POSTGRES_PASSWORD"],
)
```

---

## Source URI Format

Metric and segment specs reference their source table via a URI in the `source` field. The URI determines which connection is used and which table is queried.

### DuckDB

```
duckdb://database_path/table_name
```

| URI | Table |
|-----|-------|
| `duckdb://analytics.db/events` | `events` in `analytics.db` |
| `duckdb://:memory:/events` | `events` in an in-memory database |
| `duckdb:///abs/path/to/db/events` | `events` at an absolute path |

### BigQuery

```
bigquery://project.dataset.table
```

| URI | Project | Table |
|-----|---------|-------|
| `bigquery://my-project.analytics.events` | `my-project` | `analytics.events` |
| `bigquery://my-project/analytics.events` | `my-project` | `analytics.events` |

BigQuery URIs must contain at least three dot-separated parts (project, dataset, table).

### PostgreSQL

```
postgres://schema/table
```

| URI | SQL table reference |
|-----|---------------------|
| `postgres://public/events` | `public.events` |
| `postgres://analytics/orders` | `analytics.orders` |
| `postgres:///events` | `events` (server's default schema) |

The schema part is optional. Omit it (triple slash) when you want the server's default search path to resolve the table.

---

## Closing Connections

```python
conn.close_all()
```
