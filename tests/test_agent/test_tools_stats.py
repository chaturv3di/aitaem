"""stats group, with the v2 tests of rank_by_value, filter_by_threshold, distribution_summary,
period_over_period, and contribution_share carried over to ToolResult and artifacts."""

from __future__ import annotations

import datetime

import ibis
import pandas as pd
import pyarrow as pa
import pytest

from aitaem import MetricCompute
from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools.stats import (
    contribution_share,
    distribution_summary,
    filter_by_result,
    filter_by_threshold,
    filter_by_value,
    period_over_period,
    rank_by_value,
    stats_toolset,
)
from aitaem.agent.trace import Status
from tests.test_agent.conftest import make_ctx

BY = ProducedBy(tool="test")

_EMPTY_COLUMNS = ["metric_format", "slice_type", "slice_value", "segment_name", "segment_value"]


def _multi_row_table() -> pa.Table:
    return pa.table(
        {
            "metric_name": ["revenue"] * 5,
            "metric_value": [100.0, 300.0, 50.0, 200.0, 150.0],
            "period_type": ["all_time"] * 5,
            "period_start_date": pa.array([None] * 5, pa.date32()),
            "entity_id": ["A", "B", "C", "D", "E"],
            **{c: pa.array([None] * 5, pa.string()) for c in _EMPTY_COLUMNS},
        }
    )


def _put(deps: SessionDeps, kind: str, payload: object) -> str:
    return deps.store.put(kind, payload, type_tag="t", summary="s", produced_by=BY).id  # type: ignore[arg-type]


def _monthly(deps: SessionDeps) -> ibis.Table:
    return MetricCompute(deps.spec_cache, deps.connection_manager).compute(
        metrics=["ctr", "total_revenue"],
        slices="geo",
        time_window=("2024-01-01", "2024-07-01"),
        period_type="monthly",
    )


def _by_geo(deps: SessionDeps) -> ibis.Table:
    return MetricCompute(deps.spec_cache, deps.connection_manager).compute(
        metrics=["total_revenue", "ctr"], slices="geo"
    )


def _arrow(deps: SessionDeps, artifact_id: str) -> pa.Table:
    return deps.store.read(artifact_id).to_pyarrow()


def test_toolset_names() -> None:
    assert sorted(stats_toolset.tools) == [
        "contribution_share",
        "distribution_summary",
        "filter_by_result",
        "filter_by_threshold",
        "filter_by_value",
        "period_over_period",
        "rank_by_value",
    ]


# ── rank_by_value ──────────────────────────────────────────────────────────


def test_rank_by_value_top2_descending_writes_table(deps: SessionDeps) -> None:
    rid = _put(deps, "table", _multi_row_table())
    result = rank_by_value(make_ctx(deps), rid, top_n=2)
    assert result.facts["row_count"] == 2
    ref = deps.store.get(result.artifact_id)
    assert (ref.kind, ref.type_tag, ref.parents) == ("table", "ranked", [rid])
    assert _arrow(deps, result.artifact_id)["metric_value"].to_pylist() == [300.0, 200.0]


def test_rank_by_value_ascending_on_ibis_writes_table(deps: SessionDeps) -> None:
    rid = _put(deps, "ibis", _by_geo(deps))
    result = rank_by_value(make_ctx(deps), rid, top_n=3, ascending=True)
    assert deps.store.get(result.artifact_id).kind == "table"
    values = _arrow(deps, result.artifact_id)["metric_value"].to_pylist()
    assert values == sorted(values)


@pytest.mark.parametrize("kind", ["ibis", "table"])
def test_rank_by_value_group_by_keeps_top_n_per_group(deps: SessionDeps, kind: str) -> None:
    expr = _by_geo(deps)
    rid = _put(deps, kind, expr if kind == "ibis" else expr.to_pyarrow())
    result = rank_by_value(make_ctx(deps), rid, top_n=2, group_by=["metric_name"])
    assert deps.store.get(result.artifact_id).kind == kind
    df = _arrow(deps, result.artifact_id).to_pandas()
    assert df.groupby("metric_name").size().to_dict() == {"ctr": 2, "total_revenue": 2}
    full = expr.to_pandas()
    for metric, group in df.groupby("metric_name"):
        expected = full[full.metric_name == metric].nlargest(2, "metric_value").metric_value
        assert sorted(group.metric_value) == sorted(expected)


# ── distribution_summary ───────────────────────────────────────────────────


def test_distribution_summary_default_group(deps: SessionDeps) -> None:
    rid = _put(deps, "table", _multi_row_table())
    result = distribution_summary(make_ctx(deps), rid)
    rows = _arrow(deps, result.artifact_id).to_pylist()
    assert len(rows) == 1
    assert rows[0]["count"] == 5
    assert rows[0]["mean"] == pytest.approx(160.0)
    assert rows[0]["max_val"] == "300.0"
    assert deps.store.get(result.artifact_id).type_tag == "distribution"


