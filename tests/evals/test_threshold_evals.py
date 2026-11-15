"""Threshold governance evals: percentile statistics come only from catalog metrics, and date
grounding stays bounds-only.

The scripted dataset checks the structural guarantees through the spec coordinator. The live
dataset, skipped without ANTHROPIC_API_KEY, checks that a real model grounds thresholds with these
tools instead of inventing them; a failure there points at the spec coordinator's instructions.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem.agent import AitaemAgent, Response, Status

from ._fixtures import SOURCE, call, document, make_agent, reply, returns, role_model

PERCENTILE_FIELDS = {"mean", "std", "p25", "median", "p75"}


@dataclass
class ThresholdInput:
    request: str
    model: FunctionModel | str


@dataclass
class ThresholdOutput:
    response: Response
    agent: AitaemAgent


async def threshold_task(inputs: ThresholdInput) -> ThresholdOutput:
    agent = make_agent(inputs.model, spec_dir=Path(tempfile.mkdtemp()) / "specs")
    response = await agent.chat(inputs.request)
    return ThresholdOutput(response=response, agent=agent)


Ctx = EvaluatorContext[ThresholdInput, ThresholdOutput, None]


def _calls(ctx: Ctx, tool: str) -> list[str]:
    """The artifact IDs of every call to `tool` in the turn."""
    trace = ctx.output.agent.trace(ctx.output.response.trace_id)
    return [tc.artifact_id for tc in trace.tool_calls if tc.name == tool and tc.artifact_id]


@dataclass
class StatusOf(Evaluator[ThresholdInput, ThresholdOutput, None]):
    """Every call to `tool` produced an artifact with this status."""

    tool: str
    expected: Status

    def evaluate(self, ctx: Ctx) -> bool:
        ids = _calls(ctx, self.tool)
        store = ctx.output.agent.store
        return bool(ids) and all(store.get(i).status == self.expected for i in ids)


@dataclass
class BoundsOnly(Evaluator[ThresholdInput, ThresholdOutput, None]):
    """Every date_range result holds bounds and counts, and no percentile statistic."""

    def evaluate(self, ctx: Ctx) -> bool:
        ids = _calls(ctx, "date_range")
        store = ctx.output.agent.store
        return bool(ids) and all(not PERCENTILE_FIELDS & set(store.read(i)) for i in ids)


@dataclass
class CalledOneOf(Evaluator[ThresholdInput, ThresholdOutput, None]):
    tools: tuple[str, ...]

    def evaluate(self, ctx: Ctx) -> bool:
        return any(_calls(ctx, t) for t in self.tools)


@dataclass
class NotCalled(Evaluator[ThresholdInput, ThresholdOutput, None]):
    tool: str

    def evaluate(self, ctx: Ctx) -> bool:
        return not _calls(ctx, self.tool)


def _spec_steps(*steps: object, spec_executor: list[object] | None = None) -> FunctionModel:
    intake = {"instructions": "Define a high-value slice", "params": {"spec_type": "slice"}}
    return role_model(
        main=[call("start_spec_design", intake=intake)],
        spec=[*steps, lambda m: reply("Here is what the data shows.", returns(m)[0].artifact_id)],  # type: ignore[list-item]
        spec_executor=spec_executor or [],  # type: ignore[arg-type]
    )


def _date_research() -> FunctionModel:
    """Date grounding happens in table research: the spec executor holds date_range."""
    research = [{"instructions": "Find the date bounds", "params": {"sources": [SOURCE]}}]
    return _spec_steps(
        call("run_spec_executors", intakes=research),
        spec_executor=[
            call("date_range", source=SOURCE, date_column="transaction_date"),
            lambda m: document(
                {
                    "outcome": "complete",
                    "response": "transaction_date spans 2024.",
                    "result_ids": [returns(m)[0].artifact_id],
                }
            ),
        ],
    )


scripted: Dataset[ThresholdInput, ThresholdOutput, None] = Dataset(
    name="threshold_governance_wiring",
    cases=[
        Case(
            name="percentiles_need_a_catalog_metric",
            inputs=ThresholdInput(
                "Use the 75th percentile of amount in the raw table.",
                _spec_steps(call("column_distribution", metric_name=SOURCE, column="amount")),
            ),
            evaluators=(StatusOf("column_distribution", Status.error),),
        ),
        Case(
            name="percentiles_from_a_catalog_metric",
            inputs=ThresholdInput(
                "Use the 75th percentile of amount on the revenue metric.",
                _spec_steps(call("column_distribution", metric_name="revenue", column="amount")),
            ),
            evaluators=(StatusOf("column_distribution", Status.ok),),
        ),
        Case(
            name="date_range_is_bounds_only",
            inputs=ThresholdInput("Transactions since March 2024.", _date_research()),
            evaluators=(BoundsOnly(),),
        ),
    ],
)


def test_threshold_governance_wiring() -> None:
    report = scripted.evaluate_sync(threshold_task)
    assert len(report.cases) == 3, report.failures
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"


# ── live model ──────────────────────────────────────────────────────────────

LIVE_MODEL = "anthropic:claude-haiku-5-5"

live: Dataset[ThresholdInput, ThresholdOutput, None] = Dataset(
    name="threshold_governance_live",
    cases=[
        Case(
            name="threshold_against_existing_metric",
            inputs=ThresholdInput(
                "Define a slice of high-value vs. low-value transactions on the revenue metric, "
                "where high value is the 75th percentile of the amount column.",
                LIVE_MODEL,
            ),
            evaluators=(CalledOneOf(("column_distribution",)), NotCalled("date_range")),
        ),
        Case(
            name="pure_date_grounding",
            inputs=ThresholdInput(
                "Define a cohort slice for transactions since March 2024, using the "
                "transaction_date column of the revenue metric's source table.",
                LIVE_MODEL,
            ),
            evaluators=(CalledOneOf(("date_range",)), NotCalled("column_distribution")),
        ),
    ],
)


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_threshold_governance_live() -> None:
    from pydantic_ai import models

    models.ALLOW_MODEL_REQUESTS = True
    report = live.evaluate_sync(threshold_task)
    assert len(report.cases) == 2, report.failures
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
