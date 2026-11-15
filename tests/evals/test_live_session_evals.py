"""Live-model session evals: answers survive history stubbing, and turn 2 reads the prompt cache.

Skipped without ANTHROPIC_API_KEY. History stubbing is forced on turn 2 by lowering its thresholds,
so the follow-up answer has to come from the stubbed history, the tail block, and the artifacts.
"""

from __future__ import annotations

import os

import pytest

from aitaem.agent import Status

from ._fixtures import TRANSACTIONS, make_agent

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY"
)

TOTAL = int(sum(TRANSACTIONS["amount"].to_pylist()))  # 3329


@pytest.fixture(autouse=True)
def allow_model_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai import models

    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


@pytest.mark.asyncio
async def test_answer_survives_history_stubbing(monkeypatch: pytest.MonkeyPatch) -> None:
    import aitaem.agent.history as history

    agent = make_agent("anthropic:claude-sonnet-5-5", "anthropic:claude-haiku-5-5")
    first = await agent.chat("What is total revenue across all transactions?")
    assert first.status == Status.ok, first.response
    assert str(TOTAL) in first.response.replace(",", "")

    monkeypatch.setattr(history, "STUB_AT", 0.0)
    monkeypatch.setattr(history, "MIN_CLEAR_TOKENS", 0)
    monkeypatch.setattr(history, "KEEP_PAIRS", 0)
    second = await agent.chat("Remind me: what total revenue figure did you give me?")

    stubbed = [
        p
        for m in agent.main_conversation.messages
        for p in getattr(m, "parts", [])
        if getattr(p, "part_kind", "") == "tool-return" and str(p.content).startswith("[artifact ")
    ]
    assert stubbed, "stubbing did not run; check the model's context_window"
    assert str(TOTAL) in second.response.replace(",", "")
    agent.cleanup()


@pytest.mark.asyncio
async def test_turn_two_reads_the_prompt_cache() -> None:
    agent = make_agent("anthropic:claude-sonnet-5-5", "anthropic:claude-haiku-5-5")
    await agent.chat("Which metrics can you compute?")
    second = await agent.chat("And which slices?")

    main_runs = [r for r in agent.trace(second.trace_id).runs if r.name == "main_session"]
    assert main_runs and main_runs[0].usage.cache_read_tokens > 0
    agent.cleanup()
