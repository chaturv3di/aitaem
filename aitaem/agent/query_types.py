from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aitaem.agent.trace import Status
# Moved to resolver.py (SF-1); re-exported here since query_tools.py and
# existing callers import these from query_types.
from aitaem.agent.resolver import ExactMatch, MetricIntent, NearMiss, SpecMatchResult  # noqa: F401
# Moved to common_tools.py (SF-2); re-exported here for the same reason.
from aitaem.agent.common_tools import (  # noqa: F401
    ToolResult,
    ColumnDistribution,
    ColumnDistributionResult,
    DistributionSummaryResult,
)


# ── Server-side resolution types (LLM never sees these) ─────────────────────

@dataclass
class ResolvedSpec:
    """Validated compute parameters keyed by spec_token in QueryDeps.spec_registry.

    Constructed by resolve_intent when SpecResolver confirms an exact match.
    Consumed by compute_metrics(spec_token) to reconstruct MetricCompute arguments.
    """
    metric_name: str
    slice_specs: list[str]           # validated slice spec names
    segment_spec: str | None         # validated segment spec name
    period_type: str
    time_window: tuple[str, str] | None
    by_entity: str | None
    intent_slice_value: str | None   # from MetricIntent; for trace only
    intent_segment_value: str | None


# ── Deps (passed to every tool via RunContext) ──────────────────────────────

@dataclass
class QueryDeps:
    """Session-scoped resources available to every QueryBot tool."""
    spec_cache: Any           # aitaem.SpecCache; for spec lookups and format hints
    connection_manager: Any   # aitaem.ConnectionManager; for backend access
    store: Any                # aitaem.agent.store.ResultStore
    intents: list[MetricIntent] = field(default_factory=list)
    spec_registry: dict[str, ResolvedSpec] = field(default_factory=dict)


# ── Resolution result types (LLM-facing tool returns) ───────────────────────
# ExactMatch, NearMiss, SpecMatchResult moved to resolver.py (SF-1); imported above.

class RecordIntentResult(BaseModel):
    """Returned by record_intent. The intent_id is used in the resolve_intent call."""
    intent_id: int | None
    error: str | None = None
    """Set (with intent_id=None, no intent recorded) when both time_window and
    column_distribution_result_id are given, or column_distribution_result_id is
    invalid/unresolvable."""


class ResolveIntentResult(BaseModel):
    """Returned by resolve_intent. Wraps SpecMatchResult for the LLM."""
    exact_match: ExactMatch | None
    near_misses: list[NearMiss]


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
# ToolResult, ColumnDistribution, ColumnDistributionResult, DistributionSummaryResult
# moved to common_tools.py (SF-2); imported above.

class Q_ComputeMetricsResult(ToolResult):
    """Summary returned by compute_metrics(spec_token). Full data is in ResultStore."""
    spec_token: str = Field(
        description=(
            "The spec_token consumed to produce this result. "
            "For diagnostics and logging only — do not reuse across turns."
        )
    )
    result_id: str
    row_count: int
    sample: list[dict[str, Any]]
    columns: list[str]
    format_hints: dict[str, str]


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
