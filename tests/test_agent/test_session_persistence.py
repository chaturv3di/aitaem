from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse

from aitaem import ConnectionManager, SpecCache
from aitaem.agent import AitaemAgent, Status
from tests.test_agent.conftest import call, script, tool_returns
from tests.test_agent.test_session import CPC, executor_model, handoff, overrides, reply


def _round_trip(agent: AitaemAgent, **kwargs: Any) -> AitaemAgent:
    data = json.loads(json.dumps(agent.dump()))
    return AitaemAgent.load(data, model=script(), executor_model=executor_model(), **kwargs)


@pytest.fixture
def cm(ad_campaigns_connector: Any) -> ConnectionManager:
    manager = ConnectionManager()
    manager.add_connection("duckdb", connector=ad_campaigns_connector)
    return manager


@pytest.mark.asyncio
async def test_dump_and_load_mid_analysis_then_continue(
    cm: ConnectionManager, example_specs: SpecCache
) -> None:
    agent = AitaemAgent(cm, example_specs, model=script(), executor_model=executor_model())
    ids: dict[str, str] = {}

    def rank(messages: list[ModelMessage]) -> ModelResponse:
        ids["ctr"] = tool_returns(messages)[0].content.artifact_id
        return call("rank_by_value", result_id=ids["ctr"], top_n=1)

    def done(messages: list[ModelMessage]) -> ModelResponse:
        ids["top"] = tool_returns(messages)[0].content.artifact_id
        return handoff("complete", "The top region is computed.", [ids["ctr"], ids["top"]])

    analysis = script(
        call("compute_metrics", params={"metrics": ["ctr"], "slices": ["geo"]}), rank, done
    )
    main = script(
        call(
            "run_analysis",
            intake={"instructions": "top region by CTR", "params": {"metrics": ["ctr"]}},
        ),
        lambda m: reply("The top region is computed.", ids["top"]),
    )
    with overrides(agent, main_session=main, analysis_coordinator=analysis):
        first = await agent.chat("Top region by CTR?")
    assert first.status == Status.ok

    loaded = _round_trip(agent, connection_manager=cm, spec_cache=example_specs)

    assert loaded.store.get(ids["ctr"]).lost
    assert not loaded.store.get(ids["top"]).lost
    assert loaded.deps.turn == 1

    def recall(messages: list[ModelMessage]) -> ModelResponse:
        assert any("Top region by CTR?" in str(p) for m in messages for p in m.parts)
        return call("read_artifact", artifact_id=ids["top"])

    main2 = script(
        recall, lambda m: reply(tool_returns(m)[0].content.facts["rows"][0]["slice_value"])
    )
    with loaded.main_session.override(model=main2):
        second = await loaded.chat("Which one again?")
    top = loaded.store.read(ids["top"]).to_pyarrow()["slice_value"][0].as_py()
    assert second.response == top
    assert loaded.deps.turn == 2
    agent.cleanup()


@pytest.mark.asyncio
async def test_dump_and_load_mid_spec_flow_then_continue(
    cm: ConnectionManager, example_specs: SpecCache, tmp_path: Path
) -> None:
    spec_dir = tmp_path / "specs"
    agent = AitaemAgent(
        cm, example_specs, model=script(), executor_model=executor_model(), spec_dir=spec_dir
    )
    main = script(
        call(
            "start_spec_design", intake={"instructions": "Define CPC", "params": {"names": ["cpc"]}}
        )
    )
    spec = script(
        call("validate_spec", spec_type="metric", yaml_text=CPC),
        lambda m: reply("Draft ready. Save?", tool_returns(m)[0].content.artifact_id),
    )
    with overrides(agent, main_session=main, spec_coordinator=spec):
        first = await agent.chat("Define CPC")
    draft_id = first.attachments[0].artifact_id
    assert agent.route == "spec"

    loaded = _round_trip(agent, connection_manager=cm, spec_cache=example_specs, spec_dir=spec_dir)
    assert loaded.route == "spec"
    assert loaded.pending_call_id == agent.pending_call_id

    spec2 = script(
        call("commit_spec", draft_id=draft_id),
        lambda m: handoff("complete", "Saved cpc.", [tool_returns(m)[0].content.artifact_id]),
    )
    main2 = script(lambda m: reply("CPC is saved.", tool_returns(m)[0].content.artifact_id))
    with overrides(loaded, main_session=main2, spec_coordinator=spec2):
        second = await loaded.chat("Yes, save it.")

    assert second.response == "CPC is saved."
    assert second.attachments[0].type_tag == "handoff"
    assert loaded.route == "main"
    assert (spec_dir / "metrics" / "cpc.yaml").exists()
    agent.cleanup()


@pytest.mark.asyncio
async def test_removed_session_directory_loads_every_artifact_lost(
    cm: ConnectionManager, example_specs: SpecCache
) -> None:
    agent = AitaemAgent(cm, example_specs, model=script(), executor_model=executor_model())
    main = script(
        call("list_artifacts"),
        lambda m: reply("Nothing yet.", tool_returns(m)[0].content.artifact_id),
    )
    with agent.main_session.override(model=main):
        await agent.chat("What do we have?")
    data = agent.dump()
    shutil.rmtree(agent.store.session_dir)

    loaded = AitaemAgent.load(
        data, cm, example_specs, model=script(), executor_model=executor_model()
    )

    refs = loaded.store.refs()
    assert refs and all(r.lost for r in refs)
    loaded.cleanup()