def test_distribution_summary_custom_group_on_ibis(deps: SessionDeps) -> None:
    rid = _put(deps, "ibis", _by_geo(deps))
    result = distribution_summary(make_ctx(deps), rid, group_by=["metric_name", "slice_type"])
    assert deps.store.get(result.artifact_id).kind == "table"
    assert result.facts["row_count"] == 4  # 2 metrics × slice types "geo" and "all"


# ── filters ────────────────────────────────────────────────────────────────


def test_filter_by_threshold_greater_than(deps: SessionDeps) -> None:
    rid = _put(deps, "table", _multi_row_table())
    result = filter_by_threshold(make_ctx(deps), rid, 150.0, ">")
    assert result.facts["row_count"] == 2
    assert result.summary.startswith("metric_value > 150.0")
    assert all(v > 150.0 for v in _arrow(deps, result.artifact_id)["metric_value"].to_pylist())


def test_filter_by_threshold_custom_column(deps: SessionDeps) -> None:
    table = _multi_row_table().append_column("pct_change", pa.array([None, 20.0, 5.0, 1.0, 30.0]))
    rid = _put(deps, "table", table)
    result = filter_by_threshold(make_ctx(deps), rid, 10.0, ">", column="pct_change")
    assert result.facts["row_count"] == 2


def test_filter_by_threshold_missing_column_is_error(deps: SessionDeps) -> None:
    rid = _put(deps, "table", _multi_row_table())
    result = filter_by_threshold(make_ctx(deps), rid, 0.0, column="pct_change")
    assert result.status == Status.error
    assert "pct_change" in result.summary


@pytest.mark.parametrize("kind", ["ibis", "table"])
def test_row_preserving_tools_keep_input_kind(deps: SessionDeps, kind: str) -> None:
    expr = _by_geo(deps)
    rid = _put(deps, kind, expr if kind == "ibis" else expr.to_pyarrow())
    ctx = make_ctx(deps)
    results = [
        filter_by_threshold(ctx, rid, 0.5),
        filter_by_value(ctx, rid, "slice_value", ["USA", "EU"]),
        contribution_share(ctx, rid),
        rank_by_value(ctx, rid, group_by=["metric_name"]),
    ]
    for r in results:
        assert r.status == Status.ok, r.summary
        assert deps.store.get(r.artifact_id).kind == kind
    assert ("row_count" in results[0].facts) is (kind == "table")


@pytest.mark.parametrize("kind", ["ibis", "table"])
def test_filter_by_value(deps: SessionDeps, kind: str) -> None:
    expr = _by_geo(deps)
    rid = _put(deps, kind, expr if kind == "ibis" else expr.to_pyarrow())
    result = filter_by_value(make_ctx(deps), rid, "slice_value", ["USA", "EU"])
    df = _arrow(deps, result.artifact_id).to_pandas()
    assert set(df.slice_value) == {"USA", "EU"}
    assert len(df) == 4


def test_filter_by_result_same_backend_stays_lazy(deps: SessionDeps) -> None:
    ctx = make_ctx(deps)
    rid = _put(deps, "ibis", _by_geo(deps))
    top = rank_by_value(ctx, rid, top_n=1, group_by=["metric_name"])  # lazy, same backend
    result = filter_by_result(ctx, rid, top.artifact_id, column="slice_value")
    ref = deps.store.get(result.artifact_id)
    assert (ref.kind, ref.parents) == ("ibis", [rid, top.artifact_id])
    kept = set(_arrow(deps, result.artifact_id)["slice_value"].to_pylist())
    assert kept == set(_arrow(deps, top.artifact_id)["slice_value"].to_pylist())


def test_filter_by_result_table_inputs(deps: SessionDeps) -> None:
    left = _put(deps, "table", _multi_row_table())
    keys = _put(deps, "table", pa.table({"entity_id": ["B", "E", "Z"]}))
    result = filter_by_result(make_ctx(deps), left, keys)
    assert deps.store.get(result.artifact_id).kind == "table"
    assert sorted(_arrow(deps, result.artifact_id)["entity_id"].to_pylist()) == ["B", "E"]


def test_filter_by_result_across_backends(deps: SessionDeps) -> None:
    other_backend = ibis.duckdb.connect()
    other_backend.create_table("keys", pa.table({"slice_value": ["APAC", "ROW"]}))
    ctx = make_ctx(deps)
    rid = _put(deps, "ibis", _by_geo(deps))
    oid = _put(deps, "ibis", other_backend.table("keys"))
    result = filter_by_result(ctx, rid, oid, column="slice_value")
    ref = deps.store.get(result.artifact_id)
    assert (ref.kind, ref.parents) == ("table", [rid, oid])
    df = _arrow(deps, result.artifact_id).to_pandas()
    assert set(df.slice_value) == {"APAC", "ROW"}
    assert len(df) == 4


