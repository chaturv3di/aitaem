"""
Root-level conftest.py — shared fixtures for integration tests.

Provides a session-scoped in-memory DuckDB connection loaded with the
ad_campaigns dataset from examples/data/ad_campaigns.csv.
"""

from pathlib import Path

import pytest

from aitaem.connectors.connection import ConnectionManager
from aitaem.connectors.ibis_connector import IbisConnector

EXAMPLES_DATA_DIR = Path(__file__).parent.parent / "examples" / "data"
AD_CAMPAIGNS_CSV = EXAMPLES_DATA_DIR / "ad_campaigns.csv"

# Source URI used in all example metric/segment specs
AD_CAMPAIGNS_SOURCE_URI = "duckdb://ad_campaigns.duckdb/ad_campaigns"


@pytest.fixture(scope="session")
def ad_campaigns_connector():
    """IbisConnector backed by in-memory DuckDB with ad_campaigns table loaded.

    Reads data from examples/data/ad_campaigns.csv. The table schema matches
    the source dataset exactly:
        date (DATE), platform, campaign_type, industry, country (VARCHAR),
        impressions, clicks, conversions (INTEGER), ad_spend, revenue (DOUBLE)
    """
    connector = IbisConnector("duckdb")
    connector.connect(":memory:")

    csv_path = str(AD_CAMPAIGNS_CSV.resolve())
    connector.connection.raw_sql(
        f"""
        CREATE TABLE ad_campaigns AS
        SELECT * FROM read_csv_auto('{csv_path}', types={{
            'date': 'DATE',
            'impressions': 'INTEGER',
            'clicks': 'INTEGER',
            'conversions': 'INTEGER',
            'ad_spend': 'DOUBLE',
            'revenue': 'DOUBLE'
        }})
        """
    )

    yield connector
    connector.close()


@pytest.fixture(scope="session")
def ad_campaigns_connection_manager(ad_campaigns_connector):
    """ConnectionManager pre-loaded with the ad_campaigns DuckDB connector.

    Use this fixture in integration tests that call QueryExecutor or
    ConnectionManager.get_global().
    """
    manager = ConnectionManager()
    manager.add_connection("duckdb", connector=ad_campaigns_connector)
    return manager
