"""Plan 28 / SF-10: cross-bot parametrized composition tests.

Verifies the tools=[...] / add_tool() / extra_tools=[...] surfaces (AD-11,
minus add_bot()/as_tool() per ND-11) behave identically on QueryBot and
DefinitionBot. Bot-specific variants of these tests already live in
test_query_bot.py / test_definition_bot.py alongside each bot's other tests;
this file is where the shared mechanism is verified symmetrically in one
place rather than duplicated ad hoc.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from aitaem.agent.base import Bot
from aitaem.agent.definition_bot import DefinitionBot
from aitaem.agent.definition_types import DefinitionOutput
from aitaem.agent.query_bot import QueryBot
from aitaem.agent.query_types import QueryOutput
from aitaem.agent.trace import Status


# ---------------------------------------------------------------------------
# Fixture tools (sync/async convention — see plans/28-agent-phase5.2-composition.md)
# ---------------------------------------------------------------------------


def probe_sync() -> str:
    return "sync-value"


async def probe_async() -> str:
    return "async-value"


# ---------------------------------------------------------------------------
# Per-bot spec: how to construct each bot, its output type, its default tools
# ---------------------------------------------------------------------------


def _query_spec_cache() -> MagicMock:
    sc = MagicMock()
    sc.metrics = {"revenue": MagicMock(description="Revenue", entities=None, format=None)}
    sc.slices = {}
    sc.segments = {}
    return sc


def _definition_spec_cache() -> MagicMock:
    sc = MagicMock()
    sc.metrics = {}
    sc.slices = {}
    sc.segments = {}
    return sc


def _make_query_bot(model: Any, tools: list[Any] | None = None) -> QueryBot:
    return QueryBot(
        model=model, spec_cache=_query_spec_cache(), connection_manager=MagicMock(), tools=tools
    )


def _make_definition_bot(model: Any, tools: list[Any] | None = None) -> DefinitionBot:
    return DefinitionBot(
        model=model, spec_cache=_definition_spec_cache(), connection_manager=MagicMock(), tools=tools
    )


def _query_output(narrative: str) -> QueryOutput:
    return QueryOutput(status=Status.ok, narrative=narrative, result_ids=[])


def _definition_output(narrative: str) -> DefinitionOutput:
    return DefinitionOutput(status=Status.ok, narrative=narrative)


def _make_compute_metrics_collider():
    def compute_metrics() -> str:  # shadows QueryBot's default tool name
        return "x"

    return compute_metrics


def _make_validate_spec_collider():
    def validate_spec() -> str:  # shadows DefinitionBot's default tool name
        return "x"

    return validate_spec


@dataclass
class _BotSpec:
    label: str
    make_bot: Callable[..., Bot]
    make_output: Callable[[str], Any]
    default_tool_names: frozenset[str]
    colliding_tool_factory: Callable[[], Callable[[], str]]


_QUERY_SPEC = _BotSpec(
    label="QueryBot",
    make_bot=_make_query_bot,
    make_output=_query_output,
    default_tool_names=frozenset({
        "record_intent", "resolve_intent", "compute_metrics",
        "rank_by_value", "filter_by_threshold", "distribution_summary",
        "period_over_period", "contribution_share",
    }),
    colliding_tool_factory=_make_compute_metrics_collider,
)

_DEFINITION_SPEC = _BotSpec(
    label="DefinitionBot",
    make_bot=_make_definition_bot,
    make_output=_definition_output,
    default_tool_names=frozenset({
        "record_definition_intent", "list_tables", "describe_table",
        "draft_spec", "validate_spec",
    }),
    colliding_tool_factory=_make_validate_spec_collider,
)

_BOT_SPECS = [_QUERY_SPEC, _DEFINITION_SPEC]
_bot_spec_params = pytest.mark.parametrize("spec", _BOT_SPECS, ids=lambda s: s.label)
_sync_async_params = pytest.mark.parametrize(
    "probe,expected", [(probe_sync, "sync-value"), (probe_async, "async-value")], ids=["sync", "async"]
)


def _tool_calling_model(tool_name: str, spec: _BotSpec) -> FunctionModel:
    """FunctionModel: calls `tool_name` once, then reports its return value in narrative."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart) and p.tool_name == tool_name:
                        output = spec.make_output(f"got:{p.content}")
                        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={}, tool_call_id="tc-1")]
        )

    return FunctionModel(fn)


