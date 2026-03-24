# Implementation Plan: PostgreSQL Backend

## Overview

Add PostgreSQL as a supported backend to the aitaem connector module. The implementation extends the existing Ibis-based connector architecture to support Postgres without changing any public interfaces.

**Scope**: Extend `IbisConnector` and `ConnectionManager` to support a `postgres` backend type.
**Auth**: Username/password credentials, optionally via environment variables.
**Dependency**: `ibis-framework[postgres]` (optional extra).

---

## Architecture Summary

Changes are additive and localized to the connectors layer:

```
aitaem/
└── connectors/
    ├── backend_specs.py    # NEW: config dataclasses for all backend types (single source of truth)
    ├── ibis_connector.py   # Add 'postgres' to SUPPORTED_BACKENDS + _connect_postgres()
    └── connection.py       # Handle postgres in add_connection() + parse_source_uri()

pyproject.toml              # Add [postgres] optional dependency group
docs/                       # Update connector docs and changelog
```

No changes needed to `base.py`, `query/`, `specs/`, or `insights.py`.

---

## Source URI Format

Postgres source URIs in metric spec `source` fields follow this format:

```
postgres://schema/table
```

Examples:
- `postgres://public/events` → backend: `postgres`, schema: `public`, table: `events`
- `postgres://analytics/orders` → backend: `postgres`, schema: `analytics`, table: `orders`

The `schema` part is optional. When omitted the connection's default schema is used:
- `postgres:///events` → backend: `postgres`, schema: `''`, table: `events`

The `_parse_table_name_from_uri` helper in `query/builder.py` must be updated to return `schema.table` for postgres (when schema is non-empty), so SQL queries reference the correct schema.

---

## Connections YAML

```yaml
postgres:
  host: localhost       # optional, default: localhost
  port: 5432            # optional, default: 5432
  database: mydb        # required
  user: myuser          # required
  password: ${POSTGRES_PASSWORD}  # required
```

All fields support `${ENV_VAR}` substitution (already handled by `ConnectionManager`).

---

## Sub-Features

### Sub-Feature 1: Add `ibis-framework[postgres]` optional dependency

**File**: `pyproject.toml`

Add a `postgres` optional dependency group:
```toml
[project.optional-dependencies]
postgres = ["ibis-framework[postgres]>=9.0.0"]
```

Install locally for development: `uv pip install -e ".[postgres]"`

**Test**: Verify `import ibis.postgres` succeeds in the activated venv.

---

### Sub-Feature 2: Create `backend_specs.py` — centralized backend config dataclasses

**File**: `aitaem/connectors/backend_specs.py`

Define a dataclass per backend type, where fields without defaults are required and fields with defaults are optional. This is the single source of truth for what each backend expects — replacing all inline field-checking in `IbisConnector` and `ConnectionManager`.

Also export a `BACKEND_SPECS` registry mapping backend name → dataclass, used by `ConnectionManager` to validate config dicts generically.

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class DuckDBConfig:
    path: str
    read_only: bool = False

@dataclass
class BigQueryConfig:
    project_id: str
    dataset_id: str | None = None

@dataclass
class PostgresConfig:
    database: str
    user: str
    password: str
    host: str = "localhost"
    port: int = 5432

# Registry: backend name → config dataclass
BACKEND_SPECS: dict[str, type] = {
    "duckdb": DuckDBConfig,
    "bigquery": BigQueryConfig,
    "postgres": PostgresConfig,
}
```

A shared `validate_backend_config(backend_type, config)` helper instantiates the dataclass from the config dict and converts `TypeError` (missing required field) into a `ConfigurationError` with a YAML snippet. This replaces all per-backend `if "field" not in config` checks in `ConnectionManager`.

```python
def validate_backend_config(backend_type: str, config: dict[str, Any]) -> Any:
    """Validate config dict against the backend's dataclass spec.

    Returns:
        Instantiated config dataclass

    Raises:
        ConfigurationError: If a required field is missing or a value has the wrong type
        UnsupportedBackendError: If backend_type is not in BACKEND_SPECS
    """
