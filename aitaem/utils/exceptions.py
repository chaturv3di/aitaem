"""
aitaem.utils.exceptions - Custom exception hierarchy

All exceptions inherit from AitaemError base class and provide clear, actionable error messages.
"""


class AitaemError(Exception):
    """Base exception for all aitaem errors."""

    pass


class ConnectionError(AitaemError):
    """Raised when backend connection fails.

    Examples:
        - Database file cannot be accessed
        - BigQuery authentication fails
        - Network connection issues
    """

    pass


class ConnectionNotFoundError(AitaemError):
    """Raised when requested backend connection is not configured.

    Example:
        ConnectionNotFoundError: No connection configured for backend 'bigquery'
    """

    pass


class TableNotFoundError(AitaemError):
    """Raised when requested table doesn't exist in the backend.

    Example:
        TableNotFoundError: Table 'events' not found in DuckDB database
    """

    pass


class ConfigurationError(AitaemError):
    """Raised when configuration is invalid or incomplete.

    Examples:
        - Invalid YAML syntax
        - Missing required fields
        - Environment variables not set
    """

    pass


class InvalidURIError(AitaemError):
    """Raised when source URI is malformed or invalid.

    Examples:
        - Missing backend type (scheme)
        - Empty table name
        - BigQuery URI with insufficient parts
    """

    pass


class UnsupportedBackendError(AitaemError):
    """Raised when backend type is not supported.

    Example:
        UnsupportedBackendError: Backend type 'clickhouse' not supported
    """

    pass


class QueryBuildError(AitaemError):
    """Raised when query construction fails due to invalid or incompatible specs."""

    pass


class QueryExecutionError(AitaemError):
    """Raised when query execution fails.

    Examples:
        - SQL syntax errors
        - Type conversion failures
        - Resource exhaustion
    """

    pass


class SpecValidationError(AitaemError):
    """Raised when a YAML spec fails validation."""

    def __init__(self, spec_type: str, name: str | None, errors: list) -> None:
        self.spec_type = spec_type
        self.name = name
        self.errors = errors
        name_str = f"'{name}'" if name else "(unknown name)"
        error_lines = "\n".join(
            f"  - Field '{e.field}': {e.message}"
            + (f" (suggestion: {e.suggestion})" if e.suggestion else "")
            for e in errors
        )
        super().__init__(f"Invalid {spec_type} spec {name_str}:\n{error_lines}")


class SpecNotFoundError(AitaemError):
    """Raised when a named spec cannot be found in configured paths."""

    def __init__(self, spec_type: str, name: str, searched_paths: list[str]) -> None:
        self.spec_type = spec_type
        self.name = name
        self.searched_paths = searched_paths
        if searched_paths:
            paths_str = "\n".join(f"  - {p}" for p in searched_paths)
            msg = f"{spec_type.capitalize()} '{name}' not found.\nSearched paths:\n{paths_str}"
        else:
            msg = f"No {spec_type} paths configured"
        super().__init__(msg)
