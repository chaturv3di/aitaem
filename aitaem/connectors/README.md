# aitaem Connectors

Backend connection management for the aitaem library. Provides unified connectors for DuckDB and BigQuery via the Ibis abstraction layer.

## Overview

The connector module enables aitaem to connect to various OLAP databases and data sources:

- **DuckDB**: Fast, in-process SQL database for local analytics
- **BigQuery**: Google Cloud's serverless data warehouse (requires optional dependencies)

Key features:
- Unified API across backends via Ibis
- YAML configuration with environment variable substitution
- URI-based table routing
- Clear, actionable error messages
- Global singleton pattern for session-wide access

## Installation

### Core (DuckDB only)

```bash
pip install aitaem
```

### With BigQuery support

```bash
pip install aitaem[bigquery]
```

### Development installation

```bash
pip install -e ".[dev]"
```

## Quick Start

### 1. Create Configuration File

Create a `connections.yaml` file:

```yaml
duckdb:
  path: analytics.db

bigquery:
  project_id: my-project
```

### 2. Authenticate (BigQuery only)

```bash
gcloud auth application-default login
```

### 3. Use in Python

```python
from aitaem.connectors import ConnectionManager

# Load connections from YAML
manager = ConnectionManager.from_yaml('connections.yaml')

# Get connector for a specific backend
duckdb_conn = manager.get_connection('duckdb')

# Or get connector from a source URI
connector = manager.get_connection_for_source('duckdb://analytics.db/events')

# Get table reference
table = connector.get_table('events')

# Execute query with Ibis
import ibis
query = table.filter(table.event_type == 'click').count()
result_df = connector.execute(query, output_format='pandas')
```

## Configuration

### YAML Schema

#### DuckDB

```yaml
duckdb:
  path: analytics.db          # Required: file path or ':memory:'
  read_only: false            # Optional: read-only mode
```

**Examples:**
- In-memory: `path: ":memory:"`
- File-based: `path: /data/analytics.db`
- Read-only: `path: analytics.db` + `read_only: true`

#### BigQuery

```yaml
bigquery:
  project_id: my-project      # Required: GCP project ID
  dataset_id: analytics       # Optional: default dataset
  location: US                # Optional: query execution region
```

**Authentication**: Uses Application Default Credentials (ADC)
- Run: `gcloud auth application-default login`
- Or set: `export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json`

### Environment Variable Substitution

Use `${VAR_NAME}` syntax in YAML values:

```yaml
duckdb:
  path: ${DUCKDB_PATH}

bigquery:
  project_id: ${GCP_PROJECT_ID}
```

Then set environment variables:

```bash
export DUCKDB_PATH=":memory:"
export GCP_PROJECT_ID=my-project
```

**Error handling**: If a referenced environment variable is not set, loading will fail with a clear error message.

## URI Format

Source URIs follow the format: `backend://database_identifier/table_name`

### DuckDB URIs

```
duckdb://analytics.db/events           → database='analytics.db', table='events'
duckdb://:memory:/events                → database=':memory:', table='events'
duckdb:///abs/path/data.db/events       → database='/abs/path/data.db', table='events'
```

### BigQuery URIs

```
bigquery://my-project.dataset.table     → project='my-project', table='dataset.table'
bigquery://project/dataset.table        → project='project', table='dataset.table'
```

**Note**: BigQuery table names in URIs must have at least 3 parts (project.dataset.table). When retrieving tables, aitaem automatically extracts the `dataset.table` format needed by Ibis.

## API Reference

### ConnectionManager

Main class for managing backend connections.

#### Class Methods

**`from_yaml(yaml_path: str) -> ConnectionManager`**

Load connections from YAML configuration file.

```python
manager = ConnectionManager.from_yaml('connections.yaml')
```

**`set_global(manager: ConnectionManager) -> None`**

Set global singleton instance.

```python
ConnectionManager.set_global(manager)
```

**`get_global() -> ConnectionManager`**

Get global singleton instance.

```python
manager = ConnectionManager.get_global()
```

#### Instance Methods

**`add_connection(backend_type: str, **config) -> None`**

Add a connection manually (without YAML).

```python
manager.add_connection('duckdb', path=':memory:')
manager.add_connection('bigquery', project_id='my-project')
```

**`get_connection(backend_type: str) -> IbisConnector`**

Get connector by backend type.

```python
connector = manager.get_connection('duckdb')
```

**`get_connection_for_source(source_uri: str) -> IbisConnector`**

Parse URI and return appropriate connector.

```python
connector = manager.get_connection_for_source('duckdb://analytics.db/events')
```

**`parse_source_uri(uri: str) -> tuple[str, str, str]`** (static)

Parse source URI into (backend_type, database_identifier, table_name).

```python
backend, database, table = ConnectionManager.parse_source_uri(
    'duckdb://analytics.db/events'
)
# Returns: ('duckdb', 'analytics.db', 'events')
```

**`close_all() -> None`**

Close all connections.

```python
manager.close_all()
```

### IbisConnector

Unified connector for DuckDB and BigQuery.

#### Constructor

**`IbisConnector(backend_type: str)`**

Create connector for specified backend.

```python
connector = IbisConnector('duckdb')
```

