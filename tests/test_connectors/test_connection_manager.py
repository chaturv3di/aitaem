"""
tests.test_connectors.test_connection_manager - Tests for ConnectionManager

Test coverage for YAML loading, environment variable substitution, URI parsing,
connection management, and global singleton pattern.
"""


import pytest

from aitaem.connectors import ConnectionManager, IbisConnector
from aitaem.utils.exceptions import (
    ConfigurationError,
    ConnectionNotFoundError,
    InvalidURIError,
    UnsupportedBackendError,
)

# Check for optional dependencies
try:
    import ibis

    _ = ibis.bigquery  # Trigger lazy loading
    HAS_BIGQUERY = True
except (ImportError, AttributeError):
    HAS_BIGQUERY = False


class TestYAMLLoading:
    """Test YAML configuration loading."""

    def test_load_valid_yaml(self):
        """Test loading valid YAML file with DuckDB backend."""
        yaml_path = "tests/test_connectors/fixtures/connections_duckdb_only.yaml"
        manager = ConnectionManager.from_yaml(yaml_path)

        assert isinstance(manager, ConnectionManager)
        assert "duckdb" in manager._connections

    def test_file_not_found(self):
        """Test that missing YAML file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError) as exc_info:
            ConnectionManager.from_yaml("nonexistent.yaml")

        assert "nonexistent.yaml" in str(exc_info.value)

    def test_invalid_yaml_syntax(self, tmp_path):
        """Test that invalid YAML syntax raises ConfigurationError."""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text("invalid: yaml: syntax:")

        with pytest.raises(ConfigurationError) as exc_info:
            ConnectionManager.from_yaml(str(yaml_file))

        assert "Invalid YAML syntax" in str(exc_info.value)

    def test_empty_yaml_file(self, tmp_path):
        """Test that empty YAML file creates manager with no connections."""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")

        manager = ConnectionManager.from_yaml(str(yaml_file))
        assert len(manager._connections) == 0

    def test_missing_required_field_duckdb(self, tmp_path):
        """Test that missing 'path' in DuckDB config raises ConfigurationError."""
        yaml_file = tmp_path / "missing_path.yaml"
        yaml_file.write_text("duckdb:\n  read_only: true\n")

        with pytest.raises(ConfigurationError) as exc_info:
            ConnectionManager.from_yaml(str(yaml_file))

        assert "path" in str(exc_info.value)

    def test_missing_required_field_bigquery(self, tmp_path):
        """Test that missing 'project_id' in BigQuery config raises ConfigurationError."""
        yaml_file = tmp_path / "missing_project.yaml"
        yaml_file.write_text("bigquery:\n  dataset_id: test\n")

        with pytest.raises(ConfigurationError) as exc_info:
            ConnectionManager.from_yaml(str(yaml_file))

        assert "project_id" in str(exc_info.value)


class TestEnvironmentVariableSubstitution:
    """Test environment variable substitution in YAML configuration."""

    def test_single_substitution(self, monkeypatch, tmp_path):
        """Test single environment variable substitution."""
        yaml_file = tmp_path / "env_sub.yaml"
        yaml_file.write_text(
            """
duckdb:
  path: ${DUCKDB_PATH}
"""
        )

        monkeypatch.setenv("DUCKDB_PATH", ":memory:")

        manager = ConnectionManager.from_yaml(str(yaml_file))

        # Check that connection was created successfully
        assert "duckdb" in manager._connections

    def test_multiple_substitutions_different_fields(self, monkeypatch, tmp_path):
        """Test multiple substitutions in different fields."""
        yaml_file = tmp_path / "multi_env.yaml"
        yaml_file.write_text(
            """
duckdb:
  path: ${DB_PATH}
"""
        )

        monkeypatch.setenv("DB_PATH", ":memory:")

        manager = ConnectionManager.from_yaml(str(yaml_file))
        assert "duckdb" in manager._connections

    def test_multiple_substitutions_single_value(self, monkeypatch, tmp_path):
        """Test multiple substitutions in a single value."""
        yaml_file = tmp_path / "multi_in_one.yaml"
        yaml_file.write_text(
            """
duckdb:
  path: ${DB_PREFIX}${DB_SUFFIX}
"""
        )

        monkeypatch.setenv("DB_PREFIX", ":mem")
        monkeypatch.setenv("DB_SUFFIX", "ory:")

        manager = ConnectionManager.from_yaml(str(yaml_file))
        assert "duckdb" in manager._connections

    def test_missing_env_var(self, tmp_path):
        """Test that missing environment variable raises ConfigurationError."""
        yaml_file = tmp_path / "missing_env.yaml"
        yaml_file.write_text(
            """
