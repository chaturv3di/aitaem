# Implementation Plan: aitaem Connector Module

## Overview

Implement the connector module as the **first module** of the aitaem library, supporting DuckDB and BigQuery backends via Ibis abstraction layer. This module provides connection management, URI-based routing, and YAML configuration with environment variable substitution.

**Scope**: Phase 1 - Core connector functionality with fail-fast error handling
**Backends**: DuckDB (local analytics) + BigQuery (cloud warehouse)
**Auth**: BigQuery uses Application Default Credentials (gcloud CLI) only

---

## Architecture Summary

```
aitaem/
├── __init__.py                      # Package root (minimal for now)
├── connectors/
│   ├── __init__.py                  # Export Connector, IbisConnector, ConnectionManager
│   ├── base.py                      # Abstract Connector interface
│   ├── connection.py                # ConnectionManager class
│   └── ibis_connector.py            # IbisConnector implementation
└── utils/
    ├── __init__.py                  # Export exceptions
    └── exceptions.py                # Custom exception hierarchy
```

---

## 1. Package Setup

### 1.1 Create Directory Structure

```
aitaem/
├── __init__.py
├── connectors/
│   └── __init__.py
└── utils/
    └── __init__.py

tests/
└── test_connectors/
    ├── __init__.py
    ├── test_ibis_connector.py
    ├── test_connection_manager.py
    └── fixtures/
        ├── connections_valid.yaml
        ├── connections_with_env.yaml
        └── connections_invalid.yaml

examples/
├── connections.yaml
└── connections.template.yaml
```

### 1.2 Create pyproject.toml

**Key dependencies**:
- `ibis-framework[duckdb]>=9.0.0` - Core + DuckDB backend
- `pyyaml>=6.0` - YAML parsing
- `pandas>=2.0.0` - Default output
- `pyarrow>=14.0.0` - Zero-copy conversions

**Optional dependencies**:
- `bigquery`: `ibis-framework[bigquery]>=9.0.0`, `google-cloud-bigquery>=3.0.0`
- `dev`: `pytest>=7.4.0`, `pytest-cov>=4.1.0`, `pytest-mock>=3.12.0`, `ruff`, `mypy`

---

## 2. Exception Hierarchy

### File: `aitaem/utils/exceptions.py`

Define custom exceptions with clear, actionable error messages:

```
AitaemError (base)
├── ConnectionError - Backend connection failures
├── ConnectionNotFoundError - Backend not configured
├── TableNotFoundError - Table doesn't exist
├── ConfigurationError - Invalid YAML/config
├── InvalidURIError - Malformed source URI
├── UnsupportedBackendError - Backend not supported
└── QueryExecutionError - Query execution failures
```

**Key Principle**: Fail-fast with specific, actionable messages

**Implementation Notes**:
- All exceptions inherit from `AitaemError` base class
- Each exception includes context (backend type, URI, config path, etc.)
- Error messages should guide users to solutions
- Example: `ConfigurationError("Missing required field 'project_id' in bigquery config at connections.yaml:5")`

---

## 3. Base Connector (Abstract Interface)

### File: `aitaem/connectors/base.py`

Abstract base class defining connector contract:

**Methods**:
- `connect(connection_string, **kwargs)` - Establish connection
- `get_table(table_name) -> ibis.Table` - Get table reference
- `execute(expr, output_format) -> DataFrame` - Execute query
- `close()` - Cleanup resources
- `is_connected: bool` (property) - Connection status

**Design Notes**:
- Use `abc.ABC` and `@abstractmethod` decorators
- All methods should raise `NotImplementedError` with helpful messages
- Include comprehensive docstrings with parameter descriptions
- Type hints required on all methods

---

## 4. Ibis Connector Implementation

### File: `aitaem/connectors/ibis_connector.py`

Unified connector supporting DuckDB and BigQuery via Ibis.

### 4.1 DuckDB Support

**Connection**:
- File-based: `IbisConnector('duckdb').connect('analytics.db')`
- In-memory: `IbisConnector('duckdb').connect(':memory:')`

**Implementation Details**:
- Use `ibis.duckdb.connect(database=path)` for file/in-memory
- Auto-creates file if doesn't exist
- Optional `read_only=True` parameter via kwargs
- Store connection as `self.connection: ibis.backends.duckdb.Backend`

**Features**:
- Auto-creates file if doesn't exist
- Optional `read_only=True` parameter
- Support for relative and absolute paths

### 4.2 BigQuery Support

**Connection**:
- Primary: Application Default Credentials (ADC) via `ibis.bigquery.connect(project_id='...')`
- Uses gcloud CLI authentication (user must run `gcloud auth application-default login`)

**Implementation Details**:
- Use `ibis.bigquery.connect(project_id=project_id)` (ADC auto-detected)
- Store connection as `self.connection: ibis.backends.bigquery.Backend`
- Raise `ConnectionError` if ADC not configured with clear message

