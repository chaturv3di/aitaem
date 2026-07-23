"""SF-7: reference pydantic-evals harness for DefinitionBot.

Framing: same caveat as test_query_bot_evals.py, in full. Both cases below
use a scripted FunctionModel that already knows which tool calls to make.
They prove the harness can read validate_spec gate outcomes and
spec_draft_token/result_id off RunTrace/BotResponse — not that an LLM drafts
specs correctly or recognizes ambiguous schemas on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem.agent.definition_bot import DefinitionBot, DefinitionResponse
from aitaem.agent.definition_types import DefinitionOutput
from aitaem.agent.trace import Status

from ._fixtures import make_definition_bot_fixture

_VALID_METRIC_YAML = """\
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: transaction_date
"""

_AMBIGUOUS_METRIC_YAML = """\
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(unknown_column)"
  timestamp_col: transaction_date
"""


# ---------------------------------------------------------------------------
# Task input/output
# ---------------------------------------------------------------------------


@dataclass
class DefinitionEvalInput:
    description: str
    model: FunctionModel


@dataclass
class DefinitionEvalOutput:
    response: DefinitionResponse
    bot: DefinitionBot


async def definition_bot_task(inputs: DefinitionEvalInput) -> DefinitionEvalOutput:
    bot = make_definition_bot_fixture(inputs.model)
    response = await bot.ask(inputs.description)
    return DefinitionEvalOutput(response=response, bot=bot)


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


@dataclass
class StatusIs(Evaluator[DefinitionEvalInput, DefinitionEvalOutput, None]):
    """Asserts output.response.status == the configured Status."""

    expected: Status

    def evaluate(self, ctx: EvaluatorContext[DefinitionEvalInput, DefinitionEvalOutput, None]) -> bool:
        return ctx.output.response.status == self.expected


@dataclass
class StatusIsNot(Evaluator[DefinitionEvalInput, DefinitionEvalOutput, None]):
    """Asserts output.response.status != the configured Status."""

    not_expected: Status

    def evaluate(self, ctx: EvaluatorContext[DefinitionEvalInput, DefinitionEvalOutput, None]) -> bool:
        return ctx.output.response.status != self.not_expected


@dataclass
class MintedSpecDraftToken(Evaluator[DefinitionEvalInput, DefinitionEvalOutput, None]):
    """Asserts payload.spec_draft_token is not None, and that the
    validate_spec trace entry has a matching non-None result_id."""

    def evaluate(self, ctx: EvaluatorContext[DefinitionEvalInput, DefinitionEvalOutput, None]) -> bool:
        payload = ctx.output.response.payload
        if payload is None or payload.spec_draft_token is None:
            return False
        token = payload.spec_draft_token
        validate_call = next(
            (tc for tc in ctx.output.response.trace.tool_calls if tc.name == "validate_spec"),
            None,
        )
        return validate_call is not None and validate_call.result_id == token


@dataclass
class SpecDraftTokenIsNone(Evaluator[DefinitionEvalInput, DefinitionEvalOutput, None]):
    """Asserts payload.spec_draft_token is None (the failed-gate case)."""

    def evaluate(self, ctx: EvaluatorContext[DefinitionEvalInput, DefinitionEvalOutput, None]) -> bool:
        payload = ctx.output.response.payload
        return payload is None or payload.spec_draft_token is None


# ---------------------------------------------------------------------------
# Scripted FunctionModels
# ---------------------------------------------------------------------------


def _collect_tool_returns(messages: list) -> dict[str, dict]:
    call_names: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ModelResponse):
            for p in m.parts:
                if isinstance(p, ToolCallPart):
                    call_names[p.tool_call_id] = p.tool_name

    returns: dict[str, dict] = {}
    for m in messages:
        if isinstance(m, ModelRequest):
            for return_part in m.parts:
                if isinstance(return_part, ToolReturnPart):
                    name = call_names.get(return_part.tool_call_id, "")
                    if name:
                        returns[name] = return_part.model_response_object()
    return returns


def _full_flow_model(yaml_string: str) -> FunctionModel:
    """Drives record_definition_intent -> list_tables -> describe_table ->
    draft_spec -> validate_spec -> DefinitionOutput. Mirrors
    _make_full_flow_model() in tests/test_agent/test_definition_bot.py — kept
    in sync in spirit, not by import.
    """

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        returns = _collect_tool_returns(messages)

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
                args=json.dumps({"spec_type": "metric", "yaml_string": yaml_string}),
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
            reason=None if token else str(validate_data.get("column_errors")),
        )
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


dataset: Dataset[DefinitionEvalInput, DefinitionEvalOutput, None] = Dataset(
    name="definition_bot_wiring",
    cases=[
        Case(
            name="validate_spec_gate_wiring",
            inputs=DefinitionEvalInput(
                description="Define a revenue metric on the transactions table",
                model=_full_flow_model(_VALID_METRIC_YAML),
            ),
            evaluators=(StatusIs(expected=Status.ok), MintedSpecDraftToken()),
        ),
        Case(
            name="refusal_on_ambiguous_schema_wiring",
            inputs=DefinitionEvalInput(
                description="Define a revenue metric using an unknown column",
                model=_full_flow_model(_AMBIGUOUS_METRIC_YAML),
            ),
            evaluators=(StatusIsNot(not_expected=Status.ok), SpecDraftTokenIsNone()),
        ),
    ],
)


def test_definition_bot_eval_dataset_passes():
    """Same shape as test_query_bot_eval_dataset_passes() — asserts substrate
    wiring, not agent behavior. See the framing note at the top of this file.
    """
    report = dataset.evaluate_sync(definition_bot_task)
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
