from __future__ import annotations

import dataclasses

import pytest
from pydantic_ai import Agent, FunctionToolset, ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem import ConnectionManager
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput, ToolResult, artifact_tool
from aitaem.agent.trace import Status
from tests.test_agent.conftest import make_ctx


def _double(ctx: RunContext[SessionDeps], x: int, label: str = "n") -> ToolOutput:
    """Double a number.

    Args:
        x: The number.
        label: A label.
    """
    if x < 0:
        raise ValueError("negative")
    return ToolOutput({"x": 2 * x}, "object", "doubled", f"{label}={2 * x}", facts={"value": 2 * x})


double = artifact_tool(_double)


def test_success_writes_artifact_and_returns_result(deps: SessionDeps) -> None:
    result = double(make_ctx(deps, "call_9"), 3)

    assert result == ToolResult(
        artifact_id="a001", status=Status.ok, summary="n=6", facts={"value": 6}
    )
    ref = deps.store.get("a001")
    assert (ref.kind, ref.type_tag, ref.produced_by.tool) == ("object", "doubled", "_double")
    assert (ref.produced_by.call_id, ref.produced_by.role) == ("call_9", "main_session")
    assert deps.store.read("a001") == {"x": 6}


def test_exception_writes_error_artifact(deps: SessionDeps) -> None:
    result = double(make_ctx(deps), -1)

    assert result.status == Status.error
    assert "negative" in result.summary
    ref = deps.store.get(result.artifact_id)
    assert (ref.type_tag, ref.status) == ("error", Status.error)


def test_model_retry_passes_through(deps: SessionDeps) -> None:
    @artifact_tool
    def picky(ctx: RunContext[SessionDeps]) -> ToolOutput:
        raise ModelRetry("try again")

    with pytest.raises(ModelRetry):
        picky(make_ctx(deps))
    assert deps.store.refs() == []


@pytest.mark.asyncio
async def test_async_body(deps: SessionDeps) -> None:
    @artifact_tool
    async def slow(ctx: RunContext[SessionDeps]) -> ToolOutput:
        return ToolOutput("text", "file", "note", "a note")

    result = await slow(make_ctx(deps))
    assert deps.store.read(result.artifact_id) == "text"


def test_wrapped_schema_matches_unwrapped() -> None:
    seen: dict[str, object] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.update(
            {t.name: (t.description, t.parameters_json_schema) for t in info.function_tools}
        )
        return ModelResponse(parts=[TextPart("ok")])

    Agent(
        FunctionModel(model), toolsets=[FunctionToolset([double])], deps_type=SessionDeps
    ).run_sync("x", deps=None)
    wrapped = seen["_double"]
    Agent(
        FunctionModel(model), toolsets=[FunctionToolset([_double])], deps_type=SessionDeps
    ).run_sync("x", deps=None)
    assert wrapped == seen["_double"]
    assert wrapped[1]["properties"]["x"]["description"] == "The number."  # type: ignore[index]


@pytest.mark.parametrize("with_duckdb", [True, False])
def test_compute_guard_locks_only_when_required(make_deps, with_duckdb: bool) -> None:
    deps = make_deps() if with_duckdb else make_deps(connection_manager=ConnectionManager())
    assert deps.connection_manager.requires_compute_lock is with_duckdb

    with deps.compute_guard():
        assert deps.compute_lock.locked() is with_duckdb
    assert not deps.compute_lock.locked()


def test_child_deps_share_lock_and_store(deps: SessionDeps) -> None:
    child = dataclasses.replace(deps, role="analysis_executor")
    assert child.compute_lock is deps.compute_lock
    assert child.store is deps.store
