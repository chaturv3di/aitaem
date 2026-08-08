from __future__ import annotations

import operator
import threading
import uuid
from contextlib import nullcontext
from typing import Any, Literal, cast

import ibis
import pandas as pd
import pyarrow as pa
import sqlglot
from pydantic_ai import RunContext

from aitaem.query.builder import PeriodType
from aitaem.agent.query_types import (
    QueryDeps,
    MetricIntent,
    ResolvedSpec,
    ExactMatch,
    NearMiss,
    RecordIntentResult,
    ResolveIntentResult,
    ComputeMetricsResult,
    RankByValueResult,
    FilterByThresholdResult,
    DistributionSummaryResult,
    ColumnDistribution,
    ColumnDistributionResult,
    PeriodOverPeriodResult,
    ContributionShareResult,
)
from aitaem.agent.resolver import SpecResolver
from aitaem.agent.store import TabularEntry
from aitaem import MetricCompute

# DuckDB's ibis backend is not thread-safe. pydantic-ai dispatches parallel
# tool calls via asyncio.to_thread(), so two compute_metrics calls for a
# multi-metric question run in separate threads and race on the same connection.
# This lock serializes all compute_metrics executions within a process.
_COMPUTE_LOCK = threading.Lock()

_FILTER_OPS: dict[str, Any] = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq, "!=": operator.ne,
}


def _get_ibis_table(entry: TabularEntry) -> ibis.Table:
    """Return an ibis.Table: lazy from ibis_ref if alive, else memtable over Arrow."""
    if entry.ibis_ref is not None:
        return entry.ibis_ref
    if entry.arrow is not None:
        return ibis.memtable(entry.arrow)
    raise ValueError(f"Result entry {entry.result_id!r} has no data.")


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


# ── Step 1: record_intent ────────────────────────────────────────────────────

def record_intent(
    ctx: RunContext[QueryDeps],
    metric_concept: str,
    scope: Literal["overall", "subset"],
    subset_description: str | None = None,
    slice_type: str | None = None,
    slice_value: str | None = None,
    segment_name: str | None = None,
    segment_value: str | None = None,
    period_type: str = "all_time",
    time_window: tuple[str, str] | None = None,
    by_entity: str | None = None,
    column_distribution_result_id: str | None = None,
) -> RecordIntentResult:
    """Record the user's metric intent. Call once per metric in the question.

    Args:
        metric_concept: Free-text name as interpreted from the user's question.
            (e.g. "click-through rate", "monthly revenue"). Not a canonical catalog name.
        scope: "overall" for unfiltered aggregate; "subset" if the user wants a
            filtered or broken-down view (requires slice_type or segment_name).
        subset_description: Optional prose description of the filter (e.g.
            "only US users who clicked in January").
        slice_type: Proposed slice spec name for a breakdown (e.g. "by_country").
        slice_value: Specific filter value within the slice (e.g. "US").
        segment_name: Proposed segment spec name for entity-level segmentation.
        segment_value: Specific segment filter value.
        period_type: "all_time" | "hourly" | "daily" | "weekly" | "monthly" | "yearly".
            Non-"all_time" requires time_window.
        time_window: [start_iso, end_iso]. For hourly, use YYYY-MM-DDTHH:MM:SS,
            floored to the hour. Mutually exclusive with column_distribution_result_id.
        by_entity: Entity column for entity-level questions ("which user", "top 10 advertisers").
        column_distribution_result_id: result_id from a prior column_distribution
            call. When set (and time_window is not), time_window is derived from
            that result's stored min_val/max_val. Call column_distribution before
            record_intent — there's nothing to reference otherwise.

    Returns:
        RecordIntentResult with intent_id (integer index into the intents list).
        Pass this intent_id to resolve_intent. On failure, intent_id is None and
        error explains why — no intent is recorded.
    """
    if time_window is not None and column_distribution_result_id is not None:
        return RecordIntentResult(
            intent_id=None,
            error="Pass either time_window or column_distribution_result_id, not both.",
        )

    resolved_time_window = (time_window[0], time_window[1]) if time_window else None

    if column_distribution_result_id is not None:
        try:
            entry = ctx.deps.store.get_tabular(column_distribution_result_id)
        except Exception as exc:
            return RecordIntentResult(
                intent_id=None,
                error=f"column_distribution_result_id {column_distribution_result_id!r} not found: {exc}",
            )
        min_val = entry.metadata.get("min_val")
        max_val = entry.metadata.get("max_val")
        if min_val is None or max_val is None:
            return RecordIntentResult(
                intent_id=None,
                error=(
                    f"column_distribution_result_id {column_distribution_result_id!r} has no "
                    "min_val/max_val metadata — pass a result_id from column_distribution."
                ),
            )
        resolved_time_window = (min_val, max_val)

    intent = MetricIntent(
        metric_concept=metric_concept,
        scope=scope,
        subset_description=subset_description,
        slice_type=slice_type,
        slice_value=slice_value,
        segment_name=segment_name,
        segment_value=segment_value,
        period_type=period_type,
        time_window=resolved_time_window,
        by_entity=by_entity,
        column_distribution_result_id=column_distribution_result_id,
    )
    ctx.deps.intents.append(intent)
    return RecordIntentResult(intent_id=len(ctx.deps.intents) - 1)


