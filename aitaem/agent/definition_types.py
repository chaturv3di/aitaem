"""Type models for DefinitionBot (SF-1).

All Pydantic/dataclass types that define the LLM–bot–tool contract for spec definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from aitaem.agent.resolver import NearMiss
from aitaem.agent.store import ResultStore
from aitaem.agent.trace import Status


# ---------------------------------------------------------------------------
# Intent and draft models
# ---------------------------------------------------------------------------


@dataclass
class DefinitionIntent:
    """Records what the user wants to define. Set once per run by record_definition_intent."""

    spec_type: Literal["metric", "slice", "segment"]
    description: str
    existing_yaml: str | None = None
    is_update: bool = False
    # Parsed from existing_yaml once at intent-recording time; None if not an update
    # or if existing_yaml was malformed.
    original_name: str | None = None


@dataclass
class SpecDraft:
    """Server-side storage for a not-yet-validated YAML string. Keyed by draft_id."""

    draft_id: str
    spec_type: Literal["metric", "slice", "segment"]
    yaml_string: str


@dataclass
class DefinitionDeps:
    """Per-run dependency bundle for DefinitionBot.

    Reconstructed fresh on every agent.run() call (both ask() and each chat() turn).
    draft_registry is ephemeral: drafts exist only within the run() that created them.
    store is a reference to DefinitionBot._store and survives across turns.
    """

    connection_manager: Any
    spec_cache: Any
    store: ResultStore
    draft_registry: dict[str, SpecDraft] = field(default_factory=dict)
    definition_intent: DefinitionIntent | None = None
    # Audit trail of metric names referenced (via column_distribution or
    # compute_metrics) while drafting this spec. Appended on success only.
    dependent_metrics: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM output type
# ---------------------------------------------------------------------------


class DefinitionOutput(BaseModel):
    """Terminal LLM response — the output_type for DefinitionBot's agent."""

    model_config = ConfigDict(frozen=True)

    status: Status
    narrative: str
    # Minted by validate_spec only when all checks pass. The LLM copies this verbatim
    # from the validate_spec result — it cannot generate a valid token independently.
    spec_draft_token: str | None = None
    # Populated on status=refused or status=error.
    reason: str | None = None
    # Set only when commit_spec/delete_spec succeeded during this run.
    # spec_draft_token keeps its existing meaning (drafted-and-validated, not
    # necessarily committed) regardless of whether these are set.
    committed_spec_type: Literal["metric", "slice", "segment"] | None = None
    committed_spec_name: str | None = None
    committed_action: Literal["added", "updated", "deleted"] | None = None


# ---------------------------------------------------------------------------
# Bot payload
# ---------------------------------------------------------------------------


