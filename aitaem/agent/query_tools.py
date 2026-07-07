from __future__ import annotations

import operator
from typing import Any

import ibis
import pyarrow as pa
from pydantic_ai import RunContext

from aitaem.query.builder import PeriodType
from aitaem.agent.query_types import (
    QueryDeps,
    ComputeMetricsResult,
    RankByValueResult,
    FilterByThresholdResult,
    DistributionSummaryResult,
    MetricDistribution,
    PeriodOverPeriodResult,
    ContributionShareResult,
)
from aitaem.agent.store import ResultEntry
from aitaem import MetricCompute
from aitaem.utils.exceptions import (
    AitaemConnectionError,
    QueryBuildError,
    QueryExecutionError,
    SpecNotFoundError,
)

# Standard columns emitted by MetricCompute.compute().
_STANDARD_COLS = [
    "period_type", "period_start_date", "period_end_date", "entity_id",
    "metric_name", "metric_format", "slice_type", "slice_value",
    "segment_name", "segment_value", "metric_value",
]
_NON_VALUE_COLS = frozenset(_STANDARD_COLS) - {"metric_value"}

_FILTER_OPS: dict[str, Any] = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq, "!=": operator.ne,
}


def _get_ibis_table(entry: ResultEntry) -> ibis.Table:
    """Return an ibis.Table: lazy from ibis_ref if alive, else memtable over Arrow."""
    if entry.ibis_ref is not None:
        return entry.ibis_ref
    if entry.arrow is not None:
        return ibis.memtable(entry.arrow)
    raise ValueError(f"Result entry {entry.id!r} has no data.")


def _sample_arrow(table: pa.Table, n: int = 5) -> list[dict[str, Any]]:
    """Return up to n rows as a list of dicts with Python-native values."""
    sliced = table.slice(0, n)
    if sliced.num_rows == 0:
        return []
    return [
        {
            col: (v.as_py() if hasattr(v, "as_py") else v)
            for col, v in zip(sliced.column_names, row)
        }
        for row in zip(*[sliced.column(c) for c in sliced.column_names])
    ]


def compute_metrics(
    ctx: RunContext[QueryDeps],
    metrics: list[str],
    slices: list[str] | None = None,
    segment: str | None = None,
    time_window: tuple[str, str] | None = None,
    period_type: PeriodType = "all_time",
    by_entity: str | None = None,
) -> ComputeMetricsResult:
    """Compute one or more metrics from the spec catalog.

    Args:
        metrics: One or more metric names. Must exactly match names in the catalog.
        slices: Optional slice names to break the metric down by. Each slice
            produces additional rows in the result.
        segment: Optional segment name for entity-level segmentation. Only one
            segment per call is supported.
        time_window: (start_date, end_date) as ISO-8601 strings (e.g.
            ("2024-01-01", "2024-03-31")). Required when period_type is not
            "all_time". Requires timestamp_col to be set on each metric spec.
        period_type: Granularity for time grouping. One of: "all_time",
            "hourly", "daily", "weekly", "monthly", "yearly".
        by_entity: Column name for entity-level grouping. Each metric must
            list this column in its entities field.

    Returns:
        ComputeMetricsResult with result_id pointing to the stored artifact.
        On failure, result_id is "" and error contains the exception message.
    """
    try:
        mc = MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)
        ibis_table = mc.compute(
            metrics=metrics,
            slices=slices,
            segments=segment,
            time_window=(time_window[0], time_window[1]) if time_window else None,
            period_type=period_type,
            by_entity=by_entity,
        )
        arrow_table = ibis_table.to_pyarrow()
        result_id = ctx.deps.store.store(arrow_table, ibis_table)

        format_hints: dict[str, str] = {}
        for m in metrics:
            spec = ctx.deps.spec_cache.metrics.get(m)
            if spec and spec.format:
                format_hints[m] = spec.format

        return ComputeMetricsResult(
            result_id=result_id,
            metrics=metrics,
            slices=slices,
            segment=segment,
            row_count=len(arrow_table),
            sample=_sample_arrow(arrow_table),
            columns=arrow_table.schema.names,
            period_type=period_type,
            time_window=(time_window[0], time_window[1]) if time_window else None,
            by_entity=by_entity,
            format_hints=format_hints,
            payload_summary={
                "metrics_used": metrics,
                "slices_used": slices or [],
                "segment_used": segment,
                "period_type": period_type,
                "time_window": list(time_window) if time_window else None,
                "by_entity": by_entity,
                "format_hints": format_hints,
            },
        )

    except (SpecNotFoundError, QueryBuildError, QueryExecutionError, AitaemConnectionError) as exc:
        return ComputeMetricsResult(
            result_id="",
            metrics=metrics,
            slices=slices,
            segment=segment,
            row_count=0,
            sample=[],
            columns=[],
            period_type=period_type,
            time_window=(time_window[0], time_window[1]) if time_window else None,
            by_entity=by_entity,
            format_hints={},
            error=f"{type(exc).__name__}: {exc}",
        )


