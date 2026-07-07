from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aitaem.agent.trace import Status


# ── Deps (passed to every tool via RunContext) ──────────────────────────────

@dataclass
class QueryDeps:
    """Session-scoped resources available to every QueryBot tool."""
    spec_cache: Any           # aitaem.SpecCache; for spec lookups and format hints
    connection_manager: Any   # aitaem.ConnectionManager; for backend access
    store: Any                # aitaem.agent.store.ResultStore


# ── Final agent output (output_type — LLM fills this last) ──────────────────

class QueryOutput(BaseModel):
    """Structured final answer from the LLM after completing a QueryBot turn.

    The LLM produces exactly one QueryOutput per turn, after all tool calls.
    result_ids must reference result_id values from tool outputs in this turn.
    """
    model_config = ConfigDict(frozen=True)

    status: Status = Field(
        description=(
            "ok = data returned; empty = no rows matched; "
            "refused = question out of scope or no exact metric match; "
            "error = a tool failed."
        )
    )
    narrative: str = Field(
        description="Plain-language explanation for the user. Narrate from the tool summaries."
    )
    result_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Result store IDs to surface to the caller, ordered with the primary result first. "
            "Read result_id from each tool output and include the ones relevant to the answer. "
            "Empty when status is refused, empty, or error."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="Brief explanation when status is refused or error. Null otherwise.",
    )


# ── Bot-assembled response types (never seen by LLM) ────────────────────────

class QueryPayload(BaseModel):
    """Metadata assembled by QueryBot from QueryOutput and the turn trace."""
    model_config = ConfigDict(frozen=True)

    result_ids: list[str]
    primary_result_id: str | None   # first entry of result_ids, or None
    metrics_used: list[str]
    slices_used: list[str]
    segment_used: str | None
    time_window: tuple[str, str] | None
    period_type: str
    by_entity: str | None
    format_hints: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "metric_name → format string (e.g. 'percentage', 'currency:USD'). "
            "Callers use this to render metric values correctly."
        ),
    )
    sample: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Up to 5 rows from the primary result, with Python-native values. "
            "None when there is no primary result."
        ),
    )


# ── Tool result models (LLM reads these after each tool call) ────────────────

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


class ComputeMetricsResult(ToolResult):
    """Summary returned by compute_metrics. Full data is in ResultStore."""
    result_id: str
    metrics: list[str]
    slices: list[str] | None
    segment: str | None
    row_count: int
    sample: list[dict[str, Any]]    # up to 5 rows, metric_value included
    columns: list[str]
    period_type: str
    time_window: tuple[str, str] | None
    by_entity: str | None
    format_hints: dict[str, str]    # metric_name → format string (e.g. "percentage")


class RankByValueResult(ToolResult):
    """Summary returned by rank_by_value."""
    result_id: str
    top_rows: list[dict[str, Any]]  # up to top_n rows
    total_rows: int
    ascending: bool


class FilterByThresholdResult(ToolResult):
    """Summary returned by filter_by_threshold."""
    result_id: str
    matching_rows: int
    total_rows: int
    sample: list[dict[str, Any]]    # up to 5 matching rows
    predicate: str                  # human-readable: "metric_value > 100.0"


class MetricDistribution(BaseModel):
    """Per-metric distribution statistics."""
    metric_name: str
    count: int
    mean: float | None = None
    std: float | None = None
    min_val: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    max_val: float | None = None


class DistributionSummaryResult(ToolResult):
    """Summary returned by distribution_summary. One entry per unique metric_name."""
    result_id: str
    distributions: list[MetricDistribution]


class PeriodOverPeriodResult(ToolResult):
    """Summary returned by period_over_period."""
    result_id: str
    periods_found: int
    rows_computed: int
    sample: list[dict[str, Any]]    # up to 5 rows; includes delta and pct_change columns


class ContributionShareResult(ToolResult):
    """Summary returned by contribution_share."""
    result_id: str
    total_value: float              # sum of metric_value across all rows
    rows: int
    sample: list[dict[str, Any]]    # up to 5 rows by descending share; includes share and cumulative_share
