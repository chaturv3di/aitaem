"""SF-4 (P6.2): validates that RunTrace is faithful to the OpenTelemetry
spans pydantic-ai actually emits, not just that assemble_trace()'s own
mock-based logic is internally consistent (already covered by test_trace.py).

assemble_trace() builds RunTrace entirely from result.new_messages() — the
final, immutable message-part list, assembled after the run completes.
OTel spans are emitted live, during execution, by wrap_tool_execute's
_run_tool_span, which wraps the actual tool call in
tracer.start_as_current_span(...). Count, order, and success/failure are
computed via these two independent mechanisms, so agreement between them is
real evidence of internal consistency, not a tautology.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings

from aitaem.agent.base import Bot
from aitaem.agent.definition_bot import DefinitionBot
from aitaem.agent.definition_types import DefinitionOutput
from aitaem.agent.query_bot import QueryBot
from aitaem.agent.query_types import QueryOutput
from aitaem.agent.trace import Status


@contextlib.contextmanager
def _captured_spans(bot: Bot) -> Iterator[list[ReadableSpan]]:
    """Instrument `bot._agent` with a local, in-memory OTel span exporter for
    the duration of the `with` block, then yield the list of spans captured.

    Builds a fresh TracerProvider + InMemorySpanExporter + SimpleSpanProcessor
    per call (no global set_tracer_provider() call — the provider is passed
    directly to InstrumentationSettings, and bot._agent.instrument is restored
    to its prior value on exit). This keeps span capture scoped to a single
    test with no cross-test leakage.

    The returned list is populated only once the `with` block exits — run the
    agent inside the block, and read the list after it.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    spans: list[ReadableSpan] = []
    prior_instrument = bot._agent.instrument
    bot._agent.instrument = InstrumentationSettings(tracer_provider=provider)
    try:
        yield spans
    finally:
        spans.extend(exporter.get_finished_spans())
        bot._agent.instrument = prior_instrument


def _tool_spans(spans: list[ReadableSpan]) -> list[ReadableSpan]:
    """Filter to execute_tool spans only — wrap_tool_execute shares span
    machinery with output-processing spans, so an unfiltered list isn't
    directly comparable to tool_calls count in general. QueryBot/DefinitionBot
    both use a plain output_type (no function-based output validator), so no
    output-process span is ever emitted for them in practice — filtering by
    gen_ai.operation.name is correct regardless and doesn't depend on that.
    """
    return [s for s in spans if s.attributes.get("gen_ai.operation.name") == "execute_tool"]


# ---------------------------------------------------------------------------
# QueryBot fixtures — mirrors _make_three_step_model()/_revenue_mock_mc() in
# tests/test_agent/test_query_bot.py; kept local per this file's own
# self-containment rather than importing across test modules.
# ---------------------------------------------------------------------------


def _query_bot_spec_cache():
    sc = MagicMock()
    sc.metrics = {
        "revenue": MagicMock(description="Total revenue", entities=["user_id"], format="currency:USD"),
    }
    sc.slices = {}
    sc.segments = {}
    return sc


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


