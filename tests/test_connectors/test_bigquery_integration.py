"""
tests.test_connectors.test_bigquery_integration - BigQuery integration tests

These tests use a REAL BigQuery connection and create actual tables.
They require:
1. BigQuery dependencies installed: pip install aitaem[bigquery]
2. Valid GCP credentials: gcloud auth application-default login
3. A GCP project with BigQuery enabled
4. A dataset named 'aggregate_tables'

Run these tests explicitly:
    pytest tests/test_connectors/test_bigquery_integration.py -v

Skip these tests by default:
    pytest tests/test_connectors/ -v  (these won't run)
"""

import os

import pytest

from aitaem.connectors import ConnectionManager, IbisConnector

# Check for BigQuery availability and credentials
try:
    import ibis

    _ = ibis.bigquery
    HAS_BIGQUERY = True
except (ImportError, AttributeError):
    HAS_BIGQUERY = False

# Get project ID from environment or gcloud config
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "project-12bab95f-3152-4c49-87a")
DATASET_ID = "aggregate_tables"
TEST_TABLE_NAME = "aitaem_test_table"

# Mark all tests in this file to run only when explicitly requested
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bigquery_connector():
    """Create a BigQuery connector for integration tests."""
    if not HAS_BIGQUERY:
        pytest.skip("BigQuery backend not installed")

    connector = IbisConnector("bigquery")
    connector.connect(project_id=GCP_PROJECT_ID)

    yield connector

    # Cleanup: drop test table if it exists
    try:
        connector.connection.raw_sql(
            f"DROP TABLE IF EXISTS {DATASET_ID}.{TEST_TABLE_NAME}"
        )
    except Exception:
        pass  # Ignore cleanup errors

    connector.close()


@pytest.fixture(scope="module")
def test_table(bigquery_connector):
    """Create a test table in BigQuery."""
    # Create a simple test table
    create_sql = f"""
    CREATE OR REPLACE TABLE {DATASET_ID}.{TEST_TABLE_NAME} AS
    SELECT
        1 AS id,
        'test_value_1' AS name,
        100 AS amount
    UNION ALL
    SELECT
        2 AS id,
        'test_value_2' AS name,
        200 AS amount
    UNION ALL
    SELECT
        3 AS id,
        'test_value_3' AS name,
        300 AS amount
    """

    bigquery_connector.connection.raw_sql(create_sql)

    # Return the table reference
    return f"{DATASET_ID}.{TEST_TABLE_NAME}"


class TestBigQueryConnectionIntegration:
    """Test real BigQuery connection."""

    def test_connect_with_real_credentials(self, bigquery_connector):
        """Test connecting to BigQuery with real ADC credentials."""
        assert bigquery_connector.is_connected
        assert bigquery_connector.backend_type == "bigquery"

    def test_connection_has_valid_client(self, bigquery_connector):
        """Test that connection has a valid BigQuery client."""
        # The ibis connection should have a client attribute
        assert bigquery_connector.connection is not None
        assert hasattr(bigquery_connector.connection, "raw_sql")


