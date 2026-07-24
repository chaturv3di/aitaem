"""Tests for SF-7 through SF-10: DefinitionBot class and integration tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch
import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, AgentInfo
from pydantic_ai.models.test import TestModel

from aitaem.agent.definition_bot import (
    DefinitionBot,
    DefinitionResponse,
    _build_layer_a_definition,
    _build_layer_b_definition,
    _LARGE_CATALOG_THRESHOLD,
    _provider_cache_config_definition,
)
from aitaem.agent.definition_types import DefinitionOutput
from aitaem.agent.response import BotResponse
from aitaem.agent.store import ResultStore, TextEntry
from aitaem.agent.trace import Status
from aitaem.specs.metric import MetricSpec


# ---------------------------------------------------------------------------
# Shared fixture YAML
# ---------------------------------------------------------------------------

_VALID_METRIC_YAML = """\
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: transaction_date
"""

_VALID_SLICE_YAML = """\
slice:
  name: by_country
  values:
    - name: US
      where: "country = 'US'"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec_cache(
    metrics=None,
    slices=None,
    segments=None,
):
    sc = MagicMock()
    sc.metrics = metrics or {}
    sc.slices = slices or {}
    sc.segments = segments or {}
    return sc


def _make_bot(model=None, spec_cache=None, connection_manager=None):
    sc = spec_cache or _make_spec_cache()
    cm = connection_manager or MagicMock()
    return DefinitionBot(
        model=model or TestModel(),
        spec_cache=sc,
        connection_manager=cm,
    )


# ---------------------------------------------------------------------------
# SF-7: _build_layer_a_definition
# ---------------------------------------------------------------------------


def test_layer_a_contains_tool_names():
    layer_a = _build_layer_a_definition()
    for tool in ["record_definition_intent", "list_tables", "describe_table", "draft_spec", "validate_spec"]:
        assert tool in layer_a, f"Tool {tool!r} missing from Layer A"


def test_layer_a_contains_yaml_format_for_all_spec_types():
    layer_a = _build_layer_a_definition()
    assert "MetricSpec" in layer_a or "metric:" in layer_a
    assert "SliceSpec" in layer_a or "slice:" in layer_a
    assert "SegmentSpec" in layer_a or "segment:" in layer_a


def test_layer_a_contains_slice_subtypes():
    layer_a = _build_layer_a_definition()
    assert "composite" in layer_a.lower()
    assert "wildcard" in layer_a.lower()
    assert "leaf" in layer_a.lower() or "values:" in layer_a


def test_layer_a_contains_source_uri_examples():
    layer_a = _build_layer_a_definition()
    assert "duckdb://" in layer_a
    assert "bigquery://" in layer_a


def test_layer_a_contains_spec_precision_rule():
    layer_a = _build_layer_a_definition()
    assert "spec_draft_token" in layer_a
    assert "validate_spec" in layer_a


# ---------------------------------------------------------------------------
# SF-7: _build_layer_b_definition
# ---------------------------------------------------------------------------


def test_layer_b_always_lists_all_names_small_catalog():
    sc = _make_spec_cache(
        metrics={"revenue": MagicMock(description="Revenue", source="duckdb://db/t")},
        slices={"by_country": MagicMock(description="Country", is_composite=False, is_wildcard=False)},
        segments={"tier": MagicMock(description="Tier", source="duckdb://db/c")},
    )
    layer_b = _build_layer_b_definition(sc)
    assert "revenue" in layer_b
    assert "by_country" in layer_b
    assert "tier" in layer_b


def test_layer_b_always_lists_all_names_large_catalog():
    metrics = {f"metric_{i}": MagicMock(description=f"m{i}", source="s") for i in range(40)}
    sc = _make_spec_cache(metrics=metrics, slices={}, segments={})
    layer_b = _build_layer_b_definition(sc)
    for i in range(40):
        assert f"metric_{i}" in layer_b


