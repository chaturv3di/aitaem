from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timezone

import pyarrow as pa
import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from aitaem.agent.response import BotResponse
from aitaem.agent.store import ResultStore
from aitaem.agent.trace import RunTrace, Status, Usage
from aitaem.agent.base import Bot, _register_tool


# ---------------------------------------------------------------------------
# SF-1: package structure
# ---------------------------------------------------------------------------


def test_aitaem_agent_importable():
    import aitaem.agent  # noqa: F401


def test_public_exports_present():
    from aitaem.agent import Bot, BotResponse, Status, RunTrace, ToolCall, Usage
    from aitaem.agent import ResultEntry, ResultStore

    assert all(
        x is not None
        for x in [Bot, BotResponse, Status, RunTrace, ToolCall, Usage, ResultEntry, ResultStore]
    )


# ---------------------------------------------------------------------------
# SF-3: import-graph CI check
# ---------------------------------------------------------------------------


def test_import_graph_check_passes():
    result = subprocess.run(
        [sys.executable, "scripts/check_import_graph.py"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# SF-4: Status, Usage, ToolCall, RunTrace
# ---------------------------------------------------------------------------


def test_status_values():
    assert set(Status) == {Status.ok, Status.empty, Status.refused, Status.error}


def test_status_is_str():
    assert Status.ok == "ok"


def test_usage_total_tokens():
    u = Usage(input_tokens=100, output_tokens=50)
    assert u.total_tokens == 150


def test_usage_defaults_zero():
    u = Usage()
    assert u.requests == 0 and u.total_tokens == 0


def test_usage_total_tokens_in_serialization():
    u = Usage(input_tokens=100, output_tokens=50)
    data = u.model_dump()
    assert data["total_tokens"] == 150


def test_usage_from_run_usage():
    from unittest.mock import MagicMock

    ru = MagicMock()
    ru.requests = 2
    ru.tool_calls = 1
    ru.input_tokens = 200
    ru.output_tokens = 80
    ru.cache_read_tokens = 10
    ru.cache_write_tokens = 0
    u = Usage.from_run_usage(ru)
    assert u.requests == 2
    assert u.total_tokens == 280
    assert u.model_dump_json()


def test_run_trace_total_tokens_serialized():
    import json

    trace = RunTrace(
        run_id="r1",
        conversation_id="c1",
        timestamp=datetime.now(timezone.utc),
        tool_calls=[],
        usage=Usage(input_tokens=100, output_tokens=50),
    )
    data = json.loads(trace.model_dump_json())
    assert data["usage"]["total_tokens"] == 150


# ---------------------------------------------------------------------------
# SF-5: ResultEntry and ResultStore
# ---------------------------------------------------------------------------


def test_result_store_store_returns_unique_ids():
    store = ResultStore()
    id1 = store.store_tabular(None, None)
    id2 = store.store_tabular(None, None)
    assert id1 != id2


def test_result_store_get_retrieves_entry():
    store = ResultStore()
    table = pa.table({"x": [1, 2, 3]})
    rid = store.store_tabular(table, None, metadata={"source": "test"})
    entry = store.get(rid)
    assert entry.result_id == rid
    assert entry.arrow.equals(table)
    assert entry.metadata["source"] == "test"


def test_result_store_get_missing_raises():
    store = ResultStore()
    with pytest.raises(KeyError):
        store.get("does-not-exist")


def test_result_store_get_ibis_none_when_not_set():
    store = ResultStore()
    rid = store.store_tabular(None, None)
    assert store.get_ibis(rid) is None


def test_result_store_invalidate_ibis_refs():
    store = ResultStore()
    mock_ref = object()
    rid = store.store_tabular(None, mock_ref)
    assert store.get_ibis(rid) is mock_ref
    store.invalidate_all_ibis_refs()
    assert store.get_ibis(rid) is None


def test_result_store_len_and_ids():
    store = ResultStore()
    assert len(store) == 0
    r1 = store.store_tabular(None, None)
    r2 = store.store_tabular(None, None)
    assert len(store) == 2
    assert set(store.ids()) == {r1, r2}


def test_result_store_get_arrow():
    store = ResultStore()
    table = pa.table({"v": [1, 2]})
    rid = store.store_tabular(table, None)
    assert store.get_arrow(rid).equals(table)


# ---------------------------------------------------------------------------
# SF-6: BotResponse
# ---------------------------------------------------------------------------


def _minimal_trace() -> RunTrace:
    return RunTrace(
        run_id="r",
        conversation_id="c",
        timestamp=datetime.now(timezone.utc),
        tool_calls=[],
        usage=Usage(),
    )


def test_bot_response_frozen():
    from pydantic import ValidationError

    trace = _minimal_trace()
    resp = BotResponse(status=Status.ok, narrative="Done.", trace=trace)
    with pytest.raises(ValidationError):
        resp.status = Status.error


def test_bot_response_full_json_serialization():
    import json

    trace = _minimal_trace()
    resp = BotResponse(
        status=Status.refused,
        narrative="Cannot answer.",
        trace=trace,
        reason="No matching metric.",
    )
    data = json.loads(resp.model_dump_json())
    assert data["status"] == "refused"
    assert data["trace"]["usage"]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# SF-7: Bot abstract base class
# ---------------------------------------------------------------------------


def test_bot_is_abstract():
    with pytest.raises(TypeError):
        Bot(model="claude-sonnet-4-6")


def test_bot_subclass_must_implement_build_agent():
    class ConcreteBot(Bot):
        def _build_agent(self):
            self._toolset = FunctionToolset()
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    assert bot.store is not None
    assert isinstance(bot.store, ResultStore)


def test_bot_get_result_delegates_to_store():
    class ConcreteBot(Bot):
        def _build_agent(self):
            self._toolset = FunctionToolset()
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    table = pa.table({"v": [1]})
    rid = bot.store.store_tabular(table, None)
    entry = bot.get_result(rid)
    assert entry.arrow.equals(table)


def test_bot_chat_raises_not_implemented():
    import asyncio

    class ConcreteBot(Bot):
        def _build_agent(self):
            self._toolset = FunctionToolset()
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    with pytest.raises(NotImplementedError):
        asyncio.run(bot.chat("hello"))


# ---------------------------------------------------------------------------
# SF-7: Phase 2 public exports
# ---------------------------------------------------------------------------


def test_public_exports_include_query_bot():
    from aitaem.agent import QueryBot, QueryResponse, QueryPayload
    assert all(x is not None for x in [QueryBot, QueryResponse, QueryPayload])


# ---------------------------------------------------------------------------
# Plan 28 / SF-1: Bot.__init__ tools storage + self._toolset contract
# ---------------------------------------------------------------------------


def test_bot_init_stores_tools_list():
    def some_fn() -> str:
        return "x"

    class ConcreteBot(Bot):
        def _build_agent(self):
            self._toolset = FunctionToolset()
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6", tools=[some_fn])
    assert bot._tools == [some_fn]


def test_bot_init_tools_none_defaults_to_empty_list():
    class ConcreteBot(Bot):
        def _build_agent(self):
            self._toolset = FunctionToolset()
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    assert bot._tools == []


def test_bot_subclass_forgetting_toolset_raises_typeerror_at_construction():
    class ForgetfulBot(Bot):
        def _build_agent(self):
            return None  # does NOT set self._toolset

    with pytest.raises(TypeError, match="ForgetfulBot"):
        ForgetfulBot(model="claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Plan 28 / SF-2: _register_tool() + Bot.add_tool()
# ---------------------------------------------------------------------------


def probe_tool_sync() -> str:
    return "sync-value"


async def probe_tool_async() -> str:
    return "async-value"


class _InvokableBot(Bot):
    """Minimal concrete Bot with a real, FunctionModel-backed Agent.

    Used to test the add_tool()/_register_tool() mechanism directly against
    a live Agent, independent of any convenience bot's ask()/chat() wrapping
    (which the SF-10 composition suite covers against the real bots).
    """

    def _build_agent(self):
        from pydantic_ai import Agent

        toolset = FunctionToolset()
        for tool in self._tools:
            _register_tool(toolset, tool)
        self._toolset = toolset
        return Agent(model=self._model, toolsets=[toolset])


def _tool_calling_model(tool_name: str) -> FunctionModel:
    """FunctionModel that calls `tool_name` once, then echoes its return value."""

    def responder(messages: list, info: AgentInfo) -> ModelResponse:
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart) and p.tool_name == tool_name:
                        return ModelResponse(parts=[TextPart(f"result={p.content}")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={}, tool_call_id="tc-1")]
        )

    return FunctionModel(responder)


def _tools_present_model(tool_names_holder: list) -> FunctionModel:
    """FunctionModel that records visible tool names on the first step and stops."""

    def responder(messages: list, info: AgentInfo) -> ModelResponse:
        tool_names_holder.append(sorted(t.name for t in info.function_tools))
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(responder)


@pytest.mark.parametrize("probe", [probe_tool_sync, probe_tool_async], ids=["sync", "async"])
def test_add_tool_plain_function_is_invoked_and_returns_value(probe):
    bot = _InvokableBot(model=_tool_calling_model(probe.__name__))
    bot.add_tool(probe)
    result = asyncio.run(bot._agent.run("go"))
    expected = "sync-value" if probe is probe_tool_sync else "async-value"
    assert expected in result.output


def test_add_tool_accepts_tool_instance():
    tool = Tool(probe_tool_sync)
    bot = _InvokableBot(model=_tool_calling_model("probe_tool_sync"))
    bot.add_tool(tool)
    result = asyncio.run(bot._agent.run("go"))
    assert "sync-value" in result.output


def test_add_tool_between_calls_visible_only_from_second_call_onward():
    seen: list = []
    bot = _InvokableBot(model=_tools_present_model(seen))
    asyncio.run(bot._agent.run("first"))
    bot.add_tool(probe_tool_sync)
    asyncio.run(bot._agent.run("second"))
    assert "probe_tool_sync" not in seen[0]
    assert "probe_tool_sync" in seen[1]


def test_add_tool_mutates_toolset_in_place():
    bot = _InvokableBot(model=_tool_calling_model("probe_tool_sync"))
    toolset_before = bot._toolset
    bot.add_tool(probe_tool_sync)
    assert bot._toolset is toolset_before
    assert "probe_tool_sync" in bot._toolset.tools


def test_add_tool_collision_raises_usererror():
    bot = _InvokableBot(model=_tool_calling_model("probe_tool_sync"))
    bot.add_tool(probe_tool_sync)
    with pytest.raises(UserError, match="conflicts with existing tool"):
        bot.add_tool(probe_tool_sync)


def test_add_tool_collision_leaves_toolset_unmodified():
    bot = _InvokableBot(model=_tool_calling_model("probe_tool_sync"))
    bot.add_tool(probe_tool_sync)
    names_before = set(bot._toolset.tools)
    with pytest.raises(UserError):
        bot.add_tool(probe_tool_sync)
    assert set(bot._toolset.tools) == names_before
