"""Spec coordinator: designs specs with the user, then hands control back to the main session."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, CallDeferred, FunctionToolset, RunContext
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model

from aitaem.agent.agent_roles import load_instructions
from aitaem.agent.agent_roles.documents import Intake, check_reply_or_handoff, store_intake
from aitaem.agent.agent_roles.spec_executor.agent import (
    build_spec_executor,
    make_run_spec_executors,
)
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools.aitaem import commit_spec, delete_spec, list_specs, read_spec, validate_spec
from aitaem.agent.tools.data import column_distribution, list_tables, sample_rows

ROLE = "spec_coordinator"


class SpecParams(BaseModel):
    spec_type: Literal["metric", "slice", "segment"] | None = None
    names: list[str] = []
    """Specs to create, change, or delete."""
    sources: list[str] = []
    """Source URIs of the tables to research."""


def build_spec_coordinator(
    model: str | Model, executor_model: str | Model
) -> Agent[SessionDeps, str]:
    run_spec_executors = make_run_spec_executors(build_spec_executor(executor_model))
    agent: Agent[SessionDeps, str] = Agent(
        model,
        name=ROLE,
        deps_type=SessionDeps,
        output_type=str,
        instructions=load_instructions(__package__ or __name__),
        toolsets=[
            FunctionToolset([list_specs, read_spec, validate_spec, commit_spec, delete_spec]),
            FunctionToolset([list_tables, sample_rows, column_distribution]),
            FunctionToolset([run_spec_executors]),
        ],
        capabilities=[Caching(retention="1h", messages=True)],
    )
    agent.output_validator(check_reply_or_handoff)
    return agent


def start_spec_design(ctx: RunContext[SessionDeps], intake: Intake[SpecParams]) -> str:
    """Hand the conversation to the spec coordinator to create, change, or delete specs with the user.

    The coordinator talks with the user until the specs are done; its handoff comes back as this
    tool's result.

    Args:
        intake: What to design, the specs and source tables involved, and earlier result IDs.
    """
    intake_id = store_intake(ctx.deps, intake, "start_spec_design", ctx.tool_call_id)
    raise CallDeferred(metadata={"intake_id": intake_id})
