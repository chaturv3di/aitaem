from __future__ import annotations

import operator
import uuid
from typing import Any, Literal

import ibis
import pyarrow as pa
from pydantic_ai import RunContext

from aitaem.agent.query_types import (
    QueryDeps,
    MetricIntent,
    ResolvedSpec,
    NearMiss,
    RecordIntentResult,
    ResolveIntentResult,
    Q_ComputeMetricsResult,
    RankByValueResult,
    FilterByThresholdResult,
    PeriodOverPeriodResult,
    ContributionShareResult,
)
from aitaem.agent.resolver import SpecResolver
from aitaem.agent.common_tools import (
    _execute_metric_compute,
    _get_ibis_table,
    _sample_arrow,
    column_distribution,
    distribution_summary,
)

__all__ = [
    "record_intent",
    "resolve_intent",
    "compute_metrics",
    "rank_by_value",
    "filter_by_threshold",
    "distribution_summary",
    "column_distribution",
    "period_over_period",
    "contribution_share",
]

_FILTER_OPS: dict[str, Any] = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq, "!=": operator.ne,
}


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

    # Read metric_name/slices/segment off the validated exact_match rather than
    # the raw arguments — resolver.py's own contract (09-querybot-v0.2-design.md
    # §4.2): resolve() may one day normalize a proposal (alias expansion, slice
    # dedup/reorder); reading from the raw args would silently skip that.
    matched = match_result.exact_match

    if intent.column_distribution_result_id is not None:
        source_entry = ctx.deps.store.get_tabular(intent.column_distribution_result_id)
        source_metric = source_entry.metadata.get("metric_name")
        if source_metric != matched.metric_name:
            return ResolveIntentResult(
                exact_match=None,
                near_misses=[NearMiss(
                    name=matched.metric_name,
                    why_not="column_distribution_metric_mismatch",
                    suggestions=[source_metric] if source_metric else [],
                )],
            )

    spec_token = f"sm_{uuid.uuid4().hex}"
    resolved = ResolvedSpec(
        metric_name=matched.metric_name,
        slice_specs=matched.slices,
        segment_spec=matched.segment,
        period_type=intent.period_type,
        time_window=intent.time_window,
        by_entity=intent.by_entity,
        intent_slice_value=intent.slice_value,
        intent_segment_value=intent.segment_value,
    )
    ctx.deps.spec_registry[spec_token] = resolved

    exact = matched.model_copy(update={"spec_token": spec_token})
    return ResolveIntentResult(exact_match=exact, near_misses=[])


# ── Step 3: compute_metrics ──────────────────────────────────────────────────

def compute_metrics(
    ctx: RunContext[QueryDeps],
    spec_token: str,
) -> Q_ComputeMetricsResult:
    """Execute a resolved metric spec and store the result.

    Call this only after resolve_intent returns an exact_match. Pass
    exact_match.spec_token directly — do not construct or modify the token.

    Args:
        spec_token: Opaque handle returned by resolve_intent.exact_match.spec_token.

    Returns:
        Q_ComputeMetricsResult with result_id pointing to the stored artifact.
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
        return Q_ComputeMetricsResult(
            spec_token=spec_token,
            result_id="", row_count=0, sample=[], columns=[],
            format_hints={},
            error="spec_token already consumed. A parallel compute_metrics call with this token may have succeeded — use that result_id. Do not call resolve_intent again.",
        )

    try:
        result_id, row_count, sample, columns, format_hints = _execute_metric_compute(
            ctx.deps.spec_cache,
            ctx.deps.connection_manager,
            ctx.deps.store,
            resolved.metric_name,
            resolved.slice_specs,
            resolved.segment_spec,
            resolved.by_entity,
            resolved.period_type,
            resolved.time_window,
        )
        return Q_ComputeMetricsResult(
            spec_token=spec_token,
            result_id=result_id,
            row_count=row_count,
            sample=sample,
            columns=columns,
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
        return Q_ComputeMetricsResult(
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
