from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, AgentInfo
from pydantic_ai.models.test import TestModel

from aitaem.agent.query_bot import QueryBot, QueryResponse, _build_system_prompt
from aitaem.agent.query_types import QueryOutput
from aitaem.agent.response import BotResponse
from aitaem.agent.trace import RunTrace, ToolCall, Usage, Status


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_spec_cache(metric_names=("revenue", "ctr"), slice_names=("by_country",), segment_names=()):
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
    """Mock MetricCompute that returns a simple 1-row revenue table."""
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
# FunctionModel helpers
# ---------------------------------------------------------------------------

def _make_compute_then_answer_model(metric: str = "revenue"):
    """FunctionModel: calls compute_metrics, then produces QueryOutput."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        tool_returns = [
            p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not tool_returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="compute_metrics",
                args=json.dumps({"metrics": [metric], "period_type": "all_time"}),
                tool_call_id="tc-1",
            )])
        else:
            tool_data = tool_returns[0].model_response_object()
            result_id = tool_data.get("result_id", "")
            output = QueryOutput(
                status=Status.ok,
                narrative=f"{metric.capitalize()} computed: {tool_data.get('row_count', 0)} rows.",
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
# SF-4: System prompt tests
# ---------------------------------------------------------------------------

def test_system_prompt_contains_metric_names():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "revenue" in prompt
    assert "ctr" in prompt


def test_system_prompt_contains_metric_precision_rule():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "Metric Precision Rule" in prompt
    assert "refused" in prompt


def test_system_prompt_contains_slice_names():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "by_country" in prompt


def test_system_prompt_contains_format_hints():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "currency:USD" in prompt
    assert "percentage" in prompt


def test_system_prompt_empty_catalog():
    sc = MagicMock()
    sc.metrics = {}
    sc.slices = {}
    sc.segments = {}
    prompt = _build_system_prompt(sc)
    assert "(none)" in prompt


# ---------------------------------------------------------------------------
# SF-5: QueryBot construction tests
# ---------------------------------------------------------------------------

def test_query_bot_has_result_store():
    bot = _make_bot()
    assert bot.store is not None


def test_query_bot_is_concrete():
    bot = _make_bot()
    assert isinstance(bot, QueryBot)


def test_query_response_is_bot_response_subtype():
    assert issubclass(QueryResponse, BotResponse)


# ---------------------------------------------------------------------------
# SF-6: _assemble_payload unit tests
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
    """Build a ToolCall whose llm_summary is a JSON-serialized ToolResult."""
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


def test_assemble_payload_list_fields_union_dedup():
    """Two compute_metrics calls — metrics_used is a deduped union; scalars use first-write wins."""
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue", "ctr"],
            "period_type": "monthly",
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],   # duplicate — must be dropped
            "period_type": "weekly",       # scalar conflict — first-write wins
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
            "period_type": "weekly",      # ignored — period_type already set
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
            "format_hints": {"revenue": "currency:EUR"},  # ignored — first-write wins
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.format_hints["revenue"] == "currency:USD"


def test_assemble_payload_ignores_tools_without_payload_summary():
    """Analysis tools with no payload_summary don't affect QueryPayload metadata."""
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r2"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "rank_by_value", payload_summary=None),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == []


def test_assemble_payload_ignores_non_json_llm_summary():
    """Plain-string llm_summary (not JSON) is skipped gracefully."""
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=[])
    trace = _minimal_trace(tool_calls=[
        ToolCall(tool_call_id="tc1", name="compute_metrics",
                 args={}, llm_summary="Computed 1 metric."),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == []


# ---------------------------------------------------------------------------
# SF-8: chat() and ask() tests
# ---------------------------------------------------------------------------

def test_chat_returns_query_response():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert isinstance(response, QueryResponse)


def test_chat_status_ok_on_successful_compute():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert response.status == Status.ok


def test_chat_payload_has_result_id():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
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
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    assert bot._message_history == []
    asyncio.run(bot.ask("What was revenue?"))
    assert bot._message_history == []    # ask() must not mutate history


def test_chat_accumulates_history():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    assert bot._message_history == []
    asyncio.run(bot.chat("What was revenue?"))
    assert len(bot._message_history) > 0


def test_chat_multi_turn_history_grows():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    asyncio.run(bot.chat("First question."))
    after_turn_1 = len(bot._message_history)
    asyncio.run(bot.chat("Second question."))
    after_turn_2 = len(bot._message_history)
    assert after_turn_2 > after_turn_1


def test_trace_contains_tool_call():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert len(response.trace.tool_calls) >= 1
    assert response.trace.tool_calls[0].name == "compute_metrics"


def test_trace_usage_populated():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert response.trace.usage is not None


# ---------------------------------------------------------------------------
# SF-9: _error_response and conversation_id correlation
# ---------------------------------------------------------------------------

def test_error_response_on_first_turn_uses_fresh_uuid():
    """First-turn error (no prior conversation_id) still returns a valid UUID."""
    from datetime import datetime, timezone
    exc = RuntimeError("network timeout")
    run_start = datetime.now(timezone.utc)
    resp = QueryBot._error_response(exc, run_start, None)
    assert resp.status == Status.error
    assert resp.trace.conversation_id is not None
    assert len(resp.trace.conversation_id) > 0


def test_error_response_reuses_conversation_id():
    """After a successful turn, errors reuse the same conversation_id."""
    from datetime import datetime, timezone
    exc = RuntimeError("timeout")
    run_start = datetime.now(timezone.utc)
    existing_id = "fixed-conversation-id"
    resp = QueryBot._error_response(exc, run_start, existing_id)
    assert resp.trace.conversation_id == existing_id


def test_error_response_trace_has_error_field():
    from datetime import datetime, timezone
    exc = ValueError("bad input")
    run_start = datetime.now(timezone.utc)
    resp = QueryBot._error_response(exc, run_start, None)
    assert resp.trace.error is not None
    assert "ValueError" in resp.trace.error


# ---------------------------------------------------------------------------
# SF-10: History round-trip tests
# ---------------------------------------------------------------------------

def test_dump_history_captures_result_store():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id

    bundle = bot.dump_history()
    assert rid in bundle["artifacts"]


def test_load_history_restores_result():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id
    original_arrow = bot.get_result(rid).arrow

    bundle = bot.dump_history()

    restored = QueryBot.load_history(
        bundle,
        model=_make_compute_then_answer_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
    )

    entry = restored.get_result(rid)
    assert entry.arrow.equals(original_arrow)
    assert entry.ibis_ref is None   # ibis refs are not serialized


def test_load_history_restores_message_history():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    asyncio.run(bot.chat("What was revenue?"))
    n_messages = len(bot._message_history)

    bundle = bot.dump_history()

    restored = QueryBot.load_history(
        bundle,
        model=_make_compute_then_answer_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
    )

    assert len(restored._message_history) == n_messages
