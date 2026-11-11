from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_ai import Agent, DeferredToolRequests, FunctionToolset
from pydantic_ai.conversation import Conversation
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem.agent.agent_roles.documents import (
    Intake,
    is_handoff,
    parse_handoff,
    parse_reply,
)
from aitaem.agent.agent_roles.spec_coordinator.agent import (
    SpecParams,
    build_spec_coordinator,
    start_spec_design,
)
from aitaem.agent.deps import SessionDeps
from tests.test_agent.conftest import call, script, text, tool_returns

SOURCE = "duckdb://ad_campaigns.duckdb/ad_campaigns"
CPC = f"""\
metric:
  name: cpc
  description: Cost per click
  source: {SOURCE}
  numerator: SUM(ad_spend)
  denominator: SUM(clicks)
  timestamp_col: date
"""
COUNTRY = "slice:\n  name: country\n  values:\n    - name: US\n      where: \"country = 'USA'\"\n"


def spec_executor_model() -> FunctionModel:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = tool_returns(messages)
        if not returns:
            first = messages[0]
            assert isinstance(first, ModelRequest)
            prompt = next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
            (source,) = yaml.safe_load(prompt)["params"]["sources"]
            return call("describe_table", source=source)
        return text(
            {
                "outcome": "complete",
                "response": "ad_spend and clicks are measures; date is the timestamp.",
                "result_ids": [returns[0].content.artifact_id],
            }
        )

    return FunctionModel(model)


def reply(response: str, *attachments: str) -> dict[str, Any]:
    return {
        "response": response,
        "status": "ok",
        "attachments": [{"artifact_id": a, "reason": "see"} for a in attachments],
    }


def last_id(messages: list[ModelMessage]) -> str:
    return tool_returns(messages)[-1].content.artifact_id


class Session:
    """Runs the spec coordinator turn by turn on one Conversation, as the facade does."""

    def __init__(self, agent: Agent[SessionDeps, str], deps: SessionDeps) -> None:
        self.agent, self.deps, self.conversation = agent, deps, Conversation()

    async def turn(self, message: str) -> str:
        result = await self.agent.run(message, deps=self.deps, conversation=self.conversation)
        self.conversation = result.conversation
        return result.output


@pytest.mark.asyncio
async def test_research_draft_validate_commit_over_turns(deps: SessionDeps) -> None:
    research = [{"instructions": "Profile the ads table", "params": {"sources": [SOURCE]}}]
    ids: dict[str, str] = {}

    def show_draft(messages: list[ModelMessage]) -> ModelResponse:
        ids["draft"] = last_id(messages)
        return text(reply(f"Here is the draft:\n{CPC}", ids["draft"]))

    def handoff(messages: list[ModelMessage]) -> ModelResponse:
        ids["commit"] = last_id(messages)
        return text(
            {"outcome": "complete", "response": "Saved metric cpc.", "result_ids": [ids["commit"]]}
        )

    model = script(
        call("run_spec_executors", intakes=research),
        lambda m: text(
            reply("ad_spend and clicks can define CPC. What should it measure?", last_id(m))
        ),
        call("validate_spec", spec_type="metric", yaml_text=CPC),
        show_draft,
        lambda m: call("commit_spec", draft_id=ids["draft"]),
        handoff,
    )
    session = Session(build_spec_coordinator(model, spec_executor_model()), deps)
    intake = Intake[SpecParams](
        instructions="Define a cost-per-click metric",
        params=SpecParams(spec_type="metric", names=["cpc"], sources=[SOURCE]),
    )

    first = await session.turn(yaml.safe_dump(intake.model_dump(mode="json")))
    assert not is_handoff(first)
    fan_out = parse_reply(first).attachments[0].artifact_id
    assert deps.store.get(fan_out).type_tag == "fan_out"

    second = await session.turn("Spend divided by clicks.")
    assert parse_reply(second).attachments[0].artifact_id == ids["draft"]
    assert "cpc" not in deps.spec_cache.metrics

    third = await session.turn("Looks right, save it.")
    assert parse_handoff(third).result_ids == [ids["commit"]]
    assert (Path(deps.spec_dir) / "metrics" / "cpc.yaml").exists()
    assert "cpc" in deps.spec_cache.metrics


@pytest.mark.asyncio
async def test_user_leaves_lists_saved_deletes_chosen_then_hands_off(deps: SessionDeps) -> None:
    ids: dict[str, str] = {}

    def keep(name: str):  # type: ignore[no-untyped-def]
        def step(messages: list[ModelMessage]) -> ModelResponse:
            ids[name] = last_id(messages)
            return call("commit_spec", draft_id=ids[name])

        return step

    model = script(
        call("validate_spec", spec_type="metric", yaml_text=CPC),
        keep("cpc"),
        call("validate_spec", spec_type="slice", yaml_text=COUNTRY),
        keep("country"),
        lambda m: text(reply("Saved cpc and country. Next spec?")),
        call("list_specs"),
        lambda m: text(reply("You saved cpc and country this session. Delete any?", last_id(m))),
        call("delete_spec", spec_type="slice", name="country"),
        lambda m: text(
            {
                "outcome": "complete",
                "response": "Kept cpc; deleted country.",
                "result_ids": [last_id(m)],
            }
        ),
    )
    session = Session(build_spec_coordinator(model, spec_executor_model()), deps)

    await session.turn("Define cpc and a country slice; both are agreed.")
    listing = await session.turn("I'm done for now.")
    assert "cpc and country" in parse_reply(listing).response
    done = await session.turn("Delete country.")

    handoff = parse_handoff(done)
    assert deps.store.get(handoff.result_ids[0]).type_tag == "spec_delete"
    assert "country" not in deps.spec_cache.slices
    assert "cpc" in deps.spec_cache.metrics
    assert not (Path(deps.spec_dir) / "slices" / "country.yaml").exists()


def test_start_spec_design_defers_with_intake_id(deps: SessionDeps) -> None:
    intake = {"instructions": "Define CPC", "params": {"names": ["cpc"], "sources": [SOURCE]}}
    main = Agent(
        script(call("start_spec_design", intake=intake)),
        deps_type=SessionDeps,
        output_type=[str, DeferredToolRequests],
        toolsets=[FunctionToolset([start_spec_design])],
    )

    result = main.run_sync("define cpc", deps=deps)

    assert isinstance(result.output, DeferredToolRequests)
    (deferred,) = result.output.calls
    intake_id = result.output.metadata[deferred.tool_call_id]["intake_id"]
    ref = deps.store.get(intake_id)
    assert ref.type_tag == "intake"
    assert yaml.safe_load(deps.store.read(intake_id))["params"]["names"] == ["cpc"]


def test_spec_coordinator_tools(deps: SessionDeps) -> None:
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(sorted(t.name for t in info.function_tools))
        return text(reply("hi"))

    build_spec_coordinator(FunctionModel(model), spec_executor_model()).run_sync("x", deps=deps)
    assert seen == sorted(
        [
            "column_distribution",
            "commit_spec",
            "delete_spec",
            "list_specs",
            "list_tables",
            "read_spec",
            "run_spec_executors",
            "sample_rows",
            "validate_spec",
        ]
    )
