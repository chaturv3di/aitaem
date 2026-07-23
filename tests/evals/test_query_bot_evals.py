"""SF-6: reference pydantic-evals harness for QueryBot.

Framing — harness demonstration, not a behavioral eval:
CI correctly forbids live LLM calls, so every Case here is driven by a
hand-scripted FunctionModel that already knows which tool to call and in
what order — the assertions are near-tautological by construction. None of
the three cases below measure whether an LLM *would* select the right tool,
refuse appropriately, or produce a correct answer. They measure that
RunTrace, ResultStore, and BotResponse are consumable by
pydantic_evals.Evaluator s — i.e. that the eval substrate is wired correctly
end to end. That's a legitimate, valuable thing to ship on its own (point
this same harness at a live model outside CI and the wiring already works),
but it is substrate validation, not agent-quality evaluation. Don't mistake
"the harness runs and passes" for "the agent was evaluated."
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem.agent.query_bot import QueryBot, QueryResponse
from aitaem.agent.query_types import QueryOutput
from aitaem.agent.store import TabularEntry
from aitaem.agent.trace import Status

from ._fixtures import GROUND_TRUTH_REVENUE_TABLE, make_query_bot_fixture


# ---------------------------------------------------------------------------
# Task input/output
# ---------------------------------------------------------------------------


@dataclass
class QueryEvalInput:
    question: str
    model: FunctionModel  # drives the fake LLM's tool-calling behavior for this case


@dataclass
class QueryEvalOutput:
    response: QueryResponse
    bot: QueryBot          # for get_result() lookups in evaluators


async def query_bot_task(inputs: QueryEvalInput) -> QueryEvalOutput:
    bot = make_query_bot_fixture(inputs.model)
    response = await bot.ask(inputs.question)
    return QueryEvalOutput(response=response, bot=bot)


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


@dataclass
class CalledTool(Evaluator[QueryEvalInput, QueryEvalOutput, None]):
    """Asserts a named tool appears in output.response.trace.tool_calls."""

    tool_name: str

    def evaluate(self, ctx: EvaluatorContext[QueryEvalInput, QueryEvalOutput, None]) -> bool:
        return any(tc.name == self.tool_name for tc in ctx.output.response.trace.tool_calls)


@dataclass
class StatusIs(Evaluator[QueryEvalInput, QueryEvalOutput, None]):
    """Asserts output.response.status == the configured Status."""

    expected: Status

    def evaluate(self, ctx: EvaluatorContext[QueryEvalInput, QueryEvalOutput, None]) -> bool:
        return ctx.output.response.status == self.expected


@dataclass
class ResultMatchesGroundTruth(Evaluator[QueryEvalInput, QueryEvalOutput, None]):
    """Finds the compute_metrics ToolCall, asserts its result_id is non-None,
    calls bot.get_result(result_id) (returns the ResultEntry union — TabularEntry
    | TextEntry, per aitaem/agent/store.py), narrows with isinstance(entry,
    TabularEntry) (compute_metrics always writes a TabularEntry; this satisfies
    both a real runtime guard and mypy's union-attribute check), then asserts
    entry.arrow.equals(GROUND_TRUTH_REVENUE_TABLE). This case only became
    writable once SF-1 populated ToolCall.result_id.
    """

    def evaluate(self, ctx: EvaluatorContext[QueryEvalInput, QueryEvalOutput, None]) -> bool:
        compute_call = next(
            (tc for tc in ctx.output.response.trace.tool_calls if tc.name == "compute_metrics"),
            None,
        )
        if compute_call is None or compute_call.result_id is None:
            return False
        entry = ctx.output.bot.get_result(compute_call.result_id)
        if not isinstance(entry, TabularEntry) or entry.arrow is None:
            return False
        return entry.arrow.equals(GROUND_TRUTH_REVENUE_TABLE)


# ---------------------------------------------------------------------------
# Scripted FunctionModels
# ---------------------------------------------------------------------------


def _tool_selection_model(metric: str = "revenue") -> FunctionModel:
    """Drives record_intent -> resolve_intent -> compute_metrics -> QueryOutput."""

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
                for return_part in m.parts:
                    if isinstance(return_part, ToolReturnPart):
                        name = call_names.get(return_part.tool_call_id, "")
                        if name:
                            returns[name] = return_part.model_response_object()

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
            token = returns["resolve_intent"]["exact_match"]["spec_token"]
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


def _refusal_model() -> FunctionModel:
    """Immediately refuses without calling any tool. Mirrors _make_refused_model()
    in tests/test_agent/test_query_bot.py — kept in sync in spirit, not by import.
    """

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
# Dataset — three wiring-demonstration cases, one per eval-dimension shape
# named in Architecture §5 (tool-selection, refusal, deterministic
# correctness). Scripted, not measured; see the framing note at the top of
# this file.
# ---------------------------------------------------------------------------


dataset: Dataset[QueryEvalInput, QueryEvalOutput, None] = Dataset(
    name="query_bot_wiring",
    cases=[
        Case(
            name="tool_selection_wiring",
            inputs=QueryEvalInput(question="What was total revenue?", model=_tool_selection_model()),
            evaluators=(CalledTool(tool_name="compute_metrics"),),
        ),
        Case(
            name="refusal_wiring",
            inputs=QueryEvalInput(question="What was sales velocity?", model=_refusal_model()),
            evaluators=(StatusIs(expected=Status.refused),),
        ),
        Case(
            name="deterministic_correctness_wiring",
            inputs=QueryEvalInput(question="What was total revenue?", model=_tool_selection_model()),
            evaluators=(ResultMatchesGroundTruth(),),
        ),
    ],
)


def test_query_bot_eval_dataset_passes():
    """Builds the Dataset from the three cases above, runs
    dataset.evaluate_sync(query_bot_task), and asserts every assertion in the
    report evaluates True. Runs entirely against FunctionModel — no live LLM
    call, no API key needed, consistent with every other test in
    tests/test_agent/. This asserts the eval SUBSTRATE is consumable
    end-to-end (trace/store/evaluator wiring), not that the AGENT behaves
    correctly — every FunctionModel here is scripted to produce the outcome
    being asserted. See the framing note at the top of this file for why
    that distinction matters for a directory meant to be copied.
    """
    report = dataset.evaluate_sync(query_bot_task)
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