```

**Export** `backend_specs.py` from `aitaem/connectors/__init__.py` is not required (internal module).

**Tests** (`tests/test_connectors/test_backend_specs.py`):
- Valid configs for each backend type instantiate without error.
- Missing required field raises `ConfigurationError` with the field name in the message.
- Extra unknown fields passed to `validate_backend_config` are ignored (they may be backend-specific pass-through kwargs like `read_only`).
- `BACKEND_SPECS` contains exactly `{"duckdb", "bigquery", "postgres"}`.

---

### Sub-Feature 3: Refactor `IbisConnector` — use `backend_specs` + add Postgres

**File**: `aitaem/connectors/ibis_connector.py`

Two changes in one sub-feature:

**3a — Refactor existing backends:**
- Replace the inline `project_id` check in `_connect_bigquery` with a call to `validate_backend_config("bigquery", kwargs)` and use the returned dataclass to extract fields.
- `_connect_duckdb` already receives `connection_string` positionally so no inline check exists there; no change needed.

**3b — Add Postgres support:**
- Add `"postgres"` to `SUPPORTED_BACKENDS`.
- Add `_connect_postgres(**kwargs)` that calls `validate_backend_config("postgres", kwargs)` and then `ibis.postgres.connect(...)`. Wraps connection errors in `AitaemConnectionError`.
- Dispatch to `_connect_postgres` in `connect()`.

```python
SUPPORTED_BACKENDS = {"duckdb", "bigquery", "postgres"}

def _connect_bigquery(self, **kwargs):
    cfg = validate_backend_config("bigquery", kwargs)  # raises ConfigurationError if missing
    try:
        self.connection = ibis.bigquery.connect(project_id=cfg.project_id)
    except Exception as e:
        ...

def _connect_postgres(self, **kwargs):
    cfg = validate_backend_config("postgres", kwargs)
    try:
        self.connection = ibis.postgres.connect(
            host=cfg.host,
            port=cfg.port,
            database=cfg.database,
            user=cfg.user,
            password=cfg.password,
        )
    except Exception as e:
        raise AitaemConnectionError(f"PostgreSQL connection failed: {str(e)}") from e
```

**Tests** (`tests/test_connectors/test_ibis_connector.py`):
- `IbisConnector("postgres")` does not raise.
- `IbisConnector("postgres")` is not connected before `connect()`.
- `_connect_postgres()` with missing required field raises `ConfigurationError`.
- Mocking `ibis.postgres.connect` to raise → `AitaemConnectionError` is raised.
- `IbisConnector("postgres")` `__repr__` reflects disconnected state.
- Existing BigQuery tests still pass (behavior unchanged, only internal refactor).

---

### Sub-Feature 4: Refactor `ConnectionManager` — use `backend_specs` + add Postgres

**File**: `aitaem/connectors/connection.py`

**4a — Refactor `add_connection()` validation:**

Replace the per-backend `if "field" not in config` blocks with a single generic call to `validate_backend_config(backend_type, config)`. This works because `BACKEND_SPECS` now covers all three backends.

Before (repeated for each backend):
```python
if backend_type == "duckdb":
    if "path" not in config:
        raise ConfigurationError(...)
elif backend_type == "bigquery":
    if "project_id" not in config:
        raise ConfigurationError(...)
```

After (single call, covers all backends including postgres):
```python
validate_backend_config(backend_type, config)  # raises ConfigurationError if invalid
```

**4b — Add Postgres connect dispatch:**

```python
elif backend_type == "postgres":
    new_connector.connect(**config)
```

**4c — Add `_parse_postgres_uri()` and register in `parse_source_uri()`:**

```python
elif backend_type == "postgres":
    return ConnectionManager._parse_postgres_uri(uri, full_path)

