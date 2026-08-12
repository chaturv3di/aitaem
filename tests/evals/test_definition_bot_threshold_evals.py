"""Plan 37, SF-10: live-model threshold-governance behavior evals for DefinitionBot.

Skipped without ANTHROPIC_API_KEY (real model, not a scripted FunctionModel —
mirrors tests/test_agent/test_definition_bot_smoke.py's skip pattern). Not part
of the default CI gate: the `evals` CI job (.github/workflows/ci.yml) doesn't
set this secret, so this file is collected and skipped there.

Framing: test_definition_bot_evals.py's scripted-FunctionModel cases prove the
RunTrace/DefinitionPayload wiring for the new grounding tools works — not
that a real LLM actually chooses to use them (SF-7's Layer A prompt rules)
instead of inventing a threshold or date. That's prompt compliance, and needs
a live model. A failure here means "check Layer A's grounding-tools wording,"
not necessarily a code bug.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — threshold-governance evals require a real LLM",
)

from pydantic_evals import Case, Dataset  # noqa: E402
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge  # noqa: E402

from aitaem.agent.definition_bot import DefinitionResponse  # noqa: E402
from aitaem.agent.trace import Status  # noqa: E402

from ._fixtures import make_definition_bot_grounding_fixture  # noqa: E402

_MODEL = "anthropic:claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Task input/output
# ---------------------------------------------------------------------------


@dataclass
class ThresholdEvalInput:
    description: str


@dataclass
class ThresholdEvalOutput:
    response: DefinitionResponse


async def threshold_task(inputs: ThresholdEvalInput) -> ThresholdEvalOutput:
    bot = make_definition_bot_grounding_fixture(_MODEL)
    response = await bot.ask(inputs.description)
    return ThresholdEvalOutput(response=response)


def _tool_names(output: ThresholdEvalOutput) -> set[str]:
    return {tc.name for tc in output.response.trace.tool_calls}


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


@dataclass
class CalledOneOf(Evaluator[ThresholdEvalInput, ThresholdEvalOutput, None]):
    """Asserts at least one of the given tool names appears in trace.tool_calls."""

    tool_names: tuple[str, ...]

    def evaluate(self, ctx: EvaluatorContext[ThresholdEvalInput, ThresholdEvalOutput, None]) -> bool:
        return bool(_tool_names(ctx.output) & set(self.tool_names))


@dataclass
class NotCalled(Evaluator[ThresholdEvalInput, ThresholdEvalOutput, None]):
    """Asserts the given tool name does not appear in trace.tool_calls."""

    tool_name: str

    def evaluate(self, ctx: EvaluatorContext[ThresholdEvalInput, ThresholdEvalOutput, None]) -> bool:
        return self.tool_name not in _tool_names(ctx.output)


@dataclass
class NoFabricatedToken(Evaluator[ThresholdEvalInput, ThresholdEvalOutput, None]):
    """For the no-matching-metric case: a spec_draft_token must not be minted
    from an invented literal. Either status is refused, or the governed tool
    (column_distribution/compute_metrics) was attempted and failed — both tools
    return result_id="" on failure, which assemble_trace's _extract_result_id
    surfaces as ToolCall.result_id=None (per the ToolResult protocol) — either
    way, nothing downstream used a fabricated number."""

    def evaluate(self, ctx: EvaluatorContext[ThresholdEvalInput, ThresholdEvalOutput, None]) -> bool:
        response = ctx.output.response
        if response.status == Status.refused:
            return True
        governed_calls = [
            tc for tc in response.trace.tool_calls
            if tc.name in ("column_distribution", "compute_metrics")
        ]
        return any(tc.result_id is None for tc in governed_calls)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


dataset: Dataset[ThresholdEvalInput, ThresholdEvalOutput, None] = Dataset(
    name="definition_bot_threshold_governance",
    cases=[
        Case(
            name="threshold_against_existing_metric",
            inputs=ThresholdEvalInput(
                description=(
                    "Define a slice for high-value vs. low-value transactions on "
                    "the revenue metric, where high value is the 75th percentile "
                    "of the amount column."
                ),
            ),
            evaluators=(
                CalledOneOf(tool_names=("column_distribution", "compute_metrics")),
                NotCalled(tool_name="date_range"),
            ),
        ),
        Case(
            name="no_matching_metric_refuses_not_fabricates",
            inputs=ThresholdEvalInput(
                description=(
                    "Define a slice for high-churn vs. low-churn customers, where "
                    "high churn is above the 90th percentile of customer churn rate."
                ),
            ),
            evaluators=(
                NoFabricatedToken(),
                NotCalled(tool_name="date_range"),
                LLMJudge(
                    rubric=(
                        "The narrative or reason names 'churn' (or 'customer churn "
                        "rate') as the missing concept, and recommends the user "
                        "define a metric for it before retrying."
                    ),
                    model=_MODEL,
                ),
            ),
        ),
        Case(
            name="pure_date_grounding",
            inputs=ThresholdEvalInput(
                description=(
                    "Define a cohort slice for transactions that occurred since "
                    "March 2024, using the transaction_date column on the "
                    "revenue metric's source table."
                ),
            ),
            evaluators=(
                CalledOneOf(tool_names=("date_range",)),
                NotCalled(tool_name="column_distribution"),
                NotCalled(tool_name="compute_metrics"),
            ),
        ),
    ],
)


def test_definition_bot_threshold_governance_evals():
    """Prompt-compliance check for SF-7's grounding rules."""
    report = dataset.evaluate_sync(threshold_task)
    for case in report.cases:
        for name, result in case.assertions.items():
            assert result.value, f"case {case.name!r}: assertion {name!r} failed"
