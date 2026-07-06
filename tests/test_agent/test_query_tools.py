from __future__ import annotations

import datetime
import json

import pyarrow as pa
import pytest
from unittest.mock import MagicMock, patch

from aitaem.agent.store import ResultStore
from aitaem.agent.query_types import (
    QueryDeps,
    QueryOutput,
    QueryPayload,
    ComputeMetricsResult,
    RankByValueResult,
    FilterByThresholdResult,
    DistributionSummaryResult,
    PeriodOverPeriodResult,
    ContributionShareResult,
    MetricDistribution,
    ToolResult,
)
from aitaem.agent.query_tools import (
    compute_metrics,
    rank_by_value,
    filter_by_threshold,
    distribution_summary,
    period_over_period,
    contribution_share,
)
from aitaem.agent.trace import Status


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _sample_table():
    return pa.table({
        "metric_name": ["revenue"],
        "metric_value": [1000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": [None],
        "slice_type": [None],
        "slice_value": [None],
        "segment_name": [None],
        "segment_value": [None],
    })


def _make_deps():
    mock_spec = MagicMock()
    mock_spec.format = None
    mock_sc = MagicMock()
    mock_sc.metrics = {"revenue": mock_spec}
    mock_cm = MagicMock()
    store = ResultStore()
    return QueryDeps(spec_cache=mock_sc, connection_manager=mock_cm, store=store), store


def _make_mock_mc(arrow_table=None, raise_exc=None):
    """Return a mock MetricCompute; patch into query_tools.MetricCompute at call site."""
    mc = MagicMock()
    if raise_exc:
        mc.compute.side_effect = raise_exc
    else:
        mock_ibis = MagicMock()
        mock_ibis.to_pyarrow.return_value = arrow_table or _sample_table()
        mc.compute.return_value = mock_ibis
    return mc


def _make_deps_with_table(table: pa.Table):
    store = ResultStore()
    rid = store.store(table, None)
    mock_sc = MagicMock()
    mock_sc.metrics = {}
    deps = QueryDeps(spec_cache=mock_sc, connection_manager=MagicMock(), store=store)
    return deps, rid


def _multi_row_table():
    return pa.table({
        "metric_name": ["revenue"] * 5,
        "metric_value": [100.0, 300.0, 50.0, 200.0, 150.0],
        "period_type": ["all_time"] * 5,
        "period_start_date": [None] * 5,
        "period_end_date": [None] * 5,
        "entity_id": ["A", "B", "C", "D", "E"],
        "metric_format": [None] * 5,
        "slice_type": [None] * 5,
        "slice_value": [None] * 5,
        "segment_name": [None] * 5,
        "segment_value": [None] * 5,
    })


# ---------------------------------------------------------------------------
# SF-1: Pydantic contract model tests
# ---------------------------------------------------------------------------

def test_tool_result_base_payload_summary_defaults_none():
    assert ToolResult().payload_summary is None


def test_compute_metrics_result_is_tool_result():
    assert issubclass(ComputeMetricsResult, ToolResult)


def test_all_result_models_are_tool_results():
    for cls in [RankByValueResult, FilterByThresholdResult, DistributionSummaryResult,
                PeriodOverPeriodResult, ContributionShareResult]:
        assert issubclass(cls, ToolResult), f"{cls.__name__} must inherit ToolResult"


def test_query_output_frozen():
    from pydantic import ValidationError
    out = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    with pytest.raises(ValidationError):
        out.status = Status.error


def test_query_output_refused_has_reason():
    out = QueryOutput(status=Status.refused, narrative="N/A.", reason="No exact match.")
    assert out.reason == "No exact match."
    assert out.result_ids == []


def test_compute_metrics_result_error_case():
    r = ComputeMetricsResult(
        result_id="", metrics=["revenue"], slices=None, segment=None,
        row_count=0, sample=[], columns=[], period_type="all_time",
        time_window=None, by_entity=None, format_hints={}, error="SpecNotFoundError: revenue",
    )
    assert r.error is not None
    assert r.result_id == ""


def test_metric_distribution_optional_stats():
    d = MetricDistribution(metric_name="ctr", count=0)
    assert d.mean is None


# ---------------------------------------------------------------------------
# SF-2: compute_metrics tests
# ---------------------------------------------------------------------------

def test_compute_metrics_success_stores_result():
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"], period_type="all_time")
    assert result.error is None
    assert result.row_count == 1
    assert result.result_id != ""
    assert result.result_id in store.ids()
    entry = store.get(result.result_id)
    assert entry.arrow is not None
    assert entry.ibis_ref is not None


def test_compute_metrics_stores_ibis_ref():
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"], period_type="all_time")
    entry = store.get(result.result_id)
    assert entry.ibis_ref is not None


def test_compute_metrics_spec_not_found():
    from aitaem.utils.exceptions import SpecNotFoundError
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc(raise_exc=SpecNotFoundError("metric", "revenue", []))
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"])
    assert result.error is not None
    assert "SpecNotFoundError" in result.error
    assert result.result_id == ""
    assert len(store.ids()) == 0


def test_compute_metrics_format_hints():
    mock_spec = MagicMock()
    mock_spec.format = "percentage"
    mock_sc = MagicMock()
    mock_sc.metrics = {"ctr": mock_spec}
    store = ResultStore()
    deps = QueryDeps(spec_cache=mock_sc, connection_manager=MagicMock(), store=store)
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["ctr"])
    assert result.format_hints == {"ctr": "percentage"}