def rank_by_value(
    ctx: RunContext[QueryDeps],
    result_id: str,
    top_n: int = 10,
    ascending: bool = False,
) -> RankByValueResult:
    """Rank rows in a prior result by metric_value and return the top N.

    Args:
        result_id: ID of the result store entry to rank.
        top_n: Number of rows to return.
        ascending: If True, return lowest values first (default: highest first).

    Returns:
        RankByValueResult with a new result_id for the ranked slice.
    """
    entry = ctx.deps.store.get(result_id)
    ibis_table = _get_ibis_table(entry)

    order_fn = ibis.asc if ascending else ibis.desc
    ranked = ibis_table.order_by(order_fn("metric_value")).limit(top_n)
    result_arrow = ranked.to_pyarrow()
    new_id = ctx.deps.store.store(result_arrow, None)

    return RankByValueResult(
        result_id=new_id,
        top_rows=_sample_arrow(result_arrow, n=top_n),
        total_rows=len(result_arrow),
        ascending=ascending,
    )


def filter_by_threshold(
    ctx: RunContext[QueryDeps],
    result_id: str,
    threshold: float,
    op: str = ">",
    column: str = "metric_value",
) -> FilterByThresholdResult:
    """Filter rows in a prior result by a threshold on a numeric column.

    Args:
        result_id: ID of the result store entry to filter.
        threshold: Numeric threshold for the comparison.
        op: Comparison operator. One of: ">", ">=", "<", "<=", "==", "!=".
        column: Column to apply the threshold to. Defaults to "metric_value".
            Use "pct_change" or "delta" to filter on period_over_period output.

    Returns:
        FilterByThresholdResult with a new result_id for the filtered rows.
    """
    if op not in _FILTER_OPS:
        raise ValueError(f"op must be one of {list(_FILTER_OPS)}; got {op!r}")

    entry = ctx.deps.store.get(result_id)
    ibis_table = _get_ibis_table(entry)

    if column not in ibis_table.columns:
        return FilterByThresholdResult(
            result_id="",
            matching_rows=0,
            total_rows=entry.arrow.num_rows if entry.arrow is not None else 0,
            sample=[],
            predicate=f"{column} {op} {threshold}",
            error=f"Column {column!r} not found. Available columns: {ibis_table.columns}",
        )

    filtered = ibis_table.filter(_FILTER_OPS[op](ibis_table[column], threshold))
    result_arrow = filtered.to_pyarrow()
    new_id = ctx.deps.store.store(result_arrow, None)

    return FilterByThresholdResult(
        result_id=new_id,
        matching_rows=len(result_arrow),
        total_rows=entry.arrow.num_rows if entry.arrow is not None else 0,
        sample=_sample_arrow(result_arrow),
        predicate=f"{column} {op} {threshold}",
    )


def distribution_summary(
    ctx: RunContext[QueryDeps],
    result_id: str,
) -> DistributionSummaryResult:
    """Compute distribution statistics (mean, std, percentiles) over metric_value.

    Statistics are computed per unique metric_name in the result.
    The result store entry contains one row per metric_name with the stats columns.

    Args:
        result_id: ID of the result store entry to summarize.

    Returns:
        DistributionSummaryResult with per-metric statistics.
    """
    entry = ctx.deps.store.get(result_id)
    df = _get_ibis_table(entry).to_pandas()

    distributions: list[MetricDistribution] = []
    for metric_name, group in df.groupby("metric_name"):
        vals = group["metric_value"].dropna()
        if vals.empty:
            distributions.append(MetricDistribution(metric_name=str(metric_name), count=0))
            continue
        distributions.append(MetricDistribution(
            metric_name=str(metric_name),
            count=int(len(vals)),
            mean=float(vals.mean()),
            std=float(vals.std()) if len(vals) > 1 else 0.0,
            min_val=float(vals.min()),
            p25=float(vals.quantile(0.25)),
            median=float(vals.median()),
            p75=float(vals.quantile(0.75)),
            max_val=float(vals.max()),
        ))

    stats_rows = [d.model_dump() for d in distributions]
    stats_arrow = pa.Table.from_pylist(stats_rows) if stats_rows else pa.table({})
    new_id = ctx.deps.store.store(stats_arrow, None)

    return DistributionSummaryResult(result_id=new_id, distributions=distributions)


