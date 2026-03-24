"""
aitaem.connectors.backend_specs - Backend configuration dataclasses

Single source of truth for the required and optional fields for each
supported backend type. Used by IbisConnector and ConnectionManager to
validate configuration without duplicating field checks.
"""

from dataclasses import dataclass
from typing import Any

from aitaem.utils.exceptions import ConfigurationError, UnsupportedBackendError


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

# YAML snippet shown in error messages, keyed by backend type
_YAML_SNIPPETS: dict[str, str] = {
    "duckdb": ("  duckdb:\n    path: analytics.db"),
    "bigquery": ("  bigquery:\n    project_id: your-project-id"),
    "postgres": (
        "  postgres:\n"
        "    host: localhost\n"
        "    port: 5432\n"
        "    database: mydb\n"
        "    user: myuser\n"
        "    password: ${POSTGRES_PASSWORD}"
    ),
}


def validate_backend_config(backend_type: str, config: dict[str, Any]) -> Any:
    """Validate a config dict against the backend's dataclass spec.

    Instantiates the backend's config dataclass from the provided dict,
    ignoring any extra keys that are not part of the dataclass (they may
    be pass-through kwargs consumed directly by the Ibis backend).

    Args:
        backend_type: Backend identifier (e.g. 'duckdb', 'bigquery', 'postgres')
        config: Configuration dictionary from connections.yaml or add_connection()

    Returns:
        Instantiated config dataclass (e.g. PostgresConfig)

    Raises:
        UnsupportedBackendError: If backend_type is not in BACKEND_SPECS
        ConfigurationError: If a required field is missing or has the wrong type
    """
    if backend_type not in BACKEND_SPECS:
        raise UnsupportedBackendError(
            f"Backend type '{backend_type}' not supported\n\n"
            f"Supported backends: {', '.join(sorted(BACKEND_SPECS.keys()))}"
        )

    spec_cls = BACKEND_SPECS[backend_type]
    import dataclasses

    known_fields = {f.name for f in dataclasses.fields(spec_cls)}
    filtered = {k: v for k, v in config.items() if k in known_fields}

    try:
        return spec_cls(**filtered)
    except TypeError as e:
        # TypeError message: "__init__() missing required argument: 'field'"
        msg = str(e)
        snippet = _YAML_SNIPPETS.get(backend_type, "")
        raise ConfigurationError(
            f"Invalid configuration for '{backend_type}' backend: {msg}\n\n"
            f"Expected configuration:\n{snippet}"
        ) from e
