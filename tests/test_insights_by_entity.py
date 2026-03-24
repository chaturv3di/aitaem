"""
Integration tests for MetricCompute.compute() with by_entity parameter.

Uses an in-memory DuckDB with a small transactions table containing
user_id, device_id, amount, and event_ts columns.
"""

import pytest

from aitaem import MetricCompute
from aitaem.connectors.connection import ConnectionManager
from aitaem.connectors.ibis_connector import IbisConnector
from aitaem.specs.loader import SpecCache
from aitaem.utils.exceptions import QueryBuildError
from aitaem.utils.formatting import STANDARD_COLUMNS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REVENUE_METRIC_YAML = """
metric:
  name: revenue
  source: duckdb://test.db/transactions
  numerator: SUM(amount)
  timestamp_col: event_ts
  entities: [user_id, device_id]
"""

REVENUE_METRIC_NO_ENTITIES_YAML = """
metric:
  name: revenue_no_entities
  source: duckdb://test.db/transactions
  numerator: SUM(amount)
  timestamp_col: event_ts
"""

SETUP_SQL = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    ('u1', 'd1', 100.0, TIMESTAMPTZ '2026-01-05'),
    ('u1', 'd2', 200.0, TIMESTAMPTZ '2026-01-15'),
    ('u2', 'd1', 150.0, TIMESTAMPTZ '2026-02-05'),
    ('u2', 'd2', 250.0, TIMESTAMPTZ '2026-02-15'),
    ('u3', 'd1',  50.0, TIMESTAMPTZ '2026-03-05')
) AS t(user_id, device_id, amount, event_ts)
"""


@pytest.fixture(scope="module")
def entity_connection_manager():
    connector = IbisConnector("duckdb")
    connector.connect(":memory:")
    connector.connection.raw_sql(SETUP_SQL)
    manager = ConnectionManager()
    manager.add_connection("duckdb", connector=connector)
    yield manager
    connector.close()


@pytest.fixture(scope="module")
def entity_spec_cache():
    return SpecCache.from_string(metric_yaml=REVENUE_METRIC_YAML)


@pytest.fixture(scope="module")
def entity_mc(entity_spec_cache, entity_connection_manager):
    return MetricCompute(entity_spec_cache, entity_connection_manager)


@pytest.fixture(scope="module")
def no_entities_spec_cache():
    return SpecCache.from_string(metric_yaml=REVENUE_METRIC_NO_ENTITIES_YAML)


@pytest.fixture(scope="module")
def no_entities_mc(no_entities_spec_cache, entity_connection_manager):
    return MetricCompute(no_entities_spec_cache, entity_connection_manager)


# ---------------------------------------------------------------------------
# 1. by_entity=None (default) — entity_id is NULL, aggregated output
# ---------------------------------------------------------------------------


def test_default_no_by_entity_entity_id_null(entity_mc):
    df = entity_mc.compute("revenue")
    assert "entity_id" in df.columns
    assert df["entity_id"].isna().all()


def test_default_no_by_entity_column_order(entity_mc):
    df = entity_mc.compute("revenue")
    assert list(df.columns) == STANDARD_COLUMNS


def test_default_no_by_entity_one_aggregated_row(entity_mc):
    df = entity_mc.compute("revenue")
    assert len(df) == 1
    assert df["metric_value"].iloc[0] == pytest.approx(750.0)


# ---------------------------------------------------------------------------
# 2. by_entity='user_id' — entity-level output
# ---------------------------------------------------------------------------


def test_by_entity_user_id_column_order(entity_mc):
    df = entity_mc.compute("revenue", by_entity="user_id")
    assert list(df.columns) == STANDARD_COLUMNS


def test_by_entity_user_id_entity_id_values(entity_mc):
    df = entity_mc.compute("revenue", by_entity="user_id")
    assert set(df["entity_id"]) == {"u1", "u2", "u3"}


def test_by_entity_user_id_row_count(entity_mc):
    df = entity_mc.compute("revenue", by_entity="user_id")
    assert len(df) == 3


def test_by_entity_user_id_metric_values(entity_mc):
    df = entity_mc.compute("revenue", by_entity="user_id")
    totals = df.set_index("entity_id")["metric_value"]
    assert totals["u1"] == pytest.approx(300.0)
    assert totals["u2"] == pytest.approx(400.0)
    assert totals["u3"] == pytest.approx(50.0)


def test_by_entity_device_id_values(entity_mc):
    df = entity_mc.compute("revenue", by_entity="device_id")
    assert set(df["entity_id"]) == {"d1", "d2"}
    totals = df.set_index("entity_id")["metric_value"]
    assert totals["d1"] == pytest.approx(300.0)  # 100+150+50
    assert totals["d2"] == pytest.approx(450.0)  # 200+250


# ---------------------------------------------------------------------------
# 3. by_entity with period_type='monthly'
# ---------------------------------------------------------------------------


def test_by_entity_monthly_row_count(entity_mc):
    # u1: Jan, u2: Feb, u3: Mar → 3 rows
    df = entity_mc.compute(
        "revenue",
        by_entity="user_id",
        time_window=("2026-01-01", "2026-04-01"),
        period_type="monthly",
    )
    assert len(df) == 3
    assert set(df["entity_id"]) == {"u1", "u2", "u3"}


def test_by_entity_monthly_metric_values(entity_mc):
    df = entity_mc.compute(
        "revenue",
        by_entity="user_id",
        time_window=("2026-01-01", "2026-04-01"),
        period_type="monthly",
    )
    totals = df.set_index("entity_id")["metric_value"]
    assert totals["u1"] == pytest.approx(300.0)
    assert totals["u2"] == pytest.approx(400.0)
    assert totals["u3"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 4. Error cases
# ---------------------------------------------------------------------------


def test_by_entity_not_in_entities_raises(entity_mc):
    with pytest.raises(QueryBuildError, match="by_entity='page_id'"):
        entity_mc.compute("revenue", by_entity="page_id")


def test_by_entity_with_no_entities_on_metric_raises(no_entities_mc):
    with pytest.raises(QueryBuildError, match="by_entity='user_id'"):
        no_entities_mc.compute("revenue_no_entities", by_entity="user_id")
