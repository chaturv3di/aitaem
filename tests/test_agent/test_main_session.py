from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from aitaem import ConnectionManager, MetricCompute
from aitaem.agent.agent_roles.analysis_coordinator.agent import build_analysis_coordinator
from aitaem.agent.agent_roles.connections_coordinator.agent import build_connections_coordinator
from aitaem.agent.agent_roles.documents import parse_reply
from aitaem.agent.agent_roles.main_session.agent import build_main_session
from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status
from tests.test_agent.conftest import EXAMPLES, call, script, text, tool_returns

NOT_USED = script()  # a model that fails the test if a role it backs is ever run


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


def main_session(
    model: FunctionModel, connections: FunctionModel = NOT_USED, analysis: FunctionModel = NOT_USED
):  # type: ignore[no-untyped-def]
    return build_main_session(
        model,
        build_connections_coordinator(connections),
        build_analysis_coordinator(analysis, NOT_USED),
    )


def launches(model: FunctionModel) -> list[tuple[str, dict[str, Any]]]:
    """The launch tool calls the main session made, in order."""
    final = model.prompts[-1]  # type: ignore[attr-defined]
    return [
        (p.tool_name, p.args_as_dict())
        for m in final
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
        and p.tool_name in ("run_connections", "run_analysis", "start_spec_design")
    ]


def seed_ctr_by_geo(deps: SessionDeps) -> str:
    expr = MetricCompute(deps.spec_cache, deps.connection_manager).compute(
        metrics="ctr", slices="geo"
    )
    ref = deps.store.put(
        "ibis",
        expr,
        type_tag="metric_result",
        summary="ctr by geo",
        produced_by=ProducedBy(tool="compute_metrics"),
    )
    return ref.id


@pytest.mark.asyncio
async def test_recall_with_stats_tools_and_no_launch(deps: SessionDeps) -> None:
    earlier = seed_ctr_by_geo(deps)
    ids: dict[str, str] = {}

    def read(messages: list[ModelMessage]) -> ModelResponse:
        ids["top"] = tool_returns(messages)[0].content.artifact_id
        return call("read_artifact", artifact_id=ids["top"])

    def answer(messages: list[ModelMessage]) -> ModelResponse:
        (row,) = tool_returns(messages)[0].content.facts["rows"]
        return reply(f"{row['slice_value']} has the highest CTR.", ids["top"])

    model = script(call("rank_by_value", result_id=earlier, top_n=1), read, answer)

    result = await main_session(model).run("Which region had the highest CTR?", deps=deps)

    assert launches(model) == []
    out = parse_reply(result.output)
    assert out.status == Status.ok
    assert out.attachments[0].artifact_id == ids["top"]
    assert "has the highest CTR" in out.response


@pytest.mark.asyncio
async def test_launch_tools_take_typed_intakes(make_deps, example_specs) -> None:  # type: ignore[no-untyped-def]
    deps = make_deps(connection_manager=ConnectionManager(), spec_cache=example_specs)
    params = {
        "backend_type": "duckdb",
        "config": {"path": str(EXAMPLES / "data/ad_campaigns.duckdb")},
    }
    connections = script(
        call("connect_source", params=params),
        lambda m: handoff("complete", "Connected.", [tool_returns(m)[0].content.artifact_id]),
    )
    model = script(
        call(
            "run_connections",
            intake={"instructions": "connect", "params": {"backend_type": "sqlite"}},
        ),
        call("run_connections", intake={"instructions": "Connect the ads file", "params": params}),
        call(
            "start_spec_design", intake={"instructions": "Define CPC", "params": {"names": ["cpc"]}}
        ),
    )

    result = await main_session(model, connections=connections).run(
        "connect, then define cpc", deps=deps
    )

    retries = [
        p
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, RetryPromptPart) and p.tool_name == "run_connections"
    ]
    assert len(retries) == 1
    assert "backend_type" in str(retries[0].content)
    assert deps.connection_manager.backend_types == ["duckdb"]
    assert isinstance(result.output, DeferredToolRequests)
    assert result.output.calls[0].tool_name == "start_spec_design"
    intakes = deps.store.refs("intake")
    assert len(intakes) == 2