def test_compute_metrics_populates_payload_summary():
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"], slices=["by_country"], period_type="monthly")
    assert result.payload_summary is not None
    assert result.payload_summary["metrics_used"] == ["revenue"]
    assert result.payload_summary["slices_used"] == ["by_country"]
    assert result.payload_summary["period_type"] == "monthly"
    assert result.payload_summary["segment_used"] is None


def test_compute_metrics_sample_max_five_rows():
    big_table = pa.table({
        "metric_name": ["revenue"] * 10,
        "metric_value": [float(i) for i in range(10)],
        "period_type": ["monthly"] * 10,
        "period_start_date": [None] * 10,
        "period_end_date": [None] * 10,
        "entity_id": [None] * 10,
        "metric_format": [None] * 10,
        "slice_type": [None] * 10,
        "slice_value": [None] * 10,
        "segment_name": [None] * 10,
        "segment_value": [None] * 10,
    })
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc(arrow_table=big_table)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"])
    assert len(result.sample) <= 5


# ---------------------------------------------------------------------------
# SF-3: Analysis tool tests
# ---------------------------------------------------------------------------

def test_rank_by_value_top2_descending():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=2, ascending=False)
    assert result.total_rows == 2
    assert result.result_id in deps.store.ids()
    ranked_arrow = deps.store.get_arrow(result.result_id)
    values = ranked_arrow.column("metric_value").to_pylist()
    assert values == sorted(values, reverse=True)


def test_rank_by_value_ascending():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=3, ascending=True)
    ranked_arrow = deps.store.get_arrow(result.result_id)
    values = ranked_arrow.column("metric_value").to_pylist()
    assert values == sorted(values)


def test_filter_by_threshold_greater_than():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = filter_by_threshold(ctx, result_id=rid, threshold=150.0, op=">")
    assert result.matching_rows == 2  # 300 and 200
    assert result.predicate == "metric_value > 150.0"
    filtered_arrow = deps.store.get_arrow(result.result_id)
    values = filtered_arrow.column("metric_value").to_pylist()
    assert all(v > 150.0 for v in values)


def test_filter_by_threshold_invalid_op():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    with pytest.raises(ValueError, match="op must be one of"):
        filter_by_threshold(ctx, result_id=rid, threshold=100.0, op="between")


def test_filter_by_threshold_custom_column():
    """Filter on pct_change after period_over_period — the chaining use case."""
    pop_table = pa.table({
        "metric_name": ["revenue", "revenue"],
        "metric_value": [100.0, 120.0],
        "period_type": ["monthly", "monthly"],
        "period_start_date": [datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)],
        "period_end_date": [datetime.date(2024, 1, 31), datetime.date(2024, 2, 29)],
        "entity_id": [None, None],
        "metric_format": [None, None],
        "slice_type": [None, None],
        "slice_value": [None, None],
        "segment_name": [None, None],
        "segment_value": [None, None],
        "delta": [None, 20.0],
        "pct_change": [None, 20.0],
    })
    deps, rid = _make_deps_with_table(pop_table)
    ctx = _make_ctx(deps)
    result = filter_by_threshold(ctx, result_id=rid, threshold=10.0, op=">", column="pct_change")
    assert result.error is None
    assert result.matching_rows == 1
    assert result.predicate == "pct_change > 10.0"


