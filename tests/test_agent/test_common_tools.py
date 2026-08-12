"""Tests for aitaem/agent/common_tools.py (Plan 37, SF-2): tools and helpers
shared between QueryBot and DefinitionBot.

Moved here from test_query_tools.py (SF-1/SF-4 of Plan 36) since these are no
longer QueryBot-only. New: _execute_metric_compute tests, dependent_metrics
append-on-success-only tests, and the RunContext covariance regression test.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow as pa
import pytest
import ibis

from aitaem.agent.store import ResultStore
from aitaem.agent.query_types import QueryDeps
from aitaem.agent.definition_types import DefinitionDeps
from aitaem.agent.common_tools import (
    ToolResult,
    ColumnDistribution,
    ColumnDistributionResult,
    DistributionSummaryResult,
    _build_distribution_agg,
    _stringify_bound,
    _row_val_or_none,
    _reject_unsafe_filter,
    _execute_metric_compute,
    column_distribution,
    distribution_summary,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


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


def _make_query_deps():
    return QueryDeps(
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        store=ResultStore(),
    )


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


def _make_mock_mc(arrow_table=None, raise_exc=None):
    mc = MagicMock()
    if raise_exc:
        mc.compute.side_effect = raise_exc
    else:
        mock_ibis = MagicMock()
        mock_ibis.to_pyarrow.return_value = arrow_table or _sample_table()
        mc.compute.return_value = mock_ibis
    return mc


# ---------------------------------------------------------------------------
# Type contract tests
# ---------------------------------------------------------------------------

def test_tool_result_base_payload_summary_defaults_none():
    assert ToolResult().payload_summary is None


def test_all_result_models_are_tool_results():
    for cls in [DistributionSummaryResult, ColumnDistributionResult]:
        assert issubclass(cls, ToolResult), f"{cls.__name__} must inherit ToolResult"


def test_column_distribution_optional_stats():
    d = ColumnDistribution(group_key={"metric_name": "ctr"}, count=0)
    assert d.mean is None


# ---------------------------------------------------------------------------
# distribution_summary
# ---------------------------------------------------------------------------

def test_distribution_summary_stats():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id=rid)
    assert result.group_by == ["metric_name"]
    assert len(result.distributions) == 1
    dist = result.distributions[0]
    assert dist.group_key == {"metric_name": "revenue"}
    assert dist.count == 5
    assert dist.min_val == "50.0"
    assert dist.max_val == "300.0"
    assert result.result_id in deps.store.ids()


def test_distribution_summary_custom_group_by():
    table = pa.table({
        "metric_name": ["revenue"] * 4,
        "metric_value": [100.0, 300.0, 50.0, 200.0],
        "slice_value": ["US", "US", "IN", "IN"],
    })
    deps, rid = _make_deps_with_table(table)
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id=rid, group_by=["metric_name", "slice_value"])
    assert result.group_by == ["metric_name", "slice_value"]
    assert len(result.distributions) == 2
    group_keys = {tuple(sorted(d.group_key.items())) for d in result.distributions}
    assert (("metric_name", "revenue"), ("slice_value", "IN")) in group_keys
    assert (("metric_name", "revenue"), ("slice_value", "US")) in group_keys


def test_distribution_summary_retains_ibis_ref():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id=rid)
    entry = deps.store.get_tabular(result.result_id)
    assert entry.ibis_ref is not None


def test_distribution_summary_unknown_result_id_returns_error_not_raised():
    deps = _make_query_deps()
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id="bogus")
    assert result.error is not None
    assert result.result_id == ""
    assert result.distributions == []


def test_distribution_summary_wrong_entry_kind_hits_aitaem_error_branch():
    """store.get_tabular() on a text entry raises WrongEntryKindError (an
    AitaemError subclass) — must hit the AitaemError branch, not the generic
    Exception fallback."""
    deps = _make_query_deps()
    text_id = deps.store.store_text("not tabular", content_type="text/plain")
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id=text_id)
    assert result.error is not None
    assert "WrongEntryKindError" in result.error
    assert "Unexpected error" not in result.error


def test_distribution_summary_aggregate_failure_returns_error_not_raised():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    with patch("aitaem.agent.common_tools._build_distribution_agg", side_effect=RuntimeError("boom")):
        result = distribution_summary(ctx, result_id=rid)
    assert result.error is not None
    assert "boom" in result.error
    assert result.result_id == ""


# ---------------------------------------------------------------------------
# _build_distribution_agg / stringification helpers
# ---------------------------------------------------------------------------

def test_build_distribution_agg_numeric_ungrouped():
    t = ibis.memtable(pa.table({"v": [1.0, 2.0, 3.0, None]}))
    agg = _build_distribution_agg(t, "v", None)
    row = agg.to_pandas().iloc[0]
    assert row["count"] == 3
    assert row["null_count"] == 1
    assert row["mean"] == pytest.approx(2.0)
    assert "distinct_count" not in agg.columns


def test_build_distribution_agg_non_numeric_ungrouped():
    t = ibis.memtable(pa.table({"v": ["a", "b", "a", None]}))
    agg = _build_distribution_agg(t, "v", None)
    row = agg.to_pandas().iloc[0]
    assert row["count"] == 3
    assert row["distinct_count"] == 2
    assert "mean" not in agg.columns


def test_build_distribution_agg_grouped():
    t = ibis.memtable(pa.table({"g": ["a", "a", "b"], "v": [1.0, 2.0, 3.0]}))
    agg = _build_distribution_agg(t, "v", ["g"])
    df = agg.to_pandas().sort_values("g").reset_index(drop=True)
    assert list(df["g"]) == ["a", "b"]
    assert list(df["count"]) == [2, 1]


def test_build_distribution_agg_uses_approx_quantile_on_bigquery():
    """Regression test: exact quantile()/describe() are unimplemented for BigQuery."""
    t = ibis.memtable(pa.table({"v": [1.0, 2.0, 3.0]}))
    agg = _build_distribution_agg(t, "v", None)
    sql = ibis.to_sql(agg, dialect="bigquery").upper()
    assert "APPROX_QUANTILES" in sql
    assert "QUANTILE(" not in sql


def test_stringify_bound_none_returns_none():
    assert _stringify_bound(None) is None


def test_stringify_bound_non_temporal_uses_str():
    assert _stringify_bound(42) == "42"


def test_row_val_or_none_missing_key_returns_none():
    row = pd.Series({"count": 5})
    assert _row_val_or_none(row, "mean", float) is None


def test_row_val_or_none_nan_value_returns_none():
    row = pd.Series({"count": 5, "mean": float("nan")})
    assert _row_val_or_none(row, "mean", float) is None


def test_min_val_max_val_iso8601_not_backend_cast(ad_campaigns_connection_manager):
    """Regression test: backend-native CAST(... AS STRING) is not portable —
    min_val/max_val must be Python-side .isoformat(), not a SQL cast."""
    connector = ad_campaigns_connection_manager.get_connection("duckdb")
    table = connector.get_table("ad_campaigns")
    agg = _build_distribution_agg(table, "date", None)
    row = agg.to_pandas().iloc[0]
    min_val = _stringify_bound(row["min_val"])
    datetime.datetime.fromisoformat(min_val)  # must not raise
    assert "T" in min_val  # ISO-8601 separator; DuckDB's own CAST uses a space


# ---------------------------------------------------------------------------
# column_distribution + _reject_unsafe_filter
# ---------------------------------------------------------------------------

def _make_column_distribution_deps(ad_campaigns_connection_manager, deps_cls=QueryDeps, **extra):
    sc = MagicMock()
    rev = MagicMock()
    rev.source = "duckdb://ad_campaigns.duckdb/ad_campaigns"
    rev.timestamp_col = "date"
    sc.metrics = {"total_revenue": rev}
    return deps_cls(spec_cache=sc, connection_manager=ad_campaigns_connection_manager, store=ResultStore(), **extra)


def test_column_distribution_default_column_is_timestamp_col(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue")
    assert result.error is None
    assert result.distribution.group_key["column"] == "date"
    assert result.distribution.count == 1800


def test_column_distribution_explicit_column(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue", column="revenue")
    assert result.error is None
    assert result.distribution.mean is not None
    assert result.distribution.distinct_count is None  # numeric branch


def test_column_distribution_unknown_metric(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="bogus_metric")
    assert result.error is not None
    assert result.distribution is None
    assert result.result_id == ""


def test_column_distribution_unknown_column(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue", column="bogus_col")
    assert result.error is not None
    assert "bogus_col" in result.error


def test_column_distribution_filter_narrows_results(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    unfiltered = column_distribution(ctx, metric_name="total_revenue", column="revenue")
    filtered = column_distribution(
        ctx, metric_name="total_revenue", column="revenue", filter="revenue > 1000"
    )
    assert filtered.error is None
    assert filtered.distribution.count < unfiltered.distribution.count


def test_column_distribution_malformed_filter_returns_error_not_raised(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue", filter="x IN (SELECT y FROM dim_platforms)")
    assert result.error is not None
    assert result.result_id == ""


def test_column_distribution_no_column_no_timestamp_col_errors(ad_campaigns_connection_manager):
    sc = MagicMock()
    rev = MagicMock()
    rev.source = "duckdb://ad_campaigns.duckdb/ad_campaigns"
    rev.timestamp_col = ""
    sc.metrics = {"total_revenue": rev}
    deps = QueryDeps(spec_cache=sc, connection_manager=ad_campaigns_connection_manager, store=ResultStore())
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue")
    assert result.error is not None
    assert "timestamp_col" in result.error


def test_column_distribution_connection_resolution_failure_returns_error(ad_campaigns_connection_manager):
    sc = MagicMock()
    rev = MagicMock()
    rev.source = "bigquery://no-such-project/no_such_dataset.no_such_table"
    rev.timestamp_col = "date"
    sc.metrics = {"total_revenue": rev}
    deps = QueryDeps(spec_cache=sc, connection_manager=ad_campaigns_connection_manager, store=ResultStore())
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue")
    assert result.error is not None
    assert result.result_id == ""


def test_column_distribution_filter_references_unknown_column_returns_error(ad_campaigns_connection_manager):
    """The filter parses as valid SQL but fails at execution (unknown column) — caught, not raised."""
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(
        ctx, metric_name="total_revenue", column="revenue", filter="nonexistent_col > 5"
    )
    assert result.error is not None
    assert result.result_id == ""


def test_column_distribution_aggregate_execution_failure_returns_error(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    with patch("aitaem.agent.common_tools._build_distribution_agg", side_effect=RuntimeError("boom")):
        result = column_distribution(ctx, metric_name="total_revenue")
    assert result.error is not None
    assert "boom" in result.error
    assert result.result_id == ""


def test_column_distribution_stores_min_max_metadata(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue")
    entry = deps.store.get_tabular(result.result_id)
    assert entry.metadata["metric_name"] == "total_revenue"
    assert entry.metadata["min_val"] == result.distribution.min_val
    assert entry.metadata["max_val"] == result.distribution.max_val


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery", "postgres"])
@pytest.mark.parametrize("bad_filter", [
    "x IN (SELECT y FROM other)",
    "EXISTS (SELECT 1 FROM other WHERE other.id = t.id)",
    "x > (SELECT MAX(y) FROM other)",
])
def test_reject_unsafe_filter_rejects_subqueries(dialect, bad_filter):
    with pytest.raises(ValueError, match="subquer"):
        _reject_unsafe_filter(bad_filter, dialect)


def test_reject_unsafe_filter_rejects_statement_stacking():
    with pytest.raises(ValueError):
        _reject_unsafe_filter("1=1; DROP TABLE x", "duckdb")


def test_reject_unsafe_filter_rejects_paren_breakout():
    with pytest.raises(ValueError):
        _reject_unsafe_filter("1=1) UNION SELECT * FROM x --", "duckdb")


def test_reject_unsafe_filter_accepts_legitimate_multi_condition_filter():
    result = _reject_unsafe_filter("order_value > 1000 AND (country = 'US' OR country = 'IN')", "duckdb")
    assert "1000" in result
    assert "US" in result


def test_reject_unsafe_filter_neutralizes_comment_truncation():
    """The returned (regenerated) SQL, not the raw input, must be spliced —
    only the regenerated form re-serializes the trailing text as an inert
    block comment."""
    result = _reject_unsafe_filter("1=1 -- ' AND x", "duckdb")
    assert "/*" in result and "*/" in result


# ---------------------------------------------------------------------------
# SF-2: column_distribution — dependent_metrics (DefinitionDeps only)
# ---------------------------------------------------------------------------

def test_column_distribution_appends_dependent_metrics_on_success(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager, deps_cls=DefinitionDeps)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue")
    assert result.error is None
    assert deps.dependent_metrics == ["total_revenue"]


def test_column_distribution_dependent_metrics_deduped(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager, deps_cls=DefinitionDeps)
    ctx = _make_ctx(deps)
    column_distribution(ctx, metric_name="total_revenue")
    column_distribution(ctx, metric_name="total_revenue", column="revenue")
    assert deps.dependent_metrics == ["total_revenue"]


def test_column_distribution_unknown_metric_leaves_dependent_metrics_unmodified(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager, deps_cls=DefinitionDeps)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="bogus_metric")
    assert result.error is not None
    assert deps.dependent_metrics == []


def test_column_distribution_unknown_column_leaves_dependent_metrics_unmodified(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager, deps_cls=DefinitionDeps)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue", column="bogus_col")
    assert result.error is not None
    assert deps.dependent_metrics == []


def test_column_distribution_rejected_filter_leaves_dependent_metrics_unmodified(ad_campaigns_connection_manager):
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager, deps_cls=DefinitionDeps)
    ctx = _make_ctx(deps)
    result = column_distribution(
        ctx, metric_name="total_revenue", filter="x IN (SELECT y FROM dim_platforms)"
    )
    assert result.error is not None
    assert deps.dependent_metrics == []


def test_column_distribution_query_deps_without_dependent_metrics_field_is_noop(ad_campaigns_connection_manager):
    """QueryDeps has no dependent_metrics field — getattr-based guard must no-op, not raise."""
    deps = _make_column_distribution_deps(ad_campaigns_connection_manager)
    ctx = _make_ctx(deps)
    result = column_distribution(ctx, metric_name="total_revenue")
    assert result.error is None
    assert not hasattr(deps, "dependent_metrics")


# ---------------------------------------------------------------------------
# SF-2: _execute_metric_compute
# ---------------------------------------------------------------------------

def test_execute_metric_compute_success():
    sc = _make_spec_cache()
    store = ResultStore()
    with patch("aitaem.agent.common_tools.MetricCompute", return_value=_make_mock_mc()):
        result_id, row_count, sample, columns, format_hints = _execute_metric_compute(
            sc, MagicMock(), store, "revenue", [], None, None, "all_time", None,
        )
    assert result_id in store.ids()
    assert row_count == 1
    assert columns
    assert format_hints == {}


def test_execute_metric_compute_format_hints():
    sc = _make_spec_cache()
    sc.metrics["revenue"].format = "currency:USD"
    store = ResultStore()
    with patch("aitaem.agent.common_tools.MetricCompute", return_value=_make_mock_mc()):
        _, _, _, _, format_hints = _execute_metric_compute(
            sc, MagicMock(), store, "revenue", [], None, None, "all_time", None,
        )
    assert format_hints == {"revenue": "currency:USD"}


def test_execute_metric_compute_raises_on_failure():
    """No internal try/except — callers apply their own exception handling."""
    from aitaem.utils.exceptions import SpecNotFoundError

    sc = _make_spec_cache()
    store = ResultStore()
    mc = MagicMock()
    mc.compute.side_effect = SpecNotFoundError("metric", "revenue", [])
    with patch("aitaem.agent.common_tools.MetricCompute", return_value=mc):
        with pytest.raises(SpecNotFoundError):
            _execute_metric_compute(
                sc, MagicMock(), store, "revenue", [], None, None, "all_time", None,
            )


def test_execute_metric_compute_ibis_ref_stored():
    sc = _make_spec_cache()
    store = ResultStore()
    with patch("aitaem.agent.common_tools.MetricCompute", return_value=_make_mock_mc()):
        result_id, *_ = _execute_metric_compute(
            sc, MagicMock(), store, "revenue", [], None, None, "all_time", None,
        )
    entry = store.get_tabular(result_id)
    assert entry.ibis_ref is not None


# ---------------------------------------------------------------------------
# SF-2: RunContext covariance regression test
# ---------------------------------------------------------------------------

_COVARIANCE_FIXTURE = textwrap.dedent(
    """
    from __future__ import annotations

    from typing import Any, Protocol

    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import FunctionToolset


    class SharedDeps(Protocol):
        spec_cache: Any
        connection_manager: Any
        store: Any


    class DepsA:
        spec_cache: Any
        connection_manager: Any
        store: Any

    class DepsB:
        spec_cache: Any
        connection_manager: Any
        store: Any


    def shared_tool(ctx: RunContext[SharedDeps], x: int) -> int:
        return x


    toolset_a: FunctionToolset[DepsA] = FunctionToolset()
    toolset_a.add_function(shared_tool)

    toolset_b: FunctionToolset[DepsB] = FunctionToolset()
    toolset_b.add_function(shared_tool)
    """
)


def test_shared_tool_ctx_typechecks_on_both_deps(tmp_path):
    """Regression guard for the RunContext covariance assumption this module's
    SharedToolDeps typing relies on (see the "Typing the shared tools' ctx"
    key decision in Plan 37). If a future pydantic-ai release makes RunContext's
    deps parameter invariant/contravariant, this fixture fails to type-check
    and this test catches it here, isolated from the rest of CI's mypy run.
    """
    pytest.importorskip("mypy")

    fixture_path = tmp_path / "covariance_fixture.py"
    fixture_path.write_text(_COVARIANCE_FIXTURE)

    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-error-summary", str(fixture_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"mypy stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
