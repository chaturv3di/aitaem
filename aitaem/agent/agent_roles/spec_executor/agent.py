"""Spec executor: researches tables for spec design."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.capabilities import Caching, Instrumentation
from pydantic_ai.models import Model

from aitaem.agent.agent_roles import load_instructions, run_executors
from aitaem.agent.agent_roles.documents import Intake, check_handoff
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput, artifact_tool
from aitaem.agent.tools.data import date_range, describe_table

ROLE = "spec_executor"


class TableResearchParams(BaseModel):
    sources: list[str]
    """Source URIs of the tables to research."""


def build_spec_executor(model: str | Model) -> Agent[SessionDeps, str]:
    agent: Agent[SessionDeps, str] = Agent(
        model,
        name=ROLE,
        deps_type=SessionDeps,
        output_type=str,
        instructions=load_instructions(__package__ or __name__),
        toolsets=[FunctionToolset([describe_table, date_range])],
        capabilities=[Caching(retention="1h", messages=False), Instrumentation()],
    )
    agent.output_validator(check_handoff)
    return agent


def make_run_spec_executors(executor: Agent[SessionDeps, str]) -> Callable[..., Any]:
    """Return the fan-out tool that runs `executor` once per intake."""

    @artifact_tool
    async def run_spec_executors(
        ctx: RunContext[SessionDeps], intakes: list[Intake[TableResearchParams]]
    ) -> ToolOutput:
        """Research tables, with one executor per intake, in parallel.

        Args:
            intakes: One intake per executor, each with the tables it researches.
        """
        return await run_executors(
            ctx, executor, list(intakes), tool="run_spec_executors", role=ROLE
        )

    return run_spec_executors
