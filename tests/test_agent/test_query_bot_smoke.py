"""
Smoke test: one real LLM chat() turn against a mocked MetricCompute.
Skipped automatically when ANTHROPIC_API_KEY is unset.

Regular CI  : collected, skipped (no secret).
Dedicated CI: pytest tests/test_agent/test_query_bot_smoke.py -v
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — smoke test requires a real LLM",
)

from aitaem.agent.query_bot import QueryBot
from aitaem.agent.trace import Status


def _smoke_spec_cache():
    sc = MagicMock()
    rev = MagicMock()
    rev.description = "Total revenue in USD"
    rev.format = "currency:USD"
    rev.entities = None
    sc.metrics = {"revenue": rev}
    sc.slices = {}
    sc.segments = {}
    return sc


def _smoke_mc():
    mc = MagicMock()
    table = pa.table({
        "metric_name": ["revenue"],
        "metric_value": [125_000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": ["currency:USD"],
        "slice_type": ["none"],
        "slice_value": ["all"],
        "segment_name": ["none"],
        "segment_value": ["all"],
    })
    mock_ibis = MagicMock()
    mock_ibis.to_pyarrow.return_value = table
    mc.compute.return_value = mock_ibis
    return mc


def test_query_bot_smoke_single_turn():
    """One real-LLM chat() turn. MetricCompute is mocked; no database required."""
    bot = QueryBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=_smoke_spec_cache(),
        connection_manager=MagicMock(),
    )
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_smoke_mc()):
        response = asyncio.run(bot.chat("What was total revenue?"))

    assert response.status == Status.ok, (
        f"Expected status=ok, got {response.status!r}. reason={response.reason!r}"
    )
    rid = response.payload.primary_result_id
    assert rid is not None, "primary_result_id must be set on ok response"

    entry = bot.get_result(rid)
    assert entry.arrow is not None
    assert entry.arrow.num_rows == 1

    assert "revenue" in response.payload.metrics_used
    assert response.payload.format_hints.get("revenue") == "currency:USD", (
        f"format_hints missing or wrong: {response.payload.format_hints}"
    )
