"""Fan-out correctness and artifact completeness.

Fan-out correctness: the compatibility verdicts the analysis executors hand back match the core
`MetricCompute.scan()` on the full catalog. Artifact completeness: every tool call of the turn, in
every run, names an artifact in the manifest. Scripted (see the framing note in _fixtures.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem import MetricCompute
from aitaem.agent import AitaemAgent, Response

from ._fixtures import call, document, make_agent, prompt, reply, returns, role_model

COMBOS = [("revenue", "country"), ("revenue", "device")]


@dataclass
class FanOutInput:
    question: str
    model: FunctionModel


@dataclass
class FanOutOutput:
    response: Response
    agent: AitaemAgent


async def fan_out_task(inputs: FanOutInput) -> FanOutOutput:
    agent = make_agent(inputs.model)
    response = await agent.chat(inputs.question)
    return FanOutOutput(response=response, agent=agent)


Ctx = EvaluatorContext[FanOutInput, FanOutOutput, None]


@dataclass
class VerdictsMatchScan(Evaluator[FanOutInput, FanOutOutput, None]):
    """Each executor's compatibility verdicts equal the full-catalog scan's."""

    def evaluate(self, ctx: Ctx) -> bool:
        agent = ctx.output.agent
        scan = MetricCompute(agent.deps.spec_cache, agent.deps.connection_manager).scan()
        truth = {(r.metric_name, r.spec_name): r.compatible for r in scan.results}
        refs = agent.store.refs("compatibility")
        verdicts = [v for r in refs for v in agent.store.read(r.id)["verdicts"]]
        return len(refs) == len(COMBOS) and all(
            truth[(v["metric"], v["spec"])] == v["compatible"] for v in verdicts
        )


@dataclass
class ArtifactsComplete(Evaluator[FanOutInput, FanOutOutput, None]):
    """Every tool call in the turn's trace names an artifact the manifest holds."""

    def evaluate(self, ctx: Ctx) -> bool:
        agent = ctx.output.agent
        trace = agent.trace(ctx.output.response.trace_id)
        return bool(trace.tool_calls) and all(
            tc.artifact_id is not None and tc.artifact_id in agent.store for tc in trace.tool_calls
        )


def _executor_step(messages: list[ModelMessage]) -> ModelResponse:
    """Check the intake's combo against the catalog, then hand off the verdict."""
    if not returns(messages):
        (combo,) = yaml.safe_load(prompt(messages))["params"]["combos"]
        return call("check_compatibility", metrics=[combo["metric"]], slices=combo["slices"])
    result = returns(messages)[0]
    return document(
        {"outcome": "complete", "response": result.summary, "result_ids": [result.artifact_id]}
    )


def _model() -> FunctionModel:
    intakes = [
        {"instructions": f"{m} by {s}", "params": {"combos": [{"metric": m, "slices": [s]}]}}
        for m, s in COMBOS
    ]
    intake = {
        "instructions": "Revenue by country and by device",
        "params": {"metrics": ["revenue"]},
    }
    return role_model(
        main=[
            call("run_analysis", intake=intake),
            lambda m: reply(returns(m)[0].facts["response"], *returns(m)[0].facts["result_ids"]),
        ],
        analysis=[
            call("run_analysis_executors", intakes=intakes),
            lambda m: document(
                {
                    "outcome": "needs-input",
                    "response": "Revenue fits country; the catalog has no device column.",
                    "result_ids": [returns(m)[0].artifact_id],
                    "questions": ["Drop the device breakdown?"],
                }
            ),
        ],
        analysis_executor=[_executor_step] * (2 * len(COMBOS)),
    )


dataset: Dataset[FanOutInput, FanOutOutput, None] = Dataset(
    name="fan_out_and_artifacts",
    cases=[
        Case(
            name="fan_out_correctness",
            inputs=FanOutInput("Revenue by country and device?", _model()),
            evaluators=(VerdictsMatchScan(), ArtifactsComplete()),
        )
    ],
)


def test_fan_out_and_artifact_evals() -> None:
    report = dataset.evaluate_sync(fan_out_task)
    assert len(report.cases) == 1, report.failures
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