**Table Names**:
- Input: `'dataset.table'` or `'project.dataset.table'`
- Parse: Extract `dataset.table` for Ibis (Ibis expects 2-part format)
- Validate: 3-part fully-qualified names, extract middle two parts

**Table Name Handling Logic**:
```
Input: 'dataset.table' (2 parts)
→ Pass directly to ibis: connection.table('dataset.table')

Input: 'project.dataset.table' (3 parts)
→ Extract 'dataset.table' (middle two parts)
→ Pass to ibis: connection.table('dataset.table')

Input: 'table' (1 part)
→ Raise InvalidURIError: "BigQuery table name must have at least 2 parts (dataset.table)"
```

### 4.3 Key Implementation Details

**Constructor**: `IbisConnector(backend_type: str)`
- Validate backend_type in `{'duckdb', 'bigquery'}`
- Raise `UnsupportedBackendError` if invalid
- Initialize `self.connection = None`
- Store `self.backend_type = backend_type`

**Connection Lifecycle**:
- Store `self.connection: ibis.BaseBackend` on connect
- Set to `None` on close
- Check `is_connected` before operations
- Raise `ConnectionError` if operation attempted while not connected

**Output Formats**:
- `'pandas'`: Use `expr.to_pandas()` (default)
- `'polars'`: Use `expr.to_polars()`
- Validate format, raise `ValueError` if invalid

**Error Handling**:
- Wrap all Ibis exceptions with aitaem exceptions
- Include backend type and context in error messages
- Example: Wrap `ibis.expr.api.TableNotFound` → `TableNotFoundError`

---

## 5. Connection Manager

### File: `aitaem/connectors/connection.py`

Manages multiple backend connections and routes queries via URI parsing.

### 5.1 Core Responsibilities

1. **YAML Loading**: `ConnectionManager.from_yaml('connections.yaml')`
2. **Environment Variable Substitution**: `${VAR_NAME}` syntax (all referenced vars must be set)
3. **Connection Storage**: One `IbisConnector` per backend type
4. **URI Routing**: Parse source URIs and return appropriate connector
5. **Global Singleton**: `ConnectionManager.get_global()` for session-wide access

### 5.2 YAML Schema

```yaml
duckdb:
  path: analytics.db          # Required: file path or ':memory:'
  read_only: false            # Optional

bigquery:
  project_id: my-project      # Required
  dataset_id: analytics       # Optional default dataset
  location: US                # Optional
```

**Validation Rules**:
- `duckdb.path`: Required, must be non-empty string
- `bigquery.project_id`: Required, must be non-empty string
- Fail immediately if required fields missing
- Fail immediately if YAML syntax invalid
- Fail immediately if file doesn't exist

### 5.3 Environment Variable Substitution

**Implementation**: Simple string replacement approach
- Pattern: `${VAR_NAME}` in YAML values
- Use `os.environ.get(var_name)` to retrieve value
- Behavior:
  - If env var is set: Replace with value
  - If env var not set: Raise `ConfigurationError` with variable name

**Algorithm**:
```python
def substitute_env_vars(value: str) -> str:
    """Recursively replace ${VAR} with environment variable values."""
    # Use regex to find all ${VAR_NAME} patterns
    # For each match:
    #   - Extract VAR_NAME
    #   - Check if os.environ.get(VAR_NAME) exists
    #   - If exists: replace with value
    #   - If not exists: raise ConfigurationError(f"Environment variable '{VAR_NAME}' not set")
    # Return substituted string
```

**Examples**:
- `${GCP_PROJECT_ID}` → Replaces with value of GCP_PROJECT_ID env var
- `${DUCKDB_PATH}` → Replaces with value of DUCKDB_PATH env var
- If env var not set → Raises clear error: `ConfigurationError("Environment variable 'GCP_PROJECT_ID' referenced in connections.yaml but not set")`

### 5.4 URI Parsing Algorithm

**Format**: `backend://database_identifier/table_name`

**DuckDB Examples**:
- `duckdb://analytics.db/events` → `('duckdb', 'analytics.db', 'events')`
- `duckdb://:memory:/events` → `('duckdb', ':memory:', 'events')`
- `duckdb:///abs/path/db/events` → `('duckdb', '/abs/path/db', 'events')`

**BigQuery Examples**:
- `bigquery://my-project.dataset.table` → `('bigquery', 'my-project', 'dataset.table')`
- `bigquery://project/dataset.table` → Normalize `/` to `.` → Same as above

**Parsing Logic**:
```python
def parse_source_uri(uri: str) -> tuple[str, str, str]:
    """Parse source URI into (backend, database, table).

    Args:
        uri: Source URI (e.g., 'duckdb://analytics.db/events')

    Returns:
        Tuple of (backend_type, database_identifier, table_name)

    Raises:
        InvalidURIError: If URI is malformed
    """
    # 1. Parse with urllib.parse.urlparse()
    # 2. Extract scheme → backend_type (validate not empty)
    # 3. Combine netloc + path
    # 4. DuckDB: Split on LAST '/' to separate db from table
    # 5. BigQuery: Normalize '/' to '.', split into parts, extract project + dataset.table
    # 6. Validate: fail if empty table, fail if BigQuery <3 parts
    # 7. Return (backend_type, database_identifier, table_name)
```

