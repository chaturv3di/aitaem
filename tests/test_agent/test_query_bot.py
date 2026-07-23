from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, AgentInfo
from pydantic_ai.models.test import TestModel

from aitaem.agent.query_bot import (
    QueryBot, QueryResponse, _build_layer_a, _build_layer_b, _LARGE_CATALOG_THRESHOLD,
    _permission_fingerprint, _provider_cache_config,
)
from aitaem.agent.query_tools import compute_metrics, record_intent, resolve_intent
from aitaem.agent.query_types import QueryDeps, QueryOutput
from aitaem.agent.response import BotResponse
from aitaem.agent.store import ResultStore
from aitaem.agent.trace import RunTrace, ToolCall, Usage, Status


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_spec_cache():
    sc = MagicMock()
    sc.metrics = {
        "revenue": MagicMock(description="Total revenue", entities=["user_id"], format="currency:USD"),
        "ctr": MagicMock(description="Click-through rate", entities=None, format="percentage"),
    }
    sc.slices = {"by_country": MagicMock(description="By country")}
    sc.segments = {}
    return sc


def _make_bot(model=None):
    sc = _make_spec_cache()
    cm = MagicMock()
    return QueryBot(model=model or TestModel(), spec_cache=sc, connection_manager=cm)


def _revenue_mock_mc():
    mc = MagicMock()
    table = pa.table({
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
    mock_ibis = MagicMock()
    mock_ibis.to_pyarrow.return_value = table
    mc.compute.return_value = mock_ibis
    return mc


def _make_bot_with_model(model):
    sc = _make_spec_cache()
    cm = MagicMock()
    return QueryBot(model=model, spec_cache=sc, connection_manager=cm)


# ---------------------------------------------------------------------------
# FunctionModel helpers — 3-step flow
# ---------------------------------------------------------------------------

def _make_three_step_model(metric: str = "revenue"):
    """FunctionModel: record_intent → resolve_intent → compute_metrics → QueryOutput."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        # Map tool_call_id → tool_name from ToolCallParts in ModelResponses
        call_names: dict[str, str] = {}
        for m in messages:
            if isinstance(m, ModelResponse):
                for p in m.parts:
                    if isinstance(p, ToolCallPart):
                        call_names[p.tool_call_id] = p.tool_name

        # Collect tool returns by tool name
        returns: dict[str, dict] = {}
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart):
                        name = call_names.get(p.tool_call_id, "")
                        if name:
                            returns[name] = p.model_response_object()

        if "record_intent" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="record_intent",
                args=json.dumps({"metric_concept": metric, "scope": "overall"}),
                tool_call_id="tc-1",
            )])
        elif "resolve_intent" not in returns:
            intent_id = returns["record_intent"].get("intent_id", 0)
            return ModelResponse(parts=[ToolCallPart(
                tool_name="resolve_intent",
                args=json.dumps({"intent_id": intent_id, "metric_name": metric}),
                tool_call_id="tc-2",
            )])
        elif "compute_metrics" not in returns:
            exact = returns["resolve_intent"].get("exact_match")
            if exact is None:
                output = QueryOutput(
                    status=Status.refused,
                    narrative="Could not resolve the requested metric.",
                    result_ids=[],
                    reason="No exact match found.",
                )
                return ModelResponse(parts=[TextPart(content=output.model_dump_json())])
            token = exact["spec_token"]
            return ModelResponse(parts=[ToolCallPart(
                tool_name="compute_metrics",
                args=json.dumps({"spec_token": token}),
                tool_call_id="tc-3",
            )])
        else:
            compute_data = returns["compute_metrics"]
            result_id = compute_data.get("result_id", "")
            output = QueryOutput(
                status=Status.ok if result_id else Status.error,
                narrative=f"{metric.capitalize()} computed: {compute_data.get('row_count', 0)} rows.",
                result_ids=[result_id] if result_id else [],
            )
            return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _make_refused_model():
    """FunctionModel: immediately refuses without calling any tool."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        output = QueryOutput(
            status=Status.refused,
            narrative="That metric is not in the catalog.",
            result_ids=[],
            reason="No exact match for 'sales_velocity'.",
        )
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


# ---------------------------------------------------------------------------
# Autouse fixture: patch MetricCompute for all integration tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_metric_compute():
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_revenue_mock_mc()):
        yield


# ---------------------------------------------------------------------------
# SF-6: System prompt layer tests
# ---------------------------------------------------------------------------

