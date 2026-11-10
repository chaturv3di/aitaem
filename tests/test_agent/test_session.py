"""AitaemAgent end to end, with each role's model scripted through `override`."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem import ConnectionManager, SpecCache
from aitaem.agent import AitaemAgent, Response, Status
from aitaem.agent.trace import RunTrace
from tests.test_agent.conftest import call, script, text, tool_returns

SOURCE = "duckdb://ad_campaigns.duckdb/ad_campaigns"
CPC = (
    f"metric:\n  name: cpc\n  source: {SOURCE}\n  numerator: SUM(ad_spend)\n"
    "  denominator: SUM(clicks)\n  timestamp_col: date\n"
)


def reply(response: str, *attachments: str, status: str = "ok") -> ModelResponse:
    return text(
        {
            "response": response,
            "status": status,
            "attachments": [{"artifact_id": a, "reason": "support"} for a in attachments],
        }
    )


def handoff(outcome: str, response: str, result_ids: list[str], **extra: Any) -> ModelResponse:
    return text({"outcome": outcome, "response": response, "result_ids": result_ids, **extra})


def first_prompt(messages: list[ModelMessage]) -> str:
    first = messages[0]
    assert isinstance(first, ModelRequest)
    return next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))


def executor_model() -> FunctionModel:
    """Both executor roles: table research, or spec search for 'click rate'."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = {t.name for t in info.function_tools}
        params = yaml.safe_load(first_prompt(messages))["params"]
        returns = tool_returns(messages)
        if "describe_table" in tools:
            if not returns:
                return call("describe_table", source=params["sources"][0])
            return handoff(
                "complete", "clicks, ad_spend: measures", [returns[0].content.artifact_id]
            )
        if not returns:
            return call("check_compatibility", metrics=["ctr"], slices=["geo"])
        return handoff("complete", "metric: ctr; slices: [geo]", [returns[0].content.artifact_id])

    return FunctionModel(model)


def session(**kwargs: Any) -> AitaemAgent:
    return AitaemAgent(model=script(), executor_model=executor_model(), **kwargs)


def overrides(agent: AitaemAgent, **models: FunctionModel) -> ExitStack:
    stack = ExitStack()
    for role, model in models.items():
        stack.enter_context(getattr(agent, role).override(model=model))
    return stack


@pytest.fixture
def connected(
    ad_campaigns_connector: Any, example_specs: SpecCache, tmp_path: Path
) -> Iterator[AitaemAgent]:
    cm = ConnectionManager()
    cm.add_connection("duckdb", connector=ad_campaigns_connector)
    agent = session(connection_manager=cm, spec_cache=example_specs, spec_dir=tmp_path / "specs")
    yield agent
    agent.cleanup()


@pytest.mark.asyncio
async def test_analysis_workflow_with_needs_input_round_trip(connected: AitaemAgent) -> None:
    ids: dict[str, str] = {}

    def compute(messages: list[ModelMessage]) -> ModelResponse:
        return call("compute_metrics", params={"metrics": ["ctr"], "slices": ["geo"]})

    def done(messages: list[ModelMessage]) -> ModelResponse:
        ids["ctr"] = tool_returns(messages)[0].content.artifact_id
        return handoff("complete", "CTR by geo is computed.", [ids["ctr"]])

    combos = [
        {
            "instructions": "click rate by region",
            "params": {"combos": [{"metric": "click rate", "slices": ["region"]}]},
        }
    ]
    analysis = script(
        handoff("needs-input", "Which period?", [], questions=["All time or monthly?"]),
        call("run_analysis_executors", intakes=combos),
        compute,
        done,
    )
    intake = {"instructions": "click rate by region", "params": {"metrics": ["click rate"]}}

    def answer(messages: list[ModelMessage]) -> ModelResponse:
        facts = tool_returns(messages)[0].content.facts
        return reply(facts["response"], *facts["result_ids"])

    main = script(
        call("run_analysis", intake=intake),
        lambda m: reply(tool_returns(m)[0].content.facts["questions"][0], status="empty"),
        call("run_analysis", intake={**intake, "instructions": "click rate by region, all time"}),
        answer,
    )

    with overrides(connected, main_session=main, analysis_coordinator=analysis):
        question = await connected.chat("What's the click rate by region?")
        final = await connected.chat("All time.")

    assert (question.response, question.status) == ("All time or monthly?", Status.empty)
    assert final.status == Status.ok
    assert final.response == "CTR by geo is computed."
    assert [(a.artifact_id, a.type_tag) for a in final.attachments] == [
        (ids["ctr"], "metric_result")
    ]
    assert connected.route == "main"
    assert isinstance(connected.trace(final.trace_id), RunTrace)


@pytest.mark.asyncio
async def test_spec_workflow_saves_specs_and_returns_to_main(connected: AitaemAgent) -> None:
    ids: dict[str, str] = {}

    def show(messages: list[ModelMessage]) -> ModelResponse:
        ids["draft"] = tool_returns(messages)[0].content.artifact_id
        return reply(f"Draft:\n{CPC}", ids["draft"])

    research = [{"instructions": "Profile ads", "params": {"sources": [SOURCE]}}]
    spec = script(
        call("run_spec_executors", intakes=research),
        lambda m: reply("ad_spend and clicks fit. Define CPC as spend per click?"),
        call("validate_spec", spec_type="metric", yaml_text=CPC),
        show,
        lambda m: call("commit_spec", draft_id=ids["draft"]),
        lambda m: handoff(
            "complete", "Saved metric cpc.", [tool_returns(m)[0].content.artifact_id]
        ),
    )
    main = script(
        call(
            "start_spec_design",
            intake={
                "instructions": "Define CPC",
                "params": {"names": ["cpc"], "sources": [SOURCE]},
            },
        ),
        lambda m: reply(
            tool_returns(m)[0].content.facts["response"], tool_returns(m)[0].content.artifact_id
        ),
        reply("Back in the main session."),
    )

    with overrides(connected, main_session=main, spec_coordinator=spec):
        first = await connected.chat("Define a CPC metric")
        assert connected.route == "spec"
        second = await connected.chat("Yes, spend per click.")
        assert second.attachments[0].type_tag == "spec_draft"
        third = await connected.chat("Save it.")
        assert connected.route == "main"
        fourth = await connected.chat("Thanks")

    assert first.response.startswith("ad_spend and clicks fit")
    assert third.response == "Saved metric cpc."
    assert third.attachments[0].type_tag == "handoff"
    assert fourth.response == "Back in the main session."
    assert (Path(connected.deps.spec_dir) / "metrics" / "cpc.yaml").exists()
    assert "cpc" in connected.deps.spec_cache.metrics


def test_defaults_for_missing_connection_manager_and_spec_cache() -> None:
    agent = session()
    try:
        assert agent.deps.connection_manager.backend_types == []
        assert agent.deps.spec_cache.metrics == {}
        assert Path(agent.deps.spec_dir) == agent.store.session_dir / "specs"
        assert agent.store.session_dir.name.startswith("aitaem-session-")
    finally:
        agent.cleanup()
    assert not agent.store.session_dir.exists()


@pytest.mark.asyncio
async def test_exception_becomes_error_response() -> None:
    agent = session()

    def boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model unavailable")

    with agent.main_session.override(model=FunctionModel(boom)):
        result = await agent.chat("hello")

    assert isinstance(result, Response)
    assert result.status == Status.error
    assert "model unavailable" in result.response
    assert agent.trace(result.trace_id).error == result.response
    agent.cleanup()
