"""Tools and types shared between QueryBot and DefinitionBot (Plan 37, SF-2).

column_distribution and distribution_summary originated as QueryBot-only tools
(Plan 36) and are reused verbatim here — DefinitionBot needs the same
percentile-capable distribution stats to ground spec-definition thresholds in
real data. _execute_metric_compute is the MetricCompute execution core shared
by both bots' compute_metrics tools (QueryBot's spec_token-gated version in
query_tools.py, DefinitionBot's single-call version in definition_tools.py).
"""

from __future__ import annotations

import threading
import uuid
from contextlib import nullcontext
from typing import Any, Protocol, cast

import ibis
import pandas as pd
import pyarrow as pa
import sqlglot
from pydantic import BaseModel
from pydantic_ai import RunContext

from aitaem import MetricCompute
from aitaem.agent.store import ResultStore
from aitaem.query.builder import PeriodType
from aitaem.utils.exceptions import AitaemError

# DuckDB's ibis backend is not thread-safe. pydantic-ai dispatches parallel
# tool calls via asyncio.to_thread(), so two compute_metrics calls for a
# multi-metric question run in separate threads and race on the same connection.
# This lock serializes all compute_metrics executions within a process, across
# both QueryBot's and DefinitionBot's compute_metrics tools.
_COMPUTE_LOCK = threading.Lock()


class SharedToolDeps(Protocol):
    """Structural contract for deps passed to tools in this module.

    Both QueryDeps and DefinitionDeps satisfy this Protocol structurally — no
    inheritance change needed on either dataclass. RunContext's deps parameter
    is covariant, so RunContext[SharedToolDeps] type-checks correctly whether
    registered on QueryBot's or DefinitionBot's toolset.
    """

    spec_cache: Any
    connection_manager: Any
    store: ResultStore


# ── Tool result models (LLM reads these after each tool call) ───────────────


class ToolResult(BaseModel):
    """Base for all tool result models returned to the LLM.

    If this tool contributes to QueryPayload, populate payload_summary with
    any of the standard keys (all optional — omit inapplicable ones):
      metrics_used : list[str]             — metric names computed this call
      slices_used  : list[str]             — slice names applied
      segment_used : str | None            — segment name applied
      period_type  : str                   — granularity ("all_time", "monthly", …)
      time_window  : list[str] | None      — [start, end] ISO-8601 dates
      by_entity    : str | None            — entity grouping column
      format_hints : dict[str, str]        — metric_name → format string (e.g. "percentage")

    Leave payload_summary=None if the tool contributes nothing to the payload
    (analysis tools that only transform a prior result should do this).

    Aggregation when multiple tool calls contribute in one turn:
      - list fields  : union with deduplication, order of first appearance
      - scalar fields: first-write wins (first call that sets a field governs)
    """
    payload_summary: dict[str, Any] | None = None
    error: str | None = None   # populated on failure; result_id will be "" when set


class ColumnDistribution(BaseModel):
    """Distribution statistics for one group_by combination (distribution_summary)
    or for one raw source-table column (column_distribution).

    Numeric columns populate mean/std/percentiles; non-numeric columns populate
    distinct_count instead. min_val/max_val are always stringified (see the
    'min_val/max_val stringification' key decision in Plan 36) — ISO-8601 for
    temporal values, str() otherwise.
    """
    group_key: dict[str, str]
    count: int
    null_count: int | None = None
    mean: float | None = None
    std: float | None = None
    min_val: str | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    max_val: str | None = None
    distinct_count: int | None = None


class DistributionSummaryResult(ToolResult):
    """Summary returned by distribution_summary. One entry per group_by combination."""
    result_id: str
    group_by: list[str]
    distributions: list[ColumnDistribution]


class ColumnDistributionResult(ToolResult):
    """Summary returned by column_distribution."""
    result_id: str
    distribution: ColumnDistribution | None = None


# ── Internal helpers ─────────────────────────────────────────────────────────