def test_system_prompt_layer_a_contains_workflow():
    layer_a = _build_layer_a()
    assert "record_intent" in layer_a
    assert "resolve_intent" in layer_a
    assert "compute_metrics" in layer_a
    assert "Metric Precision Rule" in layer_a


def test_system_prompt_layer_a_contains_analysis_tools():
    layer_a = _build_layer_a()
    assert "rank_by_value" in layer_a
    assert "filter_by_threshold" in layer_a


def test_system_prompt_layer_b_contains_catalog():
    layer_b = _build_layer_b(_make_spec_cache())
    assert "revenue" in layer_b
    assert "ctr" in layer_b
    assert "by_country" in layer_b


def test_system_prompt_layer_b_no_format_hints():
    """Format hints are not in Layer B — they're returned by compute_metrics at narrative time."""
    layer_b = _build_layer_b(_make_spec_cache())
    assert "currency:USD" not in layer_b
    assert "percentage" not in layer_b


def test_system_prompt_layer_b_empty_catalog():
    sc = MagicMock()
    sc.metrics = {}
    sc.slices = {}
    sc.segments = {}
    layer_b = _build_layer_b(sc)
    assert "(none)" in layer_b


def test_system_prompt_layer_b_large_catalog_placeholder():
    sc = MagicMock()
    sc.metrics = {
        f"metric_{i}": MagicMock(description="", entities=None, format=None)
        for i in range(_LARGE_CATALOG_THRESHOLD + 1)
    }
    sc.slices = {}
    sc.segments = {}
    layer_b = _build_layer_b(sc)
    assert "resolve_intent" in layer_b
    assert "metric_0" not in layer_b


def test_permission_fingerprint_same_catalog_same_fingerprint():
    sc1 = _make_spec_cache()
    sc2 = _make_spec_cache()
    assert _permission_fingerprint(sc1) == _permission_fingerprint(sc2)


def test_permission_fingerprint_different_metrics_different_fingerprint():
    sc1 = _make_spec_cache()
    sc2 = MagicMock()
    sc2.metrics = {"unique_metric": MagicMock()}
    sc2.slices = {}
    sc2.segments = {}
    assert _permission_fingerprint(sc1) != _permission_fingerprint(sc2)


def test_provider_cache_config_anthropic():
    cfg = _provider_cache_config("anthropic:claude-haiku-4-5-20251001", "t1")
    assert cfg == {"anthropic_cache_instructions": "5m"}


def test_provider_cache_config_openai():
    cfg = _provider_cache_config("openai:gpt-4o-mini", "tenant-abc")
    assert cfg["openai_prompt_cache_key"] == "aitaem-tenant-abc"
    assert cfg["openai_prompt_cache_retention"] == "24h"


def test_provider_cache_config_unknown_returns_empty():
    cfg = _provider_cache_config("gemini:gemini-pro", "t1")
    assert cfg == {}


def test_provider_cache_config_non_string_model():
    cfg = _provider_cache_config(TestModel(), None)
    assert cfg == {}


def test_build_agent_has_record_resolve_compute():
    bot = _make_bot()
    agent = bot._agent
    all_tool_names: set[str] = set()
    for toolset in agent._user_toolsets:
        if hasattr(toolset, "tools"):
            all_tool_names.update(toolset.tools.keys())
    assert "record_intent" in all_tool_names
    assert "resolve_intent" in all_tool_names
    assert "compute_metrics" in all_tool_names


# ---------------------------------------------------------------------------
# Plan 28 / SF-3: constructor tools= composition
# ---------------------------------------------------------------------------

def custom_tool_sync() -> str:
    return "sync-value"


async def custom_tool_async() -> str:
    return "async-value"