def period_over_period(
    ctx: RunContext[QueryDeps],
    result_id: str,
) -> PeriodOverPeriodResult:
    """Compute period-over-period delta and percentage change.

    For each group (metric_name + entity/slice/segment keys), rows are sorted
    by period_start_date and the change from the preceding period is computed.
    Rows with no preceding period (first in each group) get NaN delta/pct_change.

    Result store entry retains all STANDARD_COLUMNS plus delta and pct_change.

    Args:
        result_id: ID of the result store entry. Must contain time-series data
            (period_type != "all_time" or multiple period_start_date values).

    Returns:
        PeriodOverPeriodResult with a new result_id.
    """
    entry = ctx.deps.store.get(result_id)
    df = _get_ibis_table(entry).to_pandas()

    group_keys = [
        c for c in ["metric_name", "entity_id", "slice_type", "slice_value",
                     "segment_name", "segment_value"]
        if c in df.columns and df[c].notna().any()
    ]

    if "period_start_date" in df.columns:
        non_null = df["period_start_date"].dropna()
        if group_keys and not non_null.empty:
            periods_per_group = df.groupby(group_keys)["period_start_date"].nunique()
            max_periods = int(periods_per_group.max())
        elif not non_null.empty:
            max_periods = int(df["period_start_date"].nunique())
        else:
            max_periods = 0
    else:
        max_periods = 0

    if max_periods <= 1:
        return PeriodOverPeriodResult(
            result_id="",
            periods_found=max_periods,
            rows_computed=0,
            sample=[],
            error=(
                "period_over_period requires at least 2 distinct periods per group. "
                "The result contains only 1 unique period_start_date (or none). "
                "Re-run compute_metrics with a non-'all_time' period_type."
            ),
        )

    df = df.sort_values(group_keys + ["period_start_date"])
    df["prior_value"] = df.groupby(group_keys)["metric_value"].shift(1)
    df["delta"] = df["metric_value"] - df["prior_value"]
    df["pct_change"] = (df["delta"] / df["prior_value"].abs()) * 100
    df = df.drop(columns=["prior_value"])

    result_arrow = pa.Table.from_pandas(df, preserve_index=False)
    new_id = ctx.deps.store.store(result_arrow, None)

    periods_found = int(df["period_start_date"].nunique()) if "period_start_date" in df.columns else 0
    return PeriodOverPeriodResult(
        result_id=new_id,
        periods_found=periods_found,
        rows_computed=len(df),
        sample=_sample_arrow(result_arrow),
    )


def contribution_share(
    ctx: RunContext[QueryDeps],
    result_id: str,
) -> ContributionShareResult:
    """Compute each row's share of total metric_value and cumulative share.

    Share is computed as metric_value / sum(metric_value) within each metric_name.
    Rows are ordered by descending share. Cumulative share is computed within
    each metric_name group.

    Result store entry retains all STANDARD_COLUMNS plus share and cumulative_share.

    Args:
        result_id: ID of the result store entry.

    Returns:
        ContributionShareResult with a new result_id.
    """
    entry = ctx.deps.store.get(result_id)
    df = _get_ibis_table(entry).to_pandas()

    metric_totals = df.groupby("metric_name")["metric_value"].sum()
    if (metric_totals == 0).all():
        return ContributionShareResult(
            result_id="",
            total_value=0.0,
            rows=len(df),
            sample=[],
            error=(
                "contribution_share: all metric_value entries sum to zero. "
                "Shares are undefined when the total is zero."
            ),
        )

    total_by_metric = df.groupby("metric_name")["metric_value"].transform("sum")
    df["share"] = df["metric_value"] / total_by_metric.replace(0, float("nan"))
    df = df.sort_values(["metric_name", "share"], ascending=[True, False])
    df["cumulative_share"] = df.groupby("metric_name")["share"].cumsum()

    result_arrow = pa.Table.from_pandas(df, preserve_index=False)
    new_id = ctx.deps.store.store(result_arrow, None)

    total_value = float(df["metric_value"].sum()) if not df["metric_value"].isna().all() else 0.0
    return ContributionShareResult(
        result_id=new_id,
        total_value=total_value,
        rows=len(df),
        sample=_sample_arrow(result_arrow),
    )