def _get_ibis_table(entry: Any) -> ibis.Table:
    """Return an ibis.Table: lazy from ibis_ref if alive, else memtable over Arrow."""
    if entry.ibis_ref is not None:
        return entry.ibis_ref
    if entry.arrow is not None:
        return ibis.memtable(entry.arrow)
    raise ValueError(f"Result entry {entry.result_id!r} has no data.")


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


def _build_distribution_agg(
    table: ibis.Table, value_column: str, group_by: list[str] | None
) -> ibis.Table:
    """Build a dtype-aware distribution aggregate over value_column, one query.

    Numeric columns get count/null_count/mean/std/min/max/p25/median/p75
    (percentiles via approx_quantile — exact quantile() is unimplemented for
    BigQuery). Non-numeric columns get count/null_count/min/max/distinct_count.
    min/max keep the column's native dtype here; callers stringify after
    materializing (see the "min_val/max_val stringification" key decision).
    """
    col = table[value_column]
    is_numeric = col.type().is_numeric()

    aggs: dict[str, Any] = {
        "count": col.count(),
        "null_count": col.isnull().sum(),
        "min_val": col.min(),
        "max_val": col.max(),
    }
    if is_numeric:
        aggs.update({
            "mean": col.mean(),
            "std": col.std(),
            "p25": col.approx_quantile(0.25),
            "median": col.approx_quantile(0.5),
            "p75": col.approx_quantile(0.75),
        })
    else:
        aggs["distinct_count"] = col.nunique()

    if group_by:
        return table.group_by(group_by).aggregate(**aggs)
    return table.aggregate(**aggs)


def _stringify_bound(value: Any) -> str | None:
    """Format a min_val/max_val for ColumnDistribution: ISO-8601 for temporal
    values (via .isoformat()), str() otherwise. Never a backend-native cast."""
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_val_or_none(row: pd.Series, key: str, caster: Any) -> Any:
    """row[key] cast via caster, or None if the key is absent or the value is NA."""
    if key not in row.index:
        return None
    value = row[key]
    if pd.isna(value):
        return None
    return caster(value)


def _row_to_distribution(row: pd.Series, group_key: dict[str, str]) -> ColumnDistribution:
    """Build a ColumnDistribution from one row of a _build_distribution_agg result."""
    return ColumnDistribution(
        group_key=group_key,
        count=int(row["count"]),
        null_count=_row_val_or_none(row, "null_count", int),
        mean=_row_val_or_none(row, "mean", float),
        std=_row_val_or_none(row, "std", float),
        min_val=_stringify_bound(row.get("min_val")),
        p25=_row_val_or_none(row, "p25", float),
        median=_row_val_or_none(row, "median", float),
        p75=_row_val_or_none(row, "p75", float),
        max_val=_stringify_bound(row.get("max_val")),
        distinct_count=_row_val_or_none(row, "distinct_count", int),
    )


def _reject_unsafe_filter(filter_sql: str, dialect: str) -> str:
    """Validate a column_distribution filter is a single boolean predicate with
    no subquery, and return the sqlglot-regenerated SQL to splice.

    This is a table-scope safety check, not general SQL-correctness validation:
    the resolved source table is pinned by the metric's own spec, but a
    subquery inside filter_sql could still read an arbitrary other table under
    the connection's credentials. Parsing into the bounded exp.Condition
    grammar also rejects statement-stacking and paren-breakout as a side
    effect (both fail to parse). The *regenerated* SQL — not the raw input —
    must be spliced by the caller: it neutralizes trailing-comment-truncation
    attempts by re-serializing them as an inert block comment.

    Raises:
        ValueError: filter_sql fails to parse, or its parse tree contains a
            Subquery/Select node.
    """
    try:
        parsed = sqlglot.parse_one(filter_sql, into=sqlglot.exp.Condition, dialect=dialect)
    except Exception as exc:
        raise ValueError(f"filter is not a valid SQL predicate: {exc}") from exc
    if list(parsed.find_all(sqlglot.exp.Subquery, sqlglot.exp.Select)):
        raise ValueError("filter must not contain subqueries")
    return parsed.sql(dialect=dialect)


