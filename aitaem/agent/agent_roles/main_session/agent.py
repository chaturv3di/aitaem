"""Main session: talks to the user, answers from existing results, and launches coordinators."""

from __future__ import annotations

from pydantic_ai import Agent, DeferredToolRequests, FunctionToolset
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from aitaem.agent.agent_roles import load_instructions
from aitaem.agent.agent_roles.analysis_coordinator.agent import make_run_analysis
from aitaem.agent.agent_roles.connections_coordinator.agent import make_run_connections
from aitaem.agent.agent_roles.documents import check_reply
from aitaem.agent.agent_roles.spec_coordinator.agent import start_spec_design
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools.artifacts import artifact_toolset
from aitaem.agent.tools.stats import stats_toolset

ROLE = "main_session"


def build_main_session(
    model: str | Model,
    connections_coordinator: Agent[SessionDeps, str],
    analysis_coordinator: Agent[SessionDeps, str],
) -> Agent[SessionDeps, str | DeferredToolRequests]:
    launch_tools = FunctionToolset(
        [
            make_run_connections(connections_coordinator),
            make_run_analysis(analysis_coordinator),
            start_spec_design,
        ]
    )
    agent: Agent[SessionDeps, str | DeferredToolRequests] = Agent(
        model,
        name=ROLE,
        deps_type=SessionDeps,
        output_type=[str, DeferredToolRequests],
        instructions=load_instructions(__package__ or __name__),
        toolsets=[launch_tools, artifact_toolset, stats_toolset],
        # Thinking stays off so history stubbing never breaks thinking-block binding (§A9.5).
        model_settings=ModelSettings(thinking=False),
        capabilities=[Caching(retention="1h", messages=True)],
    )
    # Output validators receive the str output only, never DeferredToolRequests.
    agent.output_validator(check_reply)  # type: ignore[arg-type]
    return agent