def _make_custom_tool_model(tool_name: str):
    """FunctionModel: calls `tool_name` once, then reports its return value in narrative."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart) and p.tool_name == tool_name:
                        output = QueryOutput(
                            status=Status.ok,
                            narrative=f"got:{p.content}",
                            result_ids=[],
                        )
                        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={}, tool_call_id="tc-1")]
        )

    return FunctionModel(fn)


def test_constructor_tools_registered_alongside_defaults():
    bot = QueryBot(
        model=TestModel(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        tools=[custom_tool_sync],
    )
    expected_defaults = {
        "record_intent", "resolve_intent", "compute_metrics",
        "rank_by_value", "filter_by_threshold", "distribution_summary",
        "period_over_period", "contribution_share",
    }
    assert "custom_tool_sync" in bot._toolset.tools
    assert expected_defaults.issubset(bot._toolset.tools.keys())


@pytest.mark.parametrize(
    "tool_fn,expected",
    [(custom_tool_sync, "sync-value"), (custom_tool_async, "async-value")],
    ids=["sync", "async"],
)
def test_constructor_tools_invoked(tool_fn, expected):
    bot = QueryBot(
        model=_make_custom_tool_model(tool_fn.__name__),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        tools=[tool_fn],
    )
    response = asyncio.run(bot.ask("use the custom tool"))
    assert expected in response.narrative


def test_constructor_tools_collision_with_default_raises_at_construction():
    def compute_metrics() -> str:  # name collides with a default tool
        return "x"

    with pytest.raises(UserError, match="conflicts with existing tool"):
        QueryBot(
            model=TestModel(),
            spec_cache=_make_spec_cache(),
            connection_manager=MagicMock(),
            tools=[compute_metrics],
        )


def test_constructor_tools_two_entries_collide_with_each_other():
    def custom_dup() -> str:
        return "a"

    with pytest.raises(UserError, match="conflicts with existing tool"):
        QueryBot(
            model=TestModel(),
            spec_cache=_make_spec_cache(),
            connection_manager=MagicMock(),
            tools=[custom_dup, custom_dup],
        )


# ---------------------------------------------------------------------------
# QueryBot construction tests
# ---------------------------------------------------------------------------

def test_query_bot_has_result_store():
    bot = _make_bot()
    assert bot.store is not None


def test_query_bot_is_concrete():
    bot = _make_bot()
    assert isinstance(bot, QueryBot)


def test_query_response_is_bot_response_subtype():
    assert issubclass(QueryResponse, BotResponse)


def test_query_bot_tenant_id_stored():
    sc = _make_spec_cache()
    bot = QueryBot(model=TestModel(), spec_cache=sc, connection_manager=MagicMock(), tenant_id="org-1")
    assert bot._tenant_id == "org-1"


# ---------------------------------------------------------------------------
# _assemble_payload unit tests
# ---------------------------------------------------------------------------

def _minimal_trace(tool_calls=None):
    return RunTrace(
        run_id="r",
        conversation_id="c",
        timestamp=datetime.now(timezone.utc),
        tool_calls=tool_calls or [],
        usage=Usage(),
    )


def _tc_with_payload(tc_id: str, name: str, payload_summary: dict | None = None) -> ToolCall:
    body: dict = {}
    if payload_summary is not None:
        body["payload_summary"] = payload_summary
    return ToolCall(tool_call_id=tc_id, name=name, args={}, llm_summary=json.dumps(body))


def test_assemble_payload_ok_with_result_ids():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1", "r2"])
    trace = _minimal_trace()
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.result_ids == ["r1", "r2"]
    assert payload.primary_result_id == "r1"


def test_assemble_payload_refused_empty_result_ids():
    output = QueryOutput(status=Status.refused, narrative="N/A.", reason="No match.")
    trace = _minimal_trace()
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.result_ids == []
    assert payload.primary_result_id is None


def test_assemble_payload_extracts_from_payload_summary():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "slices_used": ["by_country"],
            "period_type": "monthly",
            "time_window": ["2024-01-01", "2024-03-31"],
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == ["revenue"]
    assert payload.slices_used == ["by_country"]
    assert payload.period_type == "monthly"
    assert payload.time_window == ("2024-01-01", "2024-03-31")


def test_assemble_payload_sample_from_primary_result():
    rows = [{"metric_name": "revenue", "metric_value": 100.0}]
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "result_id": "r1",
            "metrics_used": ["revenue"],
            "sample": rows,
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.sample == rows


def test_assemble_payload_sample_only_from_primary_not_secondary():
    rows_primary = [{"metric_name": "revenue", "metric_value": 100.0}]
    rows_other = [{"metric_name": "ctr", "metric_value": 0.05}]
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1", "r2"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "result_id": "r1",
            "metrics_used": ["revenue"],
            "sample": rows_primary,
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "result_id": "r2",
            "metrics_used": ["ctr"],
            "sample": rows_other,
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.sample == rows_primary


def test_assemble_payload_sample_none_when_no_results():
    output = QueryOutput(status=Status.refused, narrative="N/A.", reason="No match.")
    trace = _minimal_trace()
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.sample is None


def test_assemble_payload_list_fields_union_dedup():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue", "ctr"],
            "period_type": "monthly",
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "period_type": "weekly",
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == ["revenue", "ctr"]
    assert payload.metrics_used.count("revenue") == 1
    assert payload.period_type == "monthly"


def test_assemble_payload_scalar_first_write_wins():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "period_type": "monthly",
            "time_window": ["2024-01-01", "2024-03-31"],
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["refund_rate"],
            "period_type": "weekly",
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.period_type == "monthly"
    assert set(payload.metrics_used) == {"revenue", "refund_rate"}


def test_assemble_payload_propagates_format_hints():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue", "ctr"],
            "format_hints": {"revenue": "currency:USD", "ctr": "percentage"},
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.format_hints == {"revenue": "currency:USD", "ctr": "percentage"}


def test_assemble_payload_format_hints_first_write_wins():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "format_hints": {"revenue": "currency:USD"},
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "format_hints": {"revenue": "currency:EUR"},
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.format_hints["revenue"] == "currency:USD"


def test_assemble_payload_ignores_tools_without_payload_summary():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r2"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "rank_by_value", payload_summary=None),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == []


def test_assemble_payload_ignores_non_json_llm_summary():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=[])
    trace = _minimal_trace(tool_calls=[
        ToolCall(tool_call_id="tc1", name="compute_metrics",
                 args={}, llm_summary="Computed 1 metric."),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == []


# ---------------------------------------------------------------------------
# SF-7: chat() and ask() integration tests (3-step flow)
# ---------------------------------------------------------------------------

def test_chat_returns_query_response():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert isinstance(response, QueryResponse)


def test_chat_status_ok_on_successful_compute():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert response.status == Status.ok


def test_chat_payload_has_result_id():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert len(response.payload.result_ids) == 1
    rid = response.payload.primary_result_id
    assert rid is not None
    entry = bot.get_result(rid)
    assert entry.arrow is not None


def test_chat_refused_status():
    bot = _make_bot_with_model(_make_refused_model())
    response = asyncio.run(bot.chat("What was sales velocity?"))
    assert response.status == Status.refused
    assert response.reason is not None
    assert response.payload.result_ids == []


def test_ask_does_not_accumulate_history():
    bot = _make_bot_with_model(_make_three_step_model())
    assert bot._message_history == []
    asyncio.run(bot.ask("What was revenue?"))
    assert bot._message_history == []


def test_chat_accumulates_history():
    bot = _make_bot_with_model(_make_three_step_model())
    assert bot._message_history == []
    asyncio.run(bot.chat("What was revenue?"))
    assert len(bot._message_history) > 0


def test_chat_multi_turn_history_grows():
    bot = _make_bot_with_model(_make_three_step_model())
    asyncio.run(bot.chat("First question."))
    after_turn_1 = len(bot._message_history)
    asyncio.run(bot.chat("Second question."))
    after_turn_2 = len(bot._message_history)
    assert after_turn_2 > after_turn_1


def test_trace_contains_three_step_tool_calls():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    tool_names = [tc.name for tc in response.trace.tool_calls]
    assert "record_intent" in tool_names
    assert "resolve_intent" in tool_names
    assert "compute_metrics" in tool_names


def test_trace_usage_populated():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert response.trace.usage is not None


def test_trace_compute_metrics_result_id_and_duration_populated():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    compute_call = next(
        tc for tc in response.trace.tool_calls if tc.name == "compute_metrics"
    )
    assert compute_call.result_id == response.payload.primary_result_id
    assert compute_call.duration_ms is not None
    assert compute_call.duration_ms >= 0


def test_three_step_flow_result_retrievable():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id
    entry = bot.get_result(rid)
    assert entry.arrow is not None


# ---------------------------------------------------------------------------
# Plan 28 / SF-5: per-call extra_tools= on chat() / ask()
# ---------------------------------------------------------------------------


def _visibility_recording_model(recorder: list) -> FunctionModel:
    """FunctionModel that records visible tool names on each call, then ends the turn."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        recorder.append(sorted(t.name for t in info.function_tools))
        output = QueryOutput(status=Status.ok, narrative="ok", result_ids=[])
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


