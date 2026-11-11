"""stats group: statistics on compute results and tables, run where the data lives.

Bounded outputs (aggregates, top N) are written as tables to ``session.duckdb``. Row-preserving
outputs keep their input's kind: a lazy compute result stays a lazy expression on its backend, and
a table yields a new table.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any, Literal

import duckdb
import ibis
import pyarrow as pa
from pydantic_ai import FunctionToolset, RunContext

from aitaem.agent.artifacts import ArtifactRef
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import (
    ToolOutput,
    artifact_tool,
    distribution_agg,
    stringify_bounds,
    tabular,
)

_FILTER_OPS: dict[str, Callable[[Any, Any], Any]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

# Columns that identify a series in a compute result, besides its period.
_SERIES_KEYS = [
    "metric_name",
    "entity_id",
    "slice_type",
    "slice_value",
    "segment_name",
    "segment_value",
]


def _execute(ctx: RunContext[SessionDeps], ref: ArtifactRef, expr: ibis.Table) -> pa.Table:
    if ref.kind == "ibis":
        with ctx.deps.compute_guard():
            return expr.to_pyarrow()
    return expr.to_pyarrow()


def _bounded(
    ctx: RunContext[SessionDeps], ref: ArtifactRef, expr: ibis.Table, type_tag: str, what: str
) -> ToolOutput:
    arrow = _execute(ctx, ref, expr)
    return ToolOutput(
        arrow,
        "table",
        type_tag,
        f"{what} of {ref.id}: {arrow.num_rows} rows",
        [ref.id],
        facts={"columns": arrow.column_names, "row_count": arrow.num_rows},
    )


def _row_preserving(
    ref: ArtifactRef, expr: ibis.Table, type_tag: str, what: str, parents: list[str] | None = None
) -> ToolOutput:
    parents = parents or [ref.id]
    facts: dict[str, Any] = {"columns": list(expr.columns)}
    if ref.kind == "ibis":
        return ToolOutput(
            expr, "ibis", type_tag, f"{what} of {ref.id} (lazy)", parents, facts=facts
        )
    arrow = expr.to_pyarrow()
    facts["row_count"] = arrow.num_rows
    return ToolOutput(
        arrow, "table", type_tag, f"{what} of {ref.id}: {arrow.num_rows} rows", parents, facts=facts
    )


def _require(table: ibis.Table, *columns: str) -> None:
    missing = [c for c in columns if c not in table.columns]
    if missing:
        raise ValueError(f"Column(s) {missing} not found. Available columns: {list(table.columns)}")


@artifact_tool
def rank_by_value(
    ctx: RunContext[SessionDeps],
    result_id: str,
    top_n: int = 10,
    ascending: bool = False,
    group_by: list[str] | None = None,
) -> ToolOutput:
    """Keep the rows with the highest metric_value, or the top rows within each group.

    Args:
        result_id: The result to rank.
        top_n: The rows to keep, overall or per group.
        ascending: Keep the lowest values instead.
        group_by: Columns whose groups are ranked separately, such as ["metric_name"].
    """
    ref, table = tabular(ctx, result_id)
    order = ibis.asc("metric_value") if ascending else ibis.desc("metric_value")
    if not group_by:
        _require(table, "metric_value")
        return _bounded(ctx, ref, table.order_by(order).limit(top_n), "ranked", f"top {top_n}")
    _require(table, "metric_value", *group_by)
    rank = ibis.row_number().over(ibis.window(group_by=group_by, order_by=order))
    ranked = (
        table.mutate(rank=rank + 1)
        .filter(lambda t: t["rank"] <= top_n)
        .order_by([*group_by, "rank"])
    )
    return _row_preserving(ref, ranked, "ranked", f"top {top_n} per {', '.join(group_by)}")


@artifact_tool
def distribution_summary(
    ctx: RunContext[SessionDeps], result_id: str, group_by: list[str] | None = None
) -> ToolOutput:
    """Summarize metric_value per group: count, nulls, bounds, mean, std, and percentiles.

    Args:
        result_id: The result to summarize.
        group_by: Columns to group by; ["metric_name"] when omitted.
    """
    ref, table = tabular(ctx, result_id)
    keys = group_by if group_by is not None else ["metric_name"]
    _require(table, "metric_value", *keys)
    output = _bounded(
        ctx, ref, distribution_agg(table, "metric_value", keys), "distribution", "distribution"
    )
    output.payload = stringify_bounds(output.payload)
    return output


@artifact_tool
def filter_by_threshold(
    ctx: RunContext[SessionDeps],
    result_id: str,
    threshold: float,
    op: Literal[">", ">=", "<", "<=", "==", "!="] = ">",
    column: str = "metric_value",
) -> ToolOutput:
    """Keep the rows whose numeric column compares to the threshold.

    Args:
        result_id: The result to filter.
        threshold: The value to compare against.
        op: The comparison.
        column: The column to compare, such as "metric_value", "delta", or "share".
    """
    ref, table = tabular(ctx, result_id)
    _require(table, column)
    filtered = table.filter(_FILTER_OPS[op](table[column], threshold))
    return _row_preserving(ref, filtered, "filtered", f"{column} {op} {threshold}")


@artifact_tool
def filter_by_value(
    ctx: RunContext[SessionDeps], result_id: str, column: str, values: list[str | int | float]
) -> ToolOutput:
    """Keep the rows whose column holds one of the values.

    Args:
        result_id: The result to filter.
        column: The column to match, such as "slice_value".
        values: The values to keep.
    """
    ref, table = tabular(ctx, result_id)
    _require(table, column)
    filtered = table.filter(table[column].isin(values))
    return _row_preserving(ref, filtered, "filtered", f"{column} in {values}"[:120])


@artifact_tool
def filter_by_result(
    ctx: RunContext[SessionDeps], result_id: str, other_id: str, column: str = "entity_id"
) -> ToolOutput:
    """Keep the rows whose column value appears in the same column of another result.

    Args:
        result_id: The result to filter.
        other_id: The result whose values to keep.
        column: The column the two results share.
    """
    ref, table = tabular(ctx, result_id)
    other_ref, other = tabular(ctx, other_id)
    _require(table, column)
    _require(other, column)
    what = f"{column} in {other_id}"
    if ref.kind == other_ref.kind == "ibis" and ibis.get_backend(table) is ibis.get_backend(other):
        filtered = table.filter(table[column].isin(other[column]))
        return _row_preserving(ref, filtered, "filtered", what, [result_id, other_id])
    # Different backends: copy both into session.duckdb and join there.
    left = _execute(ctx, ref, table)
    keys = _execute(ctx, other_ref, other.select(column).distinct())
    con = duckdb.connect(str(ctx.deps.store.duckdb_path))
    try:
        con.register("_left", left)
        con.register("_keys", keys)
        arrow = con.execute(
            f'SELECT * FROM _left WHERE "{column}" IN (SELECT "{column}" FROM _keys)'
        ).fetch_arrow_table()
    finally:
        con.close()
    return ToolOutput(
        arrow,
        "table",
        "filtered",
        f"{what} of {result_id}: {arrow.num_rows} rows",
        [result_id, other_id],
        facts={"columns": arrow.column_names, "row_count": arrow.num_rows},
    )


@artifact_tool
def period_over_period(ctx: RunContext[SessionDeps], result_id: str) -> ToolOutput:
    """Add each period's change from the previous period of its series: delta and pct_change.

    Args:
        result_id: A result computed with a period_type other than "all_time".
    """
    ref, table = tabular(ctx, result_id)
    _require(table, "metric_value", "period_start_date")
    keys = [c for c in _SERIES_KEYS if c in table.columns]
    prior = table.metric_value.lag().over(ibis.window(group_by=keys, order_by="period_start_date"))
    delta = table.metric_value - prior
    changed = table.mutate(delta=delta, pct_change=delta / prior.abs() * 100).order_by(
        [*keys, "period_start_date"]
    )
    return _row_preserving(ref, changed, "period_over_period", "period over period")


@artifact_tool
def contribution_share(ctx: RunContext[SessionDeps], result_id: str) -> ToolOutput:
    """Add each row's share of its metric's total metric_value, and the cumulative share by size.

    Args:
        result_id: The result to compute shares for.
    """
    ref, table = tabular(ctx, result_id)
    _require(table, "metric_value", "metric_name")
    total = table.metric_value.sum().over(ibis.window(group_by="metric_name"))
    shared = table.mutate(share=table.metric_value / total.nullif(0))
    running = ibis.window(
        group_by="metric_name", order_by=ibis.desc("share"), preceding=None, following=0
    )
    with_cumulative = shared.mutate(cumulative_share=shared.share.sum().over(running)).order_by(
        ["metric_name", ibis.desc("share")]
    )
    return _row_preserving(ref, with_cumulative, "contribution_share", "contribution share")


stats_toolset: FunctionToolset[SessionDeps] = FunctionToolset(
    [
        rank_by_value,
        distribution_summary,
        filter_by_threshold,
        filter_by_value,
        filter_by_result,
        period_over_period,
        contribution_share,
    ]
)