def test_layer_b_marks_slice_subtypes():
    composite_slice = MagicMock()
    composite_slice.is_composite = True
    composite_slice.is_wildcard = False
    composite_slice.description = "Composite"

    wildcard_slice = MagicMock()
    wildcard_slice.is_composite = False
    wildcard_slice.is_wildcard = True
    wildcard_slice.description = "Wildcard"

    leaf_slice = MagicMock()
    leaf_slice.is_composite = False
    leaf_slice.is_wildcard = False
    leaf_slice.description = "Leaf"

    sc = _make_spec_cache(slices={
        "by_composite": composite_slice,
        "by_wildcard": wildcard_slice,
        "by_leaf": leaf_slice,
    })
    layer_b = _build_layer_b_definition(sc)

    assert "(composite)" in layer_b
    assert "(wildcard)" in layer_b
    assert "(leaf)" in layer_b


def test_layer_b_shows_details_below_threshold():
    sc = _make_spec_cache(
        metrics={"revenue": MagicMock(description="Revenue", source="duckdb://db/t")},
    )
    layer_b = _build_layer_b_definition(sc)
    assert "duckdb://db/t" in layer_b
    assert "Revenue" in layer_b


def test_layer_b_omits_details_above_threshold():
    metrics = {f"m_{i}": MagicMock(description=f"desc{i}", source=f"src{i}") for i in range(_LARGE_CATALOG_THRESHOLD + 1)}
    sc = _make_spec_cache(metrics=metrics)
    layer_b = _build_layer_b_definition(sc)
    # Names are present but source URIs and descriptions are omitted
    assert "m_0" in layer_b
    assert "src0" not in layer_b


def test_layer_b_empty_catalog_shows_none():
    sc = _make_spec_cache()
    layer_b = _build_layer_b_definition(sc)
    assert "(none)" in layer_b


# ---------------------------------------------------------------------------
# SF-8: DefinitionBot instantiation
# ---------------------------------------------------------------------------


def test_definition_bot_instantiates():
    bot = _make_bot()
    assert bot is not None


def test_definition_bot_store_is_result_store():
    bot = _make_bot()
    assert isinstance(bot.store, ResultStore)


def test_definition_response_is_subtype_of_bot_response():
    assert issubclass(DefinitionResponse, BotResponse)


def test_provider_cache_config_definition_anthropic():
    cfg = _provider_cache_config_definition(
        "anthropic:claude-haiku-4-5-20251001", "t1"
    )
    assert cfg == {"anthropic_cache_instructions": "5m"}


def test_definition_bot_agent_has_five_tools():
    bot = _make_bot()
    tool_names = set(bot._toolset.tools.keys())
    expected = {
        "record_definition_intent",
        "list_tables",
        "describe_table",
        "draft_spec",
        "validate_spec",
    }
    assert expected.issubset(tool_names)


# ---------------------------------------------------------------------------
# Plan 28 / SF-4: constructor tools= composition
# ---------------------------------------------------------------------------


def def_custom_tool_sync() -> str:
    return "sync-value"


async def def_custom_tool_async() -> str:
    return "async-value"