@pytest.mark.parametrize(
    "tool_fn,expected",
    [(custom_tool_sync, "sync-value"), (custom_tool_async, "async-value")],
    ids=["sync", "async"],
)
def test_ask_extra_tools_invoked(tool_fn, expected):
    bot = _make_bot_with_model(_make_custom_tool_model(tool_fn.__name__))
    response = asyncio.run(bot.ask("use the extra tool", extra_tools=[tool_fn]))
    assert expected in response.narrative


def test_chat_extra_tools_ephemeral_not_visible_next_turn():
    seen: list = []
    bot = _make_bot_with_model(_visibility_recording_model(seen))
    asyncio.run(bot.chat("first", extra_tools=[custom_tool_sync]))
    asyncio.run(bot.chat("second"))
    assert "custom_tool_sync" in seen[0]
    assert "custom_tool_sync" not in seen[1]


def test_ask_extra_tools_none_omits_toolsets_kwarg():
    bot = _make_bot_with_model(TestModel())
    with patch.object(bot._agent, "run", wraps=bot._agent.run) as mock_run:
        asyncio.run(bot.ask("hello"))
    _, kwargs = mock_run.call_args
    assert "toolsets" not in kwargs


def test_chat_extra_tools_none_omits_toolsets_kwarg():
    bot = _make_bot_with_model(TestModel())
    with patch.object(bot._agent, "run", wraps=bot._agent.run) as mock_run:
        asyncio.run(bot.chat("hello"))
    _, kwargs = mock_run.call_args
    assert "toolsets" not in kwargs


