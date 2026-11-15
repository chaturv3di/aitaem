from __future__ import annotations

import pyarrow as pa
import pytest

from aitaem import MetricCompute
from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools.artifacts import (
    artifact_toolset,
    describe_artifact,
    list_artifacts,
    read_artifact,
)
from aitaem.agent.trace import Status
from tests.test_agent.conftest import make_ctx

BY = ProducedBy(tool="test")


@pytest.fixture
def seeded(deps: SessionDeps) -> SessionDeps:
    store = deps.store
    store.put("object", {"k": 1}, type_tag="spec", summary="obj", produced_by=BY)  # a001
    store.put("file", "outcome: complete\n", type_tag="handoff", summary="doc", produced_by=BY)
    store.put(
        "table",
        pa.table({"g": ["a", "b", "c"], "v": [1.0, 2.0, 3.0]}),
        type_tag="ranked",
        summary="tbl",
        produced_by=BY,
    )  # a003
    expr = MetricCompute(deps.spec_cache, deps.connection_manager).compute(
        metrics="ctr", slices="campaign_type"
    )
    store.put("ibis", expr, type_tag="metric_result", summary="ctr", produced_by=BY)  # a004
    return deps


def test_toolset_names() -> None:
    assert sorted(artifact_toolset.tools) == [
        "describe_artifact",
        "list_artifacts",
        "read_artifact",
    ]


def test_list_artifacts(seeded: SessionDeps) -> None:
    ctx = make_ctx(seeded)
    every = list_artifacts(ctx)
    assert every.facts["artifacts"][0] == 'a001 object spec ok "obj"'
    assert len(every.facts["artifacts"]) == 4
    handoffs = list_artifacts(ctx, type_tag="handoff")
    assert handoffs.facts["artifacts"] == ['a002 file handoff ok "doc"']


@pytest.mark.parametrize(
    ("artifact_id", "columns"),
    [("a001", None), ("a002", None), ("a003", {"g": "string", "v": "float64"})],
)
def test_describe_artifact(seeded: SessionDeps, artifact_id: str, columns: object) -> None:
    result = describe_artifact(make_ctx(seeded), artifact_id)
    assert result.status == Status.ok
    assert result.facts["id"] == artifact_id
    assert result.facts.get("columns") == columns
    assert seeded.store.get(result.artifact_id).parents == [artifact_id]


def test_describe_ibis_artifact_lists_columns(seeded: SessionDeps) -> None:
    result = describe_artifact(make_ctx(seeded), "a004")
    assert {"metric_name", "metric_value", "slice_value"} <= set(result.facts["columns"])


def test_read_object_and_file(seeded: SessionDeps) -> None:
    ctx = make_ctx(seeded)
    assert read_artifact(ctx, "a001").facts["content"] == {"k": 1}
    assert read_artifact(ctx, "a002").facts["content"] == "outcome: complete\n"


def test_read_table_selects_columns_and_rows(seeded: SessionDeps) -> None:
    ctx = make_ctx(seeded)
    first = read_artifact(ctx, "a003", columns=["v"], n=2)
    assert first.facts == {"columns": ["v"], "rows": [{"v": 1.0}, {"v": 2.0}]}
    every = read_artifact(ctx, "a003", n=None)
    assert len(every.facts["rows"]) == 3
    stored = seeded.store.get(every.artifact_id)
    assert (stored.kind, stored.type_tag, stored.parents) == ("table", "read", ["a003"])


def test_read_ibis_runs_on_backend(seeded: SessionDeps) -> None:
    ctx = make_ctx(seeded)
    every = read_artifact(ctx, "a004", columns=["slice_value", "metric_value"], n=None)
    assert every.facts["columns"] == ["slice_value", "metric_value"]
    assert len(every.facts["rows"]) == 5  # four campaign types and "all"
    assert len(read_artifact(ctx, "a004").facts["rows"]) == 5
    assert len(read_artifact(ctx, "a004", n=1).facts["rows"]) == 1


def test_read_unknown_column_is_error(seeded: SessionDeps) -> None:
    result = read_artifact(make_ctx(seeded), "a003", columns=["nope"])
    assert result.status == Status.error