def _make_def_custom_tool_model(tool_name: str):
    """FunctionModel: calls `tool_name` once, then reports its return value in narrative."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart) and p.tool_name == tool_name:
                        output = DefinitionOutput(
                            status=Status.ok,
                            narrative=f"got:{p.content}",
                        )
                        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={}, tool_call_id="tc-1")]
        )

    return FunctionModel(fn)


def test_definition_constructor_tools_registered_alongside_defaults():
    bot = DefinitionBot(
        model=TestModel(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        tools=[def_custom_tool_sync],
    )
    expected_defaults = {
        "record_definition_intent", "list_tables", "describe_table",
        "draft_spec", "validate_spec",
    }
    assert "def_custom_tool_sync" in bot._toolset.tools
    assert expected_defaults.issubset(bot._toolset.tools.keys())


@pytest.mark.parametrize(
    "tool_fn,expected",
    [(def_custom_tool_sync, "sync-value"), (def_custom_tool_async, "async-value")],
    ids=["sync", "async"],
)
def test_definition_constructor_tools_invoked(tool_fn, expected):
    bot = DefinitionBot(
        model=_make_def_custom_tool_model(tool_fn.__name__),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        tools=[tool_fn],
    )
    response = asyncio.run(
        bot.ask("use the custom tool")
    )
    assert expected in response.narrative


def test_definition_constructor_tools_collision_with_default_raises_at_construction():
    def validate_spec() -> str:  # name collides with a default tool
        return "x"

    with pytest.raises(UserError, match="conflicts with existing tool"):
        DefinitionBot(
            model=TestModel(),
            spec_cache=_make_spec_cache(),
            connection_manager=MagicMock(),
            tools=[validate_spec],
        )


def test_definition_constructor_tools_two_entries_collide_with_each_other():
    def custom_dup() -> str:
        return "a"

    with pytest.raises(UserError, match="conflicts with existing tool"):
        DefinitionBot(
            model=TestModel(),
            spec_cache=_make_spec_cache(),
            connection_manager=MagicMock(),
            tools=[custom_dup, custom_dup],
        )


# ---------------------------------------------------------------------------
# SF-9: ask() / chat() / _assemble_payload
# ---------------------------------------------------------------------------


def test_ask_returns_definition_response():
    bot = _make_bot()
    response = asyncio.run(
        bot.ask("Define a metric for revenue")
    )
    assert isinstance(response, DefinitionResponse)


def test_ask_does_not_accumulate_history():
    bot = _make_bot()
    asyncio.run(bot.ask("Define a metric for ctr"))
    assert bot._message_history == []


def test_chat_accumulates_history():
    bot = _make_bot()
    asyncio.run(bot.chat("Define a metric for ctr"))
    assert len(bot._message_history) > 0

    prev_len = len(bot._message_history)
    asyncio.run(bot.chat("Also define one for revenue"))
    assert len(bot._message_history) > prev_len


def test_assemble_payload_refused_returns_empty():
    store = ResultStore()
    output = DefinitionOutput(status=Status.refused, narrative="Out of scope", reason="No such data")
    payload = DefinitionBot._assemble_payload(output, store)
    assert payload.spec_type is None
    assert payload.yaml_string is None
    assert payload.spec_draft_token is None


def test_assemble_payload_reads_yaml_from_store():
    store = ResultStore()
    token = store.store_text(
        _VALID_METRIC_YAML,
        "application/yaml",
        metadata={"spec_type": "metric", "spec_name": "revenue"},
    )
    output = DefinitionOutput(
        status=Status.ok,
        narrative="Done",
        spec_draft_token=token,
    )
    payload = DefinitionBot._assemble_payload(output, store)
    assert payload.yaml_string == _VALID_METRIC_YAML
    assert payload.spec_name == "revenue"


def test_assemble_payload_sets_metric_spec():
    store = ResultStore()
    token = store.store_text(
        _VALID_METRIC_YAML,
        "application/yaml",
        metadata={"spec_type": "metric", "spec_name": "revenue"},
    )
    output = DefinitionOutput(status=Status.ok, narrative="Done", spec_draft_token=token)
    payload = DefinitionBot._assemble_payload(output, store)

    assert isinstance(payload.metric_spec, MetricSpec)
    assert payload.metric_spec.name == "revenue"
    assert payload.slice_spec is None
    assert payload.segment_spec is None


# ---------------------------------------------------------------------------
# SF-10: FunctionModel integration tests
# ---------------------------------------------------------------------------

# Helpers for building FunctionModel callbacks


def _collect_tool_returns(messages) -> dict[str, dict]:
    """Map tool_name → parsed return value from messages."""
    call_names: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ModelResponse):
            for p in m.parts:
                if isinstance(p, ToolCallPart):
                    call_names[p.tool_call_id] = p.tool_name

    returns: dict[str, list[dict]] = {}
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, ToolReturnPart):
                    name = call_names.get(p.tool_call_id, "")
                    if name:
                        returns.setdefault(name, []).append(p.model_response_object())
    return {k: v[-1] for k, v in returns.items()}  # last call per tool


def _make_full_flow_model(yaml_string: str = _VALID_METRIC_YAML, spec_type: str = "metric"):
    """FunctionModel that simulates the full 4-step flow and returns status=ok."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        returns = _collect_tool_returns(messages)

        if "record_definition_intent" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="record_definition_intent",
                args=json.dumps({"spec_type": spec_type, "description": "Test spec"}),
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
                args=json.dumps({"spec_type": spec_type, "yaml_string": yaml_string}),
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
        if token:
            output = DefinitionOutput(
                status=Status.ok,
                narrative="Spec defined successfully.",
                spec_draft_token=token,
            )
        else:
            output = DefinitionOutput(
                status=Status.error,
                narrative="Validation failed.",
                reason=str(validate_data.get("errors")),
            )
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _make_correction_loop_model():
    """FunctionModel: first draft fails validation, LLM corrects and succeeds."""
    invalid_yaml = """\
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "amount"
  timestamp_col: transaction_date
"""
    # Simple state machine using a counter
    state = {"step": 0, "first_draft_id": None, "second_draft_id": None}

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        returns = _collect_tool_returns(messages)

        # Step 0: record intent
        if "record_definition_intent" not in returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="record_definition_intent",
                args=json.dumps({"spec_type": "metric", "description": "Revenue"}),
                tool_call_id="tc-1",
            )])

        # Step 1: first (bad) draft
        if state["first_draft_id"] is None:
            if "draft_spec" not in returns:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="draft_spec",
                    args=json.dumps({"spec_type": "metric", "yaml_string": invalid_yaml}),
                    tool_call_id="tc-2",
                )])
            state["first_draft_id"] = returns["draft_spec"].get("draft_id", "")

        # Step 2: validate first (bad) draft
        if state["step"] == 0:
            state["step"] = 1
            return ModelResponse(parts=[ToolCallPart(
                tool_name="validate_spec",
                args=json.dumps({"draft_id": state["first_draft_id"]}),
                tool_call_id="tc-3",
            )])

        # Step 3: first validate returned errors — re-draft
        if state["step"] == 1:
            state["step"] = 2
            return ModelResponse(parts=[ToolCallPart(
                tool_name="draft_spec",
                args=json.dumps({"spec_type": "metric", "yaml_string": _VALID_METRIC_YAML}),
                tool_call_id="tc-4",
            )])

        # Step 4: get second draft id from most recent draft_spec return
        if state["step"] == 2:
            # Find second draft's draft_id by looking at all draft_spec returns
            all_draft_returns = [
                p.model_response_object()
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
                if isinstance(p, ToolReturnPart)
                and any(
                    rp.tool_name == "draft_spec"
                    for rm in messages
                    if isinstance(rm, ModelResponse)
                    for rp in rm.parts
                    if isinstance(rp, ToolCallPart) and rp.tool_call_id == p.tool_call_id
                )
            ]
            if len(all_draft_returns) >= 2:
                state["second_draft_id"] = all_draft_returns[-1].get("draft_id", "")
                state["step"] = 3
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="validate_spec",
                    args=json.dumps({"draft_id": state["second_draft_id"]}),
                    tool_call_id="tc-5",
                )])

        # Step 5: second validate returned token
        all_validate_returns = [
            p.model_response_object()
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
            and any(
                rp.tool_name == "validate_spec"
                for rm in messages
                if isinstance(rm, ModelResponse)
                for rp in rm.parts
                if isinstance(rp, ToolCallPart) and rp.tool_call_id == p.tool_call_id
            )
        ]
        last_validate = all_validate_returns[-1] if all_validate_returns else {}
        token = last_validate.get("spec_draft_token")
        output = DefinitionOutput(
            status=Status.ok if token else Status.error,
            narrative="Done.",
            spec_draft_token=token,
        )
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _make_name_lock_model():
    """FunctionModel: is_update=True, draft tries to rename to 'orders' (which exists),
    validate_spec returns name-lock error. LLM corrects to original name 'revenue'."""
    existing_yaml = _VALID_METRIC_YAML  # name=revenue
    wrong_name_yaml = _VALID_METRIC_YAML.replace("name: revenue", "name: orders")

    call_sequence: list[str] = []

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        returns = _collect_tool_returns(messages)

        if "record_definition_intent" not in returns:
            call_sequence.append("record")
            return ModelResponse(parts=[ToolCallPart(
                tool_name="record_definition_intent",
                args=json.dumps({
                    "spec_type": "metric",
                    "description": "Update revenue metric",
                    "existing_yaml": existing_yaml,
                }),
                tool_call_id="tc-1",
            )])

        if "draft_spec" not in returns:
            call_sequence.append("draft1")
            return ModelResponse(parts=[ToolCallPart(
                tool_name="draft_spec",
                args=json.dumps({"spec_type": "metric", "yaml_string": wrong_name_yaml}),
                tool_call_id="tc-2",
            )])

        # Count draft_spec calls
        draft_calls = [
            p for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
            and any(
                rp.tool_name == "draft_spec"
                for rm in messages
                if isinstance(rm, ModelResponse)
                for rp in rm.parts
                if isinstance(rp, ToolCallPart) and rp.tool_call_id == p.tool_call_id
            )
        ]

        validate_returns = [
            p.model_response_object()
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
            and any(
                rp.tool_name == "validate_spec"
                for rm in messages
                if isinstance(rm, ModelResponse)
                for rp in rm.parts
                if isinstance(rp, ToolCallPart) and rp.tool_call_id == p.tool_call_id
            )
        ]

        if not validate_returns:
            # First validate: should get name-lock error
            draft_id = returns["draft_spec"].get("draft_id", "")
            call_sequence.append("validate1")
            return ModelResponse(parts=[ToolCallPart(
                tool_name="validate_spec",
                args=json.dumps({"draft_id": draft_id}),
                tool_call_id="tc-3",
            )])

        last_validate = validate_returns[-1]
        if last_validate.get("spec_draft_token"):
            # Success
            token = last_validate["spec_draft_token"]
            output = DefinitionOutput(
                status=Status.ok,
                narrative="Revenue metric updated.",
                spec_draft_token=token,
            )
            return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

        # Name-lock error — re-draft with correct name
        if len(draft_calls) == 1:
            call_sequence.append("draft2")
            return ModelResponse(parts=[ToolCallPart(
                tool_name="draft_spec",
                args=json.dumps({"spec_type": "metric", "yaml_string": _VALID_METRIC_YAML}),
                tool_call_id="tc-4",
            )])

        if len(draft_calls) >= 2:
            # Get the latest draft_id
            last_draft_return = draft_calls[-1].model_response_object()
            second_draft_id = last_draft_return.get("draft_id", "")
            call_sequence.append("validate2")
            return ModelResponse(parts=[ToolCallPart(
                tool_name="validate_spec",
                args=json.dumps({"draft_id": second_draft_id}),
                tool_call_id="tc-5",
            )])

        output = DefinitionOutput(status=Status.error, narrative="Unexpected state.")
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn), call_sequence


