"""examples/01_aitaem_agent_example.py runs end to end against a DuckDB fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_ai.messages import ModelMessage, ModelResponse

from aitaem import ConnectionManager
from aitaem.agent import Status
from tests.evals._fixtures import call, document, prompt, reply, returns, role_model

ROOT = Path(__file__).parents[2]


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location(
        "aitaem_agent_example", ROOT / "examples/01_aitaem_agent_example.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model() -> Any:
    ids: dict[str, str] = {}

    def executor(messages: list[ModelMessage]) -> ModelResponse:
        if not returns(messages):
            (combo,) = yaml.safe_load(prompt(messages))["params"]["combos"]
            return call("find_specs", spec_type="metric", query=combo["metric"])
        if returns(messages)[0].facts.get("matches"):
            return call("check_compatibility", metrics=["ctr"], slices=["geo"])
        return document(
            {
                "outcome": "complete",
                "response": "metric: ctr; slices: [geo]",
                "result_ids": [returns(messages)[0].artifact_id],
            }
        )

    def rank(messages: list[ModelMessage]) -> ModelResponse:
        ids["ctr"] = returns(messages)[0].artifact_id
        return call("rank_by_value", result_id=ids["ctr"], top_n=1)

    def answer(messages: list[ModelMessage]) -> ModelResponse:
        (row,) = returns(messages)[0].facts["rows"]
        return document(
            {
                "outcome": "complete",
                "response": f"{row['slice_value']} leads.",
                "result_ids": [ids["ctr"], returns(messages)[0].artifact_id],
            }
        )

    combos = [
        {
            "instructions": "click-through rate by region",
            "params": {"combos": [{"metric": "click-through rate", "slices": ["region"]}]},
        }
    ]
    intake = {"instructions": "Highest CTR region", "params": {"metrics": ["ctr"]}}
    return role_model(
        main=[
            call("run_analysis", intake=intake),
            lambda m: reply(returns(m)[0].facts["response"], *returns(m)[0].facts["result_ids"]),
            lambda m: call("rank_by_value", result_id=ids["ctr"], top_n=1, ascending=True),
            lambda m: reply("ROW trails.", returns(m)[0].artifact_id),
        ],
        analysis=[
            call("run_analysis_executors", intakes=combos),
            call("compute_metrics", params={"metrics": ["ctr"], "slices": ["geo"]}),
            rank,
            lambda m: call("read_artifact", artifact_id=returns(m)[0].artifact_id),
            answer,
        ],
        analysis_executor=[executor] * 3,
    )


@pytest.mark.asyncio
async def test_example_runs(
    ad_campaigns_connector: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    example = _load_example()
    cm = ConnectionManager()
    cm.add_connection("duckdb", connector=ad_campaigns_connector)
    model = _model()
    agent = example.build_agent(cm, model=model, executor_model=model)
    try:
        first, second = await example.main(agent)
    finally:
        agent.cleanup()

    assert first.status == second.status == Status.ok
    assert first.response.endswith("leads.")
    assert {a.type_tag for a in first.attachments} == {"metric_result", "read"}
    out = capsys.readouterr().out
    assert "analysis_executor:" in out and "main_session:" in out
