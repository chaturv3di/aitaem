"""Analysis executor: finds the catalog specs for metric, slice, and segment combos."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model

from aitaem.agent.agent_roles import load_instructions, run_executors
from aitaem.agent.agent_roles.documents import Intake, check_handoff
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput, artifact_tool
from aitaem.agent.tools.aitaem import check_compatibility, find_specs, read_spec

ROLE = "analysis_executor"


class SpecCombo(BaseModel):
    """A metric the plan needs, with the breakdowns it must support."""

    metric: str
    """The measure needed, in the coordinator's words."""
    slices: list[str] = []
    """Breakdowns the metric must support."""
    segment: str | None = None
    """Entity grouping the metric must support."""


class SpecSearchParams(BaseModel):
    combos: list[SpecCombo]


def build_analysis_executor(model: str | Model) -> Agent[SessionDeps, str]:
    agent: Agent[SessionDeps, str] = Agent(
        model,
        name=ROLE,
        deps_type=SessionDeps,
        output_type=str,
        instructions=load_instructions(__package__ or __name__),
        toolsets=[FunctionToolset([find_specs, read_spec, check_compatibility])],
        capabilities=[Caching(retention="1h", messages=False)],
    )
    agent.output_validator(check_handoff)
    return agent


def make_run_analysis_executors(executor: Agent[SessionDeps, str]) -> Callable[..., Any]:
    """Return the fan-out tool that runs `executor` once per intake."""

    @artifact_tool
    async def run_analysis_executors(
        ctx: RunContext[SessionDeps], intakes: list[Intake[SpecSearchParams]]
    ) -> ToolOutput:
        """Find the catalog specs for metric combos, with one executor per intake, in parallel.

        Args:
            intakes: One intake per executor, each with the combos it searches for.
        """
        return await run_executors(
            ctx, executor, list(intakes), tool="run_analysis_executors", role=ROLE
        )

    return run_analysis_executors
