"""Contract tests: the pydantic-ai and DuckDB mechanics Plan 42 relies on.

Each test pins a behavior confirmed by execution in research/agent_module/42-*.md (Updates of
2026-11-10). A failure here means a dependency changed under the agent module.
"""

from __future__ import annotations

import functools
import threading
from typing import Any

import duckdb
import yaml
from pydantic import BaseModel
from pydantic_ai import (
    Agent,
    CallDeferred,
    DeferredToolRequests,
    DeferredToolResults,
    ModelRetry,
)
from pydantic_ai.conversation import Conversation
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


class _Doc(BaseModel):
    outcome: str
    response: str


def test_yaml_text_output_retries_on_model_retry() -> None:
    outputs = iter(["outcome: [unclosed", "outcome: complete\nresponse: |\n  Done: 'x'\n"])

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(next(outputs))])

    agent = Agent(FunctionModel(model), output_type=str)

    @agent.output_validator
    def check(text: str) -> str:
        try:
            _Doc.model_validate(yaml.safe_load(text))
        except Exception as e:
            raise ModelRetry(f"bad document: {e}") from e
        return text

    result = agent.run_sync("go")

    assert _Doc.model_validate(yaml.safe_load(result.output)).response == "Done: 'x'\n"
    parts = [p for m in result.all_messages() for p in m.parts]
    assert any(p.part_kind == "retry-prompt" for p in parts)


def test_call_deferred_ends_run_and_resumes_from_conversation() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart):
            return ModelResponse(parts=[TextPart(f"report: {last.content}")])
        return ModelResponse(parts=[ToolCallPart("launch", {"instructions": "make ctr"})])

    agent = Agent(FunctionModel(model), output_type=[str, DeferredToolRequests])

    @agent.tool_plain
    def launch(instructions: str) -> str:
        """Launch a task."""
        raise CallDeferred(metadata={"instructions": instructions})

    first = agent.run_sync("define ctr", conversation=Conversation())
    assert isinstance(first.output, DeferredToolRequests)
    call = first.output.calls[0]
    assert first.output.metadata[call.tool_call_id] == {"instructions": "make ctr"}
    assert first.conversation.deferred_tool_requests is not None

    second = agent.run_sync(
        conversation=first.conversation,
        deferred_tool_results=DeferredToolResults(calls={call.tool_call_id: "handoff a007"}),
    )
    assert second.output == "report: handoff a007"


def test_functools_wraps_keeps_tool_description_and_schema() -> None:
    def wrap(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return {"artifact_id": "a001", "payload": fn(*args, **kwargs)}

        return wrapper

    seen: dict[str, Any] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.update({t.name: t for t in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    @wrap
    def rank_by_value(result_id: str, top_n: int = 10) -> int:
        """Rank rows of a result by metric_value."""
        return top_n

    agent.run_sync("x")

    tool = seen["rank_by_value"]
    assert tool.description == "Rank rows of a result by metric_value."
    assert sorted(tool.parameters_json_schema["properties"]) == ["result_id", "top_n"]


def test_threads_write_separate_tables_to_one_duckdb_file(tmp_path: Any) -> None:
    path = str(tmp_path / "session.duckdb")
    duckdb.connect(path).close()
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            con = duckdb.connect(path)
            con.execute(f"CREATE TABLE t_a{i:03d} AS SELECT range AS x FROM range({100 * (i + 1)})")
            row = con.execute(f"SELECT count(*) FROM t_a{i:03d}").fetchone()
            assert row is not None and row[0] == 100 * (i + 1)
            con.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    con = duckdb.connect(path)
    row = con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()
    con.close()
    assert row is not None and row[0] == 8