# ── Step 2: resolve_intent ───────────────────────────────────────────────────

def resolve_intent(
    ctx: RunContext[QueryDeps],
    intent_id: int,
    metric_name: str,
    slices: list[str] | None = None,
    segment: str | None = None,
) -> ResolveIntentResult:
    """Validate proposed canonical names against the catalog and mint a spec_token.

    Must be called after record_intent. Pass the intent_id from record_intent.

    Args:
        intent_id: Integer returned by record_intent for this metric.
        metric_name: Proposed canonical metric name (must exactly match catalog).
        slices: Proposed slice spec names (for breakdowns). Defaults to no slices.
        segment: Proposed segment spec name. Defaults to no segment.

    Returns:
        ResolveIntentResult:
          - exact_match: set if the proposal is valid. spec_token is the handle
            for compute_metrics. Proceed to compute_metrics(spec_token=...).
          - near_misses: set when exact_match is None. Each entry explains why a
            proposed name did not match. Set status=refused and cite these.
            why_not="column_distribution_metric_mismatch" means the intent's
            column_distribution_result_id was computed against a different
            metric — call column_distribution again for this metric_name
            rather than reusing that result_id.
    """
    if intent_id < 0 or intent_id >= len(ctx.deps.intents):
        return ResolveIntentResult(
            exact_match=None,
            near_misses=[NearMiss(name=metric_name, why_not="unknown_metric")],
        )

    intent = ctx.deps.intents[intent_id]
    resolver = SpecResolver()
    match_result = resolver.resolve(
        intent=intent,
        proposed_metric_name=metric_name,
        proposed_slices=slices or [],
        proposed_segment=segment,
        spec_cache=ctx.deps.spec_cache,
    )

    if match_result.exact_match is None:
        return ResolveIntentResult(exact_match=None, near_misses=match_result.near_misses)

    if intent.column_distribution_result_id is not None:
        source_entry = ctx.deps.store.get_tabular(intent.column_distribution_result_id)
        source_metric = source_entry.metadata.get("metric_name")
        if source_metric != metric_name:
            return ResolveIntentResult(
                exact_match=None,
                near_misses=[NearMiss(
                    name=metric_name,
                    why_not="column_distribution_metric_mismatch",
                    suggestions=[source_metric] if source_metric else [],
                )],
            )

    spec_token = f"sm_{uuid.uuid4().hex}"
    resolved = ResolvedSpec(
        metric_name=metric_name,
        slice_specs=slices or [],
        segment_spec=segment,
        period_type=intent.period_type,
        time_window=intent.time_window,
        by_entity=intent.by_entity,
        intent_slice_value=intent.slice_value,
        intent_segment_value=intent.segment_value,
    )
    ctx.deps.spec_registry[spec_token] = resolved

    exact = ExactMatch(
        spec_token=spec_token,
        metric_name=metric_name,
        slices=slices or [],
        segment=segment,
    )
    return ResolveIntentResult(exact_match=exact, near_misses=[])


# ── Step 3: compute_metrics ──────────────────────────────────────────────────