def test_ask_extra_tools_collision_surfaces_as_error_status():
    def compute_metrics() -> str:  # collides with the default compute_metrics tool
        return "x"

    bot = _make_bot_with_model(TestModel())
    response = asyncio.run(bot.ask("hello", extra_tools=[compute_metrics]))
    assert response.status == Status.error
    assert "conflicts with existing tool" in (response.reason or "")


def test_chat_extra_tools_collision_surfaces_as_error_status():
    def compute_metrics() -> str:  # collides with the default compute_metrics tool
        return "x"

    bot = _make_bot_with_model(TestModel())
    response = asyncio.run(bot.chat("hello", extra_tools=[compute_metrics]))
    assert response.status == Status.error
    assert "conflicts with existing tool" in (response.reason or "")


# ---------------------------------------------------------------------------
# _error_response and conversation_id correlation
# ---------------------------------------------------------------------------

def test_error_response_on_first_turn_uses_fresh_uuid():
    exc = RuntimeError("network timeout")
    run_start = datetime.now(timezone.utc)
    resp = QueryBot._error_response(exc, run_start, None)
    assert resp.status == Status.error
    assert resp.trace.conversation_id is not None
    assert len(resp.trace.conversation_id) > 0


def test_error_response_reuses_conversation_id():
    exc = RuntimeError("timeout")
    run_start = datetime.now(timezone.utc)
    existing_id = "fixed-conversation-id"
    resp = QueryBot._error_response(exc, run_start, existing_id)
    assert resp.trace.conversation_id == existing_id


def test_error_response_trace_has_error_field():
    exc = ValueError("bad input")
    run_start = datetime.now(timezone.utc)
    resp = QueryBot._error_response(exc, run_start, None)
    assert resp.trace.error is not None
    assert "ValueError" in resp.trace.error


# ---------------------------------------------------------------------------
# History round-trip tests
# ---------------------------------------------------------------------------

def test_dump_history_captures_result_store():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id

    bundle = bot.dump_history()
    assert rid in bundle["artifacts"]


def test_load_history_restores_result():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id
    original_arrow = bot.get_result(rid).arrow

    bundle = bot.dump_history()

    restored = QueryBot.load_history(
        bundle,
        model=_make_three_step_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
    )

    entry = restored.get_result(rid)
    assert entry.arrow.equals(original_arrow)
    assert entry.ibis_ref is None


def test_load_history_restores_message_history():
    bot = _make_bot_with_model(_make_three_step_model())
    asyncio.run(bot.chat("What was revenue?"))
    n_messages = len(bot._message_history)

    bundle = bot.dump_history()

    restored = QueryBot.load_history(
        bundle,
        model=_make_three_step_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
    )

    assert len(restored._message_history) == n_messages


# ---------------------------------------------------------------------------
# Plan 31, SF-2: compute_metrics restores spec_token on failure
# ---------------------------------------------------------------------------


def _make_ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _make_query_deps():
    return QueryDeps(
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        store=ResultStore(),
    )