def test_filter_by_threshold_missing_column_returns_error():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = filter_by_threshold(ctx, result_id=rid, threshold=0.0, column="pct_change")
    assert result.error is not None
    assert result.result_id == ""
    assert "pct_change" in result.error


def test_distribution_summary_stats():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id=rid)
    assert len(result.distributions) == 1
    dist = result.distributions[0]
    assert dist.metric_name == "revenue"
    assert dist.count == 5
    assert dist.min_val == pytest.approx(50.0)
    assert dist.max_val == pytest.approx(300.0)
    assert result.result_id in deps.store.ids()


def _time_series_table():
    return pa.table({
        "metric_name": ["revenue"] * 3,
        "metric_value": [100.0, 120.0, 150.0],
        "period_type": ["monthly"] * 3,
        "period_start_date": [
            datetime.date(2024, 1, 1),
            datetime.date(2024, 2, 1),
            datetime.date(2024, 3, 1),
        ],
        "period_end_date": [
            datetime.date(2024, 1, 31),
            datetime.date(2024, 2, 29),
            datetime.date(2024, 3, 31),
        ],
        "entity_id": [None] * 3,
        "metric_format": [None] * 3,
        "slice_type": [None] * 3,
        "slice_value": [None] * 3,
        "segment_name": [None] * 3,
        "segment_value": [None] * 3,
    })


def test_period_over_period_computes_delta():
    deps, rid = _make_deps_with_table(_time_series_table())
    ctx = _make_ctx(deps)
    result = period_over_period(ctx, result_id=rid)
    assert result.rows_computed == 3
    arrow = deps.store.get_arrow(result.result_id)
    assert "delta" in arrow.schema.names
    assert "pct_change" in arrow.schema.names
    deltas = arrow.column("delta").to_pylist()
    non_null_deltas = [d for d in deltas if d is not None]
    assert 30.0 in non_null_deltas


def test_contribution_share_sums_to_one():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = contribution_share(ctx, result_id=rid)
    arrow = deps.store.get_arrow(result.result_id)
    shares = [v for v in arrow.column("share").to_pylist() if v is not None]
    assert abs(sum(shares) - 1.0) < 1e-9
    assert result.rows == 5


def test_period_over_period_single_period_returns_error():
    """1-row all_time result has only 1 period — must return error, not NaN garbage."""
    single_row = pa.table({
        "metric_name": ["revenue"],
        "metric_value": [1000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": [None],
        "slice_type": [None],
        "slice_value": [None],
        "segment_name": [None],
        "segment_value": [None],
    })
    deps, rid = _make_deps_with_table(single_row)
    ctx = _make_ctx(deps)
    result = period_over_period(ctx, result_id=rid)
    assert result.error is not None
    assert result.result_id == ""
    assert "period" in result.error.lower()


def test_contribution_share_all_zero_returns_error():
    """All-zero metric values make shares undefined — must return error, not NaN."""
    zero_table = pa.table({
        "metric_name": ["revenue"] * 3,
        "metric_value": [0.0, 0.0, 0.0],
        "period_type": ["all_time"] * 3,
        "period_start_date": [None] * 3,
        "period_end_date": [None] * 3,
        "entity_id": ["A", "B", "C"],
        "metric_format": [None] * 3,
        "slice_type": [None] * 3,
        "slice_value": [None] * 3,
        "segment_name": [None] * 3,
        "segment_value": [None] * 3,
    })
    deps, rid = _make_deps_with_table(zero_table)
    ctx = _make_ctx(deps)
    result = contribution_share(ctx, result_id=rid)
    assert result.error is not None
    assert result.result_id == ""


def test_analysis_tool_result_id_not_source():
    """Analysis tools must create a NEW result_id, not reuse the source."""
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=3)
    assert result.result_id != rid
