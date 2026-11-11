from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.history import KEEP_PAIRS, stub_history
from aitaem.agent.tools import ToolResult
from aitaem.agent.trace import Status

WINDOW = 100_000
BULK = "x" * 4_000  # about 1,000 tokens per tool return


def _turn(
    deps: SessionDeps, n: int, run_id: str, tokens: int, when: datetime
) -> list[ModelMessage]:
    """One user turn: a prompt, a tool call, its return (with a stored artifact), and a reply."""
    ref = deps.store.put(
        "object",
        {},
        type_tag="read",
        summary=f"rows {n}",
        produced_by=ProducedBy(tool="read_artifact"),
    )
    result = ToolResult(
        artifact_id=ref.id, status=Status.ok, summary=f"rows {n}", facts={"rows": BULK}
    )
    usage = RequestUsage(input_tokens=tokens)
    return [
        ModelRequest(parts=[UserPromptPart(f"question {n}")], run_id=run_id),
        ModelResponse(
            parts=[ToolCallPart("read_artifact", {"artifact_id": ref.id}, tool_call_id=f"c{n}")],
            usage=usage,
            timestamp=when,
            run_id=run_id,
        ),
        ModelRequest(
            parts=[ToolReturnPart("read_artifact", result, tool_call_id=f"c{n}")], run_id=run_id
        ),
        ModelResponse(parts=[TextPart(f"answer {n}")], usage=usage, timestamp=when, run_id=run_id),
    ]


def _history(
    deps: SessionDeps, turns: int, fraction: float, idle: timedelta = timedelta(0)
) -> list[ModelMessage]:
    when = datetime.now(timezone.utc) - idle
    messages: list[ModelMessage] = []
    for n in range(turns):
        messages += _turn(deps, n, f"run{n}", int(fraction * WINDOW), when)
    # the current run: a new user prompt not yet answered
    messages.append(ModelRequest(parts=[UserPromptPart("current question")], run_id="current"))
    return messages


def _ctx(
    deps: SessionDeps, messages: list[ModelMessage], cache: bool = False
) -> RunContext[SessionDeps]:
    profile: dict[str, Any] = {"context_window": WINDOW}
    if cache:
        profile.update(
            supports_cache=True,
            default_cache_retention=timedelta(minutes=5),
            supported_cache_retentions=(timedelta(minutes=5),),
        )
    return RunContext(
        deps=deps,
        model=FunctionModel(lambda m, i: ModelResponse(parts=[]), profile=profile),  # type: ignore[arg-type]
        usage=RunUsage(),
        messages=messages,
        run_id="current",
    )


def _returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]


def _pairs_intact(messages: list[ModelMessage]) -> bool:
    calls = {
        p.tool_call_id
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    }
    return calls == {p.tool_call_id for p in _returns(messages)}


def test_below_threshold_history_unchanged(deps: SessionDeps) -> None:
    messages = _history(deps, turns=8, fraction=0.5)
    assert stub_history(_ctx(deps, messages), messages) is messages


def test_without_context_window_history_unchanged(deps: SessionDeps) -> None:
    messages = _history(deps, turns=8, fraction=0.9)
    ctx = _ctx(deps, messages)
    ctx.model = FunctionModel(lambda m, i: ModelResponse(parts=[]))
    assert stub_history(ctx, messages) is messages


def test_above_threshold_old_returns_become_stubs(deps: SessionDeps) -> None:
    messages = _history(deps, turns=8, fraction=0.66)

    result = stub_history(_ctx(deps, messages), messages)

    returns = _returns(result)
    stubbed = [p for p in returns if isinstance(p.content, str)]
    assert len(stubbed) == 8 - KEEP_PAIRS
    first = deps.store.refs()[0]
    assert (
        stubbed[0].content
        == f"[artifact {first.id} · object · rows 0 · read_artifact or describe_artifact]"
    )
    assert all(not isinstance(p.content, str) for p in returns[-KEEP_PAIRS:])
    assert _pairs_intact(result)
    assert len(result) == len(messages)
    assert not isinstance(_returns(messages)[0].content, str)  # the input history is not mutated


def test_cold_cache_lowers_the_threshold(deps: SessionDeps) -> None:
    messages = _history(deps, turns=8, fraction=0.4, idle=timedelta(hours=2))
    assert stub_history(_ctx(deps, messages), messages) is messages  # cache state unknown
    result = stub_history(_ctx(deps, messages, cache=True), messages)
    assert any(isinstance(p.content, str) for p in _returns(result))
    warm = _history(deps, turns=8, fraction=0.4)
    assert stub_history(_ctx(deps, warm, cache=True), warm) == warm


def test_small_clear_is_skipped(deps: SessionDeps) -> None:
    messages = _history(deps, turns=KEEP_PAIRS + 1, fraction=0.66)  # one stub: ~1,000 tokens
    result = stub_history(_ctx(deps, messages), messages)
    assert not any(isinstance(p.content, str) for p in _returns(result))


def test_escalation_drops_oldest_turns_at_user_prompt_boundaries(deps: SessionDeps) -> None:
    messages = _history(deps, turns=8, fraction=1.5)

    result = stub_history(_ctx(deps, messages), messages)

    assert len(result) < len(messages)
    first = result[0]
    assert isinstance(first, ModelRequest) and isinstance(first.parts[0], UserPromptPart)
    assert result[-1] == messages[-1]
    assert _pairs_intact(result)


@pytest.mark.parametrize("fraction", [0.66, 1.5])
def test_current_run_untouched(deps: SessionDeps, fraction: float) -> None:
    messages = _history(deps, turns=8, fraction=fraction)
    current = _turn(deps, 99, "current", int(fraction * WINDOW), datetime.now(timezone.utc))[1:3]
    messages += current
    result = stub_history(_ctx(deps, messages), messages)
    assert result[-2:] == current
