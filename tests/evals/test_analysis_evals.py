"""Analysis evals for AitaemAgent: status, Metric Precision refusals, and result attachments.

Scripted (see the framing note in _fixtures.py): the cases check that a turn's RunTrace,
Response, and artifacts are consumable by pydantic_evals evaluators.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai.models.function import FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem.agent import AitaemAgent, Response, Status

from ._fixtures import call, document, ground_truth, make_agent, reply, returns, role_model


@dataclass
class AnalysisInput:
    question: str
    model: FunctionModel


@dataclass
class AnalysisOutput:
    response: Response
    agent: AitaemAgent


async def analysis_task(inputs: AnalysisInput) -> AnalysisOutput:
    agent = make_agent(inputs.model)
    response = await agent.chat(inputs.question)
    return AnalysisOutput(response=response, agent=agent)


Ctx = EvaluatorContext[AnalysisInput, AnalysisOutput, None]


@dataclass
class StatusIs(Evaluator[AnalysisInput, AnalysisOutput, None]):
    expected: Status

    def evaluate(self, ctx: Ctx) -> bool:
        return ctx.output.response.status == self.expected


@dataclass
class CalledTool(Evaluator[AnalysisInput, AnalysisOutput, None]):
    """A tool appears in the turn's trace, in any run (main session, coordinator, executor)."""

    tool_name: str

    def evaluate(self, ctx: Ctx) -> bool:
        trace = ctx.output.agent.trace(ctx.output.response.trace_id)
        return any(tc.name == self.tool_name for tc in trace.tool_calls)


@dataclass
class AttachmentMatchesGroundTruth(Evaluator[AnalysisInput, AnalysisOutput, None]):
    """The attached compute result holds the same rows as a direct MetricCompute call."""

    compute: dict[str, Any]

    def evaluate(self, ctx: Ctx) -> bool:
        agent = ctx.output.agent
        results = [a for a in ctx.output.response.attachments if a.type_tag == "metric_result"]
        if len(results) != 1:
            return False
        got = agent.store.read(results[0].artifact_id).to_pyarrow()
        expected = ground_truth(agent, **self.compute)
        order = [("slice_value", "ascending")]
        return bool(got.sort_by(order).equals(expected.sort_by(order)))


COMPUTE = {"metrics": ["revenue"], "slices": ["country"]}


def _computing_model() -> FunctionModel:
    """Main session launches the analysis; the coordinator computes revenue by country."""
    intake = {"instructions": "Revenue by country", "params": {"metrics": ["revenue"]}}
    return role_model(
        main=[
            call("run_analysis", intake=intake),
            lambda m: reply("Revenue by country is ready.", *returns(m)[0].facts["result_ids"]),
        ],
        analysis=[
            call("compute_metrics", params=COMPUTE),
            lambda m: document(
                {
                    "outcome": "complete",
                    "response": "Revenue by country.",
                    "result_ids": [returns(m)[0].artifact_id],
                }
            ),
        ],
    )


def _refusal_model() -> FunctionModel:
    """No spec measures churn, so the main session refuses instead of approximating."""
    return role_model(main=[reply("The catalog has no churn metric.", status="refused")])


dataset: Dataset[AnalysisInput, AnalysisOutput, None] = Dataset(
    name="aitaem_agent_analysis_wiring",
    cases=[
        Case(
            name="tool_selection_wiring",
            inputs=AnalysisInput("What is revenue by country?", _computing_model()),
            evaluators=(
                CalledTool("run_analysis"),
                CalledTool("compute_metrics"),
                StatusIs(Status.ok),
            ),
        ),
        Case(
            name="metric_precision_refusal_wiring",
            inputs=AnalysisInput("What is churn by country?", _refusal_model()),
            evaluators=(StatusIs(Status.refused),),
        ),
        Case(
            name="result_attachment_wiring",
            inputs=AnalysisInput("What is revenue by country?", _computing_model()),
            evaluators=(AttachmentMatchesGroundTruth(COMPUTE),),
        ),
    ],
)


def test_analysis_eval_dataset_passes() -> None:
    report = dataset.evaluate_sync(analysis_task)
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
    assert len(report.cases) == 3