duckdb:
  path: ${MISSING_VAR}
"""
        )

        with pytest.raises(ConfigurationError) as exc_info:
            ConnectionManager.from_yaml(str(yaml_file))

        assert "MISSING_VAR" in str(exc_info.value)
        assert "not set" in str(exc_info.value)


class TestConnectionManagement:
    """Test connection management functionality."""

    def test_add_connection_duckdb(self):
        """Test adding DuckDB connection."""
        manager = ConnectionManager()
        manager.add_connection("duckdb", path=":memory:")

        assert "duckdb" in manager._connections
        assert isinstance(manager._connections["duckdb"], IbisConnector)
        assert manager._connections["duckdb"].is_connected

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_add_connection_bigquery(self, mocker):
        """Test adding BigQuery connection (mocked)."""
        mock_backend = mocker.Mock()
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        manager = ConnectionManager()
        manager.add_connection("bigquery", project_id="test-project")

        assert "bigquery" in manager._connections
        assert isinstance(manager._connections["bigquery"], IbisConnector)

    def test_get_connection_success(self):
        """Test getting existing connection by backend type."""
        manager = ConnectionManager()
        manager.add_connection("duckdb", path=":memory:")

        connector = manager.get_connection("duckdb")
        assert isinstance(connector, IbisConnector)
        assert connector.backend_type == "duckdb"

    def test_get_connection_not_found(self):
        """Test that getting non-existent connection raises ConnectionNotFoundError."""
        manager = ConnectionManager()

        with pytest.raises(ConnectionNotFoundError) as exc_info:
            manager.get_connection("bigquery")

        assert "bigquery" in str(exc_info.value)

    def test_unsupported_backend(self):
        """Test that unsupported backend in config raises UnsupportedBackendError."""
        manager = ConnectionManager()

        with pytest.raises(UnsupportedBackendError):
            manager.add_connection("clickhouse", host="localhost")


class TestURIParsing:
    """Test URI parsing functionality."""

    def test_parse_duckdb_uri_simple(self):
        """Test parsing simple DuckDB URI."""
        backend, database, table = ConnectionManager.parse_source_uri(
            "duckdb://analytics.db/events"
        )
        assert backend == "duckdb"
        assert database == "analytics.db"
        assert table == "events"

    def test_parse_duckdb_uri_memory(self):
        """Test parsing DuckDB in-memory URI."""
        backend, database, table = ConnectionManager.parse_source_uri("duckdb://:memory:/events")
        assert backend == "duckdb"
        assert database == ":memory:"
        assert table == "events"

    def test_parse_duckdb_uri_absolute_path(self):
        """Test parsing DuckDB URI with absolute path."""
        backend, database, table = ConnectionManager.parse_source_uri("duckdb:///abs/path/db/table")
        assert backend == "duckdb"
        assert database == "/abs/path/db"
        assert table == "table"

    def test_parse_duckdb_uri_nested_path(self):
        """Test parsing DuckDB URI with nested path."""
        backend, database, table = ConnectionManager.parse_source_uri(
            "duckdb://data/analytics/prod.db/events"
        )
        assert backend == "duckdb"
        assert database == "data/analytics/prod.db"
        assert table == "events"

    def test_parse_bigquery_uri_dot_format(self):
        """Test parsing BigQuery URI with dot format."""
        backend, project, table = ConnectionManager.parse_source_uri(
            "bigquery://my-project.dataset.table"
        )
        assert backend == "bigquery"
        assert project == "my-project"
        assert table == "dataset.table"

    def test_parse_bigquery_uri_slash_format(self):
        """Test parsing BigQuery URI with slash format (normalized to dots)."""
        backend, project, table = ConnectionManager.parse_source_uri(
            "bigquery://project/dataset.table"
        )
        assert backend == "bigquery"
        assert project == "project"
        assert table == "dataset.table"

    def test_parse_bigquery_uri_extra_parts(self):
        """Test parsing BigQuery URI with extra parts (>3)."""
        backend, project, table = ConnectionManager.parse_source_uri("bigquery://proj.ds.tbl.extra")
        assert backend == "bigquery"
        assert project == "proj"
        assert table == "ds.tbl.extra"

    def test_missing_scheme(self):
        """Test that URI without scheme raises InvalidURIError."""
        with pytest.raises(InvalidURIError) as exc_info:
            ConnectionManager.parse_source_uri("analytics.db/events")

        assert "Missing backend type" in str(exc_info.value)

    def test_empty_table_name(self):
        """Test that empty table name raises InvalidURIError."""
        with pytest.raises(InvalidURIError) as exc_info:
            ConnectionManager.parse_source_uri("duckdb://analytics.db/")

        assert "Empty table name" in str(exc_info.value)

    def test_bigquery_insufficient_parts(self):
        """Test that BigQuery URI with <3 parts raises InvalidURIError."""
        with pytest.raises(InvalidURIError) as exc_info:
            ConnectionManager.parse_source_uri("bigquery://project.dataset")

        assert "at least 3 parts" in str(exc_info.value)

    def test_duckdb_no_table_separator(self):
        """Test that DuckDB URI without table separator raises InvalidURIError."""
        with pytest.raises(InvalidURIError) as exc_info:
            ConnectionManager.parse_source_uri("duckdb://analytics.db")

        assert "table separator" in str(exc_info.value).lower()


class TestConnectionRouting:
    """Test get_connection_for_source() routing functionality."""

    def test_get_connection_for_source_duckdb(self):
        """Test routing DuckDB URI to correct connection."""
        manager = ConnectionManager()
        manager.add_connection("duckdb", path=":memory:")

        connector = manager.get_connection_for_source("duckdb://:memory:/events")
        assert isinstance(connector, IbisConnector)
        assert connector.backend_type == "duckdb"

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_get_connection_for_source_bigquery(self, mocker):
        """Test routing BigQuery URI to correct connection."""
        mock_backend = mocker.Mock()
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        manager = ConnectionManager()
        manager.add_connection("bigquery", project_id="test-project")

        connector = manager.get_connection_for_source("bigquery://test-project.dataset.table")
        assert isinstance(connector, IbisConnector)
        assert connector.backend_type == "bigquery"

    def test_get_connection_for_source_not_configured(self):
        """Test that routing to unconfigured backend raises ConnectionNotFoundError."""
        manager = ConnectionManager()

        with pytest.raises(ConnectionNotFoundError):
            manager.get_connection_for_source("bigquery://project.dataset.table")


class TestGlobalSingleton:
    """Test global singleton pattern."""

    def test_set_global(self):
        """Test setting global ConnectionManager instance."""
        manager = ConnectionManager()
        ConnectionManager.set_global(manager)

        global_manager = ConnectionManager.get_global()
        assert global_manager is manager

    def test_get_global_before_set(self):
        """Test that getting global before set raises RuntimeError."""
        # Reset global instance
        ConnectionManager._global_instance = None

        with pytest.raises(RuntimeError) as exc_info:
            ConnectionManager.get_global()

        assert "No global ConnectionManager set" in str(exc_info.value)

    def test_global_singleton_persistence(self):
        """Test that global singleton persists across get_global() calls."""
        manager = ConnectionManager()
        manager.add_connection("duckdb", path=":memory:")

        ConnectionManager.set_global(manager)

        global1 = ConnectionManager.get_global()
        global2 = ConnectionManager.get_global()

        assert global1 is global2
        assert "duckdb" in global1._connections


class TestIntegration:
    """Test end-to-end integration scenarios."""

    def test_yaml_to_connection_end_to_end(self, monkeypatch, tmp_path):
        """Test complete flow: load YAML, get connection for URI."""
        yaml_file = tmp_path / "end_to_end.yaml"
        yaml_file.write_text(
            """
duckdb:
  path: ${DUCKDB_PATH}
"""
        )

        monkeypatch.setenv("DUCKDB_PATH", ":memory:")

        manager = ConnectionManager.from_yaml(str(yaml_file))

        # Get connection via URI
        connector = manager.get_connection_for_source("duckdb://:memory:/events")
        assert connector.is_connected
        assert connector.backend_type == "duckdb"

    def test_close_all(self):
        """Test closing all connections."""
        manager = ConnectionManager()
        manager.add_connection("duckdb", path=":memory:")

        assert manager._connections["duckdb"].is_connected

        manager.close_all()

        assert len(manager._connections) == 0

    def test_repr(self):
        """Test string representation of ConnectionManager."""
        manager = ConnectionManager()
        manager.add_connection("duckdb", path=":memory:")

        repr_str = repr(manager)
        assert "ConnectionManager" in repr_str
        assert "duckdb" in repr_str
