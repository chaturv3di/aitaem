"""
aitaem.connectors.connection - Connection manager for multiple backends

Manages multiple backend connections and routes queries via URI parsing.
"""

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from aitaem.connectors.ibis_connector import IbisConnector
from aitaem.utils.exceptions import (
    ConfigurationError,
    ConnectionNotFoundError,
    InvalidURIError,
    UnsupportedBackendError,
)


class ConnectionManager:
    """Manages multiple backend connections and routes queries.

    Responsibilities:
        - Load connections from YAML configuration
        - Environment variable substitution
        - Store and retrieve connectors by backend type
        - Parse source URIs and route to appropriate connector
        - Global singleton pattern for session-wide access
    """

    _global_instance: "ConnectionManager | None" = None

    def __init__(self) -> None:
        """Initialize empty connection manager."""
        self._connections: dict[str, IbisConnector] = {}

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "ConnectionManager":
        """Load all connections from YAML file.

        Args:
            yaml_path: Path to YAML configuration file

        Returns:
            ConnectionManager instance with loaded connections

        Raises:
            FileNotFoundError: If YAML file doesn't exist
            ConfigurationError: If YAML is invalid or missing required fields
        """
        yaml_file = Path(yaml_path)
        if not yaml_file.exists():
            raise FileNotFoundError(
                f"Connection configuration file not found: {yaml_path}\n\n"
                "Check that the file path is correct and the file exists."
            )

        try:
            with open(yaml_file, "r") as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigurationError(
                f"Invalid YAML syntax in {yaml_path}:\n{str(e)}\n\n"
                "Fix the YAML syntax and try again."
            ) from e

        if config is None:
            config = {}

        manager = cls()

        # Process each backend configuration
        for backend_type, backend_config in config.items():
            if not isinstance(backend_config, dict):
                raise ConfigurationError(
                    f"Invalid configuration for backend '{backend_type}' in {yaml_path}\n"
                    "Expected a mapping (key-value pairs)."
                )

            # Substitute environment variables in all config values
            substituted_config = manager._substitute_env_vars_in_dict(backend_config, yaml_path)

            try:
                manager.add_connection(backend_type, **substituted_config)
            except Exception as e:
                if isinstance(e, (ConfigurationError, UnsupportedBackendError)):
                    raise
                raise ConfigurationError(
                    f"Failed to create connection for backend '{backend_type}': {str(e)}"
                ) from e

        return manager

    def _substitute_env_vars_in_dict(
        self, config: dict[str, Any], yaml_path: str
    ) -> dict[str, Any]:
        """Recursively substitute environment variables in config dictionary.

        Args:
            config: Configuration dictionary
            yaml_path: Path to YAML file (for error messages)

        Returns:
            Dictionary with environment variables substituted

        Raises:
            ConfigurationError: If referenced environment variable is not set
        """
        result = {}
        for key, value in config.items():
            if isinstance(value, str):
                result[key] = self._substitute_env_vars(value, yaml_path)
            elif isinstance(value, dict):
                result[key] = self._substitute_env_vars_in_dict(value, yaml_path)
            else:
                result[key] = value
        return result

    def _substitute_env_vars(self, value: str, yaml_path: str) -> str:
        """Replace ${VAR_NAME} with environment variable values.

        Args:
            value: String potentially containing ${VAR_NAME} patterns
            yaml_path: Path to YAML file (for error messages)

        Returns:
            String with environment variables substituted

        Raises:
            ConfigurationError: If environment variable is not set
        """
        pattern = r"\$\{([^}]+)\}"

        def replace_var(match: re.Match) -> str:
            var_name = match.group(1)
            var_value = os.environ.get(var_name)
            if var_value is None:
                raise ConfigurationError(
                    f"Environment variable '{var_name}' referenced in {yaml_path} but not set\n\n"
                    f"Set the environment variable:\n"
                    f"  export {var_name}=your-value"
                )
            return var_value

        return re.sub(pattern, replace_var, value)

    def add_connection(self, backend_type: str, **config: Any) -> None:
        """Add single connection by creating and storing IbisConnector.

        Args:
            backend_type: Backend type ('duckdb', 'bigquery', etc.)
            **config: Backend-specific configuration
                - DuckDB: path (str), read_only (bool, optional)
                - BigQuery: project_id (str), dataset_id (str, optional)

        Raises:
            UnsupportedBackendError: If backend_type is not supported
            ConfigurationError: If required fields are missing
            ValueError: If configuration is invalid
        """
        # Validate required fields based on backend type
        if backend_type == "duckdb":
            if "path" not in config:
                raise ConfigurationError(
                    "Missing required field 'path' in duckdb configuration\n\n"
                    "Add the required field:\n"
                    "  duckdb:\n"
                    "    path: analytics.db"
                )
        elif backend_type == "bigquery":
            if "project_id" not in config:
                raise ConfigurationError(
                    "Missing required field 'project_id' in bigquery configuration\n\n"
                    "Add the required field:\n"
                    "  bigquery:\n"
                    "    project_id: your-project-id"
                )

        # Create connector
        try:
            connector = IbisConnector(backend_type)
        except UnsupportedBackendError:
            raise

        # Connect using appropriate method
        if backend_type == "duckdb":
            path = config.pop("path")
            connector.connect(path, **config)
        elif backend_type == "bigquery":
            connector.connect(**config)

        # Store connector
        self._connections[backend_type] = connector

    def get_connection(self, backend_type: str) -> IbisConnector:
        """Get connector by backend type.

        Args:
            backend_type: Backend type ('duckdb', 'bigquery', etc.)

        Returns:
            IbisConnector for the specified backend

        Raises:
            ConnectionNotFoundError: If backend not configured
        """
        if backend_type not in self._connections:
            raise ConnectionNotFoundError(
                f"No connection configured for backend '{backend_type}'\n\n"
                "Add the connection to your connections.yaml:\n"
                f"  {backend_type}:\n"
                "    # backend-specific config\n\n"
                "Or call add_connection():\n"
                f"  manager.add_connection('{backend_type}', ...)"
            )
        return self._connections[backend_type]

    def get_connection_for_source(self, source_uri: str) -> IbisConnector:
        """Parse URI and return appropriate connector.

        Args:
            source_uri: Source URI (e.g., 'duckdb://analytics.db/events')

        Returns:
            IbisConnector for the backend specified in URI

        Raises:
            InvalidURIError: If URI is malformed
            ConnectionNotFoundError: If backend not configured
        """
        backend_type, _, _ = self.parse_source_uri(source_uri)
        return self.get_connection(backend_type)

    @staticmethod
    def parse_source_uri(uri: str) -> tuple[str, str, str]:
        """Parse source URI into (backend, database, table).

        Format: backend://database_identifier/table_name

        DuckDB Examples:
            - 'duckdb://analytics.db/events' → ('duckdb', 'analytics.db', 'events')
            - 'duckdb://:memory:/events' → ('duckdb', ':memory:', 'events')
            - 'duckdb:///abs/path/db/events' → ('duckdb', '/abs/path/db', 'events')

        BigQuery Examples:
            - 'bigquery://my-project.dataset.table' → ('bigquery', 'my-project', 'dataset.table')
            - 'bigquery://project/dataset.table' → ('bigquery', 'project', 'dataset.table')

        Args:
            uri: Source URI

        Returns:
            Tuple of (backend_type, database_identifier, table_name)

        Raises:
            InvalidURIError: If URI is malformed
        """
        # Parse URI
        parsed = urlparse(uri)

        # Extract backend type (scheme)
        backend_type = parsed.scheme
        if not backend_type:
            raise InvalidURIError(
                f"Missing backend type in URI: '{uri}'\n\n"
                "URI must start with backend type:\n"
                "  duckdb://analytics.db/events\n"
                "  bigquery://project.dataset.table"
            )

        # Combine netloc and path
        full_path = parsed.netloc + parsed.path

        if not full_path:
            raise InvalidURIError(
                f"Empty path in URI: '{uri}'\n\nURI must include database and table."
            )

        # Backend-specific parsing
        if backend_type == "duckdb":
            return ConnectionManager._parse_duckdb_uri(uri, full_path)
        elif backend_type == "bigquery":
            return ConnectionManager._parse_bigquery_uri(uri, full_path)
        else:
            # For unknown backends, try generic parsing
            # This will fail later in get_connection with UnsupportedBackendError
            if "/" not in full_path:
                raise InvalidURIError(f"Missing table separator '/' in URI: '{uri}'")
            last_slash = full_path.rfind("/")
            database = full_path[:last_slash]
            table = full_path[last_slash + 1 :]
            if not table:
                raise InvalidURIError(
                    f"Empty table name in URI: '{uri}'\n\nURI must include table name."
                )
            return (backend_type, database, table)

    @staticmethod
    def _parse_duckdb_uri(uri: str, full_path: str) -> tuple[str, str, str]:
        """Parse DuckDB URI.

        Args:
            uri: Original URI (for error messages)
            full_path: Combined netloc + path

        Returns:
            Tuple of ('duckdb', database_path, table_name)

        Raises:
            InvalidURIError: If URI is malformed
        """
        if "/" not in full_path:
            raise InvalidURIError(
                f"Missing table separator '/' in URI: '{uri}'\n\n"
                "DuckDB URI format:\n"
                "  duckdb://database.db/table_name"
            )

        # Find LAST '/' to separate database from table
        last_slash = full_path.rfind("/")
        database = full_path[:last_slash]
        table = full_path[last_slash + 1 :]

        if not table:
            raise InvalidURIError(
                f"Empty table name in URI: '{uri}'\n\n"
                "URI must include table name:\n"
                "  duckdb://analytics.db/events"
            )

        return ("duckdb", database, table)

    @staticmethod
    def _parse_bigquery_uri(uri: str, full_path: str) -> tuple[str, str, str]:
        """Parse BigQuery URI.

        Args:
            uri: Original URI (for error messages)
            full_path: Combined netloc + path

        Returns:
            Tuple of ('bigquery', project_id, 'dataset.table')

        Raises:
            InvalidURIError: If URI is malformed or has <3 parts
        """
        # Normalize: replace all '/' with '.'
        normalized = full_path.replace("/", ".")

        # Split on '.'
        parts = normalized.split(".")

        if len(parts) < 3:
            raise InvalidURIError(
                f"BigQuery URI must have at least 3 parts (project.dataset.table): '{uri}'\n\n"
                "Valid formats:\n"
                "  bigquery://project.dataset.table\n"
                "  bigquery://project/dataset.table"
            )

        # Extract project (first part) and table (everything else as dataset.table)
        project = parts[0]
        table = ".".join(parts[1:])

        return ("bigquery", project, table)

    @classmethod
    def set_global(cls, manager: "ConnectionManager") -> None:
        """Set global singleton instance.

        Args:
            manager: ConnectionManager instance to set as global
        """
        cls._global_instance = manager

    @classmethod
    def get_global(cls) -> "ConnectionManager":
        """Get global singleton instance.

        Returns:
            Global ConnectionManager instance

        Raises:
            RuntimeError: If global instance not set
        """
        if cls._global_instance is None:
            raise RuntimeError(
                "No global ConnectionManager set. Call set_global() first.\n\n"
                "Example:\n"
                "  manager = ConnectionManager.from_yaml('connections.yaml')\n"
                "  ConnectionManager.set_global(manager)"
            )
        return cls._global_instance

    def close_all(self) -> None:
        """Close all connections."""
        for connector in self._connections.values():
            connector.close()
        self._connections.clear()

    def __repr__(self) -> str:
        """Return string representation of manager."""
        backends = list(self._connections.keys())
        return f"ConnectionManager(backends={backends})"
