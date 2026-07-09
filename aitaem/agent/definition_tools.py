"""DefinitionBot tools (SF-2 through SF-6).

Five tools in 4-step gate order:
  1. record_definition_intent — capture spec type, description, optional existing YAML
  2. list_tables              — enumerate tables across backends
  3. describe_table           — schema for one table
  4. draft_spec               — store LLM-written YAML, return draft_id
  5. validate_spec            — 5-check anti-hallucination gate; mints spec_draft_token
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Literal, Union, cast

if TYPE_CHECKING:
    from aitaem.specs.metric import MetricSpec
    from aitaem.specs.segment import SegmentSpec
    from aitaem.specs.slice import SliceSpec

    AnySpec = Union[MetricSpec, SliceSpec, SegmentSpec]

from pydantic_ai import RunContext

from aitaem.agent.definition_types import (
    ColumnInfo,
    DefinitionDeps,
    DefinitionIntent,
    DescribeTableResult,
    DraftSpecResult,
    ListTablesResult,
    RecordDefinitionIntentResult,
    SpecDraft,
    ValidateSpecResult,
    ValidationIssue,
)

_DRAFT_ID_PREFIX = "dd_"
_YAML_PREVIEW_MAX = 800


# ── Step 1: record_definition_intent ────────────────────────────────────────


def record_definition_intent(
    ctx: RunContext[DefinitionDeps],
    spec_type: Literal["metric", "slice", "segment"],
    description: str,
    existing_yaml: str | None = None,
) -> RecordDefinitionIntentResult:
    """Record the user's spec-definition intent. Call once per spec.

    Args:
        spec_type: The kind of spec to define: "metric", "slice", or "segment".
        description: Natural-language description of what the spec should compute.
        existing_yaml: Optional YAML string of an existing spec to update.
            When provided, is_update=True and the spec name is locked for the
            duration of this run — validate_spec will reject any name change.

    Returns:
        RecordDefinitionIntentResult confirming the intent was recorded.
        existing_yaml_parse_warning is set when existing_yaml could not be parsed.
    """
    is_update = False
    original_name: str | None = None
    parse_warning: str | None = None

    if existing_yaml is not None:
        try:
            spec = _parse_yaml_to_spec(spec_type, existing_yaml)
            original_name = spec.name
            is_update = True
        except Exception as exc:
            parse_warning = (
                f"Could not parse existing_yaml ({type(exc).__name__}: {exc}). "
                "Treating as a new spec (is_update=False)."
            )

    ctx.deps.definition_intent = DefinitionIntent(
        spec_type=spec_type,
        description=description,
        existing_yaml=existing_yaml,
        is_update=is_update,
        original_name=original_name,
    )

    return RecordDefinitionIntentResult(
        spec_type=spec_type,
        has_existing_yaml=existing_yaml is not None,
        existing_yaml_parse_warning=parse_warning,
    )


# ── Step 2: list_tables ─────────────────────────────────────────────────────


def list_tables(
    ctx: RunContext[DefinitionDeps],
    backend_type: str | None = None,
) -> ListTablesResult:
    """List tables available across backends.

    Args:
        backend_type: Optional backend to query. When None, all registered backends
            are queried and results are aggregated. Use a specific backend_type to
            restrict the query.

    Returns:
        ListTablesResult with tables keyed by backend_type and per-backend errors.
        Both fields may be non-empty on partial success — the LLM can act on
        available backends while noting which failed.
    """
    cm = ctx.deps.connection_manager
    tables: dict[str, list[str]] = {}
    errors: dict[str, str] = {}

    if backend_type is not None:
        backends = [backend_type]
    else:
        backends = cm.backend_types

    for bt in backends:
        try:
            connector = cm.get_connection(bt)
            tables[bt] = connector.list_tables()
        except Exception as exc:
            errors[bt] = f"{type(exc).__name__}: {exc}"

    return ListTablesResult(tables=tables, errors=errors)


# ── Step 3: describe_table ───────────────────────────────────────────────────


def describe_table(
    ctx: RunContext[DefinitionDeps],
    table_name: str,
    backend_type: str,
) -> DescribeTableResult:
    """Retrieve the schema (column names and types) for a single table.

    Args:
        table_name: Name of the table to describe.
        backend_type: Backend where the table lives. Required — always available
            from a prior list_tables call. Making this required keeps traces stable
            regardless of how many backends are registered.

    Returns:
        DescribeTableResult with column info, or error set on failure.
    """
    try:
        connector = ctx.deps.connection_manager.get_connection(backend_type)
    except Exception as exc:
        return DescribeTableResult(
            table_name=table_name,
            backend_type=backend_type,
            columns=[],
            error=f"Unknown backend {backend_type!r}: {exc}",
        )

    try:
        ibis_table = connector.get_table(table_name)
        schema = ibis_table.schema()
        columns = [
            ColumnInfo(name=name, dtype=str(dtype))
            for name, dtype in zip(schema.names, schema.types)
        ]
        return DescribeTableResult(
            table_name=table_name,
            backend_type=backend_type,
            columns=columns,
        )
    except Exception as exc:
        return DescribeTableResult(
            table_name=table_name,
            backend_type=backend_type,
            columns=[],
            error=f"{type(exc).__name__}: {exc}",
        )


# ── Step 4: draft_spec ───────────────────────────────────────────────────────


def draft_spec(
    ctx: RunContext[DefinitionDeps],
    spec_type: Literal["metric", "slice", "segment"],
    yaml_string: str,
) -> DraftSpecResult:
    """Store the LLM-written YAML draft and return a draft_id.

    No validation is performed here — all checks are deferred to validate_spec.
    Each call creates a new draft entry; repeated calls for corrections produce
    distinct draft_ids.

    Args:
        spec_type: The kind of spec being drafted.
        yaml_string: Free-form YAML string for the spec.

    Returns:
        DraftSpecResult with draft_id and first 800 chars as yaml_preview.
    """
    draft_id = f"{_DRAFT_ID_PREFIX}{uuid.uuid4().hex}"
    ctx.deps.draft_registry[draft_id] = SpecDraft(
        draft_id=draft_id,
        spec_type=spec_type,
        yaml_string=yaml_string,
    )
    return DraftSpecResult(
        draft_id=draft_id,
        spec_type=spec_type,
        yaml_preview=yaml_string[:_YAML_PREVIEW_MAX],
    )


# ── Step 5: validate_spec ────────────────────────────────────────────────────


def validate_spec(
    ctx: RunContext[DefinitionDeps],
    draft_id: str,
) -> ValidateSpecResult:
    """Validate a draft spec through five checks and mint a spec_draft_token on full pass.

    Checks in order (stops at first failure category):
      1. Draft lookup — draft_id must exist in draft_registry.
      2. Structural + SQL — *Spec.from_yaml() must succeed.
      3. Name conflict / name lock — new specs must not conflict; updates must not rename.
      4. Composite cross-reference (slice only) — all cross_product names must exist.
      5. Column existence (best-effort) — referenced columns checked against live schema.

    On full pass: stores the YAML as a TextEntry in ResultStore and returns the
    entry ID as spec_draft_token. The LLM must copy this token verbatim into
    DefinitionOutput.spec_draft_token.

    Args:
        draft_id: ID returned by draft_spec.

    Returns:
        ValidateSpecResult. spec_draft_token is set only when all checks pass.
    """
    # Check 1: draft lookup
    draft = ctx.deps.draft_registry.get(draft_id)
    if draft is None:
        return ValidateSpecResult(
            error=f"draft_id {draft_id!r} not found. Call draft_spec first to register a YAML draft."
        )

    # Check 2: structural + SQL validation
    try:
        spec = _parse_yaml_to_spec(draft.spec_type, draft.yaml_string)
    except Exception as exc:
        from aitaem.utils.exceptions import SpecValidationError

        if isinstance(exc, SpecValidationError):
            issues = [
                ValidationIssue(
                    field=e.field,
                    message=e.message,
                    suggestion=e.suggestion,
                )
                for e in exc.errors
            ]
        else:
            issues = [ValidationIssue(field="yaml", message=str(exc))]
        return ValidateSpecResult(errors=issues)

    # Check 3: name conflict / name lock
    intent = ctx.deps.definition_intent
    is_update = intent.is_update if intent is not None else False
    original_name = intent.original_name if intent is not None else None

    if is_update:
        # Name lock: the spec name must not have changed from existing_yaml.
        if original_name is not None and spec.name != original_name:
            return ValidateSpecResult(
                errors=[
                    ValidationIssue(
                        field="name",
                        message=(
                            f"Spec name cannot be changed during an update. "
                            f"Expected {original_name!r}, got {spec.name!r}."
                        ),
                        suggestion=f"Set name: {original_name}",
                    )
                ]
            )
        # When name matches, skip the conflict check entirely.
    else:
        # New spec: check for name conflict.
        spec_cache = ctx.deps.spec_cache
        existing = _get_spec_cache_bucket(spec_cache, draft.spec_type)
        if spec.name in existing:
            return ValidateSpecResult(
                errors=[
                    ValidationIssue(
                        field="name",
                        message=(
                            f"A {draft.spec_type} spec named {spec.name!r} already exists. "
                            "Use a different name or pass existing_yaml to record_definition_intent "
                            "to update it."
                        ),
                        suggestion="Choose a unique name or set is_update=True via existing_yaml.",
                    )
                ]
            )

    # Check 4: composite cross-reference (slice only)
    if draft.spec_type == "slice":
        from aitaem.specs.slice import SliceSpec as _SliceSpec
        slice_spec = cast(_SliceSpec, spec)
    if draft.spec_type == "slice" and slice_spec.is_composite:
        spec_cache = ctx.deps.spec_cache
        missing = [
            name for name in slice_spec.cross_product if name not in spec_cache.slices
        ]
        if missing:
            return ValidateSpecResult(
                errors=[
                    ValidationIssue(
                        field="cross_product",
                        message=(
                            f"Composite slice references unknown slice(s): {missing}. "
                            "All names in cross_product must exist in the spec catalog."
                        ),
                    )
                ]
            )

    # Check 5: column existence (best-effort)
    warnings: list[str] = []
    column_errors: list[ValidationIssue] = []
    referenced_columns: dict[str, list[str]] | None = None

    try:
        validation_result = spec.validate()
        referenced_columns = validation_result.referenced_columns

        if referenced_columns:
            # Collect all referenced column names across all fields.
            all_referenced: set[str] = set()
            for col_names in referenced_columns.values():
                all_referenced.update(col_names)

            # Resolve the source URI from the spec object itself.
            # Metrics and segments have spec.source; slices have no single source.
            source_uri = getattr(spec, "source", None)
            if source_uri and all_referenced:
                try:
                    connector = ctx.deps.connection_manager.get_connection_for_source(source_uri)
                    _, _, table_name = ctx.deps.connection_manager.parse_source_uri(source_uri)
                    ibis_table = connector.get_table(table_name)
                    live_columns = set(ibis_table.columns)
                    for col in sorted(all_referenced):
                        if col not in live_columns:
                            column_errors.append(
                                ValidationIssue(
                                    field="source",
                                    message=(
                                        f"Column {col!r} not found in table {table_name!r}. "
                                        f"Available columns: {sorted(live_columns)}"
                                    ),
                                    suggestion=(
                                        f"Call describe_table(table_name={table_name!r}, "
                                        f"backend_type=...) to see the current schema."
                                    ),
                                )
                            )
                except Exception as col_exc:
                    warnings.append(
                        f"Column existence check skipped: "
                        f"{type(col_exc).__name__}: {col_exc}"
                    )
    except Exception as exc:
        warnings.append(
            f"Column existence check unavailable: {type(exc).__name__}: {exc}"
        )

    if column_errors:
        return ValidateSpecResult(
            column_errors=column_errors,
            warnings=warnings,
            referenced_columns=referenced_columns,
        )

    # All checks passed — mint spec_draft_token.
    metadata: dict = {
        "spec_type": draft.spec_type,
        "spec_name": spec.name,
        "referenced_columns": json.dumps(referenced_columns) if referenced_columns else None,
        "warnings": json.dumps(warnings) if warnings else None,
    }
    spec_draft_token = ctx.deps.store.store_text(
        text=draft.yaml_string,
        content_type="application/yaml",
        metadata=metadata,
    )

    return ValidateSpecResult(
        spec_draft_token=spec_draft_token,
        warnings=warnings,
        referenced_columns=referenced_columns,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_yaml_to_spec(
    spec_type: Literal["metric", "slice", "segment"], yaml_string: str
) -> AnySpec:
    """Parse YAML string into the appropriate spec object. Raises on failure."""
    from aitaem.specs.metric import MetricSpec
    from aitaem.specs.slice import SliceSpec
    from aitaem.specs.segment import SegmentSpec

    if spec_type == "metric":
        return MetricSpec.from_yaml(yaml_string)
    elif spec_type == "slice":
        return SliceSpec.from_yaml(yaml_string)
    else:
        return SegmentSpec.from_yaml(yaml_string)


def _get_spec_cache_bucket(spec_cache: object, spec_type: str) -> dict:
    """Return the appropriate spec_cache dict for the given spec_type."""
    if spec_type == "metric":
        return spec_cache.metrics  # type: ignore[attr-defined]
    elif spec_type == "slice":
        return spec_cache.slices  # type: ignore[attr-defined]
    else:
        return spec_cache.segments  # type: ignore[attr-defined]


