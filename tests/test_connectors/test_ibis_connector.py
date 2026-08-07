"""
tests.test_connectors.test_ibis_connector - Tests for IbisConnector

Test coverage for DuckDB and BigQuery connector functionality.
"""

import pytest

from aitaem.connectors import IbisConnector
from aitaem.utils.exceptions import (
    AitaemConnectionError,
    ConfigurationError,
    TableNotFoundError,
    UnsupportedBackendError,
)

# Check for optional dependencies
try:
    import ibis

    _ = ibis.bigquery  # Trigger lazy loading
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

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_connect_without_dataset_id(self, mocker):
        """Test that dataset_id is not forwarded when not specified."""
        mock_connect = mocker.patch("ibis.bigquery.connect", return_value=mocker.Mock())

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")

        mock_connect.assert_called_once_with(project_id="test-project")

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_connect_with_dataset_id(self, mocker):
        """Test that dataset_id is forwarded to ibis when specified."""
        mock_connect = mocker.patch("ibis.bigquery.connect", return_value=mocker.Mock())

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project", dataset_id="my_dataset")

        mock_connect.assert_called_once_with(project_id="test-project", dataset_id="my_dataset")

    def test_connect_missing_project_id(self):
        """Test that missing project_id raises ConfigurationError."""
        connector = IbisConnector("bigquery")
        with pytest.raises(ConfigurationError) as exc_info:
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
        """Test getting table from BigQuery (mocked), database passed as a separate kwarg."""
        mock_table = mocker.Mock()
        mock_backend = mocker.Mock()
        mock_backend.table.return_value = mock_table
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")

        table = connector.get_table("table", database="dataset")
        assert table is not None
        mock_backend.table.assert_called_once_with("table", database="dataset")

        connector.close()

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_get_table_bigquery_no_database_passthrough(self, mocker):
        """No database kwarg: table_name passed straight through, no local resolution."""
        mock_table = mocker.Mock()
        mock_backend = mocker.Mock()
        mock_backend.table.return_value = mock_table
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project", dataset_id="my_dataset")

        table = connector.get_table("table")
        assert table is not None
        mock_backend.table.assert_called_once_with("table")

        connector.close()

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_get_table_bigquery_cross_project_dataset_succeeds(self, mocker):
        """A database naming a different project/dataset than the connection's own
        default now succeeds — scope enforcement was removed (Plan 35): the
        connection's own credentials are the real boundary, not an app-level check."""
        mock_table = mocker.Mock()
        mock_backend = mocker.Mock()
        mock_backend.table.return_value = mock_table
        mocker.patch("ibis.bigquery.connect", return_value=mock_backend)

        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project", dataset_id="my_dataset")

        table = connector.get_table("table", database="other-project.other_dataset")
        assert table is not None
        mock_backend.table.assert_called_once_with(
            "table", database="other-project.other_dataset"
        )

        connector.close()

    def test_get_table_postgres_schema_as_separate_kwarg(self, mocker):
        """Gap B regression: schema is passed as database=, never dot-joined into
        table_name — the join-then-resplit pattern that broke every Postgres
        table lookup is gone."""
        import aitaem.connectors.ibis_connector as ibis_connector_module

        mock_table = mocker.Mock()
        mock_backend = mocker.MagicMock()
        mock_backend.table.return_value = mock_table
        mock_backend.raw_sql.return_value.fetchone.return_value = ("public",)
        mock_ibis = mocker.MagicMock()
        mock_ibis.postgres.connect.return_value = mock_backend
        mocker.patch.object(ibis_connector_module, "ibis", mock_ibis)

        connector = IbisConnector("postgres")
        connector.connect(database="mydb", user="myuser", password="secret")

        table = connector.get_table("specs", database="public")
        assert table is mock_table
        mock_backend.table.assert_called_once_with("specs", database="public")

    def test_get_table_postgres_table_name_with_literal_dot(self, mocker):
        """A quoted Postgres identifier containing a literal '.' is passed through
        intact as table_name — never split on the embedded dot."""
        import aitaem.connectors.ibis_connector as ibis_connector_module

        mock_table = mocker.Mock()
        mock_backend = mocker.MagicMock()
        mock_backend.table.return_value = mock_table
        mock_backend.raw_sql.return_value.fetchone.return_value = ("public",)
        mock_ibis = mocker.MagicMock()
        mock_ibis.postgres.connect.return_value = mock_backend
        mocker.patch.object(ibis_connector_module, "ibis", mock_ibis)

        connector = IbisConnector("postgres")
        connector.connect(database="mydb", user="myuser", password="secret")

        table = connector.get_table("my.weird.table", database="public")
        assert table is mock_table
        mock_backend.table.assert_called_once_with("my.weird.table", database="public")

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