def test_filter_by_result_ibis_and_table(deps: SessionDeps) -> None:
    rid = _put(deps, "ibis", _by_geo(deps))
    tid = _put(deps, "table", pa.table({"slice_value": ["USA"]}))
    result = filter_by_result(make_ctx(deps), rid, tid, column="slice_value")
    assert set(_arrow(deps, result.artifact_id)["slice_value"].to_pylist()) == {"USA"}


# ── period_over_period, contribution_share vs the v2 pandas versions ────────


def _v2_period_over_period(df: pd.DataFrame) -> pd.DataFrame:
    keys = [
        c
        for c in [
            "metric_name",
            "entity_id",
            "slice_type",
            "slice_value",
            "segment_name",
            "segment_value",
        ]
        if c in df.columns and df[c].notna().any()
    ]
    df = df.sort_values(keys + ["period_start_date"])
    df["prior_value"] = df.groupby(keys)["metric_value"].shift(1)
    df["delta"] = df["metric_value"] - df["prior_value"]
    df["pct_change"] = (df["delta"] / df["prior_value"].abs()) * 100
    return df.drop(columns=["prior_value"])


def _v2_contribution_share(df: pd.DataFrame) -> pd.DataFrame:
    total = df.groupby("metric_name")["metric_value"].transform("sum")
    df["share"] = df["metric_value"] / total.replace(0, float("nan"))
    df = df.sort_values(["metric_name", "share"], ascending=[True, False])
    df["cumulative_share"] = df.groupby("metric_name")["share"].cumsum()
    return df


def _compare(got: pd.DataFrame, expected: pd.DataFrame, keys: list[str], cols: list[str]) -> None:
    got = got.sort_values(keys).reset_index(drop=True)
    expected = expected.sort_values(keys).reset_index(drop=True)
    for c in cols:
        assert got[c].astype(float).tolist() == pytest.approx(
            expected[c].astype(float).tolist(), nan_ok=True
        )


@pytest.mark.parametrize("kind", ["ibis", "table"])
def test_period_over_period_matches_v2(deps: SessionDeps, kind: str) -> None:
    expr = _monthly(deps)
    rid = _put(deps, kind, expr if kind == "ibis" else expr.to_pyarrow())
    result = period_over_period(make_ctx(deps), rid)
    assert deps.store.get(result.artifact_id).kind == kind
    got = _arrow(deps, result.artifact_id).to_pandas()
    expected = _v2_period_over_period(expr.to_pandas())
    keys = ["metric_name", "slice_value", "period_start_date"]
    _compare(got, expected, keys, ["metric_value", "delta", "pct_change"])
    assert got.delta.notna().sum() == 2 * 5 * 5  # 2 metrics × 5 slice values × 5 later months


def test_period_over_period_computes_delta(deps: SessionDeps) -> None:
    table = pa.table(
        {
            "metric_name": ["revenue"] * 3,
            "metric_value": [100.0, 120.0, 150.0],
            "period_start_date": [datetime.date(2024, m, 1) for m in (1, 2, 3)],
            "entity_id": pa.array([None] * 3, pa.string()),
        }
    )
    result = period_over_period(make_ctx(deps), _put(deps, "table", table))
    arrow = _arrow(deps, result.artifact_id)
    assert arrow["delta"].to_pylist() == [None, 20.0, 30.0]
    assert arrow["pct_change"].to_pylist() == [None, pytest.approx(20.0), pytest.approx(25.0)]


@pytest.mark.parametrize("kind", ["ibis", "table"])
def test_contribution_share_matches_v2(deps: SessionDeps, kind: str) -> None:
    expr = _by_geo(deps).filter(lambda t: t.slice_value != "all")
    rid = _put(deps, kind, expr if kind == "ibis" else expr.to_pyarrow())
    result = contribution_share(make_ctx(deps), rid)
    got = _arrow(deps, result.artifact_id).to_pandas()
    expected = _v2_contribution_share(expr.to_pandas())
    _compare(got, expected, ["metric_name", "slice_value"], ["share", "cumulative_share"])


def test_contribution_share_sums_to_one(deps: SessionDeps) -> None:
    result = contribution_share(make_ctx(deps), _put(deps, "table", _multi_row_table()))
    arrow = _arrow(deps, result.artifact_id)
    assert sum(arrow["share"].to_pylist()) == pytest.approx(1.0)
    assert arrow["cumulative_share"].to_pylist()[-1] == pytest.approx(1.0)
    assert arrow["entity_id"].to_pylist()[0] == "B"


def test_stats_on_object_artifact_is_error(deps: SessionDeps) -> None:
    oid = _put(deps, "object", {"k": 1})
    result = rank_by_value(make_ctx(deps), oid)
    assert result.status == Status.error
    assert "takes a table or ibis artifact" in result.summary
