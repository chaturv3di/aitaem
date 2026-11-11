"""Spec design evals for AitaemAgent: validate → commit, and the column check that blocks a draft.

Scripted (see the framing note in _fixtures.py). Each case is one user message: the main session
starts spec design, and the spec coordinator validates, commits when the draft passes, and hands
off.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem.agent import AitaemAgent, Response

from ._fixtures import SOURCE, call, document, make_agent, reply, returns, role_model

AOV = f"""\
metric:
  name: avg_order_value
  description: Average amount per transaction
  source: {SOURCE}
  numerator: AVG(amount)
  timestamp_col: transaction_date
"""


@dataclass
class SpecInput:
    request: str
    model: FunctionModel


@dataclass
class SpecOutput:
    response: Response
    agent: AitaemAgent


async def spec_task(inputs: SpecInput) -> SpecOutput:
    agent = make_agent(inputs.model, spec_dir=Path(tempfile.mkdtemp()) / "specs")
    response = await agent.chat(inputs.request)
    return SpecOutput(response=response, agent=agent)


Ctx = EvaluatorContext[SpecInput, SpecOutput, None]


def _tools(ctx: Ctx) -> list[str]:
    return [tc.name for tc in ctx.output.agent.trace(ctx.output.response.trace_id).tool_calls]


@dataclass
class CalledInOrder(Evaluator[SpecInput, SpecOutput, None]):
    first: str
    then: str

    def evaluate(self, ctx: Ctx) -> bool:
        tools = _tools(ctx)
        return self.first in tools and self.then in tools[tools.index(self.first) :]


@dataclass
class NotCalled(Evaluator[SpecInput, SpecOutput, None]):
    tool_name: str

    def evaluate(self, ctx: Ctx) -> bool:
        return self.tool_name not in _tools(ctx)


@dataclass
class SpecSaved(Evaluator[SpecInput, SpecOutput, None]):
    """The spec is in the catalog and saved to spec_dir, or neither when `saved` is False."""

    name: str
    saved: bool = True

    def evaluate(self, ctx: Ctx) -> bool:
        deps = ctx.output.agent.deps
        on_disk = (Path(deps.spec_dir) / "metrics" / f"{self.name}.yaml").exists()
        in_cache = self.name in deps.spec_cache.metrics
        return on_disk is in_cache is self.saved


def _flow(yaml_text: str) -> FunctionModel:
    """Validate the draft; commit it when it passes; hand off either way."""

    def after_validate(messages: list[ModelMessage]) -> ModelResponse:
        draft = returns(messages)[0]
        if draft.status == "ok":
            return call("commit_spec", draft_id=draft.artifact_id)
        return document(
            {
                "outcome": "inconclusive",
                "response": f"The draft has issues: {draft.facts['issues']}",
                "result_ids": [draft.artifact_id],
            }
        )

    def handoff(messages: list[ModelMessage]) -> ModelResponse:
        result = returns(messages)[0]
        return document(
            {"outcome": "complete", "response": result.summary, "result_ids": [result.artifact_id]}
        )

    intake = {"instructions": "Define average order value", "params": {"spec_type": "metric"}}
    return role_model(
        main=[
            call("start_spec_design", intake=intake),
            lambda m: reply(returns(m)[0].facts["response"], returns(m)[0].artifact_id),
        ],
        spec=[
            call("validate_spec", spec_type="metric", yaml_text=yaml_text),
            after_validate,
            handoff,
        ],
    )


dataset: Dataset[SpecInput, SpecOutput, None] = Dataset(
    name="aitaem_agent_spec_design_wiring",
    cases=[
        Case(
            name="validate_then_commit_wiring",
            inputs=SpecInput("Define average order value; it is agreed.", _flow(AOV)),
            evaluators=(
                CalledInOrder("validate_spec", "commit_spec"),
                SpecSaved("avg_order_value"),
            ),
        ),
        Case(
            name="column_check_blocks_commit_wiring",
            inputs=SpecInput(
                "Define average order value on the order_total column.",
                _flow(AOV.replace("AVG(amount)", "AVG(order_total)")),
            ),
            evaluators=(NotCalled("commit_spec"), SpecSaved("avg_order_value", saved=False)),
        ),
    ],
)


def test_spec_design_eval_dataset_passes() -> None:
    report = dataset.evaluate_sync(spec_task)
    assert len(report.cases) == 2, report.failures
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