def compute_metrics(
    ctx: RunContext[QueryDeps],
    spec_token: str,
) -> ComputeMetricsResult:
    """Execute a resolved metric spec and store the result.

    Call this only after resolve_intent returns an exact_match. Pass
    exact_match.spec_token directly — do not construct or modify the token.

    Args:
        spec_token: Opaque handle returned by resolve_intent.exact_match.spec_token.

    Returns:
        ComputeMetricsResult with result_id pointing to the stored artifact.
        On failure, result_id is "" and error contains the exception message.
    """
    # Pop on consume: single-use by design. With Anthropic parallel tool calls the LLM
    # can emit two compute_metrics(spec_token=X) in the same message; popping here
    # prevents double warehouse execution and duplicate result_ids from one query.
    # Safety depends on there being no suspension point (await, or anything else that
    # could yield to another concurrent caller) between this pop and either a
    # successful return or the except block's restore below — true today only because
    # this function is fully synchronous and spec_registry is a plain in-memory dict.
    # Making this async with a real await in that span, or swapping spec_registry for
    # something with its own yield point, breaks the "at most one caller ever holds
    # resolved" guarantee this relies on.
    resolved = ctx.deps.spec_registry.pop(spec_token, None)
    if resolved is None:
        return ComputeMetricsResult(
            spec_token=spec_token,
            result_id="", row_count=0, sample=[], columns=[],
            format_hints={},
            error="spec_token already consumed. A parallel compute_metrics call with this token may have succeeded — use that result_id. Do not call resolve_intent again.",
        )

    try:
        lock = _COMPUTE_LOCK if ctx.deps.connection_manager.requires_compute_lock else nullcontext()
        with lock:
            mc = MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)
            ibis_table = mc.compute(
                metrics=[resolved.metric_name],
                slices=resolved.slice_specs or None,
                segments=resolved.segment_spec,
                time_window=resolved.time_window,
                period_type=cast(PeriodType, resolved.period_type),
                by_entity=resolved.by_entity,
            )
            arrow_table = ibis_table.to_pyarrow()

        result_id = ctx.deps.store.store_tabular(arrow_table, ibis_table)

        format_hints: dict[str, str] = {}
        spec = ctx.deps.spec_cache.metrics.get(resolved.metric_name)
        if spec and spec.format:
            format_hints[resolved.metric_name] = spec.format

        sample = _sample_arrow(arrow_table)
        return ComputeMetricsResult(
            spec_token=spec_token,
            result_id=result_id,
            row_count=len(arrow_table),
            sample=sample,
            columns=arrow_table.schema.names,
            format_hints=format_hints,
            payload_summary={
                "result_id": result_id,
                "metrics_used": [resolved.metric_name],
                "slices_used": resolved.slice_specs or [],
                "segment_used": resolved.segment_spec,
                "period_type": resolved.period_type,
                "time_window": list(resolved.time_window) if resolved.time_window else None,
                "by_entity": resolved.by_entity,
                "format_hints": format_hints,
                "sample": sample,
            },
        )
    except Exception as exc:
        # Restore on failure: this attempt produced no result, so the token is still
        # usable — for the LLM's own retry, or for a genuine duplicate call in the
        # same batch that arrives after this one fails. Only ever restores after a
        # failure, never after success, so this can't reopen the double-execution
        # risk the pop above exists to prevent.
        ctx.deps.spec_registry[spec_token] = resolved
        return ComputeMetricsResult(
            spec_token=spec_token,
            result_id="", row_count=0, sample=[], columns=[],
            format_hints={},
            error=f"{type(exc).__name__}: {exc}",
        )


# ── Analysis tools (unchanged) ───────────────────────────────────────────────

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
    entry = ctx.deps.store.get_tabular(result_id)
    ibis_table = _get_ibis_table(entry)

    order_fn = ibis.asc if ascending else ibis.desc
    ranked = ibis_table.order_by(order_fn("metric_value")).limit(top_n)
    result_arrow = ranked.to_pyarrow()
    new_id = ctx.deps.store.store_tabular(result_arrow, None)

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

    entry = ctx.deps.store.get_tabular(result_id)
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
    new_id = ctx.deps.store.store_tabular(result_arrow, None)

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
    entry = ctx.deps.store.get_tabular(result_id)
    table = _get_ibis_table(entry)
    agg = _build_distribution_agg(table, "metric_value", effective_group_by)
    df = agg.to_pandas()

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


def column_distribution(
    ctx: RunContext[QueryDeps],
    metric_name: str,
    column: str | None = None,
    filter: str | None = None,
) -> ColumnDistributionResult:
    """Summarize a metric's raw source-table column — e.g. its real timestamp range.

    Runs directly against the metric's source table, before compute_metrics and
    with no spec_token gate. Use this to discover real column bounds (so
    time_window is never fabricated) or other distribution stats before
    resolving an intent. Must be called before record_intent when its result
    will feed record_intent's column_distribution_result_id — that param reads
    an already-stored result, so nothing exists to reference if record_intent
    runs first.

    Args:
        metric_name: Canonical catalog metric name; its source table is used.
        column: Column to summarize. Defaults to the metric's timestamp_col.
        filter: Optional SQL boolean predicate (e.g. "order_value > 1000"),
            applied as a WHERE clause on the source table — plain SQL
            referencing the source table's own columns, not a slice name.

    Returns:
        ColumnDistributionResult with distribution stats, or error set on failure.
    """
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
    except Exception as exc:
        return ColumnDistributionResult(result_id="", error=f"{type(exc).__name__}: {exc}")

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
        except Exception as exc:
            return ColumnDistributionResult(result_id="", error=f"{type(exc).__name__}: {exc}")

    try:
        agg = _build_distribution_agg(filtered_table, resolved_column, group_by=None)
        df = agg.to_pandas()
    except Exception as exc:
        return ColumnDistributionResult(result_id="", error=f"{type(exc).__name__}: {exc}")

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
    return ColumnDistributionResult(result_id=result_id, distribution=distribution)


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
    entry = ctx.deps.store.get_tabular(result_id)
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
    new_id = ctx.deps.store.store_tabular(result_arrow, None)

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
    entry = ctx.deps.store.get_tabular(result_id)
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
    new_id = ctx.deps.store.store_tabular(result_arrow, None)

    total_value = float(df["metric_value"].sum()) if not df["metric_value"].isna().all() else 0.0
    return ContributionShareResult(
        result_id=new_id,
        total_value=total_value,
        rows=len(df),
        sample=_sample_arrow(result_arrow),
    )
