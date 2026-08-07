"""
aitaem.connectors.ibis_connector - Ibis-based multi-backend connector

Provides unified connector for DuckDB and BigQuery via Ibis abstraction layer.
"""

from typing import Any

import ibis
import pandas as pd

from aitaem.connectors.backend_specs import validate_backend_config
from aitaem.utils.exceptions import (
    AitaemConnectionError,
    ConfigurationError,
    QueryExecutionError,
    TableNotFoundError as AitaemTableNotFoundError,
    UnsupportedBackendError,
)

# Import ibis-specific exceptions
try:
    from ibis.common.exceptions import IbisError
except ImportError:
    IbisError = Exception


class IbisConnector:
    """Unified connector supporting DuckDB and BigQuery via Ibis.

    Attributes:
        backend_type: Type of backend ('duckdb' or 'bigquery')
        connection: Ibis backend connection object
    """

    SUPPORTED_BACKENDS = {"duckdb", "bigquery", "postgres"}

    def __init__(self, backend_type: str):
        """Initialize connector for specified backend type.

        Args:
            backend_type: Backend type - 'duckdb' or 'bigquery'

        Raises:
            UnsupportedBackendError: If backend_type is not supported
        """
        if backend_type not in self.SUPPORTED_BACKENDS:
            raise UnsupportedBackendError(
                f"Backend type '{backend_type}' not supported\n\n"
                f"Supported backends: {', '.join(sorted(self.SUPPORTED_BACKENDS))}"
            )

        self.backend_type = backend_type
        self.connection: ibis.BaseBackend | None = None
        self._bq_project_id: str | None = None
        self._bq_dataset_id: str | None = None
        self._duckdb_database: str | None = None
        self._pg_schema: str | None = None

    def connect(self, connection_string: str | None = None, **kwargs: Any) -> None:
        """Establish connection to the backend.

        Args:
            connection_string: Backend-specific connection string
                - DuckDB: file path or ':memory:' (default: ':memory:')
                - BigQuery: Not used (pass project_id via kwargs)
            **kwargs: Additional backend-specific parameters
                - DuckDB: read_only (bool)
                - BigQuery: project_id (str, required)

        Raises:
            AitaemConnectionError: If connection fails
            ValueError: If required parameters are missing
        """
        try:
            if self.backend_type == "duckdb":
                self._connect_duckdb(connection_string, **kwargs)
            elif self.backend_type == "bigquery":
                self._connect_bigquery(**kwargs)
            elif self.backend_type == "postgres":
                self._connect_postgres(**kwargs)
        except Exception as e:
            if isinstance(e, (AitaemConnectionError, ConfigurationError, ValueError)):
                raise
            raise AitaemConnectionError(
                f"Failed to connect to {self.backend_type}: {str(e)}"
            ) from e

    def _connect_duckdb(self, connection_string: str | None = None, **kwargs: Any) -> None:
        """Connect to DuckDB database.

        Args:
            connection_string: File path or ':memory:' (default: ':memory:')
            **kwargs: Additional parameters (e.g., read_only)
        """
        database = connection_string if connection_string is not None else ":memory:"

        try:
            self.connection = ibis.duckdb.connect(database=database, **kwargs)
            self._duckdb_database = database
        except Exception as e:
            raise AitaemConnectionError(
                f"DuckDB connection failed for database '{database}': {str(e)}"
            ) from e

    def _connect_bigquery(self, **kwargs: Any) -> None:
        """Connect to BigQuery using Application Default Credentials.

        Args:
            **kwargs: Must include 'project_id'

        Raises:
            ConfigurationError: If project_id is missing
            AitaemConnectionError: If connection fails or ADC not configured
        """
        cfg = validate_backend_config("bigquery", kwargs)

        try:
            bq_kwargs = {"project_id": cfg.project_id}
            if cfg.dataset_id is not None:
                bq_kwargs["dataset_id"] = cfg.dataset_id
            self.connection = ibis.bigquery.connect(**bq_kwargs)
            self._bq_project_id = cfg.project_id
            self._bq_dataset_id = cfg.dataset_id
        except Exception as e:
            error_msg = str(e).lower()
            if "credentials" in error_msg or "authentication" in error_msg:
                raise AitaemConnectionError(
                    "BigQuery connection failed. Application Default Credentials not found.\n\n"
                    "To fix this, run:\n"
                    "  gcloud auth application-default login\n\n"
                    "Or set GOOGLE_APPLICATION_CREDENTIALS environment variable:\n"
                    "  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json"
                ) from e
            raise AitaemConnectionError(f"BigQuery connection failed: {str(e)}") from e

    def _connect_postgres(self, **kwargs: Any) -> None:
        """Connect to PostgreSQL.

        Args:
            **kwargs: Must include 'database', 'user', 'password'.
                Optional: 'host' (default 'localhost'), 'port' (default 5432)

        Raises:
            ConfigurationError: If required fields are missing
            AitaemConnectionError: If connection fails
        """
        cfg = validate_backend_config("postgres", kwargs)

        try:
            self.connection = ibis.postgres.connect(
                host=cfg.host,
                port=cfg.port,
                database=cfg.database,
                user=cfg.user,
                password=cfg.password,
            )
            cursor = self.connection.raw_sql("SELECT current_schema()")
            self._pg_schema = cursor.fetchone()[0]
        except Exception as e:
            raise AitaemConnectionError(f"PostgreSQL connection failed: {str(e)}") from e

    def list_tables(self) -> list[str]:
        """List all tables available in the connected backend.

        Returns:
            List of table names available in the backend.

        Raises:
            AitaemConnectionError: If not connected.
        """
        if not self.is_connected:
            raise AitaemConnectionError(
                f"Not connected to {self.backend_type}. Call connect() first."
            )
        assert self.connection is not None
        return self.connection.list_tables()

    def build_source_uri(self, table_name: str) -> str | None:
        """Build a ready-to-use source: URI for a bare table name on this connection.

        Returns None when the connection can't unambiguously locate the
        table: not connected, or BigQuery with no default dataset configured
        (see Plan 35 §1 — not reachable via list_tables() today, since a
        project-only-scoped BigQuery connection's list_tables() call already
        fails for the whole backend before any name is returned).

        Args:
            table_name: Bare table name, as returned by list_tables().

        Returns:
            A source: URI (e.g. 'duckdb://analytics.db/events'), or None.
        """
        if self.connection is None:
            return None
        if self.backend_type == "duckdb":
            return f"duckdb://{self._duckdb_database}/{table_name}"
        if self.backend_type == "postgres":
            if self._pg_schema is None:
                return None
            return f"postgres://{self._pg_schema}/{table_name}"
        if self.backend_type == "bigquery":
            if self._bq_dataset_id is None:
                return None
            return f"bigquery://{self._bq_project_id}/{self._bq_dataset_id}.{table_name}"
        return None

    def get_table(
        self, table_name: str, database: str | None = None
    ) -> ibis.expr.types.Table:
        """Get a table reference from the backend.

        Args:
            table_name: Bare table name.
            database: Database/schema location, when the backend needs one to
                resolve the table — BigQuery: 'dataset' or 'project.dataset';
                Postgres: schema. None for DuckDB.

        Returns:
            Ibis table expression

        Raises:
            AitaemConnectionError: If not connected
            AitaemTableNotFoundError: If table doesn't exist
        """
        if not self.is_connected:
            raise AitaemConnectionError(
                f"Not connected to {self.backend_type}. Call connect() first."
            )

        try:
            assert self.connection is not None
            if database is not None:
                return self.connection.table(table_name, database=database)
            return self.connection.table(table_name)
        except IbisError as e:
            # Check if it's a table not found error
            error_msg = str(e).lower()
            error_type = type(e).__name__.lower()
            if (
                "not found" in error_msg
                or "does not exist" in error_msg
                or "tablenotfound" in error_type
            ):
                raise AitaemTableNotFoundError(
                    f"Table '{table_name}' not found in {self.backend_type} backend"
                ) from e
            raise
        except Exception as e:
            # Catch other exceptions and check for table not found patterns
            error_msg = str(e).lower()
            error_type = type(e).__name__.lower()
            if (
                "not found" in error_msg
                or "does not exist" in error_msg
                or "tablenotfound" in error_type
            ):
                raise AitaemTableNotFoundError(
                    f"Table '{table_name}' not found in {self.backend_type} backend"
                ) from e
            raise

    def execute(
        self, expr: ibis.expr.types.Expr, output_format: str = "pandas"
    ) -> pd.DataFrame | Any:
        """Execute a query and return results.

        Args:
            expr: Ibis expression to execute
            output_format: Output format - 'pandas' or 'polars'

        Returns:
            Query results as DataFrame (pandas or polars)

        Raises:
            AitaemConnectionError: If not connected
            QueryExecutionError: If query execution fails
            ValueError: If output_format is invalid
        """
        if not self.is_connected:
            raise AitaemConnectionError(
                f"Not connected to {self.backend_type}. Call connect() first."
            )

        if output_format not in {"pandas", "polars"}:
            raise ValueError(
                f"Invalid output_format '{output_format}'. Supported formats: 'pandas', 'polars'"
            )

        try:
            if output_format == "pandas":
                return expr.to_pandas()
            else:  # polars
                return expr.to_polars()
        except Exception as e:
            raise QueryExecutionError(
                f"Query execution failed on {self.backend_type}: {str(e)}"
            ) from e

    def close(self) -> None:
        """Close the connection and release the backend file lock."""
        conn = getattr(self, "connection", None)
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass
            self.connection = None

    def __del__(self) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        """Check if connection is active.

        Returns:
            True if connected, False otherwise
        """
        return self.connection is not None

    def __repr__(self) -> str:
        """Return string representation of connector."""
        status = "connected" if self.is_connected else "disconnected"
        return f"IbisConnector(backend='{self.backend_type}', status='{status}')"
