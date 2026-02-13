"""
tests.test_connectors.test_ibis_connector - Tests for IbisConnector

Test coverage for DuckDB and BigQuery connector functionality.
"""

import pytest

from aitaem.connectors import IbisConnector
from aitaem.utils.exceptions import (
    ConnectionError as AitaemConnectionError,
    InvalidURIError,
    TableNotFoundError,
    UnsupportedBackendError,
)

# Check for optional dependencies
try:
    import ibis.bigquery  # noqa: F401

    HAS_BIGQUERY = True
except (ImportError, AttributeError):
    HAS_BIGQUERY = False

try:
    import polars  # noqa: F401

    HAS_POLARS = True
except ImportError:
    HAS_POLARS = False


class TestIbisConnectorInitialization:
    """Test IbisConnector initialization and validation."""

    def test_valid_backend_duckdb(self):
        """Test creating connector with valid DuckDB backend."""
        connector = IbisConnector("duckdb")
        assert connector.backend_type == "duckdb"
        assert not connector.is_connected

    def test_valid_backend_bigquery(self):
        """Test creating connector with valid BigQuery backend."""
        connector = IbisConnector("bigquery")
        assert connector.backend_type == "bigquery"
        assert not connector.is_connected

    def test_invalid_backend_type(self):
        """Test that invalid backend type raises UnsupportedBackendError."""
        with pytest.raises(UnsupportedBackendError) as exc_info:
            IbisConnector("clickhouse")
        assert "clickhouse" in str(exc_info.value)
        assert "Supported backends" in str(exc_info.value)

    def test_initial_state_disconnected(self):
        """Test that connector starts in disconnected state."""
        connector = IbisConnector("duckdb")
        assert not connector.is_connected
        assert connector.connection is None