def _three_step_model(metric: str = "revenue") -> FunctionModel:
    """FunctionModel: record_intent -> resolve_intent -> compute_metrics -> QueryOutput."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        call_names: dict[str, str] = {}
        for m in messages:
            if isinstance(m, ModelResponse):
                for p in m.parts:
                    if isinstance(p, ToolCallPart):
                        call_names[p.tool_call_id] = p.tool_name

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
                narrative=f"{metric.capitalize()} computed.",
                result_ids=[result_id] if result_id else [],
            )
            return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


@pytest.fixture(autouse=True)
def _patch_metric_compute():
    from unittest.mock import patch

    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_revenue_mock_mc()):
        yield


def _make_query_bot() -> QueryBot:
    return QueryBot(
        model=_three_step_model(),
        spec_cache=_query_bot_spec_cache(),
        connection_manager=MagicMock(),
    )


# ---------------------------------------------------------------------------
# DefinitionBot fixtures — mirrors _make_full_flow_model()/_make_spec_cache()
# in tests/test_agent/test_definition_bot.py; kept local per this file's own
# self-containment rather than importing across test modules.
# ---------------------------------------------------------------------------


_VALID_METRIC_YAML = """\
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: transaction_date
"""


def _definition_spec_cache():
    sc = MagicMock()
    sc.metrics = {}
    sc.slices = {}
    sc.segments = {}
    return sc


def _definition_connection_manager():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis_table = MagicMock()
    mock_ibis_table.schema.return_value = MagicMock(
        names=["amount", "transaction_date"], types=["float64", "date"]
    )
    mock_ibis_table.columns = ["amount", "transaction_date"]
    mock_connector.get_table.return_value = mock_ibis_table
    mock_cm.get_connection.return_value = mock_connector
    return mock_cm


def _full_flow_model() -> FunctionModel:
    """FunctionModel that simulates the full 4-step DefinitionBot flow."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        call_names: dict[str, str] = {}
        for m in messages:
            if isinstance(m, ModelResponse):
                for p in m.parts:
                    if isinstance(p, ToolCallPart):
                        call_names[p.tool_call_id] = p.tool_name

        returns: dict[str, dict] = {}
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart):
                        name = call_names.get(p.tool_call_id, "")
                        if name:
                            returns[name] = p.model_response_object()

        if "record_definition_intent" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="record_definition_intent",
                args=json.dumps({"spec_type": "metric", "description": "Test spec"}),
                tool_call_id="tc-1",
            )])
        if "list_tables" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="list_tables",
                args=json.dumps({}),
                tool_call_id="tc-2",
            )])
        if "describe_table" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="describe_table",
                args=json.dumps({"table_name": "transactions", "backend_type": "duckdb"}),
                tool_call_id="tc-3",
            )])
        if "draft_spec" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="draft_spec",
                args=json.dumps({"spec_type": "metric", "yaml_string": _VALID_METRIC_YAML}),
                tool_call_id="tc-4",
            )])
        if "validate_spec" not in returns:
            draft_id = returns["draft_spec"].get("draft_id", "")
            return ModelResponse(parts=[ToolCallPart(
                tool_name="validate_spec",
                args=json.dumps({"draft_id": draft_id}),
                tool_call_id="tc-5",
            )])

        validate_data = returns["validate_spec"]
        token = validate_data.get("spec_draft_token")
        output = DefinitionOutput(
            status=Status.ok if token else Status.error,
            narrative="Spec defined successfully." if token else "Validation failed.",
            spec_draft_token=token,
        )
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _make_definition_bot() -> DefinitionBot:
    return DefinitionBot(
        model=_full_flow_model(),
        spec_cache=_definition_spec_cache(),
        connection_manager=_definition_connection_manager(),
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_bot, question",
    [
        (_make_query_bot, "What was revenue?"),
        (_make_definition_bot, "Define a revenue metric on the transactions table"),
    ],
)
def test_span_count_matches_tool_call_count(make_bot, question):
    import asyncio

    bot = make_bot()
    with _captured_spans(bot) as spans:
        response = asyncio.run(bot.chat(question))
    assert len(_tool_spans(spans)) == len(response.trace.tool_calls)


@pytest.mark.parametrize(
    "make_bot, question",
    [
        (_make_query_bot, "What was revenue?"),
        (_make_definition_bot, "Define a revenue metric on the transactions table"),
    ],
)
def test_span_tool_call_ids_match_trace(make_bot, question):
    import asyncio

    bot = make_bot()
    with _captured_spans(bot) as spans:
        response = asyncio.run(bot.chat(question))

    span_ids = {
        (s.attributes["gen_ai.tool.name"], s.attributes["gen_ai.tool.call.id"])
        for s in _tool_spans(spans)
    }
    trace_ids = {(tc.name, tc.tool_call_id) for tc in response.trace.tool_calls}
    assert span_ids == trace_ids


@pytest.mark.parametrize(
    "make_bot, question",
    [
        (_make_query_bot, "What was revenue?"),
        (_make_definition_bot, "Define a revenue metric on the transactions table"),
    ],
)
def test_span_order_matches_trace_order(make_bot, question):
    import asyncio

    bot = make_bot()
    with _captured_spans(bot) as spans:
        response = asyncio.run(bot.chat(question))

    tool_spans_sorted = sorted(_tool_spans(spans), key=lambda s: s.start_time)
    span_order = [s.attributes["gen_ai.tool.call.id"] for s in tool_spans_sorted]
    trace_order = [tc.tool_call_id for tc in response.trace.tool_calls]
    assert span_order == trace_order


@pytest.mark.parametrize(
    "make_bot, question",
    [
        (_make_query_bot, "What was revenue?"),
        (_make_definition_bot, "Define a revenue metric on the transactions table"),
    ],
)
def test_duration_ms_covers_span_duration(make_bot, question):
    import asyncio

    bot = make_bot()
    with _captured_spans(bot) as spans:
        response = asyncio.run(bot.chat(question))

    trace_by_id = {tc.tool_call_id: tc for tc in response.trace.tool_calls}
    for span in _tool_spans(spans):
        call_id = span.attributes["gen_ai.tool.call.id"]
        span_duration_ms = (span.end_time - span.start_time) / 1e6
        tool_call = trace_by_id[call_id]
        assert tool_call.duration_ms is not None
        assert tool_call.duration_ms >= span_duration_ms