# ── Integration test: full flow ──


def test_full_flow_returns_status_ok():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis_table = MagicMock()
    mock_ibis_table.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_ibis_table.columns = ["amount", "transaction_date"]
    mock_connector.get_table.return_value = mock_ibis_table
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Define a revenue metric on the transactions table")
    )

    assert response.status == Status.ok
    assert response.payload.yaml_string is not None


def test_full_flow_validate_spec_trace_result_id_and_duration_populated():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis_table = MagicMock()
    mock_ibis_table.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_ibis_table.columns = ["amount", "transaction_date"]
    mock_connector.get_table.return_value = mock_ibis_table
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Define a revenue metric on the transactions table")
    )

    validate_call = next(
        tc for tc in response.trace.tool_calls if tc.name == "validate_spec"
    )
    assert validate_call.result_id == response.payload.spec_draft_token
    assert validate_call.duration_ms is not None


def test_full_flow_payload_metric_spec_populated():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis = MagicMock()
    mock_ibis.columns = ["amount", "transaction_date"]
    mock_ibis.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_connector.get_table.return_value = mock_ibis
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Define a revenue metric")
    )

    assert isinstance(response.payload.metric_spec, MetricSpec)
    assert response.payload.metric_spec.name == "revenue"


def test_full_flow_get_result_returns_text_entry():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis = MagicMock()
    mock_ibis.columns = ["amount", "transaction_date"]
    mock_ibis.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_connector.get_table.return_value = mock_ibis
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Define a revenue metric")
    )

    assert response.payload.spec_draft_token is not None
    entry = bot.get_result(response.payload.spec_draft_token)
    assert isinstance(entry, TextEntry)