def _execute_metric_compute(
    spec_cache: Any,
    connection_manager: Any,
    store: ResultStore,
    metric_name: str,
    slices: list[str] | None,
    segment: str | None,
    by_entity: str | None,
    period_type: str,
    time_window: tuple[str, str] | None,
) -> tuple[str, int, list[dict[str, Any]], list[str], dict[str, str]]:
    """Execute a validated metric spec via MetricCompute and store the result.

    The MetricCompute execution core shared by QueryBot's compute_metrics
    (spec_token-gated, reads params off a ResolvedSpec) and DefinitionBot's
    compute_metrics (validates via SpecResolver, then calls this directly).

    Raises on failure — no try/except here; each caller applies its own
    exception handling and translates into its own result shape.

    Returns:
        (result_id, row_count, sample, columns, format_hints).
    """
    lock = _COMPUTE_LOCK if connection_manager.requires_compute_lock else nullcontext()
    with lock:
        mc = MetricCompute(spec_cache, connection_manager)
        ibis_table = mc.compute(
            metrics=[metric_name],
            slices=slices or None,
            segments=segment,
            time_window=time_window,
            period_type=cast(PeriodType, period_type),
            by_entity=by_entity,
        )
        arrow_table = ibis_table.to_pyarrow()

    result_id = store.store_tabular(arrow_table, ibis_table)

    format_hints: dict[str, str] = {}
    spec = spec_cache.metrics.get(metric_name)
    if spec and spec.format:
        format_hints[metric_name] = spec.format

    sample = _sample_arrow(arrow_table)
    return result_id, len(arrow_table), sample, arrow_table.schema.names, format_hints


# ── Shared tools ──────────────────────────────────────────────────────────────


