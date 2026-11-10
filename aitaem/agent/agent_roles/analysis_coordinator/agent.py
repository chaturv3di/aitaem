"""Analysis coordinator: finds the specs, runs the compute and stats chain, and answers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model

from aitaem.agent.agent_roles import launch, load_instructions
from aitaem.agent.agent_roles.analysis_executor.agent import (
    build_analysis_executor,
    make_run_analysis_executors,
)
from aitaem.agent.agent_roles.documents import Intake, check_handoff
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput, artifact_tool
from aitaem.agent.tools.aitaem import MetricComputeParams, compute_metrics
from aitaem.agent.tools.artifacts import describe_artifact, read_artifact
from aitaem.agent.tools.stats import stats_toolset

ROLE = "analysis_coordinator"


def build_analysis_coordinator(
    model: str | Model, executor_model: str | Model
) -> Agent[SessionDeps, str]:
    run_analysis_executors = make_run_analysis_executors(build_analysis_executor(executor_model))
    agent: Agent[SessionDeps, str] = Agent(
        model,
        name=ROLE,
        deps_type=SessionDeps,
        output_type=str,
        instructions=load_instructions(__package__ or __name__),
        toolsets=[
            FunctionToolset([compute_metrics]),
            stats_toolset,
            FunctionToolset([describe_artifact, read_artifact, run_analysis_executors]),
        ],
        capabilities=[Caching(retention="1h", messages=True)],
    )
    agent.output_validator(check_handoff)
    return agent


def make_run_analysis(coordinator: Agent[SessionDeps, str]) -> Callable[..., Any]:
    """Return the tool that launches `coordinator`."""

    @artifact_tool
    async def run_analysis(
        ctx: RunContext[SessionDeps], intake: Intake[MetricComputeParams]
    ) -> ToolOutput:
        """Compute metrics and run statistics to answer a question; returns the coordinator's handoff.

        Args:
            intake: The question, the metrics and breakdowns it needs, and earlier result IDs.
        """
        return await launch(ctx, coordinator, intake, tool="run_analysis", role=ROLE)

    return run_analysis
