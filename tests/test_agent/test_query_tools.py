from __future__ import annotations

import datetime

import pyarrow as pa
import pytest
from unittest.mock import MagicMock, patch

from aitaem.agent.store import ResultStore
from aitaem.agent.query_types import (
    QueryDeps,
    QueryOutput,
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
    record_intent,
    resolve_intent,
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


def _make_spec_cache():
    sc = MagicMock()
    rev = MagicMock()
    rev.entities = ["user_id"]
    rev.timestamp_col = "ts"
    rev.format = None
    sc.metrics = {"revenue": rev}
    sc.slices = {"by_country": MagicMock()}
    sc.segments = {"by_advertiser": MagicMock()}
    return sc


def _make_deps():
    return QueryDeps(
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        store=ResultStore(),
    )


def _make_mock_mc(arrow_table=None, raise_exc=None):
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
    rid = store.store_tabular(table, None)
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


def _setup_resolved_token(deps, ctx):
    """Record + resolve 'revenue' and return the minted spec_token."""
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    return result.exact_match.spec_token


# ---------------------------------------------------------------------------
# Type contract tests
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


def test_metric_distribution_optional_stats():
    d = MetricDistribution(metric_name="ctr", count=0)
    assert d.mean is None


# ---------------------------------------------------------------------------
# SF-3: record_intent tests
# ---------------------------------------------------------------------------

def test_record_intent_appends_to_deps():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    assert len(deps.intents) == 1
    assert deps.intents[0].metric_concept == "revenue"


def test_record_intent_returns_index():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    r0 = record_intent(ctx, metric_concept="revenue", scope="overall")
    r1 = record_intent(ctx, metric_concept="ctr", scope="overall")
    assert r0.intent_id == 0
    assert r1.intent_id == 1
    assert len(deps.intents) == 2


def test_record_intent_stores_time_window():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(
        ctx, metric_concept="revenue", scope="overall",
        period_type="monthly", time_window=("2024-01-01", "2024-03-31"),
    )
    assert deps.intents[0].time_window == ("2024-01-01", "2024-03-31")
    assert deps.intents[0].period_type == "monthly"


def test_record_intent_scope_subset():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="subset", slice_type="by_country")
    assert deps.intents[0].scope == "subset"
    assert deps.intents[0].slice_type == "by_country"


# ---------------------------------------------------------------------------
# SF-4: resolve_intent tests
# ---------------------------------------------------------------------------

def test_resolve_intent_exact_match_mints_token():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    assert result.exact_match is not None
    assert result.exact_match.spec_token.startswith("sm_")
    assert len(result.near_misses) == 0


def test_resolve_intent_token_stored_in_registry():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    token = result.exact_match.spec_token
    assert token in ctx.deps.spec_registry
    assert ctx.deps.spec_registry[token].metric_name == "revenue"


def test_resolve_intent_near_miss_unknown_slice():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue", slices=["by_platform"])
    assert result.exact_match is None
    assert any(nm.why_not == "unknown_slice" for nm in result.near_misses)


def test_resolve_intent_with_valid_slice():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue", slices=["by_country"])
    assert result.exact_match is not None
    assert result.exact_match.slices == ["by_country"]


def test_resolve_intent_invalid_intent_id():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    assert result.exact_match is None


def test_resolve_intent_multiple_intents_correct_index():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="ctr", scope="overall")
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=1, metric_name="revenue")
    assert result.exact_match is not None


def test_resolve_intent_each_token_unique():
    """Two valid resolve calls must produce different tokens."""
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    record_intent(ctx, metric_concept="revenue", scope="overall")
    r1 = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    r2 = resolve_intent(ctx, intent_id=1, metric_name="revenue")
    assert r1.exact_match.spec_token != r2.exact_match.spec_token


# ---------------------------------------------------------------------------
# SF-5: compute_metrics (token-based) tests
# ---------------------------------------------------------------------------

def test_compute_metrics_success_via_token():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_make_mock_mc()):
        result = compute_metrics(ctx, spec_token=token)
    assert result.error is None
    assert result.result_id != ""
    assert result.row_count == 1
    assert result.result_id in deps.store.ids()


def test_compute_metrics_unknown_token():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    result = compute_metrics(ctx, spec_token="sm_bogus")
    assert result.error is not None
    assert "already consumed" in result.error
    assert result.result_id == ""


def test_compute_metrics_token_consumed_on_use():
    """Second call with the same spec_token returns an error (pop-on-consume)."""
    deps = _make_deps()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_make_mock_mc()):
        r1 = compute_metrics(ctx, spec_token=token)
        r2 = compute_metrics(ctx, spec_token=token)
    assert r1.error is None
    assert r2.error is not None
    assert "already consumed" in r2.error


def test_compute_metrics_spec_not_found():
    from aitaem.utils.exceptions import SpecNotFoundError
    deps = _make_deps()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    mc = MagicMock()
    mc.compute.side_effect = SpecNotFoundError("metric", "revenue", [])
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mc):
        result = compute_metrics(ctx, spec_token=token)
    assert result.error is not None
    assert "SpecNotFoundError" in result.error


def test_compute_metrics_payload_summary_from_resolved_spec():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_make_mock_mc()):
        result = compute_metrics(ctx, spec_token=token)
    assert result.payload_summary["metrics_used"] == ["revenue"]


def test_compute_metrics_ibis_ref_stored():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_make_mock_mc()):
        result = compute_metrics(ctx, spec_token=token)
    entry = deps.store.get_tabular(result.result_id)
    assert entry.ibis_ref is not None


def test_compute_metrics_spec_token_in_result():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_make_mock_mc()):
        result = compute_metrics(ctx, spec_token=token)
    assert result.spec_token == token


def test_compute_metrics_format_hints():
    sc = _make_spec_cache()
    sc.metrics["revenue"].format = "currency:USD"
    deps = QueryDeps(spec_cache=sc, connection_manager=MagicMock(), store=ResultStore())
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_make_mock_mc()):
        result = compute_metrics(ctx, spec_token=token)
    assert result.format_hints == {"revenue": "currency:USD"}


# ---------------------------------------------------------------------------
# Analysis tool tests (unchanged from Phase 2)
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
    assert result.matching_rows == 2
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
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=3)
    assert result.result_id != rid