def test_ask_does_not_accumulate_history_on_full_flow():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis = MagicMock()
    mock_ibis.columns = ["amount", "transaction_date"]
    mock_ibis.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_connector.get_table.return_value = mock_ibis
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    asyncio.run(bot.ask("First ask"))
    assert bot._message_history == []
    asyncio.run(bot.ask("Second ask"))
    assert bot._message_history == []


def test_trace_contains_all_five_tool_names():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis = MagicMock()
    mock_ibis.columns = ["amount", "transaction_date"]
    mock_ibis.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_connector.get_table.return_value = mock_ibis
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Define a revenue metric")
    )

    tool_names = {tc.name for tc in response.trace.tool_calls}
    expected = {
        "record_definition_intent",
        "list_tables",
        "describe_table",
        "draft_spec",
        "validate_spec",
    }
    assert expected == tool_names


# ── Integration test: correction loop ──


def test_correction_loop_final_status_ok():
    """Validate that draft→validate→fix cycle works end-to-end."""
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis = MagicMock()
    mock_ibis.columns = ["amount", "transaction_date"]
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_correction_loop_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Define a revenue metric")
    )

    assert response.status == Status.ok
    assert response.payload.spec_draft_token is not None


# ── Integration test: is_update rename conflict ──


def test_is_update_rename_conflict_name_lock_fires():
    """is_update=True with renamed spec fires name-lock error, then corrects."""
    sc = _make_spec_cache(metrics={"orders": MagicMock()})
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_cm.get_connection.return_value = mock_connector

    model, call_sequence = _make_name_lock_model()
    bot = DefinitionBot(
        model=model,
        spec_cache=sc,
        connection_manager=mock_cm,
    )

    response = asyncio.run(
        bot.ask("Update the revenue metric")
    )

    # Should ultimately succeed after correction
    assert response.status == Status.ok
    assert response.payload.spec_name == "revenue"
    # Verify the name-lock validate happened before the correction
    assert "validate1" in call_sequence
    assert "draft2" in call_sequence