**Detailed Algorithm**:
```
Step 1: Parse URI
  - Use urllib.parse.urlparse(uri)
  - Extract: scheme, netloc, path

Step 2: Extract backend type
  - backend_type = scheme
  - If empty: raise InvalidURIError("Missing backend type in URI")

Step 3: Combine netloc + path
  - full_path = netloc + path
  - Example: 'analytics.db/events' or 'my-project.dataset.table'

Step 4: Backend-specific parsing
  DuckDB:
    - Find LAST '/' in full_path
    - database = everything before last '/'
    - table = everything after last '/'
    - If no '/': raise InvalidURIError("Missing table separator '/'")

  BigQuery:
    - Normalize: replace all '/' with '.'
    - Split on '.'
    - If <3 parts: raise InvalidURIError("BigQuery URI needs 3 parts: project.dataset.table")
    - project = parts[0]
    - table = '.'.join(parts[1:])  # dataset.table

Step 5: Validate
  - If table is empty: raise InvalidURIError("Empty table name")
  - Return (backend_type, database, table)
```

**Validation**:
- Fail if no scheme
- Fail if empty table name
- Fail if BigQuery has <3 parts

### 5.5 Key Methods

**Class Structure**:
```python
class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, IbisConnector] = {}

    @classmethod
    def from_yaml(cls, yaml_path: str) -> 'ConnectionManager':
        """Load all connections from YAML file."""

    def add_connection(self, backend_type: str, **config):
        """Add single connection by creating and storing IbisConnector."""

    def get_connection(self, backend_type: str) -> IbisConnector:
        """Get connector by backend type. Raises ConnectionNotFoundError if not found."""

    def get_connection_for_source(self, source_uri: str) -> IbisConnector:
        """Parse URI and return appropriate connector."""

    @staticmethod
    def parse_source_uri(uri: str) -> tuple[str, str, str]:
        """Parse source URI. Returns (backend, database, table)."""

    @classmethod
    def set_global(cls, manager: 'ConnectionManager'):
        """Set global singleton instance."""

    @classmethod
    def get_global(cls) -> 'ConnectionManager':
        """Get global singleton. Raises RuntimeError if not set."""

    def close_all(self):
        """Close all connections."""
```

**Global Singleton Pattern**:
- Class variable: `_global_instance: ConnectionManager | None = None`
- `set_global(manager)`: Sets `_global_instance = manager`
- `get_global()`: Returns `_global_instance` or raises `RuntimeError("No global ConnectionManager set. Call set_global() first.")`

---

## 6. Edge Cases and Error Handling

### 6.1 URI Parsing Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| `analytics.db/events` | FAIL | `InvalidURIError` (no scheme) |
| `duckdb://analytics.db/` | FAIL | `InvalidURIError` (empty table) |
| `duckdb://analytics.db` | FAIL | `InvalidURIError` (no table separator) |
| `duckdb:///abs/path/db/table` | SUCCESS | `('duckdb', '/abs/path/db', 'table')` |
| `bigquery://project.dataset` | FAIL | `InvalidURIError` (need 3 parts) |
| `bigquery://p.d.t.extra` | SUCCESS | `('bigquery', 'p', 'd.t.extra')` |
| `bigquery://proj/ds.tbl` | SUCCESS | Normalize to `proj.ds.tbl` |
| `unknown://path/table` | SUCCESS (parse) | Later fails in `get_connection` with `UnsupportedBackendError` |

### 6.2 Connection Edge Cases

| Scenario | Handling |
|----------|----------|
| YAML file doesn't exist | Raise `FileNotFoundError` immediately in `from_yaml()` |
| Invalid YAML syntax | Raise `ConfigurationError` immediately in `from_yaml()` |
| Missing required field (project_id) | Raise `ConfigurationError` immediately in `from_yaml()` |
| Environment variable not set | Raise `ConfigurationError` immediately during substitution |
| BigQuery ADC not configured | Raise `ConnectionError` on `connect()` with helpful message |
| Unknown backend type in config | Raise `UnsupportedBackendError` in `add_connection()` |
| Calling `get_global()` before `set_global()` | Raise `RuntimeError` immediately |

**BigQuery ADC Error Message**:
```
ConnectionError: BigQuery connection failed. Application Default Credentials not found.

To fix this, run:
  gcloud auth application-default login

Or set GOOGLE_APPLICATION_CREDENTIALS environment variable.
```

### 6.3 BigQuery Table Name Edge Cases

| Input | Handling | Passed to Ibis |
|-------|----------|----------------|
| `dataset.table` | 2 parts | `dataset.table` (valid) |
| `project.dataset.table` | 3 parts | Extract `dataset.table` |
| `project-123.ds.tbl` | 3 parts | Extract `ds.tbl` (handle dash in project) |
| `dataset` | 1 part | FAIL - missing table |
| `proj.ds.tbl.extra.parts` | 5 parts | Extract `ds.tbl.extra.parts` (everything after first part) |