def distribution_summary(
    ctx: RunContext[SharedToolDeps],
    result_id: str,
    group_by: list[str] | None = None,
) -> DistributionSummaryResult:
    """Compute distribution statistics (mean, std, percentiles) over metric_value.

    Pushed down to the backend as a single group_by().aggregate() query — the
    prior result is never materialized to pandas before aggregating.

    Args:
        result_id: ID of the result store entry to summarize.
        group_by: STANDARD_COLUMNS field names to group by (e.g.
            ["metric_name", "slice_value"]). Defaults to ["metric_name"].

    Returns:
        DistributionSummaryResult with one entry per group_by combination.
    """
    effective_group_by = group_by if group_by is not None else ["metric_name"]

    try:
        entry = ctx.deps.store.get_tabular(result_id)
        table = _get_ibis_table(entry)
        agg = _build_distribution_agg(table, "metric_value", effective_group_by)
        df = agg.to_pandas()
    except AitaemError as exc:
        return DistributionSummaryResult(
            result_id="", group_by=effective_group_by, distributions=[],
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        return DistributionSummaryResult(
            result_id="", group_by=effective_group_by, distributions=[],
            error=f"Unexpected error: {type(exc).__name__}: {exc}",
        )

    distributions = [
        _row_to_distribution(row, {k: str(row[k]) for k in effective_group_by})
        for _, row in df.iterrows()
    ]

    stats_rows = [d.model_dump() for d in distributions]
    stats_arrow = pa.Table.from_pylist(stats_rows) if stats_rows else pa.table({})
    # ibis_ref is deliberately retained here, unlike its four sibling analysis
    # tools (rank_by_value, filter_by_threshold, period_over_period,
    # contribution_share): this result is bounded by construction — one row
    # per group_by combination, regardless of source size — so retaining a
    # live ref costs nothing and can't reintroduce the expensive-re-query
    # problem this redesign exists to fix.
    new_id = ctx.deps.store.store_tabular(stats_arrow, agg)

    return DistributionSummaryResult(
        result_id=new_id, group_by=effective_group_by, distributions=distributions
    )


def column_distribution(
    ctx: RunContext[SharedToolDeps],
    metric_name: str,
    column: str | None = None,
    filter: str | None = None,
) -> ColumnDistributionResult:
    """Summarize a metric's raw source-table column — e.g. its real timestamp range.

    Runs directly against the metric's source table, before compute_metrics and
    with no spec_token gate. Use this to discover real column bounds (so
    time_window is never fabricated) or other distribution stats before
    resolving an intent/spec.

    Deliberately metric-only — there is no raw source: URI mode. A concept
    without a matching catalog metric can't get percentile grounding at all:
    define the metric first, then call this. The alternative (accepting a
    raw source: URI) would let a threshold-setting statistic trace back to an
    unreviewed table instead of the catalog's approved definition — the
    percentile-capable path is only reachable through the catalog by design.

    Args:
        metric_name: Canonical catalog metric name; its source table is used.
        column: Column to summarize. Defaults to the metric's timestamp_col.
        filter: Optional SQL boolean predicate (e.g. "order_value > 1000"),
            applied as a WHERE clause on the source table — plain SQL
            referencing the source table's own columns, not a slice name.

    Returns:
        ColumnDistributionResult with distribution stats, or error set on failure.
    """
    from aitaem.agent.resolver import SpecResolver

    spec, suggestions = SpecResolver.resolve_metric_name(metric_name, ctx.deps.spec_cache)
    if spec is None:
        detail = f" Did you mean: {suggestions}?" if suggestions else ""
        return ColumnDistributionResult(result_id="", error=f"Unknown metric {metric_name!r}.{detail}")

    resolved_column = column or spec.timestamp_col
    if not resolved_column:
        return ColumnDistributionResult(
            result_id="",
            error="No column specified and metric has no timestamp_col; pass an explicit column.",
        )

    try:
        connector = ctx.deps.connection_manager.get_connection_for_source(spec.source)
        table_name, database = ctx.deps.connection_manager.resolve_table_reference(spec.source)
        ibis_table = connector.get_table(table_name, database=database)
    except AitaemError as exc:
        return ColumnDistributionResult(result_id="", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return ColumnDistributionResult(result_id="", error=f"Unexpected error: {type(exc).__name__}: {exc}")

    if resolved_column not in ibis_table.columns:
        return ColumnDistributionResult(
            result_id="",
            error=f"Column {resolved_column!r} not found. Available columns: {ibis_table.columns}",
        )

    filtered_table = ibis_table
    if filter is not None:
        try:
            safe_filter = _reject_unsafe_filter(filter, connector.backend_type)
        except ValueError as exc:
            return ColumnDistributionResult(result_id="", error=str(exc))
        try:
            alias = f"_col_dist_src_{uuid.uuid4().hex[:8]}"
            t_src = ibis_table.alias(alias)
            filtered_table = t_src.sql(f"SELECT * FROM {alias} WHERE {safe_filter}")
        except AitaemError as exc:
            return ColumnDistributionResult(result_id="", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return ColumnDistributionResult(result_id="", error=f"Unexpected error: {type(exc).__name__}: {exc}")

    try:
        agg = _build_distribution_agg(filtered_table, resolved_column, group_by=None)
        df = agg.to_pandas()
    except AitaemError as exc:
        return ColumnDistributionResult(result_id="", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return ColumnDistributionResult(result_id="", error=f"Unexpected error: {type(exc).__name__}: {exc}")

    distribution = _row_to_distribution(
        df.iloc[0], {"metric_name": metric_name, "column": resolved_column}
    )
    result_id = ctx.deps.store.store_tabular(
        pa.Table.from_pylist([distribution.model_dump()]),
        agg,
        metadata={
            "metric_name": metric_name,
            "column": resolved_column,
            "min_val": distribution.min_val,
            "max_val": distribution.max_val,
        },
    )

    dependent_metrics = getattr(ctx.deps, "dependent_metrics", None)
    if isinstance(dependent_metrics, list) and metric_name not in dependent_metrics:
        dependent_metrics.append(metric_name)

    return ColumnDistributionResult(result_id=result_id, distribution=distribution)
