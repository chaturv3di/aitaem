from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock


from aitaem.agent.trace import assemble_trace


# ---------------------------------------------------------------------------
# SF-9: assemble_trace
# ---------------------------------------------------------------------------


def test_assemble_trace_no_tool_calls():
    result = MagicMock()
    result.run_id = "run-1"
    result.conversation_id = "conv-1"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = []
    result.usage.requests = 1
    result.usage.tool_calls = 0
    result.usage.input_tokens = 100
    result.usage.output_tokens = 50
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)

    assert trace.run_id == "run-1"
    assert trace.conversation_id == "conv-1"
    assert trace.tool_calls == []
    assert trace.usage.input_tokens == 100
    assert trace.usage.total_tokens == 150
    assert trace.traceparent is None


def test_assemble_trace_with_tool_call():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "compute_metrics"
    tc_part.args = {"metrics": ["ctr"]}
    tc_part.tool_call_id = "tc-42"

    tr_part = MagicMock(spec=ToolReturnPart)
    tr_part.tool_name = "compute_metrics"
    tr_part.content = "Computed 1 metric."
    tr_part.tool_call_id = "tc-42"
    tr_part.outcome = "success"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]

    model_request = MagicMock(spec=ModelRequest)
    model_request.parts = [tr_part]

    result = MagicMock()
    result.run_id = "run-2"
    result.conversation_id = "conv-2"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response, model_request]
    result.usage.requests = 1
    result.usage.tool_calls = 1
    result.usage.input_tokens = 200
    result.usage.output_tokens = 80
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = "00-abc-def-01"

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)

    assert len(trace.tool_calls) == 1
    tc = trace.tool_calls[0]
    assert tc.name == "compute_metrics"
    assert tc.args == {"metrics": ["ctr"]}
    assert tc.llm_summary == "Computed 1 metric."
    assert tc.success is True
    assert tc.result_id is None
    assert trace.traceparent == "00-abc-def-01"


def test_assemble_trace_failed_tool_call():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "compute_metrics"
    tc_part.args = {}
    tc_part.tool_call_id = "tc-99"

    tr_part = MagicMock(spec=ToolReturnPart)
    tr_part.tool_name = "compute_metrics"
    tr_part.content = "Metric not found."
    tr_part.tool_call_id = "tc-99"
    tr_part.outcome = "failed"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]
    model_request = MagicMock(spec=ModelRequest)
    model_request.parts = [tr_part]

    result = MagicMock()
    result.run_id = "r"
    result.conversation_id = "c"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response, model_request]
    result.usage.requests = 1
    result.usage.tool_calls = 1
    result.usage.input_tokens = 50
    result.usage.output_tokens = 20
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)

    assert trace.tool_calls[0].success is False


def test_assemble_trace_none_args_becomes_empty_dict():
    """ToolCallPart.args=None is normalised to {} in the ToolCall record."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "no_args_tool"
    tc_part.args = None
    tc_part.tool_call_id = "tc-none"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]

    result = MagicMock()
    result.run_id = "r"
    result.conversation_id = "c"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response]
    result.usage.requests = 1
    result.usage.tool_calls = 0
    result.usage.input_tokens = 0
    result.usage.output_tokens = 0
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    trace = assemble_trace(result, datetime.now(timezone.utc))
    assert trace.tool_calls[0].args == {}


def test_assemble_trace_string_args_parsed():
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "rank_by_value"
    tc_part.args = json.dumps({"result_id": "abc", "limit": 10})
    tc_part.tool_call_id = "tc-str"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]

    result = MagicMock()
    result.run_id = "r"
    result.conversation_id = "c"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response]
    result.usage.requests = 1
    result.usage.tool_calls = 0
    result.usage.input_tokens = 0
    result.usage.output_tokens = 0
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)
    assert trace.tool_calls[0].args == {"result_id": "abc", "limit": 10}