**Implementation**:
```python
def _parse_bigquery_table_name(table_name: str) -> str:
    """Extract dataset.table from fully-qualified BigQuery table name.

    Args:
        table_name: 'dataset.table' or 'project.dataset.table'

    Returns:
        'dataset.table' format for Ibis

    Raises:
        InvalidURIError: If table name has <2 parts
    """
    parts = table_name.split('.')
    if len(parts) < 2:
        raise InvalidURIError(f"BigQuery table name must have at least 2 parts: {table_name}")
    if len(parts) == 2:
        return table_name  # Already in dataset.table format
    # 3+ parts: extract everything after first part (project)
    return '.'.join(parts[1:])
```

---

## 7. Test Strategy

### 7.1 Test Structure

```
tests/test_connectors/
├── __init__.py
├── test_ibis_connector.py       # 15+ test cases
├── test_connection_manager.py   # 20+ test cases
└── fixtures/
    ├── connections_valid.yaml
    ├── connections_with_env.yaml
    └── connections_invalid.yaml
```

### 7.2 Key Test Cases

**IbisConnector** (15 tests):

1. **Initialization**:
   - ✓ Valid backend types (duckdb, bigquery)
   - ✓ Invalid backend type raises `UnsupportedBackendError`
   - ✓ Initial state: `is_connected = False`

2. **DuckDB Connection**:
   - ✓ Connect to `:memory:` database
   - ✓ Connect to file-based database (auto-create)
   - ✓ Connect with `read_only=True` config
   - ✓ Connection state: `is_connected = True` after connect

3. **BigQuery Connection**:
   - ✓ Connect with valid project_id (mocked ADC)
   - ✓ Connect fails if ADC not configured (clear error message)
   - ✓ Missing project_id raises error

4. **get_table()**:
   - ✓ Get table successfully (DuckDB, with test data)
   - ✓ Get table successfully (BigQuery, mocked)
   - ✓ Get table with 3-part name (BigQuery, extracts dataset.table)
   - ✓ Table not found raises `TableNotFoundError`
   - ✓ Not connected raises `ConnectionError`

5. **execute()**:
   - ✓ Execute query, return pandas DataFrame
   - ✓ Execute query, return polars DataFrame
   - ✓ Invalid output format raises `ValueError`
   - ✓ Not connected raises `ConnectionError`

6. **Lifecycle**:
   - ✓ Close connection sets `is_connected = False`
   - ✓ `__repr__` shows backend type and connection status

**ConnectionManager** (20+ tests):

1. **YAML Loading**:
   - ✓ Load valid YAML file with multiple backends
   - ✓ File not found raises `FileNotFoundError`
   - ✓ Invalid YAML syntax raises `ConfigurationError`
   - ✓ Empty YAML file handled gracefully (no connections)
   - ✓ Missing required field raises `ConfigurationError`

2. **Environment Variable Substitution**:
   - ✓ Replace `${VAR}` with env var value
   - ✓ Replace multiple `${VAR1}` and `${VAR2}` in same file
   - ✓ Missing env var raises `ConfigurationError` with var name
   - ✓ Nested values substituted correctly

3. **Connection Management**:
   - ✓ Add connection for DuckDB
   - ✓ Add connection for BigQuery
   - ✓ Get connection by backend type
   - ✓ Get connection for non-existent backend raises `ConnectionNotFoundError`
   - ✓ Unsupported backend in config raises `UnsupportedBackendError`

4. **URI Parsing** (static method tests):
   - ✓ Parse DuckDB URI: `duckdb://analytics.db/events`
   - ✓ Parse DuckDB in-memory: `duckdb://:memory:/events`
   - ✓ Parse DuckDB absolute path: `duckdb:///abs/path/db/table`
   - ✓ Parse BigQuery URI: `bigquery://project.dataset.table`
   - ✓ Parse BigQuery with slash: `bigquery://project/dataset.table`
   - ✓ Missing scheme raises `InvalidURIError`
   - ✓ Empty table name raises `InvalidURIError`
   - ✓ BigQuery <3 parts raises `InvalidURIError`
   - ✓ No table separator (DuckDB) raises `InvalidURIError`

5. **Global Singleton**:
   - ✓ Set global instance with `set_global()`
   - ✓ Get global instance with `get_global()`
   - ✓ Get global before set raises `RuntimeError`

6. **Integration**:
   - ✓ Load YAML, get connection for source URI (end-to-end)
   - ✓ Close all connections

### 7.3 Test Fixtures

**fixtures/connections_valid.yaml**:
```yaml
duckdb:
  path: :memory:

bigquery:
  project_id: test-project
  dataset_id: test_dataset
```

**fixtures/connections_with_env.yaml**:
```yaml
duckdb:
  path: ${DUCKDB_PATH}

bigquery:
  project_id: ${GCP_PROJECT_ID}
```