class TestBuildSourceURI:
    """Test build_source_uri() functionality (SF-1)."""

    def test_not_connected_returns_none(self):
        connector = IbisConnector("duckdb")
        assert connector.build_source_uri("events") is None

    def test_duckdb_memory(self):
        connector = IbisConnector("duckdb")
        connector.connect(":memory:")
        assert connector.build_source_uri("events") == "duckdb://:memory:/events"
        connector.close()

    def test_duckdb_file_path(self, tmp_path):
        db_path = tmp_path / "test.db"
        connector = IbisConnector("duckdb")
        connector.connect(str(db_path))
        assert connector.build_source_uri("events") == f"duckdb://{db_path}/events"
        connector.close()

    def test_postgres_with_schema(self, mocker):
        import aitaem.connectors.ibis_connector as ibis_connector_module

        mock_backend = mocker.MagicMock()
        mock_backend.raw_sql.return_value.fetchone.return_value = ("public",)
        mock_ibis = mocker.MagicMock()
        mock_ibis.postgres.connect.return_value = mock_backend
        mocker.patch.object(ibis_connector_module, "ibis", mock_ibis)

        connector = IbisConnector("postgres")
        connector.connect(database="mydb", user="myuser", password="secret")
        assert connector.build_source_uri("orders") == "postgres://public/orders"

    def test_postgres_without_captured_schema_returns_none(self, mocker):
        import aitaem.connectors.ibis_connector as ibis_connector_module

        mock_backend = mocker.MagicMock()
        mock_backend.raw_sql.return_value.fetchone.return_value = ("public",)
        mock_ibis = mocker.MagicMock()
        mock_ibis.postgres.connect.return_value = mock_backend
        mocker.patch.object(ibis_connector_module, "ibis", mock_ibis)

        connector = IbisConnector("postgres")
        connector.connect(database="mydb", user="myuser", password="secret")
        connector._pg_schema = None  # simulate current_schema() capture never happening
        assert connector.build_source_uri("orders") is None

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_bigquery_with_default_dataset(self, mocker):
        mocker.patch("ibis.bigquery.connect", return_value=mocker.Mock())
        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project", dataset_id="my_dataset")
        assert connector.build_source_uri("table") == "bigquery://test-project/my_dataset.table"
        connector.close()

    @pytest.mark.skipif(not HAS_BIGQUERY, reason="BigQuery backend not installed")
    def test_bigquery_without_default_dataset_returns_none(self, mocker):
        """Defensive only — not reachable via list_tables() today (Plan 35 §1):
        a project-only-scoped BigQuery connection's list_tables() call fails for
        the whole backend before any bare name is returned."""
        mocker.patch("ibis.bigquery.connect", return_value=mocker.Mock())
        connector = IbisConnector("bigquery")
        connector.connect(project_id="test-project")
        assert connector.build_source_uri("table") is None
        connector.close()


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


class TestPostgresConnection:
    """Test Postgres connection behaviour (mocked)."""

    def test_valid_backend_postgres(self):
        connector = IbisConnector("postgres")
        assert connector.backend_type == "postgres"
        assert not connector.is_connected

    def test_repr_disconnected(self):
        connector = IbisConnector("postgres")
        assert "postgres" in repr(connector)
        assert "disconnected" in repr(connector)

    def test_connect_missing_database_raises(self):
        connector = IbisConnector("postgres")
        with pytest.raises(ConfigurationError) as exc_info:
            connector.connect(user="myuser", password="secret")
        assert "database" in str(exc_info.value)

    def test_connect_missing_user_raises(self):
        connector = IbisConnector("postgres")
        with pytest.raises(ConfigurationError) as exc_info:
            connector.connect(database="mydb", password="secret")
        assert "user" in str(exc_info.value)

    def test_connect_missing_password_raises(self):
        connector = IbisConnector("postgres")
        with pytest.raises(ConfigurationError) as exc_info:
            connector.connect(database="mydb", user="myuser")
        assert "password" in str(exc_info.value)

    def test_connect_failure_raises_connection_error(self, mocker):
        import aitaem.connectors.ibis_connector as ibis_connector_module

        mock_ibis = mocker.MagicMock()
        mock_ibis.postgres.connect.side_effect = Exception("connection refused")
        mocker.patch.object(ibis_connector_module, "ibis", mock_ibis)
        connector = IbisConnector("postgres")
        with pytest.raises(AitaemConnectionError, match="PostgreSQL connection failed"):
            connector.connect(database="mydb", user="myuser", password="secret")

    def test_connect_success_captures_current_schema(self, mocker):
        """SF-1: current_schema() is queried once at connect time and stored."""
        import aitaem.connectors.ibis_connector as ibis_connector_module

        mock_backend = mocker.MagicMock()
        mock_backend.raw_sql.return_value.fetchone.return_value = ("public",)
        mock_ibis = mocker.MagicMock()
        mock_ibis.postgres.connect.return_value = mock_backend
        mocker.patch.object(ibis_connector_module, "ibis", mock_ibis)

        connector = IbisConnector("postgres")
        connector.connect(database="mydb", user="myuser", password="secret")

        assert connector.is_connected
        assert connector._pg_schema == "public"
        mock_backend.raw_sql.assert_called_once_with("SELECT current_schema()")


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