@pytest.mark.asyncio
async def test_needs_input_handoff_becomes_questions(deps: SessionDeps) -> None:
    analysis = script(
        handoff(
            "needs-input",
            "No churn metric in the catalog.",
            [],
            questions=["Which metric is churn?"],
        )
    )

    def ask(messages: list[ModelMessage]) -> ModelResponse:
        facts = tool_returns(messages)[0].content.facts
        assert facts["outcome"] == "needs-input"
        return reply("\n".join(facts["questions"]), status="empty")

    intake = {"instructions": "Churn by region", "params": {"metrics": ["churn"]}}
    model = script(call("run_analysis", intake=intake), ask)

    result = await main_session(model, analysis=analysis).run("What's churn by region?", deps=deps)

    assert parse_reply(result.output).response == "Which metric is churn?"


@pytest.mark.asyncio
async def test_complete_handoff_that_falls_short_is_relaunched(deps: SessionDeps) -> None:
    earlier = seed_ctr_by_geo(deps)
    analysis = script(
        handoff("complete", "CTR by region computed.", [earlier]),
        lambda m: handoff("complete", "ROAS by region computed too.", [earlier]),
    )
    ask = {"instructions": "CTR and ROAS by region", "params": {"metrics": ["ctr", "roas"]}}

    def relaunch(messages: list[ModelMessage]) -> ModelResponse:
        facts = tool_returns(messages)[0].content.facts
        assert "ROAS" not in facts["response"]  # the answer misses ROAS
        missing = {
            "instructions": "Add ROAS by region; CTR is done",
            "params": {"metrics": ["roas"]},
            "result_ids": facts["result_ids"],
        }
        return call("run_analysis", intake=missing)

    def answer(messages: list[ModelMessage]) -> ModelResponse:
        return reply("CTR and ROAS by region are ready.", earlier)

    model = script(call("run_analysis", intake=ask), relaunch, answer)

    result = await main_session(model, analysis=analysis).run("CTR and ROAS by region?", deps=deps)

    calls = launches(model)
    assert [name for name, _ in calls] == ["run_analysis", "run_analysis"]
    assert calls[1][1]["intake"]["result_ids"] == [earlier]
    second_intake = deps.store.refs("intake")[1]
    assert second_intake.parents == [earlier]
    assert parse_reply(result.output).status == Status.ok


@pytest.mark.asyncio
async def test_two_coordinators_in_sequence(make_deps, example_specs) -> None:  # type: ignore[no-untyped-def]
    deps = make_deps(connection_manager=ConnectionManager(), spec_cache=example_specs)
    params = {
        "backend_type": "duckdb",
        "config": {"path": str(EXAMPLES / "data/ad_campaigns.duckdb")},
    }
    connections = script(
        call("connect_source", params=params),
        lambda m: handoff(
            "complete", "Connected duckdb.", [tool_returns(m)[0].content.artifact_id]
        ),
    )
    analysis = script(
        call("compute_metrics", params={"metrics": ["ctr"]}),
        lambda m: handoff("complete", "CTR is 3.9%.", [tool_returns(m)[0].content.artifact_id]),
    )

    def analyse(messages: list[ModelMessage]) -> ModelResponse:
        assert tool_returns(messages)[0].content.facts["outcome"] == "complete"
        return call(
            "run_analysis", intake={"instructions": "Overall CTR", "params": {"metrics": ["ctr"]}}
        )

    def answer(messages: list[ModelMessage]) -> ModelResponse:
        facts = tool_returns(messages)[0].content.facts
        return reply(facts["response"], *facts["result_ids"])

    model = script(
        call("run_connections", intake={"instructions": "Connect the ads file", "params": params}),
        analyse,
        answer,
    )

    result = await main_session(model, connections=connections, analysis=analysis).run(
        "Connect the ads data and give me overall CTR", deps=deps
    )

    assert [name for name, _ in launches(model)] == ["run_connections", "run_analysis"]
    first, second = deps.store.refs("intake")
    handoffs = deps.store.refs("handoff")
    assert int(handoffs[0].id[1:]) < int(
        second.id[1:]
    )  # the second intake follows the first handoff
    assert parse_reply(result.output).response == "CTR is 3.9%."
    assert yaml.safe_load(deps.store.read(first.id))["params"]["backend_type"] == "duckdb"
