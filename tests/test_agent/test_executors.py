from __future__ import annotations

import asyncio
from typing import Any

import pytest
import yaml
from pydantic_ai import Agent, CancellationToken, FunctionToolset
from pydantic_ai.exceptions import RunCancelled
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem.agent.agent_roles.analysis_executor.agent import (
    build_analysis_executor,
    make_run_analysis_executors,
)
from aitaem.agent.agent_roles.documents import parse_handoff
from aitaem.agent.agent_roles.spec_executor.agent import (
    build_spec_executor,
    make_run_spec_executors,
)
from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status


def _prompt(messages: list[ModelMessage]) -> str:
    first = messages[0]
    assert isinstance(first, ModelRequest)
    return next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))


class ExecutorScript:
    """An executor model: calls find_specs once, then hands off the artifact it got."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.started = 0
        self.cancelled = 0

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        intake = yaml.safe_load(_prompt(messages))
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart):
            handoff = {
                "outcome": "complete",
                "response": f"found {intake['params']['combos'][0]['metric']}",
                "result_ids": [last.content.artifact_id],
            }
            return ModelResponse(parts=[TextPart(yaml.safe_dump(handoff))])
        self.started += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
        if intake["instructions"] == "fail":
            raise RuntimeError("executor broke")
        query = intake["params"]["combos"][0]["metric"]
        return ModelResponse(
            parts=[ToolCallPart("find_specs", {"spec_type": "metric", "query": query})]
        )


def _coordinator(fan_out: Any, intakes: list[dict[str, Any]]) -> Agent[SessionDeps, str]:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("run_analysis_executors", {"intakes": intakes})])

    return Agent(FunctionModel(model), deps_type=SessionDeps, toolsets=[FunctionToolset([fan_out])])


def _intake(metric: str, instructions: str = "find it") -> dict[str, Any]:
    return {"instructions": instructions, "params": {"combos": [{"metric": metric}]}}


def _fan_out_result(result: Any) -> Any:
    return next(
        p.content
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == "run_analysis_executors"
    )


@pytest.mark.asyncio
async def test_at_most_four_executors_at_once(deps: SessionDeps) -> None:
    script = ExecutorScript()
    executor = build_analysis_executor(FunctionModel(script))
    coordinator = _coordinator(make_run_analysis_executors(executor), [_intake("revenue")] * 7)

    result = await coordinator.run("go", deps=deps)

    assert script.started == 7
    assert script.peak == 4
    entries = _fan_out_result(result).facts["executors"]
    assert [e["outcome"] for e in entries] == ["complete"] * 7


@pytest.mark.asyncio
async def test_failure_leaves_siblings_intact(deps: SessionDeps) -> None:
    executor = build_analysis_executor(FunctionModel(ExecutorScript()))
    intakes = [_intake("revenue"), _intake("ctr", "fail"), _intake("roas")]
    coordinator = _coordinator(make_run_analysis_executors(executor), intakes)

    result = await coordinator.run("go", deps=deps)

    entries = _fan_out_result(result).facts["executors"]
    assert [e["outcome"] for e in entries] == ["complete", "error", "complete"]
    assert "executor broke" in entries[1]["response"]
    error = deps.store.get(entries[1]["error_id"])
    assert (error.type_tag, error.status) == ("error", Status.error)
    assert parse_handoff(deps.store.read(entries[2]["handoff_id"])).response == "found roas"


@pytest.mark.asyncio
async def test_usage_accumulates_on_parent_run(deps: SessionDeps) -> None:
    executor = build_analysis_executor(FunctionModel(ExecutorScript()))
    coordinator = _coordinator(make_run_analysis_executors(executor), [_intake("revenue")] * 3)

    result = await coordinator.run("go", deps=deps)

    # 2 coordinator requests + 3 executors × 2 requests each
    assert result.usage.requests == 8


@pytest.mark.asyncio
async def test_handoff_parents_link_intake_and_results(deps: SessionDeps) -> None:
    executor = build_analysis_executor(FunctionModel(ExecutorScript()))
    coordinator = _coordinator(make_run_analysis_executors(executor), [_intake("revenue")])

    result = await coordinator.run("go", deps=deps)

    fan_out = _fan_out_result(result)
    handoff_id = fan_out.facts["executors"][0]["handoff_id"]
    intake_id, spec_matches = deps.store.get(handoff_id).parents
    assert deps.store.get(intake_id).type_tag == "intake"
    assert deps.store.get(spec_matches).type_tag == "spec_matches"
    assert deps.store.get(spec_matches).produced_by.role == "analysis_executor"
    assert deps.store.get(fan_out.artifact_id).parents == [handoff_id]


@pytest.mark.asyncio
async def test_cancelling_coordinator_cancels_executors(deps: SessionDeps) -> None:
    script = ExecutorScript(delay=5)
    executor = build_analysis_executor(FunctionModel(script))
    coordinator = _coordinator(make_run_analysis_executors(executor), [_intake("revenue")] * 3)
    token = CancellationToken()

    task = asyncio.create_task(coordinator.run("go", deps=deps, cancellation_token=token))
    while script.started < 3:
        await asyncio.sleep(0.01)
    token.cancel()

    with pytest.raises(RunCancelled):
        await task
    assert script.cancelled == 3


def test_executor_toolsets_and_names() -> None:
    seen: dict[str, list[str]] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen[_prompt(messages)] = sorted(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("outcome: complete\nresponse: ok\n")])

    analysis = build_analysis_executor(FunctionModel(model))
    spec = build_spec_executor(FunctionModel(model))
    analysis.run_sync("a", deps=None)  # type: ignore[arg-type]
    spec.run_sync("s", deps=None)  # type: ignore[arg-type]
    assert seen == {
        "a": ["check_compatibility", "find_specs", "read_spec"],
        "s": ["date_range", "describe_table"],
    }
    assert (analysis.name, spec.name) == ("analysis_executor", "spec_executor")
    assert make_run_spec_executors(spec).__name__ == "run_spec_executors"