# ── chat() accumulation ──


def test_chat_accumulates_history_on_full_flow():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["transactions"]
    mock_ibis = MagicMock()
    mock_ibis.columns = ["amount", "transaction_date"]
    mock_ibis.schema.return_value = MagicMock(names=["amount", "transaction_date"], types=["float64", "date"])
    mock_connector.get_table.return_value = mock_ibis
    mock_cm.get_connection.return_value = mock_connector

    bot = DefinitionBot(
        model=_make_full_flow_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=mock_cm,
    )

    asyncio.run(bot.chat("First message"))
    assert len(bot._message_history) > 0

    history_len_after_first = len(bot._message_history)
    asyncio.run(bot.chat("Second message"))
    assert len(bot._message_history) > history_len_after_first


# ---------------------------------------------------------------------------
# Plan 28 / SF-6: per-call extra_tools= on chat() / ask()
# ---------------------------------------------------------------------------


def _def_visibility_recording_model(recorder: list) -> FunctionModel:
    """FunctionModel that records visible tool names on each call, then ends the turn."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        recorder.append(sorted(t.name for t in info.function_tools))
        output = DefinitionOutput(status=Status.ok, narrative="ok")
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


@pytest.mark.parametrize(
    "tool_fn,expected",
    [(def_custom_tool_sync, "sync-value"), (def_custom_tool_async, "async-value")],
    ids=["sync", "async"],
)
def test_definition_ask_extra_tools_invoked(tool_fn, expected):
    bot = _make_bot(model=_make_def_custom_tool_model(tool_fn.__name__))
    response = asyncio.run(
        bot.ask("use the extra tool", extra_tools=[tool_fn])
    )
    assert expected in response.narrative


def test_definition_chat_extra_tools_ephemeral_not_visible_next_turn():
    seen: list = []
    bot = _make_bot(model=_def_visibility_recording_model(seen))
    asyncio.run(
        bot.chat("first", extra_tools=[def_custom_tool_sync])
    )
    asyncio.run(bot.chat("second"))
    assert "def_custom_tool_sync" in seen[0]
    assert "def_custom_tool_sync" not in seen[1]


def test_definition_ask_extra_tools_none_omits_toolsets_kwarg():
    bot = _make_bot()
    with patch.object(bot._agent, "run", wraps=bot._agent.run) as mock_run:
        asyncio.run(bot.ask("hello"))
    _, kwargs = mock_run.call_args
    assert "toolsets" not in kwargs


def test_definition_extra_tools_collision_surfaces_as_error_status():
    def validate_spec() -> str:  # collides with the default validate_spec tool
        return "x"

    bot = _make_bot()
    response = asyncio.run(
        bot.ask("hello", extra_tools=[validate_spec])
    )
    assert response.status == Status.error
    assert "conflicts with existing tool" in (response.reason or "")
