"""Turn traces and OTel spans across the main session, coordinators, and executors."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aitaem import ConnectionManager, SpecCache
from aitaem.agent import AitaemAgent
from tests.test_agent.conftest import call, script, tool_returns
from tests.test_agent.test_session import executor_model, handoff, overrides, reply

_EXPORTER = InMemorySpanExporter()


def _install_provider() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)  # the global provider can be set once per process


_install_provider()


@pytest.fixture
def agent(ad_campaigns_connector: Any, example_specs: SpecCache) -> Iterator[AitaemAgent]:
    cm = ConnectionManager()
    cm.add_connection("duckdb", connector=ad_campaigns_connector)
    session = AitaemAgent(cm, example_specs, model=script(), executor_model=executor_model())
    yield session
    session.cleanup()


async def _analysis_turn(agent: AitaemAgent) -> Any:
    combos = [
        {"instructions": f"find {m}", "params": {"combos": [{"metric": m, "slices": ["region"]}]}}
        for m in ("click rate", "conversion rate")
    ]
    analysis = script(
        call("run_analysis_executors", intakes=combos),
        call("compute_metrics", params={"metrics": ["ctr"], "slices": ["geo"]}),
        lambda m: handoff("complete", "CTR by geo.", [tool_returns(m)[0].content.artifact_id]),
    )
    main = script(
        call(
            "run_analysis", intake={"instructions": "CTR by region", "params": {"metrics": ["ctr"]}}
        ),
        lambda m: reply("CTR by geo.", *tool_returns(m)[0].content.facts["result_ids"]),
    )
    with overrides(agent, main_session=main, analysis_coordinator=analysis):
        return await agent.chat("CTR by region?")


@pytest.mark.asyncio
async def test_turn_trace_covers_every_run(agent: AitaemAgent) -> None:
    response = await _analysis_turn(agent)
    turn = agent.trace(response.trace_id)

    names = [r.name for r in turn.runs]
    assert sorted(names) == sorted(
        ["analysis_executor", "analysis_executor", "analysis_coordinator", "main_session"]
    )
    assert names[-1] == "main_session"
    assert turn.usage.requests == sum(r.usage.requests for r in turn.runs) == 2 + 3 + 2 * 2
    executor_calls = [
        c.name for r in turn.runs if r.name == "analysis_executor" for c in r.tool_calls
    ]
    assert executor_calls == ["check_compatibility", "check_compatibility"]
    assert len(turn.tool_calls) == sum(len(r.tool_calls) for r in turn.runs) == 1 + 2 + 2


@pytest.mark.asyncio
async def test_every_tool_call_names_an_artifact_in_the_manifest(agent: AitaemAgent) -> None:
    response = await _analysis_turn(agent)
    turn = agent.trace(response.trace_id)
    manifest = {line.split('"id":"')[1].split('"')[0] for line in agent.store.manifest_records()}
    assert turn.tool_calls
    for tool_call in turn.tool_calls:
        assert tool_call.artifact_id in manifest, tool_call.name


@pytest.mark.asyncio
async def test_executor_spans_nest_under_the_fan_out_tool_span(agent: AitaemAgent) -> None:
    _EXPORTER.clear()
    await _analysis_turn(agent)
    finished = list(_EXPORTER.get_finished_spans())
    by_id = {s.context.span_id: s for s in finished}

    def ancestors(span: ReadableSpan) -> list[ReadableSpan]:
        chain = []
        while span.parent is not None and span.parent.span_id in by_id:
            span = by_id[span.parent.span_id]
            chain.append(span)
        return chain

    executor_runs = [s for s in finished if s.name == "invoke_agent analysis_executor"]
    assert len(executor_runs) == 2
    for run in executor_runs:
        chain = [s.name for s in ancestors(run)]
        assert chain[0] == "execute_tool run_analysis_executors"
        assert "invoke_agent analysis_coordinator" in chain
        assert chain[-1] == "invoke_agent main_session"
