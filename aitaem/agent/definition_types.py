"""Type models for DefinitionBot (SF-1).

All Pydantic/dataclass types that define the LLM–bot–tool contract for spec definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

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
    # Exactly one of these is set based on spec_type; others are None.
    metric_spec: Any | None = None
    slice_spec: Any | None = None
    segment_spec: Any | None = None


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

    table_name: str
    backend_type: str
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
