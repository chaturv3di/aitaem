from __future__ import annotations

import re
from typing import Any

import pytest
import yaml
from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem.agent.agent_roles.analysis_coordinator.agent import (
    build_analysis_coordinator,
    make_run_analysis,
)
from aitaem.agent.deps import SessionDeps
from tests.test_agent.conftest import call, script, text, tool_returns

# What the analysis executors find, by the measure the coordinator asks for.
CATALOG = {"click rate": ("ctr", "geo"), "revenue": ("total_revenue", "geo")}


def executor_model() -> FunctionModel:
    """Searches with find_specs, checks the pairing, and hands off the specs it is sure of."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        first = messages[0]
        assert isinstance(first, ModelRequest)
        prompt = next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
        combo = yaml.safe_load(prompt)["params"]["combos"][0]
        returns = tool_returns(messages)
        steps = sum(isinstance(m, ModelResponse) for m in messages)
        if combo["metric"] not in CATALOG:
            if steps == 0:
                return call("find_specs", spec_type="metric", query=combo["metric"])
            return text({"outcome": "inconclusive", "response": "no metric fits"})
        metric, slice_name = CATALOG[combo["metric"]]
        if steps == 0:
            return call("find_specs", spec_type="metric", query=combo["metric"])
        if steps == 1:
            return call("check_compatibility", metrics=[metric], slices=[slice_name])
        return text(
            {
                "outcome": "complete",
                "response": f"metric: {metric}; slices: [{slice_name}]",
                "result_ids": [returns[0].content.artifact_id],
            }
        )

    return FunctionModel(model)


def _fan_out_specs(messages: list[ModelMessage]) -> list[str]:
    (fan_out,) = tool_returns(messages)
    return [
        m.group(1)
        for e in fan_out.content.facts["executors"]
        if (m := re.search(r"metric: (\w+)", e["response"]))
    ]


def _launcher(run_analysis: Any, intake: dict[str, Any]) -> Agent[SessionDeps, str]:
    model = script(call("run_analysis", intake=intake), text("reported"))
    return Agent(model, deps_type=SessionDeps, toolsets=[FunctionToolset([run_analysis])])


def _launched(result: Any) -> Any:
    return next(
        p.content
        for m in result.all_messages()
        for p in m.parts
        if getattr(p, "tool_name", None) == "run_analysis" and p.part_kind == "tool-return"
    )


INTAKE = {
    "instructions": "Which region has the highest click rate?",
    "params": {"metrics": ["click rate"], "slices": ["region"]},
}


@pytest.mark.asyncio
async def test_fan_out_supplies_specs_then_chain_completes(deps: SessionDeps) -> None:
    ids: dict[str, str] = {}

    def compute(messages: list[ModelMessage]) -> ModelResponse:
        (metric,) = _fan_out_specs(messages)
        return call("compute_metrics", params={"metrics": [metric], "slices": ["geo"]})

    def rank(messages: list[ModelMessage]) -> ModelResponse:
        ids["compute"] = tool_returns(messages)[0].content.artifact_id
        return call("rank_by_value", result_id=ids["compute"], top_n=1)

    def read(messages: list[ModelMessage]) -> ModelResponse:
        ids["ranked"] = tool_returns(messages)[0].content.artifact_id
        return call(
            "read_artifact", artifact_id=ids["ranked"], columns=["slice_value", "metric_value"]
        )

    def answer(messages: list[ModelMessage]) -> ModelResponse:
        (row,) = tool_returns(messages)[0].content.facts["rows"]
        return text(
            {
                "outcome": "complete",
                "response": f"{row['slice_value']} has the highest CTR, {row['metric_value']:.4f}.",
                "result_ids": [ids["compute"], ids["ranked"]],
            }
        )

    combos = [
        {
            "instructions": "Find click rate by region",
            "params": {"combos": [{"metric": "click rate", "slices": ["region"]}]},
        }
    ]
    model = script(call("run_analysis_executors", intakes=combos), compute, rank, read, answer)
    coordinator = build_analysis_coordinator(model, executor_model())

    result = await _launcher(make_run_analysis(coordinator), INTAKE).run("go", deps=deps)

    launched = _launched(result)
    assert launched.facts["outcome"] == "complete"
    assert "has the highest CTR" in launched.facts["response"]
    compute_ref = deps.store.get(ids["compute"])
    assert (compute_ref.kind, compute_ref.produced_by.role) == ("ibis", "analysis_coordinator")
    assert deps.store.get(ids["ranked"]).parents == [ids["compute"]]
    assert deps.store.get(launched.artifact_id).parents[1:] == [ids["compute"], ids["ranked"]]


@pytest.mark.asyncio
async def test_no_compatible_spec_needs_input_before_compute(deps: SessionDeps) -> None:
    def ask(messages: list[ModelMessage]) -> ModelResponse:
        assert _fan_out_specs(messages) == []
        return text(
            {
                "outcome": "needs-input",
                "response": "The catalog has no churn metric.",
                "questions": ["Which metric measures churn?"],
            }
        )

    combos = [{"instructions": "Find churn", "params": {"combos": [{"metric": "churn"}]}}]
    model = script(call("run_analysis_executors", intakes=combos), ask)
    coordinator = build_analysis_coordinator(model, executor_model())
    intake = {"instructions": "What is churn by region?", "params": {"metrics": ["churn"]}}

    result = await _launcher(make_run_analysis(coordinator), intake).run("go", deps=deps)

    launched = _launched(result)
    assert launched.facts["outcome"] == "needs-input"
    assert launched.facts["questions"] == ["Which metric measures churn?"]
    assert deps.store.refs("metric_result") == []


@pytest.mark.asyncio
async def test_relaunch_reuses_earlier_results(deps: SessionDeps) -> None:
    first_model = script(
        call("compute_metrics", params={"metrics": ["ctr"], "slices": ["geo"]}),
        lambda m: text(
            {
                "outcome": "complete",
                "response": "CTR by geo computed.",
                "result_ids": [tool_returns(m)[0].content.artifact_id],
            }
        ),
    )
    first = await _launcher(
        make_run_analysis(build_analysis_coordinator(first_model, executor_model())), INTAKE
    ).run("go", deps=deps)
    (earlier,) = _launched(first).facts["result_ids"]

    second_model = script(
        call("filter_by_value", result_id=earlier, column="slice_value", values=["USA", "EU"]),
        lambda m: text(
            {
                "outcome": "complete",
                "response": "Filtered.",
                "result_ids": [earlier, tool_returns(m)[0].content.artifact_id],
            }
        ),
    )
    intake = {
        "instructions": "Keep USA and EU from the earlier CTR result",
        "params": {"metrics": ["ctr"]},
        "result_ids": [earlier],
    }
    second = await _launcher(
        make_run_analysis(build_analysis_coordinator(second_model, executor_model())), intake
    ).run("go", deps=deps)

    launched = _launched(second)
    assert launched.facts["outcome"] == "complete"
    assert len(deps.store.refs("metric_result")) == 1
    intake_id = deps.store.get(launched.artifact_id).parents[0]
    assert deps.store.get(intake_id).parents == [earlier]
    filtered = launched.facts["result_ids"][1]
    assert deps.store.get(filtered).parents == [earlier]


def test_coordinator_tools() -> None:
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(sorted(t.name for t in info.function_tools))
        return text("outcome: complete\nresponse: ok\n")

    build_analysis_coordinator(FunctionModel(model), executor_model()).run_sync("x", deps=None)  # type: ignore[arg-type]
    assert seen == sorted(
        [
            "compute_metrics",
            "contribution_share",
            "describe_artifact",
            "distribution_summary",
            "filter_by_result",
            "filter_by_threshold",
            "filter_by_value",
            "period_over_period",
            "rank_by_value",
            "read_artifact",
            "run_analysis_executors",
        ]
    )
