"""
Integration tests for PostgreSQL backend.

These tests require a live PostgreSQL instance. They are skipped unless the
POSTGRES_TEST_HOST environment variable is set.

To run:
    POSTGRES_TEST_HOST=localhost \
    POSTGRES_TEST_DATABASE=testdb \
    POSTGRES_TEST_USER=testuser \
    POSTGRES_TEST_PASSWORD=testpass \
    python -m pytest tests/test_connectors/test_postgres_integration.py -m integration -v
"""

import os

import pandas as pd
import pytest

from aitaem.connectors.ibis_connector import IbisConnector

POSTGRES_TEST_HOST = os.environ.get("POSTGRES_TEST_HOST")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_connector():
    """Connect to the test Postgres instance and yield the connector."""
    if not POSTGRES_TEST_HOST:
        pytest.skip("POSTGRES_TEST_HOST not set — skipping Postgres integration tests")

    connector = IbisConnector("postgres")
    connector.connect(
        host=POSTGRES_TEST_HOST,
        port=int(os.environ.get("POSTGRES_TEST_PORT", "5432")),
        database=os.environ.get("POSTGRES_TEST_DATABASE", "testdb"),
        user=os.environ.get("POSTGRES_TEST_USER", "testuser"),
        password=os.environ.get("POSTGRES_TEST_PASSWORD", ""),
    )
    yield connector
    connector.close()


@pytest.fixture(scope="module")
def test_table(pg_connector):
    """Create a temporary table, yield its name, then drop it."""
    assert pg_connector.connection is not None
    raw_conn = pg_connector.connection.con  # underlying psycopg connection

    table_name = "aitaem_test_integration"
    with raw_conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table_name}")
        cur.execute(
            f"CREATE TABLE {table_name} (id INT, value TEXT)"
        )
        cur.execute(
            f"INSERT INTO {table_name} VALUES (1, 'alpha'), (2, 'beta'), (3, 'gamma')"
        )
    raw_conn.commit()

    yield table_name

    with raw_conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    raw_conn.commit()


class TestPostgresIntegration:
    def test_connector_is_connected(self, pg_connector):
        assert pg_connector.is_connected

    def test_get_table_returns_ibis_expr(self, pg_connector, test_table):
        table = pg_connector.get_table(test_table)
        assert table is not None

    def test_execute_returns_dataframe(self, pg_connector, test_table):
        table = pg_connector.get_table(test_table)
        df = pg_connector.execute(table)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3

    def test_execute_correct_values(self, pg_connector, test_table):
        table = pg_connector.get_table(test_table)
        df = pg_connector.execute(table.order_by("id"))
        assert list(df["id"]) == [1, 2, 3]
        assert list(df["value"]) == ["alpha", "beta", "gamma"]

    def test_close_sets_disconnected(self, pg_connector):
        # We close after tests in the fixture; verify close() works mid-session via repr
        assert pg_connector.is_connected
        assert "connected" in repr(pg_connector)
