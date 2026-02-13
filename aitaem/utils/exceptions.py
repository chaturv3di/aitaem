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


class QueryExecutionError(AitaemError):
    """Raised when query execution fails.

    Examples:
        - SQL syntax errors
        - Type conversion failures
        - Resource exhaustion
    """

    pass
