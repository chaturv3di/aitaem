"""
aitaem.connectors.ibis_connector - Ibis-based multi-backend connector

Provides unified connector for DuckDB and BigQuery via Ibis abstraction layer.
"""

from typing import Any

import ibis
import pandas as pd

from aitaem.connectors.base import Connector
from aitaem.utils.exceptions import (
    ConnectionError as AitaemConnectionError,
    InvalidURIError,
    QueryExecutionError,
    TableNotFoundError as AitaemTableNotFoundError,
    UnsupportedBackendError,
)

# Import ibis-specific exceptions
try:
    from ibis.common.exceptions import IbisError
except ImportError:
    IbisError = Exception


class IbisConnector(Connector):
    """Unified connector supporting DuckDB and BigQuery via Ibis.

    Attributes:
        backend_type: Type of backend ('duckdb' or 'bigquery')
        connection: Ibis backend connection object
    """

    SUPPORTED_BACKENDS = {"duckdb", "bigquery"}

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
        except Exception as e:
            if isinstance(e, (AitaemConnectionError, ValueError)):
                raise
            raise AitaemConnectionError(
                f"Failed to connect to {self.backend_type}: {str(e)}"
            ) from e

    def _connect_duckdb(
        self, connection_string: str | None = None, **kwargs: Any
    ) -> None:
        """Connect to DuckDB database.

        Args:
            connection_string: File path or ':memory:' (default: ':memory:')
            **kwargs: Additional parameters (e.g., read_only)
        """
        database = connection_string if connection_string is not None else ":memory:"

        try:
            self.connection = ibis.duckdb.connect(database=database, **kwargs)
        except Exception as e:
            raise AitaemConnectionError(
                f"DuckDB connection failed for database '{database}': {str(e)}"
            ) from e

    def _connect_bigquery(self, **kwargs: Any) -> None:
        """Connect to BigQuery using Application Default Credentials.

        Args:
            **kwargs: Must include 'project_id'

        Raises:
            ValueError: If project_id is missing
            AitaemConnectionError: If connection fails or ADC not configured
        """
        project_id = kwargs.get("project_id")
        if not project_id:
            raise ValueError(
                "Missing required parameter 'project_id' for BigQuery connection\n\n"
                "Add the project_id to your connections.yaml:\n"
                "  bigquery:\n"
                "    project_id: your-project-id"
            )

        try:
            self.connection = ibis.bigquery.connect(project_id=project_id)
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
            raise AitaemConnectionError(
                f"BigQuery connection failed: {str(e)}"
            ) from e

    def get_table(self, table_name: str) -> ibis.expr.types.Table:
        """Get a table reference from the backend.

        Args:
            table_name: Name of the table
                - DuckDB: simple table name (e.g., 'events')
                - BigQuery: 'dataset.table' or 'project.dataset.table'

        Returns:
            Ibis table expression

        Raises:
            AitaemConnectionError: If not connected
            AitaemTableNotFoundError: If table doesn't exist
            InvalidURIError: If BigQuery table name format is invalid
        """
        if not self.is_connected:
            raise AitaemConnectionError(
                f"Not connected to {self.backend_type}. Call connect() first."
            )

        try:
            # For BigQuery, parse table name to extract dataset.table
            if self.backend_type == "bigquery":
                table_name = self._parse_bigquery_table_name(table_name)

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

    def _parse_bigquery_table_name(self, table_name: str) -> str:
        """Extract dataset.table from fully-qualified BigQuery table name.

        Args:
            table_name: 'dataset.table' or 'project.dataset.table'

        Returns:
            'dataset.table' format for Ibis

        Raises:
            InvalidURIError: If table name has <2 parts
        """
        parts = table_name.split(".")
        if len(parts) < 2:
            raise InvalidURIError(
                f"BigQuery table name must have at least 2 parts (dataset.table): {table_name}\n\n"
                "Valid formats:\n"
                "  dataset.table\n"
                "  project.dataset.table"
            )
        if len(parts) == 2:
            return table_name  # Already in dataset.table format
        # 3+ parts: extract everything after first part (project)
        return ".".join(parts[1:])

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
                f"Invalid output_format '{output_format}'. "
                "Supported formats: 'pandas', 'polars'"
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
        """Close the connection and cleanup resources."""
        if self.connection is not None:
            # Ibis connections don't have an explicit close method in all backends
            # Setting to None will allow garbage collection
            self.connection = None

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
