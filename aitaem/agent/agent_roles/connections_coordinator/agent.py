"""Connections coordinator: connects a backend and lists its tables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.capabilities import Caching, Instrumentation
from pydantic_ai.models import Model

from aitaem.agent.agent_roles import launch, load_instructions
from aitaem.agent.agent_roles.documents import Intake, check_handoff
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput, artifact_tool
from aitaem.agent.tools.aitaem import ConnectionParams, connect_source
from aitaem.agent.tools.data import describe_table, list_tables

ROLE = "connections_coordinator"


def build_connections_coordinator(model: str | Model) -> Agent[SessionDeps, str]:
    agent: Agent[SessionDeps, str] = Agent(
        model,
        name=ROLE,
        deps_type=SessionDeps,
        output_type=str,
        instructions=load_instructions(__package__ or __name__),
        toolsets=[FunctionToolset([connect_source, list_tables, describe_table])],
        capabilities=[Caching(retention="1h", messages=True), Instrumentation()],
    )
    agent.output_validator(check_handoff)
    return agent


def make_run_connections(coordinator: Agent[SessionDeps, str]) -> Callable[..., Any]:
    """Return the tool that launches `coordinator`."""

    @artifact_tool
    async def run_connections(
        ctx: RunContext[SessionDeps], intake: Intake[ConnectionParams]
    ) -> ToolOutput:
        """Connect a DuckDB or BigQuery source and list its tables; returns the coordinator's handoff.

        Args:
            intake: What to connect, the backend and its settings, and earlier result IDs.
        """
        return await launch(ctx, coordinator, intake, tool="run_connections", role=ROLE)

    return run_connections
