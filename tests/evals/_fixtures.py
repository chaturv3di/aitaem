"""Self-contained eval fixtures for tests/evals/ (SF-5).

tests/evals/ doubles as the reference example ND-09/OQ-2 ships (blueprint
philosophy, G2) — a user should be able to read and copy it without also
understanding tests/test_agent/'s internal fixtures. This module therefore
defines its own minimal spec-cache/connection-manager/ground-truth fixtures,
duplicating a small amount of structure already present in
tests/test_agent/test_query_bot.py and tests/test_agent/test_definition_bot.py.
Deliberate, bounded exception to "don't duplicate" — legibility in isolation
is the point.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pyarrow as pa

from aitaem.agent.definition_bot import DefinitionBot
from aitaem.agent.query_bot import QueryBot

GROUND_TRUTH_REVENUE_TABLE: pa.Table = pa.table({
    "metric_name": ["revenue"],
    "metric_value": [1000.0],
    "period_type": ["all_time"],
    "period_start_date": [None],
    "period_end_date": [None],
    "entity_id": [None],
    "metric_format": [None],
    "slice_type": [None],
    "slice_value": [None],
    "segment_name": [None],
    "segment_value": [None],
})
# Fixed ground-truth Arrow table for the deterministic-correctness eval case.
# Small, hand-constructed (a handful of rows) — not derived from any real backend.


def _mock_metric_compute() -> MagicMock:
    mc = MagicMock()
    mock_ibis = MagicMock()
    mock_ibis.to_pyarrow.return_value = GROUND_TRUTH_REVENUE_TABLE
    mc.compute.return_value = mock_ibis
    return mc


# Patched once, at import time, for the lifetime of the eval harness process:
# every QueryBot built by make_query_bot_fixture() below deterministically
# returns GROUND_TRUTH_REVENUE_TABLE via compute_metrics(), regardless of the
# (fake) backend it's constructed against.
patch(
    "aitaem.agent.query_tools.MetricCompute",
    return_value=_mock_metric_compute(),
).start()


def _query_bot_spec_cache() -> Any:
    sc = MagicMock()
    sc.metrics = {
        "revenue": MagicMock(description="Total revenue", entities=["user_id"], format="currency:USD"),
    }
    sc.slices = {}
    sc.segments = {}
    return sc


def make_query_bot_fixture(model: Any) -> QueryBot:
    """Build a QueryBot against a minimal one-metric SpecCache stand-in and a
    MagicMock ConnectionManager, with aitaem.agent.query_tools.MetricCompute
    patched (at import time, above) so compute_metrics() deterministically
    returns GROUND_TRUTH_REVENUE_TABLE regardless of the (fake) backend.
    """
    return QueryBot(model=model, spec_cache=_query_bot_spec_cache(), connection_manager=MagicMock())


def _definition_spec_cache() -> Any:
    sc = MagicMock()
    sc.metrics = {}
    sc.slices = {}
    sc.segments = {}
    return sc


def _definition_connection_manager() -> Any:
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis_table = MagicMock()
    mock_ibis_table.schema.return_value = MagicMock(
        names=["amount", "transaction_date"], types=["float64", "date"]
    )
    mock_ibis_table.columns = ["amount", "transaction_date"]
    mock_connector.get_table.return_value = mock_ibis_table
    # describe_table()/list_tables() go through get_connection(backend_type);
    # validate_spec()'s live-schema column check goes through
    # get_connection_for_source(source_uri) + parse_source_uri(source_uri).
    # Both must resolve to the same table for the column-existence check
    # (SF-7's refusal-on-ambiguous-schema case) to exercise real logic
    # instead of silently no-op'ing into a warning.
    mock_cm.get_connection.return_value = mock_connector
    mock_cm.get_connection_for_source.return_value = mock_connector
    mock_cm.parse_source_uri.return_value = ("duckdb", "analytics.db", "transactions")
    return mock_cm


def make_definition_bot_fixture(model: Any) -> DefinitionBot:
    """Build a DefinitionBot against a MagicMock ConnectionManager exposing one
    `transactions` table with a fixed two-column schema (amount, transaction_date).
    """
    return DefinitionBot(
        model=model,
        spec_cache=_definition_spec_cache(),
        connection_manager=_definition_connection_manager(),
    )