def _mint_token(ctx, metric="revenue"):
    """record_intent + resolve_intent -> a valid spec_token for `metric`."""
    record_intent(ctx, metric_concept=metric, scope="overall")
    resolved = resolve_intent(ctx, intent_id=0, metric_name=metric)
    assert resolved.exact_match is not None
    return resolved.exact_match.spec_token


def _mock_mc_raising(exc: Exception):
    mc = MagicMock()
    mc.compute.side_effect = exc
    return mc


def test_compute_metrics_restores_token_on_failure_for_retry():
    deps = _make_query_deps()
    ctx = _make_ctx(deps)
    token = _mint_token(ctx)

    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_mock_mc_raising(RuntimeError("boom"))):
        first = compute_metrics(ctx, spec_token=token)
    assert first.result_id == ""
    assert first.error is not None

    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_revenue_mock_mc()):
        second = compute_metrics(ctx, spec_token=token)
    assert second.error is None
    assert second.result_id != ""


def test_compute_metrics_success_still_consumes_token():
    """Restore-on-failure must not weaken the pop's protection on the success path."""
    deps = _make_query_deps()
    ctx = _make_ctx(deps)
    token = _mint_token(ctx)

    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_revenue_mock_mc()):
        first = compute_metrics(ctx, spec_token=token)
    assert first.error is None

    second = compute_metrics(ctx, spec_token=token)
    assert second.error is not None
    assert "already consumed" in second.error
    assert second.result_id == ""


def test_compute_metrics_concurrent_race_never_double_executes():
    """Adversarial, real-OS-thread race: hammer pop attempts right as a failing
    call's restore lands, and assert the durable invariant — max concurrent
    depth inside MetricCompute.compute() for this token never exceeds 1 —
    rather than a timing-window success/duplicate count, which could pass by
    luck today and stay green even if the invariant later breaks.
    """
    deps = _make_query_deps()
    ctx = _make_ctx(deps)
    token = _mint_token(ctx)

    depth_lock = threading.Lock()
    depth = 0
    max_depth = 0

    call_count_lock = threading.Lock()
    call_count = 0

    about_to_restore = threading.Event()
    stop_polling = threading.Event()

    results: list = []
    results_lock = threading.Lock()

    def fake_compute(*args, **kwargs):
        nonlocal depth, max_depth, call_count
        with depth_lock:
            depth += 1
            max_depth = max(max_depth, depth)
        try:
            with call_count_lock:
                call_count += 1
                is_first_call = call_count == 1
            if is_first_call:
                # Widen the window, then signal pollers right before this
                # thread's except block restores the token — maximizes the
                # chance of a poller's pop landing concurrently with the
                # restore, without being the assertion itself.
                time.sleep(0.02)
                about_to_restore.set()
                raise RuntimeError("simulated transient failure")
            mock_ibis = MagicMock()
            mock_ibis.to_pyarrow.return_value = _revenue_mock_mc().compute.return_value.to_pyarrow.return_value
            return mock_ibis
        finally:
            with depth_lock:
                depth -= 1

    def mc_factory(*_args, **_kwargs):
        mc = MagicMock()
        mc.compute.side_effect = fake_compute
        return mc

    def call_once():
        results_local = compute_metrics(ctx, spec_token=token)
        with results_lock:
            results.append(results_local)

    def poller():
        about_to_restore.wait(timeout=2.0)
        for _ in range(50):
            if stop_polling.is_set():
                break
            r = compute_metrics(ctx, spec_token=token)
            with results_lock:
                results.append(r)
            if r.error is None:
                break

    with patch("aitaem.agent.query_tools.MetricCompute", side_effect=mc_factory):
        t_fail = threading.Thread(target=call_once)
        t_poll1 = threading.Thread(target=poller)
        t_poll2 = threading.Thread(target=poller)

        t_fail.start()
        t_poll1.start()
        t_poll2.start()

        t_fail.join(timeout=5.0)
        t_poll1.join(timeout=5.0)
        stop_polling.set()
        t_poll2.join(timeout=5.0)

    # Primary, durable assertion: at most one caller was ever inside compute()
    # for this token at a time, regardless of how the timing landed.
    assert max_depth <= 1, f"observed concurrent compute() depth {max_depth} for a single token"

    # Secondary: exactly one successful execution resulted from the race.
    successes = [r for r in results if r.error is None]
    assert len(successes) == 1
