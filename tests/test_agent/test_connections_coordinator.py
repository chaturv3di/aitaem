from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.messages import ModelMessage, ModelResponse

from aitaem import ConnectionManager
from aitaem.agent.agent_roles.connections_coordinator.agent import (
    build_connections_coordinator,
    make_run_connections,
)
from aitaem.agent.agent_roles.documents import parse_handoff
from aitaem.agent.deps import SessionDeps
from tests.test_agent.conftest import EXAMPLES, call, script, text, tool_returns

DUCKDB = str(EXAMPLES / "data/ad_campaigns.duckdb")


def _launcher(run_connections: Any, intake: dict[str, Any]) -> Agent[SessionDeps, str]:
    model = script(call("run_connections", intake=intake), text("reported"))
    return Agent(model, deps_type=SessionDeps, toolsets=[FunctionToolset([run_connections])])


def _handoff_from_returns(outcome: str, **extra: Any):  # type: ignore[no-untyped-def]
    def step(messages: list[ModelMessage]) -> ModelResponse:
        ids = [r.content.artifact_id for r in tool_returns(messages)]
        return text({"outcome": outcome, "response": "see results", "result_ids": ids, **extra})

    return step


def _launch_result(result: Any) -> Any:
    return next(
        p.content
        for m in result.all_messages()
        for p in m.parts
        if getattr(p, "tool_name", None) == "run_connections" and p.part_kind == "tool-return"
    )


@pytest.mark.asyncio
async def test_complete_after_connecting_duckdb(make_deps) -> None:
    deps = make_deps(connection_manager=ConnectionManager())
    params = {"backend_type": "duckdb", "config": {"path": DUCKDB}}
    coordinator = build_connections_coordinator(
        script(
            call("connect_source", params=params),
            call("list_tables"),
            _handoff_from_returns("complete"),
        )
    )
    intake = {"instructions": "Connect the ads DuckDB file", "params": params}

    result = await _launcher(make_run_connections(coordinator), intake).run("go", deps=deps)

    assert deps.connection_manager.backend_types == ["duckdb"]
    launched = _launch_result(result)
    assert launched.facts["outcome"] == "complete"
    handoff_ref = deps.store.get(launched.artifact_id)
    assert (handoff_ref.kind, handoff_ref.type_tag) == ("file", "handoff")
    intake_id, list_tables_id = handoff_ref.parents
    assert deps.store.get(intake_id).type_tag == "intake"
    assert deps.store.get(list_tables_id).type_tag == "table_list"
    assert deps.store.get(list_tables_id).produced_by.role == "connections_coordinator"
    assert parse_handoff(deps.store.read(launched.artifact_id)).result_ids == [list_tables_id]
    assert launched.facts["result_ids"] == [list_tables_id]


@pytest.mark.asyncio
async def test_needs_input_for_missing_config(make_deps) -> None:
    deps = make_deps(connection_manager=ConnectionManager())
    params = {"backend_type": "bigquery", "config": {}}
    coordinator = build_connections_coordinator(
        script(
            call("connect_source", params=params),
            _handoff_from_returns("needs-input", questions=["Which BigQuery project_id?"]),
        )
    )
    intake = {"instructions": "Connect BigQuery", "params": params}

    result = await _launcher(make_run_connections(coordinator), intake).run("go", deps=deps)

    launched = _launch_result(result)
    assert launched.facts["outcome"] == "needs-input"
    assert launched.facts["questions"] == ["Which BigQuery project_id?"]
    (error_id,) = launched.facts["result_ids"]
    assert deps.store.get(error_id).type_tag == "error"
    assert deps.connection_manager.backend_types == []
