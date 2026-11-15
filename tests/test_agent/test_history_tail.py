from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem import ConnectionManager, SpecCache
from aitaem.agent import AitaemAgent
from aitaem.agent.agent_roles.documents import Handoff, Intake, store_handoff, store_intake, to_yaml
from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.history import render_state, session_state, tail_block
from aitaem.agent.trace import Status
from tests.test_agent.conftest import call, text, tool_returns
from tests.test_agent.test_session import executor_model, reply


def _tail_parts(messages: list[ModelMessage]) -> list[UserPromptPart]:
    return [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
        and not isinstance(p.content, str)
        and any(isinstance(c, CachePoint) for c in p.content)
    ]


class Recorder:
    """A main-session model that records what each request carries."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(
            {
                "tails": _tail_parts(messages),
                "instructions": info.instructions,
                "tools": [
                    (t.name, t.description, json.dumps(t.parameters_json_schema))
                    for t in info.function_tools
                ],
            }
        )
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step


@pytest.fixture
def agent(ad_campaigns_connector: Any, example_specs: SpecCache) -> Any:
    cm = ConnectionManager()
    cm.add_connection("duckdb", connector=ad_campaigns_connector)
    session = AitaemAgent(
        cm,
        example_specs,
        model=FunctionModel(lambda m, i: text("")),
        executor_model=executor_model(),
    )
    yield session
    session.cleanup()


@pytest.mark.asyncio
async def test_tail_block_on_every_request_and_out_of_history(agent: AitaemAgent) -> None:
    recorder = Recorder(
        call("list_artifacts"),
        lambda m: reply("One artifact.", tool_returns(m)[0].content.artifact_id),
        reply("Still here."),
    )
    with agent.main_session.override(model=FunctionModel(recorder)):
        await agent.chat("What do we have?")
        await agent.chat("And now?")

    assert len(recorder.requests) == 3
    for request in recorder.requests:
        (tail,) = request["tails"]  # exactly one block, on the latest request only
        assert isinstance(tail.content[0], CachePoint)
        assert "<session-state>" in tail.content[1] and "<artifact-index>" in tail.content[1]
    assert "a001 object artifact_list ok" in recorder.requests[1]["tails"][0].content[1]
    assert _tail_parts(agent.main_conversation.messages) == []


@pytest.mark.asyncio
async def test_static_prefix_identical_across_turns(agent: AitaemAgent) -> None:
    recorder = Recorder(
        call("list_artifacts"),
        lambda m: reply("ok"),
        reply("ok again"),
    )
    with agent.main_session.override(model=FunctionModel(recorder)):
        await agent.chat("first")
        await agent.chat("second")

    first, *rest = recorder.requests
    for request in rest:
        assert request["instructions"] == first["instructions"]
        assert request["tools"] == first["tools"]


def test_session_state(deps: SessionDeps) -> None:
    store = deps.store
    intake = Intake[Any](instructions="CTR by region", params={})
    intake_id = store_intake(deps, intake, "run_analysis", "c1")
    store_handoff(
        deps,
        Handoff(outcome="needs-input", response="Which period?", questions=["All time?"]),
        intake_id,
        "run_analysis",
        "analysis_coordinator",
    )
    by = ProducedBy(tool="describe_table")
    store.put("object", {}, type_tag="table_schema", summary="ok", produced_by=by)
    store.put(
        "object", {}, type_tag="error", summary="old failure", produced_by=by, status=Status.error
    )
    store.put("object", {}, type_tag="table_schema", summary="ok", produced_by=by)
    store.put(
        "object",
        {},
        type_tag="error",
        summary="still failing",
        produced_by=ProducedBy(tool="date_range"),
        status=Status.error,
    )

    state = session_state(deps)

    assert state["goal"] == "CTR by region"
    assert state["connected_backends"] == ["duckdb"]
    assert state["specs"]["slices"] == ["campaign_type", "geo", "industry"]
    assert state["route"] == "main"
    assert state["open_questions"] == ["All time?"]
    assert state["unresolved_errors"] == ["a006 date_range: still failing"]
    assert to_yaml(intake).startswith("instructions:")


def test_state_is_capped() -> None:
    state = {"goal": "g", "specs": {"metrics": [f"m{i:04d}" for i in range(500)]}}
    rendered = render_state(state, max_chars=200)
    assert len(rendered) <= 200
    assert json.loads(rendered)["specs"] == {"metrics": 500}


def test_tail_block_in_spec_route(deps: SessionDeps) -> None:
    spec_deps = dataclasses.replace(deps, role="spec_coordinator")
    assert '"route":"spec"' in tail_block(spec_deps)
