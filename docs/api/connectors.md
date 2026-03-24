# Connectors API

## ConnectionManager

::: aitaem.connectors.connection.ConnectionManager

---

## Backend Configuration

Each backend type has a corresponding configuration dataclass. These are the authoritative definitions of what fields each backend accepts — required fields have no default, optional fields do.

`ConnectionManager.add_connection()` and `ConnectionManager.from_yaml()` both validate the supplied config against the relevant dataclass and raise `ConfigurationError` with a clear message and YAML snippet if a required field is missing.

### DuckDB

::: aitaem.connectors.backend_specs.DuckDBConfig

### BigQuery

::: aitaem.connectors.backend_specs.BigQueryConfig

### PostgreSQL

::: aitaem.connectors.backend_specs.PostgresConfig

---

## validate_backend_config

::: aitaem.connectors.backend_specs.validate_backend_config