@staticmethod
def _parse_postgres_uri(uri: str, full_path: str) -> tuple[str, str, str]:
    """Parse Postgres URI.

    Format: postgres://schema/table  or  postgres:///table (no schema)

    Returns:
        Tuple of ('postgres', schema, table_name)
    """
    if "/" not in full_path:
        raise InvalidURIError(
            f"Missing table separator '/' in URI: '{uri}'\n\n"
            "Postgres URI format:\n"
            "  postgres://public/events\n"
            "  postgres:///events  (default schema)"
        )
    last_slash = full_path.rfind("/")
    schema = full_path[:last_slash]
    table = full_path[last_slash + 1:]
    if not table:
        raise InvalidURIError(
            f"Empty table name in URI: '{uri}'\n\n"
            "URI must include table name:\n"
            "  postgres://public/events"
        )
    return ("postgres", schema, table)
```

**Tests** (`tests/test_connectors/test_connection_manager.py`):
- `parse_source_uri("postgres://public/events")` → `("postgres", "public", "events")`.
- `parse_source_uri("postgres:///events")` → `("postgres", "", "events")`.
- `parse_source_uri("postgres://analytics/orders")` → `("postgres", "analytics", "orders")`.
- Malformed URIs raise `InvalidURIError`.
- `add_connection("postgres", ...)` without required fields raises `ConfigurationError`.
- `ConnectionManager.from_yaml(...)` with a valid postgres YAML config (mocked connect) succeeds.
- Existing DuckDB and BigQuery `add_connection` tests still pass.

---

### Sub-Feature 5: Update `_parse_table_name_from_uri` in query builder

**File**: `aitaem/query/builder.py`

For postgres, if a schema was specified, return `schema.table`; otherwise return just `table`.

```python
@staticmethod
def _parse_table_name_from_uri(source_uri: str) -> str:
    backend_type, schema, table = ConnectionManager.parse_source_uri(source_uri)
    if backend_type == "bigquery":
        return table  # already 'dataset.table'
    if backend_type == "postgres" and schema:
        return f"{schema}.{table}"
    return table
```

**Tests** (`tests/test_query/` or existing builder tests):
- `_parse_table_name_from_uri("postgres://public/events")` → `"public.events"`.
- `_parse_table_name_from_uri("postgres:///events")` → `"events"`.
- Existing DuckDB and BigQuery cases still pass.

---

### Sub-Feature 6: Integration test with a real Postgres connection

**File**: `tests/test_connectors/test_postgres_integration.py`

Write an integration test (marked `pytest.mark.integration`) that:
1. Connects to a local Postgres instance (skip if `POSTGRES_TEST_HOST` env var is not set).
2. Creates a temporary table, inserts rows.
3. Calls `connector.get_table()` and `connector.execute()`.
4. Verifies the results match inserted data.
5. Drops the temp table and closes connection.

This test is not run in CI by default. Add a `pytest.ini` marker or `conftest.py` skip guard.

---

### Sub-Feature 7: Update docs and changelog

Per the Documentation Instructions in CLAUDE.md:

1. **`aitaem/connectors/README.md`** — Add Postgres examples alongside existing DuckDB/BigQuery examples:
   - connections.yaml snippet
   - Source URI format examples
   - `add_connection()` call example

2. **`docs/api/`** — No new public modules are exported; no new API page needed. Update `docs/api/index.md` overview table to list `postgres` as a supported backend.

3. **`mkdocs.yml`** — No new nav entry needed (no new API page).

4. **`docs/changelog.md`** — Add entry under the next unreleased version:
   ```
   - Added PostgreSQL backend support via `ibis-framework[postgres]`
   ```

---

## Implementation Order

| # | Sub-Feature | Depends on |
|---|-------------|-----------|
| 1 | Add `postgres` optional dependency | — |
| 2 | Create `backend_specs.py` with config dataclasses | — |
| 3 | Refactor `IbisConnector` + add Postgres | 1, 2 |
| 4 | Refactor `ConnectionManager` + add Postgres | 2, 3 |
| 5 | Update `_parse_table_name_from_uri` | 4 |
| 6 | Integration test | 3, 4, 5 |
| 7 | Docs and changelog | 3, 4, 5 |

---

## Out of Scope

- SSL/TLS configuration for Postgres connections
- Connection pooling
- Schema introspection helpers
- Full connection string (`url`) support (straightforward future addition to `PostgresConfig` and `_connect_postgres`)
- Any changes to `specs/`, `insights.py`, or the query optimizer