class DefinitionPayload(BaseModel):
    """Assembled bot payload returned in DefinitionResponse.payload."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec_type: Literal["metric", "slice", "segment"] | None = None
    spec_name: str | None = None
    yaml_string: str | None = None
    spec_draft_token: str | None = None
    validation_warnings: list[str] = []
    # Column names referenced in SQL, keyed by table/source field name.
    referenced_columns: dict[str, list[str]] | None = None
    # Metric names referenced via column_distribution/compute_metrics during this run.
    dependent_metrics: list[str] = []
    # Exactly one of these is set based on spec_type; others are None.
    metric_spec: Any | None = None
    slice_spec: Any | None = None
    segment_spec: Any | None = None
    # Mirrors DefinitionOutput's committed_* fields — set only when
    # commit_spec/delete_spec succeeded during this run.
    committed_spec_type: Literal["metric", "slice", "segment"] | None = None
    committed_spec_name: str | None = None
    committed_action: Literal["added", "updated", "deleted"] | None = None


# ---------------------------------------------------------------------------
# Tool result models
# ---------------------------------------------------------------------------


class RecordDefinitionIntentResult(BaseModel):
    """Returned by record_definition_intent."""

    spec_type: Literal["metric", "slice", "segment"]
    has_existing_yaml: bool
    # Set when existing_yaml was provided but could not be parsed.
    existing_yaml_parse_warning: str | None = None


class ColumnInfo(BaseModel):
    """One column in a table schema."""

    name: str
    dtype: str


class ListTablesResult(BaseModel):
    """Returned by list_tables. Both fields may be non-empty on partial success."""

    # Keyed by backend_type; contains the list of table names for each successful backend.
    tables: dict[str, list[str]] = {}
    # Per-backend failure messages for backends that errored.
    errors: dict[str, str] = {}


class DescribeTableResult(BaseModel):
    """Returned by describe_table."""

    source: str
    columns: list[ColumnInfo] = []
    # Set on unknown backend or table-not-found; columns will be empty.
    error: str | None = None


class DraftSpecResult(BaseModel):
    """Returned by draft_spec."""

    draft_id: str
    spec_type: Literal["metric", "slice", "segment"]
    # First 800 chars of the YAML; truncated for large specs (800-char limit per ND-10).
    yaml_preview: str


class ValidationIssue(BaseModel):
    """One validation failure from validate_spec."""

    field: str
    message: str
    suggestion: str | None = None


class ValidateSpecResult(BaseModel):
    """Returned by validate_spec — the anti-hallucination gate."""

    model_config = ConfigDict(extra="forbid")

    # Set only when all five checks pass. The canonical ResultStore pointer
    # (ToolResult protocol, 03-component-architecture.md §2). Excluded from
    # serialization — the LLM only ever sees spec_draft_token, derived below.
    result_id: str | None = Field(default=None, exclude=True)
    # Structural / SQL / name-conflict / composite cross-ref failures.
    errors: list[ValidationIssue] = []
    # Live-schema column mismatches (best-effort; never a hard blocker).
    column_errors: list[ValidationIssue] = []
    # Soft warnings (e.g. connection unavailable for column check).
    warnings: list[str] = []
    # Referenced column names by table, populated on success.
    referenced_columns: dict[str, list[str]] | None = None
    # Tool-level failure (e.g. draft_id not found). Distinct from errors/column_errors.
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def spec_draft_token(self) -> str | None:
        """LLM-facing token. The LLM must copy this verbatim into DefinitionOutput.spec_draft_token."""
        return self.result_id or None


class CommitSpecResult(BaseModel):
    """Returned by commit_spec. action distinguishes add-vs-update, derived from live cache state."""

    spec_type: Literal["metric", "slice", "segment"] | None = None
    spec_name: str | None = None
    action: Literal["added", "updated"] | None = None
    # Set on an invalid/unknown token or a cache-consistency rejection (e.g. a
    # nested-composite conflict introduced since validate_spec ran).
    error: str | None = None


class DeleteSpecResult(BaseModel):
    """Returned by delete_spec."""

    spec_type: Literal["metric", "slice", "segment"]
    spec_name: str
    deleted: bool
    # Set when the spec was not found, or removal was blocked by a dependent composite.
    error: str | None = None


class DateRangeResult(BaseModel):
    """Returned by date_range. Bounds-only — no percentile-shaped field exists on
    this type, so no code path can ever return one, regardless of input dtype."""

    result_id: str
    min_val: str | None = None
    max_val: str | None = None
    count: int | None = None
    null_count: int | None = None
    distinct_count: int | None = None
    error: str | None = None


class D_ComputeMetricsResult(BaseModel):
    """Returned by DefinitionBot's compute_metrics. Validates via SpecResolver.resolve()
    then executes in one call — see QueryBot's Q_ComputeMetricsResult (query_types.py)
    for the spec_token-gated two-step equivalent."""

    result_id: str
    row_count: int
    sample: list[dict[str, Any]]
    columns: list[str]
    # Set when SpecResolver.resolve() found no exact_match — no compute attempted.
    near_misses: list[NearMiss] = []
    error: str | None = None
