"""
aitaem.connectors.base - Abstract connector interface

Defines the contract for all backend connectors.
"""

from abc import ABC, abstractmethod
from typing import Any

import ibis
import pandas as pd


class Connector(ABC):
    """Abstract base class for backend connectors.

    All connector implementations must inherit from this class and implement
    all abstract methods.
    """

    @abstractmethod
    def connect(self, connection_string: str, **kwargs: Any) -> None:
        """Establish connection to the backend.

        Args:
            connection_string: Backend-specific connection string
                - DuckDB: file path or ':memory:'
                - BigQuery: project ID
            **kwargs: Additional backend-specific connection parameters

        Raises:
            ConnectionError: If connection fails
            UnsupportedBackendError: If backend type is not supported
        """
        raise NotImplementedError("Subclasses must implement connect()")

    @abstractmethod
    def get_table(self, table_name: str) -> ibis.expr.types.Table:
        """Get a table reference from the backend.

        Args:
            table_name: Name of the table to retrieve
                - DuckDB: simple table name
                - BigQuery: 'dataset.table' or 'project.dataset.table'

        Returns:
            Ibis table expression

        Raises:
            ConnectionError: If not connected
            TableNotFoundError: If table doesn't exist
            InvalidURIError: If table name format is invalid
        """
        raise NotImplementedError("Subclasses must implement get_table()")

    @abstractmethod
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
            ConnectionError: If not connected
            QueryExecutionError: If query execution fails
            ValueError: If output_format is invalid
        """
        raise NotImplementedError("Subclasses must implement execute()")

    @abstractmethod
    def close(self) -> None:
        """Close the connection and cleanup resources.

        After calling this method, is_connected should return False.
        """
        raise NotImplementedError("Subclasses must implement close()")

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connection is active.

        Returns:
            True if connected, False otherwise
        """
        raise NotImplementedError("Subclasses must implement is_connected property")