**fixtures/connections_invalid.yaml**:
```yaml
bigquery:
  # Missing required project_id
  dataset_id: test_dataset

unknown_backend:
  some_config: value
```

### 7.4 Mocking Strategy

**BigQuery**:
- Mock `ibis.bigquery.connect()` for tests without real credentials
- Use `pytest-mock` or `unittest.mock.patch`
- Mock returns a fake backend object with `table()` method

**Environment Variables**:
- Use `pytest.monkeypatch.setenv()` to set temporary env vars
- Clean up automatically after each test

**DuckDB**:
- Use real connections (lightweight, in-memory)
- Create test tables with sample data for `get_table()` tests

**Example Mock**:
```python
def test_bigquery_connect_success(mocker):
    # Mock ibis.bigquery.connect
    mock_backend = mocker.Mock()
    mocker.patch('ibis.bigquery.connect', return_value=mock_backend)

    connector = IbisConnector('bigquery')
    connector.connect(project_id='test-project')

    assert connector.is_connected
    ibis.bigquery.connect.assert_called_once_with(project_id='test-project')
```

### 7.5 Coverage Goals

- **Line coverage**: >90%
- **Branch coverage**: >85%
- **Critical paths**: 100% (connection logic, URI parsing, error handling)

---

## 8. Example Configurations

### examples/connections.yaml (Minimal)
```yaml
# Minimal configuration for local development

duckdb:
  path: :memory:

bigquery:
  project_id: my-project
```

### examples/connections.template.yaml (Production)
```yaml
# Production configuration template with all options

# DuckDB - Local analytics database
duckdb:
  path: /data/analytics/production.db  # or ':memory:' for testing
  read_only: false                      # Optional: set to true for read-only access

# BigQuery - Cloud data warehouse
bigquery:
  project_id: ${GCP_PROJECT_ID}        # Required: GCP project ID

  # Optional: Default dataset (if not specified in table names)
  # dataset_id: analytics

  # Optional: Region for query execution
  # location: US

# Note: BigQuery uses Application Default Credentials (gcloud CLI)
# Run this command to authenticate:
#   gcloud auth application-default login
```

**Usage Instructions** (in template comments):
```yaml
# Setup Instructions:
# 1. Copy this file to connections.yaml
# 2. Set environment variables:
#    export GCP_PROJECT_ID=your-project-id
# 3. Authenticate with gcloud:
#    gcloud auth application-default login
# 4. Load in Python:
#    from aitaem.connectors import ConnectionManager
#    manager = ConnectionManager.from_yaml('connections.yaml')
```

---

## 9. Documentation Updates

### 9.1 New Documentation

**File**: `aitaem/connectors/README.md`

**Sections**:
1. **Overview**: Purpose of connector module, supported backends
2. **Installation**: How to install with optional dependencies
3. **Quick Start**: Minimal example with DuckDB and BigQuery
4. **Connection Configuration**:
   - YAML schema reference
   - Environment variable syntax
   - Backend-specific options
5. **URI Format**: Examples for DuckDB and BigQuery
6. **Authentication**: BigQuery ADC setup instructions
7. **Error Handling**: Common errors and solutions
8. **API Reference**: All public classes and methods

**Key Content**:
```markdown
## Quick Start

### Installation
```bash
# Core + DuckDB
pip install aitaem

# With BigQuery support
pip install aitaem[bigquery]
```

### Basic Usage
```python
from aitaem.connectors import ConnectionManager

# Load connections
manager = ConnectionManager.from_yaml('connections.yaml')

# Get connector for a source URI
connector = manager.get_connection_for_source('duckdb://analytics.db/events')

# Get table reference
table = connector.get_table('events')

# Execute query
import ibis
query = table.filter(table.event_type == 'click').count()
result_df = connector.execute(query, output_format='pandas')
```

### BigQuery Authentication
```bash
# Authenticate with gcloud CLI
gcloud auth application-default login
```
```

### 9.2 Updates to Existing Docs

**README.md**:
- Add "Setup" section after "Installation"
- Show connection configuration as first step
- Add example connections.yaml
- Update metric examples to show full URIs (e.g., `duckdb://analytics.db/events`)

**Example Addition to README.md**:
```markdown
## Setup

### 1. Create Connection Configuration

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

### 3. Load Connections

```python
from aitaem import set_connections

set_connections('connections.yaml')
```
```

**CLAUDE.md**:
- Add connector module development notes
- Testing guidance for BigQuery (when to mock vs. real credentials)
- How to run tests without BigQuery credentials

**Example Addition to CLAUDE.md**:
```markdown
## Testing Connector Module

### DuckDB Tests
- Use real in-memory connections (`:memory:`)
- No mocking needed, fast and reliable

### BigQuery Tests
- **Default**: Mock `ibis.bigquery.connect()` to avoid credential requirements
- **Optional**: Set `GOOGLE_APPLICATION_CREDENTIALS` for integration tests with real BigQuery
- Skip BigQuery tests if credentials unavailable (use `@pytest.mark.skipif`)
```

