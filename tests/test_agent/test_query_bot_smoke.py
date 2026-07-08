"""
Smoke tests: real LLM runs against a mocked MetricCompute.
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

from aitaem.agent.query_bot import QueryBot  # noqa: E402
from aitaem.agent.trace import Status  # noqa: E402


def _smoke_spec_cache():
    sc = MagicMock()
    rev = MagicMock()
    rev.description = "Total revenue in USD"
    rev.format = "currency:USD"
    rev.entities = None
    rev.timestamp_col = "created_at"
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


def test_query_bot_smoke_three_step_flow():
    """One real-LLM chat() turn exercising record_intent → resolve_intent → compute_metrics."""
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

    # Verify the 3-step flow was used
    tool_names = [tc.name for tc in response.trace.tool_calls]
    assert "record_intent" in tool_names, f"record_intent missing from tool_calls: {tool_names}"
    assert "resolve_intent" in tool_names, f"resolve_intent missing from tool_calls: {tool_names}"
    assert "compute_metrics" in tool_names, f"compute_metrics missing from tool_calls: {tool_names}"

    assert "revenue" in response.payload.metrics_used
    assert response.payload.format_hints.get("revenue") == "currency:USD", (
        f"format_hints missing or wrong: {response.payload.format_hints}"
    )


def test_query_bot_smoke_prompt_cache_hit_on_turn_2():
    """Turn 2 in the same session should show cache_read_tokens > 0 (Anthropic only)."""
    bot = QueryBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=_smoke_spec_cache(),
        connection_manager=MagicMock(),
    )
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_smoke_mc()):
        asyncio.run(bot.chat("What was total revenue?"))
        response2 = asyncio.run(bot.chat("What about last month?"))

    # On turn 2, Layers A+B must be served from cache (Anthropic only).
    # RunTrace.Usage.cache_read_tokens mirrors pydantic-ai's Usage.cache_read_tokens.
    # Anthropic: populated from the API response's cache_read_input_tokens.
    # OpenAI: server-side caching is not guaranteed in test environments, so we
    # skip the assertion for non-Anthropic providers.
    if "anthropic:" in bot._model:
        assert response2.trace.usage.cache_read_tokens > 0, (
            "cache_read_tokens is 0 — Layers A+B were not served from cache on turn 2. "
            "Check that anthropic_cache_instructions='5m' is set and that the static "
            "instructions are not being regenerated between turns."
        )
