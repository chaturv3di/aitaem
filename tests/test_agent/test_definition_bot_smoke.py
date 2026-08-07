"""
Smoke tests: real LLM runs against a mocked ConnectionManager.
Skipped automatically when ANTHROPIC_API_KEY is unset.

Regular CI  : collected, skipped (no secret).
Dedicated CI: pytest tests/test_agent/test_definition_bot_smoke.py -v
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — smoke test requires a real LLM",
)

from aitaem.agent.definition_bot import DefinitionBot  # noqa: E402


def _smoke_spec_cache():
    sc = MagicMock()
    rev = MagicMock()
    rev.description = "Total revenue in USD"
    rev.source = "duckdb://analytics.db/transactions"
    sc.metrics = {"revenue": rev}
    sc.slices = {}
    sc.segments = {}
    return sc


def test_definition_bot_smoke_prompt_cache_hit_on_turn_2():
    """Turn 2 in the same session should show cache_read_tokens > 0 (Anthropic only).

    No commit_spec/delete_spec call in between — this is the no-mutation case,
    which under the version-gated rebuild (Plan 33 SF-5) must not rebuild
    self._agent between turns 1 and 2, keeping Layers A+B cache-eligible.
    """
    bot = DefinitionBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=_smoke_spec_cache(),
        connection_manager=MagicMock(),
    )
    response1 = asyncio.run(bot.chat("What fields does the revenue metric have?"))
    response2 = asyncio.run(bot.chat("Thanks — that's all I needed."))

    assert response1.status is not None  # sanity: turn 1 completed

    # On turn 2, Layers A+B must be served from cache (Anthropic only).
    # RunTrace.Usage.cache_read_tokens mirrors pydantic-ai's Usage.cache_read_tokens.
    if "anthropic:" in bot._model:
        assert response2.trace.usage.cache_read_tokens > 0, (
            "cache_read_tokens is 0 — Layers A+B were not served from cache on turn 2. "
            "Check that anthropic_cache_instructions='5m' is set and that the "
            "version-gated rebuild in DefinitionBot.chat() did not rebuild self._agent "
            "between turns despite no commit_spec/delete_spec call."
        )