---

## 10. Implementation Sequence

### Phase 1: Foundation (Files 1-5)

**File 1**: `pyproject.toml`
- Define package metadata
- Core dependencies: ibis-framework[duckdb], pyyaml, pandas, pyarrow
- Optional dependencies: bigquery, dev
- Build system configuration

**File 2**: `aitaem/__init__.py`
- Minimal package root
- Future: Will export `set_connections()` function

**File 3**: `aitaem/utils/__init__.py`
- Export custom exceptions

**File 4**: `aitaem/utils/exceptions.py`
- Implement exception hierarchy
- All exceptions with clear docstrings
- Example error messages in docstrings

**File 5**: `aitaem/connectors/__init__.py`
- Export: `Connector`, `IbisConnector`, `ConnectionManager`

### Phase 2: Abstract Interface (File 6)

**File 6**: `aitaem/connectors/base.py`
- Abstract `Connector` class
- Define method signatures with type hints
- Comprehensive docstrings for each method
- Properties: `is_connected`

### Phase 3: Ibis Connector (File 7)

**File 7**: `aitaem/connectors/ibis_connector.py`
- Implement `IbisConnector` class
- Constructor with backend validation
- DuckDB connection logic
- BigQuery connection logic (ADC only)
- `get_table()` with BigQuery table name parsing
- `execute()` with pandas/polars support
- Connection lifecycle methods (`close()`, `is_connected`)
- Error handling (wrap Ibis exceptions)

**Critical Functionality**:
1. Backend validation in `__init__`
2. Connection state management
3. BigQuery table name parsing (3-part → 2-part)
4. Output format conversion
5. Clear error messages

### Phase 4: Connection Manager (File 8)

**File 8**: `aitaem/connectors/connection.py`
- Implement `ConnectionManager` class
- YAML parsing with validation
- Environment variable substitution
- `add_connection()` method
- URI parsing (`parse_source_uri()` static method)
- Connection routing (`get_connection_for_source()`)
- Global singleton pattern (`set_global()`, `get_global()`)

**Critical Functionality**:
1. YAML loading with fail-fast validation
2. Environment variable substitution (regex-based)
3. URI parsing algorithm (backend-specific logic)
4. Connection storage (dict of IbisConnectors)
5. Global singleton management

### Phase 5: Test Fixtures (Files 9-12)

**File 9**: `tests/test_connectors/__init__.py`
- Empty (marks as package)

**File 10**: `tests/test_connectors/fixtures/connections_valid.yaml`
- Minimal valid configuration

**File 11**: `tests/test_connectors/fixtures/connections_with_env.yaml`
- Configuration with environment variables

**File 12**: `tests/test_connectors/fixtures/connections_invalid.yaml`
- Invalid configuration for error testing

### Phase 6: Unit Tests (Files 13-14)

**File 13**: `tests/test_connectors/test_ibis_connector.py`
- 15+ test cases covering:
  - Initialization and validation
  - DuckDB connection (memory, file)
  - BigQuery connection (mocked)
  - Table operations (get_table with different formats)
  - Query execution (pandas, polars)
  - Error cases (not connected, invalid format, table not found)
  - Lifecycle (close, is_connected)

**File 14**: `tests/test_connectors/test_connection_manager.py`
- 20+ test cases covering:
  - YAML loading (valid, invalid, missing file)
  - Environment variable substitution
  - Connection management (add, get, not found)
  - URI parsing (all edge cases from section 6.1)
  - Global singleton (set, get, not set)
  - Integration (end-to-end flow)

### Phase 7: Examples (Files 15-16)

**File 15**: `examples/connections.yaml`
- Minimal working example
- In-memory DuckDB + BigQuery with ADC

**File 16**: `examples/connections.template.yaml`
- Production-ready template with comments
- All configuration options documented
- Setup instructions in comments
- Environment variable examples

### Phase 8: Documentation (Files 17-19)

**File 17**: `aitaem/connectors/README.md`
- New comprehensive connector documentation
- All sections from 9.1

**File 18**: Update `README.md`
- Add Setup section
- Add connection configuration example
- Update installation instructions

**File 19**: Update `CLAUDE.md`
- Add connector testing guidance
- Add mocking strategies
- Add development notes

---

## 11. Verification Checklist

Before marking complete, verify all items:

### Code Quality
- [ ] All 8 connector module files created
- [ ] Type hints on all public methods and functions
- [ ] Docstrings on all classes, methods, and public functions
- [ ] Code formatted with `ruff format`
- [ ] No linting errors: `ruff check`
- [ ] Type checking passes: `mypy aitaem/`