class TestBigQueryTableOperations:
    """Test real table operations with BigQuery."""

    def test_get_table_with_two_part_name(self, bigquery_connector, test_table):
        """Test getting table with dataset.table format."""
        table = bigquery_connector.get_table(f"{DATASET_ID}.{TEST_TABLE_NAME}")
        assert table is not None

        # Verify table structure
        schema = table.schema()
        assert "id" in schema.names
        assert "name" in schema.names
        assert "amount" in schema.names

    def test_get_table_with_three_part_name(self, bigquery_connector, test_table):
        """Test getting table with project.dataset.table format."""
        full_name = f"{GCP_PROJECT_ID}.{DATASET_ID}.{TEST_TABLE_NAME}"
        table = bigquery_connector.get_table(full_name)
        assert table is not None

        # Verify table structure
        schema = table.schema()
        assert "id" in schema.names
        assert "name" in schema.names
        assert "amount" in schema.names

    def test_execute_query_returns_data(self, bigquery_connector, test_table):
        """Test executing a query and getting real data."""
        table = bigquery_connector.get_table(f"{DATASET_ID}.{TEST_TABLE_NAME}")

        # Execute a simple query
        result = bigquery_connector.execute(table, output_format="pandas")

        # Verify results (sort by id since order is not guaranteed)
        result = result.sort_values("id").reset_index(drop=True)
        assert len(result) == 3
        assert list(result["id"]) == [1, 2, 3]
        assert list(result["name"]) == [
            "test_value_1",
            "test_value_2",
            "test_value_3",
        ]
        assert list(result["amount"]) == [100, 200, 300]

    def test_execute_filtered_query(self, bigquery_connector, test_table):
        """Test executing a filtered query."""
        table = bigquery_connector.get_table(f"{DATASET_ID}.{TEST_TABLE_NAME}")

        # Filter for id > 1
        filtered = table.filter(table.id > 1)
        result = bigquery_connector.execute(filtered, output_format="pandas")

        # Verify results
        assert len(result) == 2
        assert list(result["id"]) == [2, 3]

    def test_execute_aggregation_query(self, bigquery_connector, test_table):
        """Test executing an aggregation query."""
        table = bigquery_connector.get_table(f"{DATASET_ID}.{TEST_TABLE_NAME}")

        # Aggregate: sum of amounts (returns a table with one row)
        total_expr = table.aggregate(total=table.amount.sum())
        result = bigquery_connector.execute(total_expr, output_format="pandas")

        # Verify result
        assert len(result) == 1
        assert result["total"].iloc[0] == 600  # 100 + 200 + 300


class TestConnectionManagerWithBigQuery:
    """Test ConnectionManager with real BigQuery."""

    def test_add_bigquery_connection_real(self):
        """Test adding a real BigQuery connection via ConnectionManager."""
        manager = ConnectionManager()
        manager.add_connection("bigquery", project_id=GCP_PROJECT_ID)

        assert "bigquery" in manager._connections
        connector = manager.get_connection("bigquery")
        assert connector.is_connected

        connector.close()

    def test_yaml_config_with_bigquery(self, tmp_path):
        """Test loading BigQuery connection from YAML config."""
        yaml_file = tmp_path / "test_config.yaml"
        yaml_file.write_text(
            f"""
bigquery:
  project_id: {GCP_PROJECT_ID}
"""
        )

        manager = ConnectionManager.from_yaml(str(yaml_file))
        assert "bigquery" in manager._connections

        connector = manager.get_connection("bigquery")
        assert connector.is_connected

        manager.close_all()

    def test_uri_routing_to_bigquery(self, test_table):
        """Test routing BigQuery URI to connection."""
        manager = ConnectionManager()
        manager.add_connection("bigquery", project_id=GCP_PROJECT_ID)

        # Test URI routing
        uri = f"bigquery://{GCP_PROJECT_ID}.{DATASET_ID}.{TEST_TABLE_NAME}"
        connector = manager.get_connection_for_source(uri)

        assert connector.backend_type == "bigquery"
        assert connector.is_connected

        # Verify we can get the table through the routed connector
        table = connector.get_table(f"{DATASET_ID}.{TEST_TABLE_NAME}")
        assert table is not None

        manager.close_all()


class TestBigQueryEndToEnd:
    """End-to-end integration tests."""

    def test_full_workflow(self, test_table):
        """Test complete workflow: YAML → Connection → Query → Results."""
        # 1. Create YAML config
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(
                f"""
bigquery:
  project_id: {GCP_PROJECT_ID}
"""
            )
            config_path = f.name

        try:
            # 2. Load configuration
            manager = ConnectionManager.from_yaml(config_path)

            # 3. Get connection via URI
            uri = f"bigquery://{GCP_PROJECT_ID}.{DATASET_ID}.{TEST_TABLE_NAME}"
            connector = manager.get_connection_for_source(uri)

            # 4. Get table
            table = connector.get_table(f"{DATASET_ID}.{TEST_TABLE_NAME}")

            # 5. Execute query
            query = table.filter(table.amount >= 200)
            result = connector.execute(query, output_format="pandas")

            # 6. Verify results
            assert len(result) == 2
            assert all(result["amount"] >= 200)

            # 7. Cleanup
            manager.close_all()

        finally:
            # Remove temp file
            os.unlink(config_path)