def _visibility_model(recorder: list, spec: _BotSpec) -> FunctionModel:
    """FunctionModel that records visible tool names on each call, then ends the turn."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        recorder.append(sorted(t.name for t in info.function_tools))
        output = spec.make_output("done")
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _run(bot: Bot, method: str, message: str, **kwargs: Any) -> Any:
    return asyncio.run(getattr(bot, method)(message, **kwargs))


# ---------------------------------------------------------------------------
# tools=[...] (constructor)
# ---------------------------------------------------------------------------


@_bot_spec_params
def test_constructor_tools_registered(spec: _BotSpec):
    bot = spec.make_bot(TestModel(), tools=[probe_sync])
    assert "probe_sync" in bot._toolset.tools
    assert spec.default_tool_names.issubset(bot._toolset.tools.keys())


@_bot_spec_params
@_sync_async_params
def test_constructor_tools_invoked(spec: _BotSpec, probe, expected):
    bot = spec.make_bot(_tool_calling_model(probe.__name__, spec), tools=[probe])
    response = _run(bot, "ask", "go")
    assert expected in response.narrative


# ---------------------------------------------------------------------------
# add_tool() (persistent runtime addition)
# ---------------------------------------------------------------------------


@_bot_spec_params
@_sync_async_params
def test_add_tool_invoked(spec: _BotSpec, probe, expected):
    bot = spec.make_bot(_tool_calling_model(probe.__name__, spec))
    bot.add_tool(probe)
    response = _run(bot, "ask", "go")
    assert expected in response.narrative


@_bot_spec_params
def test_add_tool_not_visible_before_call(spec: _BotSpec):
    seen: list = []
    bot = spec.make_bot(_visibility_model(seen, spec))
    _run(bot, "ask", "go")
    assert "probe_sync" not in seen[0]


@_bot_spec_params
def test_add_tool_persists_across_turns(spec: _BotSpec):
    seen: list = []
    bot = spec.make_bot(_visibility_model(seen, spec))
    _run(bot, "chat", "first")
    bot.add_tool(probe_sync)
    _run(bot, "chat", "second")
    _run(bot, "chat", "third")
    assert "probe_sync" not in seen[0]
    assert "probe_sync" in seen[1]
    assert "probe_sync" in seen[2]


@_bot_spec_params
def test_add_tool_accepts_tool_instance_and_plain_function(spec: _BotSpec):
    bot = spec.make_bot(TestModel())
    bot.add_tool(probe_sync)
    bot.add_tool(Tool(probe_async))
    assert "probe_sync" in bot._toolset.tools
    assert "probe_async" in bot._toolset.tools


# ---------------------------------------------------------------------------
# extra_tools=[...] (per-call ephemeral)
# ---------------------------------------------------------------------------


@_bot_spec_params
@_sync_async_params
def test_extra_tools_ephemeral_on_ask(spec: _BotSpec, probe, expected):
    bot = spec.make_bot(_tool_calling_model(probe.__name__, spec))
    response = _run(bot, "ask", "go", extra_tools=[probe])
    assert expected in response.narrative


@_bot_spec_params
def test_extra_tools_ephemeral_on_chat(spec: _BotSpec):
    seen: list = []
    bot = spec.make_bot(_visibility_model(seen, spec))
    _run(bot, "chat", "first", extra_tools=[probe_sync])
    _run(bot, "chat", "second")
    assert "probe_sync" in seen[0]
    assert "probe_sync" not in seen[1]


@_bot_spec_params
def test_extra_tools_none_is_noop_regression(spec: _BotSpec):
    bot = spec.make_bot(TestModel())
    with patch.object(bot._agent, "run", wraps=bot._agent.run) as mock_run:
        _run(bot, "ask", "go")
    _, kwargs = mock_run.call_args
    assert "toolsets" not in kwargs


# ---------------------------------------------------------------------------
# self._toolset contract
# ---------------------------------------------------------------------------


def test_toolset_contract_violation_raises_typeerror_at_construction():
    class ForgetfulBot(Bot):
        def _build_agent(self):
            return None  # does NOT set self._toolset

    with pytest.raises(TypeError, match="ForgetfulBot"):
        ForgetfulBot(model="claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Tool-name collisions — cases (a), (b), (c)
# ---------------------------------------------------------------------------


@_bot_spec_params
def test_collision_constructor_tools_vs_default_raises_usererror_at_construction(spec: _BotSpec):
    with pytest.raises(UserError, match="conflicts with existing tool"):
        spec.make_bot(TestModel(), tools=[spec.colliding_tool_factory()])


@_bot_spec_params
def test_collision_add_tool_vs_existing_raises_usererror(spec: _BotSpec):
    bot = spec.make_bot(TestModel())
    with pytest.raises(UserError, match="conflicts with existing tool"):
        bot.add_tool(spec.colliding_tool_factory())


@_bot_spec_params
def test_collision_add_tool_leaves_toolset_unmodified(spec: _BotSpec):
    bot = spec.make_bot(TestModel())
    names_before = set(bot._toolset.tools)
    with pytest.raises(UserError):
        bot.add_tool(spec.colliding_tool_factory())
    assert set(bot._toolset.tools) == names_before


@_bot_spec_params
@pytest.mark.parametrize("method", ["ask", "chat"])
def test_collision_extra_tools_vs_persistent_surfaces_as_error_status(spec: _BotSpec, method: str):
    bot = spec.make_bot(TestModel())
    response = _run(bot, method, "go", extra_tools=[spec.colliding_tool_factory()])
    assert response.status == Status.error
    assert "conflicts with existing tool" in (response.reason or "")