### Functionality
- [ ] DuckDB connection works (file-based and in-memory)
- [ ] DuckDB can create tables and execute queries
- [ ] BigQuery connection works with mocked ADC
- [ ] BigQuery handles 2-part table names (`dataset.table`)
- [ ] BigQuery handles 3-part table names (`project.dataset.table`)
- [ ] URI parsing handles all documented edge cases (section 6.1)
- [ ] YAML loading with env var substitution works
- [ ] Environment variable errors fail fast with clear messages
- [ ] Global ConnectionManager singleton works
- [ ] All error cases fail fast with actionable messages

### Testing
- [ ] All unit tests pass: `pytest tests/test_connectors/`
- [ ] Code coverage >90%: `pytest --cov=aitaem.connectors --cov-report=term`
- [ ] Test fixtures created (3 YAML files)
- [ ] BigQuery mocking works (tests pass without real credentials)
- [ ] Environment variable mocking works
- [ ] DuckDB tests use real in-memory connections

### Documentation
- [ ] Example YAML files created (2 files)
- [ ] Connector README written with all sections
- [ ] Main README updated with Setup section
- [ ] CLAUDE.md updated with testing guidance
- [ ] All public APIs have docstrings with examples

### Independence
- [ ] No dependencies on other aitaem modules (connectors is first module)
- [ ] Package installable: `pip install -e .`
- [ ] Optional dependencies work: `pip install -e ".[bigquery]"`
- [ ] Can import successfully: `from aitaem.connectors import ConnectionManager`

### Edge Cases
- [ ] All URI parsing edge cases from section 6.1 tested
- [ ] All connection edge cases from section 6.2 tested
- [ ] All BigQuery table name edge cases from section 6.3 tested
- [ ] Error messages are clear and actionable
- [ ] No silent failures (all errors raise exceptions)

---

## 12. Critical Files Summary

**Implementation Priority** (19 files total):

| Priority | File | Purpose | Lines (Est.) |
|----------|------|---------|--------------|
| 1 | `pyproject.toml` | Package configuration | 60 |
| 2 | `aitaem/__init__.py` | Package root | 10 |
| 3 | `aitaem/utils/__init__.py` | Utils exports | 5 |
| 4 | `aitaem/utils/exceptions.py` | Exception hierarchy | 100 |
| 5 | `aitaem/connectors/__init__.py` | Connector exports | 5 |
| 6 | `aitaem/connectors/base.py` | Abstract interface | 80 |
| 7 | `aitaem/connectors/ibis_connector.py` | Core connector implementation | 250 |
| 8 | `aitaem/connectors/connection.py` | Connection manager | 300 |
| 9 | `tests/test_connectors/__init__.py` | Test package | 0 |
| 10 | `tests/test_connectors/fixtures/connections_valid.yaml` | Valid config fixture | 10 |
| 11 | `tests/test_connectors/fixtures/connections_with_env.yaml` | Env var fixture | 10 |
| 12 | `tests/test_connectors/fixtures/connections_invalid.yaml` | Invalid config fixture | 10 |
| 13 | `tests/test_connectors/test_ibis_connector.py` | IbisConnector tests | 400 |
| 14 | `tests/test_connectors/test_connection_manager.py` | ConnectionManager tests | 600 |
| 15 | `examples/connections.yaml` | Minimal example | 10 |
| 16 | `examples/connections.template.yaml` | Production template | 30 |
| 17 | `aitaem/connectors/README.md` | Connector documentation | 300 |
| 18 | `README.md` (update) | Main README updates | +50 |
| 19 | `CLAUDE.md` (update) | Development notes updates | +40 |

**Total Estimated Lines**: ~2,270 lines (including tests, docs, and config)

---

## 13. Dependencies and Installation

### 13.1 Core Dependencies

```toml
[project.dependencies]
ibis-framework = {extras = ["duckdb"], version = ">=9.0.0"}
pyyaml = ">=6.0"
pandas = ">=2.0.0"
pyarrow = ">=14.0.0"
```

**Rationale**:
- `ibis-framework[duckdb]`: Core query abstraction + DuckDB backend
- `pyyaml`: YAML parsing for connection configs
- `pandas`: Default output format (required by core)
- `pyarrow`: Zero-copy conversions between backends and DataFrames

### 13.2 Optional Dependencies

```toml
[project.optional-dependencies]
bigquery = [
    "ibis-framework[bigquery]>=9.0.0",
    "google-cloud-bigquery>=3.0.0",
]
dev = [
    "pytest>=7.4.0",
    "pytest-cov>=4.1.0",
    "pytest-mock>=3.12.0",
    "ruff>=0.3.0",
    "mypy>=1.8.0",
]
all = ["aitaem[bigquery,dev]"]
```

**Installation Examples**:
```bash
# Core only (DuckDB support)
pip install aitaem

# With BigQuery
pip install aitaem[bigquery]

# Development
pip install -e ".[dev]"

# Everything
pip install -e ".[all]"
```

---

## 14. Error Message Examples

Clear, actionable error messages are critical for user experience.

### 14.1 Configuration Errors

**Missing YAML file**:
```
FileNotFoundError: Connection configuration file not found: /path/to/connections.yaml

Check that the file path is correct and the file exists.
```