Supported backends: `'duckdb'`, `'bigquery'`

#### Methods

**`connect(connection_string: str | None = None, **kwargs) -> None`**

Establish connection to backend.

```python
# DuckDB
connector.connect(':memory:')
connector.connect('analytics.db')
connector.connect('analytics.db', read_only=True)

# BigQuery
connector.connect(project_id='my-project')
```

**`get_table(table_name: str) -> ibis.expr.types.Table`**

Get table reference from backend.

```python
# DuckDB
table = connector.get_table('events')

# BigQuery
table = connector.get_table('dataset.table')
table = connector.get_table('project.dataset.table')  # Auto-extracts dataset.table
```

**`execute(expr: ibis.expr.types.Expr, output_format: str = 'pandas') -> DataFrame`**

Execute query and return results.

```python
# Pandas output (default)
result = connector.execute(query, output_format='pandas')

# Polars output (requires polars package)
result = connector.execute(query, output_format='polars')
```

**`close() -> None`**

Close connection and cleanup resources.

```python
connector.close()
```

#### Properties

**`is_connected: bool`**

Check if connection is active.

```python
if connector.is_connected:
    print("Connected")
```

## Error Handling

The connector module uses a clear exception hierarchy with actionable error messages:

### Common Errors

**`ConnectionError`**: Backend connection failed
```
BigQuery connection failed. Application Default Credentials not found.

To fix this, run:
  gcloud auth application-default login
```

**`ConfigurationError`**: Invalid or missing configuration
```
Missing required field 'project_id' in bigquery configuration

Add the required field:
  bigquery:
    project_id: your-project-id
```

**`InvalidURIError`**: Malformed source URI
```
Missing backend type in URI: 'analytics.db/events'

URI must start with backend type:
  duckdb://analytics.db/events
  bigquery://project.dataset.table
```

**`ConnectionNotFoundError`**: Backend not configured
```
No connection configured for backend 'bigquery'

Add the connection to your connections.yaml:
  bigquery:
    project_id: your-project-id
```

**`TableNotFoundError`**: Table doesn't exist
```
Table 'events' not found in duckdb backend
```

**`UnsupportedBackendError`**: Backend not supported
```
Backend type 'clickhouse' not supported

Supported backends: bigquery, duckdb
```

All exceptions inherit from `AitaemError` base class for easy catching:

```python
from aitaem.utils.exceptions import AitaemError

try:
    manager = ConnectionManager.from_yaml('connections.yaml')
except AitaemError as e:
    print(f"Configuration error: {e}")
```

## Examples

### Local Development with DuckDB

```python
from aitaem.connectors import ConnectionManager

# In-memory database for testing
manager = ConnectionManager()
manager.add_connection('duckdb', path=':memory:')

connector = manager.get_connection('duckdb')
connector.connection.raw_sql("CREATE TABLE events (id INT, name VARCHAR)")
connector.connection.raw_sql("INSERT INTO events VALUES (1, 'test')")

table = connector.get_table('events')
result = connector.execute(table)
print(result)
```

### Production with BigQuery

```yaml
# connections.yaml
bigquery:
  project_id: ${GCP_PROJECT_ID}
```

```python
import os
from aitaem.connectors import ConnectionManager

# Set environment variable
os.environ['GCP_PROJECT_ID'] = 'my-production-project'

# Load configuration
manager = ConnectionManager.from_yaml('connections.yaml')

# Set as global for easy access
ConnectionManager.set_global(manager)

# Use anywhere in your application
manager = ConnectionManager.get_global()
connector = manager.get_connection('bigquery')

table = connector.get_table('analytics.user_events')
# ... run queries
```

### Multi-Backend Setup

```yaml
# connections.yaml
duckdb:
  path: local_cache.db

bigquery:
  project_id: my-project
```

```python
from aitaem.connectors import ConnectionManager

manager = ConnectionManager.from_yaml('connections.yaml')

# Use DuckDB for local aggregation
duckdb_conn = manager.get_connection('duckdb')
local_table = duckdb_conn.get_table('cache')

# Use BigQuery for cloud data warehouse
bq_conn = manager.get_connection('bigquery')
cloud_table = bq_conn.get_table('dataset.events')
```

## Testing

### Running Tests

```bash
# All connector tests
pytest tests/test_connectors/ -v

# Specific test file
pytest tests/test_connectors/test_connection_manager.py -v

# With coverage
pytest tests/test_connectors/ --cov=aitaem.connectors --cov-report=term-missing
```

### Optional Dependencies

Some tests require optional dependencies:
- BigQuery tests are skipped if `ibis-framework[bigquery]` is not installed
- Polars tests are skipped if `polars` is not installed

To run all tests:

```bash
pip install -e ".[bigquery,dev]"
pip install polars
pytest tests/test_connectors/ -v
```

## Future Enhancements

Planned for Phase 2:
- Additional backends (ClickHouse, Snowflake, PostgreSQL)
- Advanced authentication methods (service account JSON, OAuth)
- Connection pooling and automatic reconnection
- Remote YAML loading (HTTP/S3)
- Configuration validation CLI tool

## Support

For issues and questions:
- Report bugs: https://github.com/anthropics/aitaem/issues
- Documentation: See main README.md