class TestDuckDBConnection:
    """Test DuckDB connection functionality."""

    def test_connect_memory_database(self):
        """Test connecting to in-memory DuckDB database."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")
        assert connector.is_connected
        connector.close()

    def test_connect_memory_database_default(self):
        """Test connecting to DuckDB with no connection string defaults to memory."""
        connector = IbisConnector("duckdb")
        connector.connect()
        assert connector.is_connected
        connector.close()

    def test_connect_file_database(self, tmp_path):
        """Test connecting to file-based DuckDB database."""
        db_path = tmp_path / "test.db"
        connector = IbisConnector("duckdb")
        connector.connect(str(db_path))
        assert connector.is_connected
        assert db_path.exists()
        connector.close()

    def test_connect_with_read_only(self, tmp_path):
        """Test connecting to DuckDB with read_only parameter."""
        db_path = tmp_path / "test.db"
        # First create the database
        connector1 = IbisConnector("duckdb")
        connector1.connect(str(db_path))
        connector1.close()

        # Then connect read-only
        connector2 = IbisConnector("duckdb")
        connector2.connect(str(db_path), read_only=True)
        assert connector2.is_connected
        connector2.close()

    def test_connection_state_after_connect(self):
        """Test that is_connected is True after successful connection."""
        connector = IbisConnector("duckdb")
        assert not connector.is_connected
        connector.connect(":memory:")
        assert connector.is_connected
        connector.close()


class TestBigQueryConnection:
    """Test BigQuery connection functionality (mocked)."""

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_connect_success(self, mocker):
        """Test successful BigQuery connection with mocked credentials."""
        mock_backend = mocker.Mock()
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")

        assert connector.is_connected
        connector.close()

    def test_connect_missing_project_id(self):
        """Test that missing project_id raises ValueError."""
        connector = IbisConnector("bigquery")
        with pytest.raises(ValueError) as exc_info:
            connector.connect()
        assert "project_id" in str(exc_info.value)

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_connect_adc_not_configured(self, mocker):
        """Test that missing ADC raises clear error message."""
        mocker.patch(
            "ibis.bigquery.connect",
            side_effect=Exception("Could not automatically determine credentials"),
        )

        connector = IbisConnector("bigquery")
        with pytest.raises(AitaemConnectionError) as exc_info:
            connector.connect(project_id="test-project")

        error_msg = str(exc_info.value)
        assert "Application Default Credentials" in error_msg
        assert "gcloud auth application-default login" in error_msg


class TestGetTable:
    """Test get_table() functionality."""

    def test_get_table_duckdb_success(self):
        """Test getting table from DuckDB with test data."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")

        # Create a test table
        connector.connection.raw_sql("CREATE TABLE events (id INTEGER, name VARCHAR)")
        connector.connection.raw_sql("INSERT INTO events VALUES (1, 'test')")

        # Get table reference
        table = connector.get_table("events")
        assert table is not None
        assert "events" in str(table)

        connector.close()

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_get_table_bigquery_success(self, mocker):
        """Test getting table from BigQuery (mocked)."""
        mock_table = mocker.Mock()
        mock_backend = mocker.Mock()
        mock_backend.table.return_value = mock_table
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")

        table = connector.get_table("dataset.table")
        assert table is not None
        mock_backend.table.assert_called_once_with("dataset.table")

        connector.close()

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_get_table_bigquery_three_part_name(self, mocker):
        """Test getting table with 3-part BigQuery name extracts dataset.table."""
        mock_table = mocker.Mock()
        mock_backend = mocker.Mock()
        mock_backend.table.return_value = mock_table
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")

        table = connector.get_table("project.dataset.table")
        assert table is not None
        # Should extract 'dataset.table' from 'project.dataset.table'
        mock_backend.table.assert_called_once_with("dataset.table")

        connector.close()

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_get_table_bigquery_invalid_name(self, mocker):
        """Test that single-part BigQuery table name raises InvalidURIError."""
        mock_backend = mocker.Mock()
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")

        with pytest.raises(InvalidURIError) as exc_info:
            connector.get_table("table")

        assert "at least 2 parts" in str(exc_info.value)
        connector.close()

    def test_get_table_not_found(self):
        """Test that non-existent table raises TableNotFoundError."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")

        # Try to get a table that doesn't exist
        # The connector should wrap it in TableNotFoundError
        with pytest.raises(TableNotFoundError) as exc_info:
            connector.get_table("nonexistent")

        assert "nonexistent" in str(exc_info.value)
        connector.close()

    def test_get_table_not_connected(self):
        """Test that get_table raises ConnectionError when not connected."""
        connector = IbisConnector("duckdb")
        with pytest.raises(AitaemConnectionError) as exc_info:
            connector.get_table("events")
        assert "Not connected" in str(exc_info.value)


class TestExecute:
    """Test execute() functionality."""

    def test_execute_pandas_output(self):
        """Test executing query and returning pandas DataFrame."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")

        # Create test table
        connector.connection.raw_sql("CREATE TABLE events (id INTEGER, name VARCHAR)")
        connector.connection.raw_sql("INSERT INTO events VALUES (1, 'test')")

        # Execute query
        table = connector.get_table("events")
        result = connector.execute(table, output_format="pandas")

        assert result is not None
        assert len(result) == 1
        assert result.iloc[0]["id"] == 1
        assert result.iloc[0]["name"] == "test"

        connector.close()

    @pytest.mark.skipif(not HAS_POLARS, reason="Polars not installed")
    def test_execute_polars_output(self):
        """Test executing query and returning polars DataFrame."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")

        # Create test table
        connector.connection.raw_sql("CREATE TABLE events (id INTEGER, name VARCHAR)")
        connector.connection.raw_sql("INSERT INTO events VALUES (1, 'test')")

        # Execute query
        table = connector.get_table("events")
        result = connector.execute(table, output_format="polars")

        assert result is not None
        assert len(result) == 1
        assert result["id"][0] == 1
        assert result["name"][0] == "test"

        connector.close()

    def test_execute_invalid_output_format(self):
        """Test that invalid output format raises ValueError."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")

        connector.connection.raw_sql("CREATE TABLE events (id INTEGER)")
        table = connector.get_table("events")

        with pytest.raises(ValueError) as exc_info:
            connector.execute(table, output_format="invalid")

        assert "Invalid output_format" in str(exc_info.value)
        connector.close()

    def test_execute_not_connected(self):
        """Test that execute raises ConnectionError when not connected."""
        connector = IbisConnector("duckdb")
        # Create a mock expression
        import ibis

        expr = ibis.literal(1)

        with pytest.raises(AitaemConnectionError) as exc_info:
            connector.execute(expr)
        assert "Not connected" in str(exc_info.value)


class TestLifecycle:
    """Test connection lifecycle management."""

    def test_close_connection(self):
        """Test that close() sets is_connected to False."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")
        assert connector.is_connected

        connector.close()
        assert not connector.is_connected

    def test_repr_disconnected(self):
        """Test __repr__ shows disconnected status."""
        connector = IbisConnector("duckdb")
        repr_str = repr(connector)
        assert "duckdb" in repr_str
        assert "disconnected" in repr_str

    def test_repr_connected(self):
        """Test __repr__ shows connected status."""
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")
        repr_str = repr(connector)
        assert "duckdb" in repr_str
        assert "connected" in repr_str
        connector.close()