**Invalid YAML syntax**:
```
ConfigurationError: Invalid YAML syntax in connections.yaml at line 5:
  Expected a mapping but found a sequence

Fix the YAML syntax and try again.
```

**Missing required field**:
```
ConfigurationError: Missing required field 'project_id' in bigquery configuration at connections.yaml:3

Add the required field:
  bigquery:
    project_id: your-project-id
```

**Missing environment variable**:
```
ConfigurationError: Environment variable 'GCP_PROJECT_ID' referenced in connections.yaml but not set

Set the environment variable:
  export GCP_PROJECT_ID=your-project-id
```

### 14.2 Connection Errors

**Unsupported backend**:
```
UnsupportedBackendError: Backend type 'clickhouse' not supported

Supported backends: duckdb, bigquery

Install support for additional backends:
  pip install aitaem[clickhouse]
```

**BigQuery ADC not configured**:
```
ConnectionError: BigQuery connection failed. Application Default Credentials not found.

To fix this, run:
  gcloud auth application-default login

Or set the GOOGLE_APPLICATION_CREDENTIALS environment variable:
  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
```

**Connection not found**:
```
ConnectionNotFoundError: No connection configured for backend 'bigquery'

Add the connection to your connections.yaml:
  bigquery:
    project_id: your-project-id

Or call add_connection():
  manager.add_connection('bigquery', project_id='your-project-id')
```

### 14.3 URI Errors

**Missing scheme**:
```
InvalidURIError: Missing backend type in URI: 'analytics.db/events'

URI must start with backend type:
  duckdb://analytics.db/events
  bigquery://project.dataset.table
```

**Empty table name**:
```
InvalidURIError: Empty table name in URI: 'duckdb://analytics.db/'

URI must include table name:
  duckdb://analytics.db/events
```

**BigQuery insufficient parts**:
```
InvalidURIError: BigQuery URI must have at least 3 parts (project.dataset.table): 'project.dataset'

Valid formats:
  bigquery://project.dataset.table
  bigquery://project/dataset.table
```

---

## 15. Testing Commands Reference

### 15.1 Run Tests

```bash
# All connector tests
pytest tests/test_connectors/ -v

# Specific test file
pytest tests/test_connectors/test_connection_manager.py -v

# Specific test
pytest tests/test_connectors/test_connection_manager.py::test_parse_duckdb_uri -v

# With coverage
pytest tests/test_connectors/ --cov=aitaem.connectors --cov-report=term-missing

# Coverage HTML report
pytest tests/test_connectors/ --cov=aitaem.connectors --cov-report=html
```

### 15.2 Code Quality

```bash
# Format code
ruff format aitaem/ tests/

# Check linting
ruff check aitaem/ tests/

# Fix auto-fixable issues
ruff check --fix aitaem/ tests/

# Type checking
mypy aitaem/

# Run all checks
ruff check aitaem/ tests/ && mypy aitaem/ && pytest tests/test_connectors/
```

### 15.3 Test with Environment Variables

```bash
# Set env vars and run tests
DUCKDB_PATH=":memory:" GCP_PROJECT_ID="test-project" pytest tests/test_connectors/test_connection_manager.py::test_env_var_substitution -v
```

---

## 16. Future Enhancements (Phase 2)

Features explicitly deferred to Phase 2:

1. **Additional Backends**:
   - ClickHouse support
   - Druid support
   - Snowflake support
   - PostgreSQL support (via DuckDB federation)

2. **Advanced Authentication**:
   - BigQuery service account JSON (inline or file)
   - OAuth flows for interactive use
   - Credential caching
   - Refresh token handling

3. **Connection Pooling**:
   - Reuse connections across queries
   - Connection timeout management
   - Automatic reconnection on failure

4. **Enhanced Error Handling**:
   - Partial computation with graceful degradation
   - Retry logic for transient failures
   - Detailed error metadata in exceptions

5. **Configuration Enhancements**:
   - Database-backed spec storage
   - Remote YAML loading (HTTP/S3)
   - Configuration validation CLI tool
   - Environment-specific configs (dev/staging/prod)

6. **Testing Enhancements**:
   - Integration tests with real BigQuery (optional)
   - Performance benchmarks
   - Load testing for connection manager

---

## Notes

- **This is the first module** to be implemented - no other aitaem code exists yet
- Follows ARCHITECTURE.md design patterns (import depth ≤ 2, fail-fast, Ibis abstraction)
- ClickHouse/Druid/other backends deferred to Phase 2
- Polars output support included but not priority (pandas is primary)
- BigQuery authentication uses ADC (gcloud CLI) as **only** method in Phase 1
- All connections fail fast - **no graceful degradation** in Phase 1
- Clear, actionable error messages are critical for user experience
- Comprehensive tests (35+ test cases) ensure reliability
- Documentation-first approach for better developer experience

---

**Plan Status**: Ready for Implementation
**Estimated Implementation Time**: 8-10 hours
**Target Completion**: After approval, implementation can begin immediately
