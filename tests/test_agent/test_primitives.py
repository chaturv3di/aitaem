from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

import pyarrow as pa
import pytest

from aitaem.agent.response import BotResponse
from aitaem.agent.store import ResultStore
from aitaem.agent.trace import RunTrace, Status, Usage
from aitaem.agent.base import Bot


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
        [sys.executable, "tools/check_import_graph.py"],
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
    id1 = store.store(None, None)
    id2 = store.store(None, None)
    assert id1 != id2


def test_result_store_get_retrieves_entry():
    store = ResultStore()
    table = pa.table({"x": [1, 2, 3]})
    rid = store.store(table, None, metadata={"source": "test"})
    entry = store.get(rid)
    assert entry.id == rid
    assert entry.arrow.equals(table)
    assert entry.metadata["source"] == "test"


def test_result_store_get_missing_raises():
    store = ResultStore()
    with pytest.raises(KeyError):
        store.get("does-not-exist")


def test_result_store_get_ibis_none_when_not_set():
    store = ResultStore()
    rid = store.store(None, None)
    assert store.get_ibis(rid) is None


def test_result_store_invalidate_ibis_refs():
    store = ResultStore()
    mock_ref = object()
    rid = store.store(None, mock_ref)
    assert store.get_ibis(rid) is mock_ref
    store.invalidate_all_ibis_refs()
    assert store.get_ibis(rid) is None


def test_result_store_len_and_ids():
    store = ResultStore()
    assert len(store) == 0
    r1 = store.store(None, None)
    r2 = store.store(None, None)
    assert len(store) == 2
    assert set(store.ids()) == {r1, r2}


def test_result_store_get_arrow():
    store = ResultStore()
    table = pa.table({"v": [1, 2]})
    rid = store.store(table, None)
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
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    assert bot.store is not None
    assert isinstance(bot.store, ResultStore)


def test_bot_get_result_delegates_to_store():
    class ConcreteBot(Bot):
        def _build_agent(self):
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    table = pa.table({"v": [1]})
    rid = bot.store.store(table, None)
    entry = bot.get_result(rid)
    assert entry.arrow.equals(table)


def test_bot_chat_raises_not_implemented():
    import asyncio

    class ConcreteBot(Bot):
        def _build_agent(self):
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
