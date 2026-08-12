"""DefinitionBot tools.

Nine tools. Tools 1-5 form the 4-step gate order; tools 6-9 are callable any
time after validate_spec mints a token (delete_spec is standalone — no token
needed; date_range/compute_metrics/column_distribution are optional grounding
tools, callable any time before draft_spec):
  1. record_definition_intent — capture spec type, description, optional existing YAML
  2. list_tables              — enumerate tables across backends
  3. describe_table           — schema for one table
  4. draft_spec               — store LLM-written YAML, return draft_id
  5. validate_spec            — 5-check anti-hallucination gate; mints spec_draft_token
  6. commit_spec               — save a validated draft to SpecCache (add or update)
  7. delete_spec               — remove a spec from SpecCache
  8. date_range                 — bounds of a temporal column on any raw table
  9. compute_metrics            — validate + execute a catalog metric, for
                                   distribution_summary-derived thresholds

column_distribution and distribution_summary (percentile-capable grounding
tools) are also registered on DefinitionBot but live in common_tools.py,
shared verbatim with QueryBot.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Literal, Union, cast

import ibis
import pyarrow as pa
import yaml

if TYPE_CHECKING:
    from aitaem.specs.metric import MetricSpec
    from aitaem.specs.segment import SegmentSpec
    from aitaem.specs.slice import SliceSpec

    AnySpec = Union[MetricSpec, SliceSpec, SegmentSpec]

from pydantic_ai import RunContext

from aitaem.agent.definition_types import (
    ColumnInfo,
    CommitSpecResult,
    D_ComputeMetricsResult,
    DateRangeResult,
    DefinitionDeps,
    DefinitionIntent,
    DeleteSpecResult,
    DescribeTableResult,
    DraftSpecResult,
    ListTablesResult,
    RecordDefinitionIntentResult,
    SpecDraft,
    ValidateSpecResult,
    ValidationIssue,
)
from aitaem.agent.common_tools import _execute_metric_compute, _reject_unsafe_filter, _stringify_bound
from aitaem.agent.resolver import MetricIntent, SpecResolver
from aitaem.utils.exceptions import AitaemError

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
            raw_names = connector.list_tables()
            tables[bt] = [
                uri
                for name in raw_names
                if (uri := connector.build_source_uri(name)) is not None
            ]
        except Exception as exc:
            errors[bt] = f"{type(exc).__name__}: {exc}"

    return ListTablesResult(tables=tables, errors=errors)


# ── Step 3: describe_table ───────────────────────────────────────────────────


def describe_table(
    ctx: RunContext[DefinitionDeps],
    source: str,
) -> DescribeTableResult:
    """Retrieve the schema (column names and types) for a single table.

    Args:
        source: Source URI identifying the table, exactly as returned by a
            prior list_tables() call. Reuse it verbatim — never reconstruct
            one from parts.

    Returns:
        DescribeTableResult with column info, or error set on failure.
    """
    cm = ctx.deps.connection_manager

    try:
        backend_type, _, _ = cm.parse_source_uri(source)
        connector = cm.get_connection(backend_type)
    except Exception as exc:
        return DescribeTableResult(
            source=source,
            columns=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    try:
        table_name, database = cm.resolve_table_reference(source)
        ibis_table = connector.get_table(table_name, database=database)
        schema = ibis_table.schema()
        columns = [
            ColumnInfo(name=name, dtype=str(dtype))
            for name, dtype in zip(schema.names, schema.types)
        ]
        return DescribeTableResult(
            source=source,
            columns=columns,
        )
    except Exception as exc:
        return DescribeTableResult(
            source=source,
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


def _normalize_bigquery_source_uri(uri: str) -> str:
    """Rewrite an all-slash BigQuery URI to the core-canonical form.

    ``bigquery://<project>/<dataset>/<table>`` (the prompt's canonical LLM-facing
    shape) becomes ``bigquery://<project>/<dataset>.<table>``. No-op for any
    other scheme or shape, including forms ``_parse_bigquery_uri`` already
    accepts unchanged (e.g. the fully-dotted form) — only the specific shape
    the prompt asks the LLM to produce is normalized.
    """
    prefix = "bigquery://"
    if not uri.startswith(prefix):
        return uri
    parts = uri[len(prefix) :].split("/")
    if len(parts) != 3 or not all(parts):
        return uri
    project, dataset, table = parts
    return f"{prefix}{project}/{dataset}.{table}"


def _normalize_source_in_yaml(yaml_text: str) -> str:
    """Normalize a BigQuery ``source:`` URI in raw spec YAML text.

    Fails open (returns ``yaml_text`` unchanged) on anything unexpected: YAML
    parse failure, a top-level shape other than a single spec-type key mapping
    to a dict, a missing ``source`` key, or a ``source`` value that isn't a
    string. Matches Check 5's warn-not-block posture.
    """
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return yaml_text

    if not isinstance(data, dict) or len(data) != 1:
        return yaml_text
    (body,) = data.values()
    if not isinstance(body, dict) or "source" not in body:
        return yaml_text

    source = body["source"]
    if not isinstance(source, str):
        return yaml_text

    normalized = _normalize_bigquery_source_uri(source)
    if normalized == source:
        return yaml_text

    body["source"] = normalized
    return yaml.safe_dump(data)


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

    # Normalize a BigQuery source: URI (all-slash LLM form -> core-canonical
    # form) before any later step reads draft.yaml_string. Fails open.
    draft.yaml_string = _normalize_source_in_yaml(draft.yaml_string)

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
        already_composite = [
            name for name in slice_spec.cross_product if spec_cache.slices[name].is_composite
        ]
        if already_composite:
            return ValidateSpecResult(
                errors=[
                    ValidationIssue(
                        field="cross_product",
                        message=(
                            f"Composite slice references already-composite slice(s): "
                            f"{already_composite}. Nested composite slices are not supported."
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
                from aitaem.utils.exceptions import TableNotFoundError as AitaemTableNotFoundError

                try:
                    connector = ctx.deps.connection_manager.get_connection_for_source(source_uri)
                    table_name, database = ctx.deps.connection_manager.resolve_table_reference(
                        source_uri
                    )
                    ibis_table = connector.get_table(table_name, database=database)
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
                                        f"Call describe_table(source={source_uri!r}) "
                                        "to see the current schema."
                                    ),
                                )
                            )
                except AitaemTableNotFoundError as table_exc:
                    column_errors.append(
                        ValidationIssue(
                            field="source",
                            message=(
                                f"Source {source_uri!r} could not be resolved: {table_exc}. "
                                "The table may not exist, or may not be accessible with the "
                                "current connection's credentials."
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
    result_id = ctx.deps.store.store_text(
        text=draft.yaml_string,
        content_type="application/yaml",
        metadata=metadata,
    )

    return ValidateSpecResult(
        result_id=result_id,
        warnings=warnings,
        referenced_columns=referenced_columns,
    )


# ── Step 6: commit_spec ──────────────────────────────────────────────────────


def commit_spec(
    ctx: RunContext[DefinitionDeps],
    spec_draft_token: str,
) -> CommitSpecResult:
    """Commit a validated spec draft to the SpecCache.

    Call only after the user explicitly confirms they want the draft saved.
    Add-vs-update is derived from live cache state at commit time (name already
    present -> update, else add) — not from anything recorded at validate_spec
    time, since the cache may have changed since then.

    Args:
        spec_draft_token: Token returned by validate_spec on a full pass.

    Returns:
        CommitSpecResult with action="added"|"updated" on success, or error set
        on an invalid token or a cache-consistency rejection.
    """
    from aitaem.agent.store import WrongEntryKindError
    from aitaem.utils.exceptions import SpecNotFoundError, SpecValidationError

    try:
        entry = ctx.deps.store.get_text(spec_draft_token)
    except (KeyError, WrongEntryKindError):
        return CommitSpecResult(
            error=f"spec_draft_token {spec_draft_token!r} not found or is not a spec draft. "
            "Call validate_spec first to obtain a valid token."
        )

    spec_type = entry.metadata.get("spec_type")
    if spec_type not in ("metric", "slice", "segment"):
        return CommitSpecResult(
            error=f"spec_draft_token {spec_draft_token!r} has no valid spec_type metadata."
        )

    try:
        spec = _parse_yaml_to_spec(spec_type, entry.text)
    except Exception as exc:
        return CommitSpecResult(
            error=f"Failed to re-parse validated YAML: {type(exc).__name__}: {exc}"
        )

    spec_cache = ctx.deps.spec_cache
    existing = _get_spec_cache_bucket(spec_cache, spec_type)
    try:
        if spec.name in existing:
            spec_cache.update(spec)
            action: Literal["added", "updated"] = "updated"
        else:
            spec_cache.add(spec)
            action = "added"
    except (SpecValidationError, SpecNotFoundError) as exc:
        return CommitSpecResult(error=f"{type(exc).__name__}: {exc}")

    return CommitSpecResult(spec_type=spec_type, spec_name=spec.name, action=action)


# ── Step 7: delete_spec ──────────────────────────────────────────────────────


def delete_spec(
    ctx: RunContext[DefinitionDeps],
    spec_type: Literal["metric", "slice", "segment"],
    name: str,
) -> DeleteSpecResult:
    """Delete a spec from the SpecCache. Immediate — no draft/token/confirm step.

    Call only on explicit user request. Not reversible within the session.

    Args:
        spec_type: The kind of spec to delete: "metric", "slice", or "segment".
        name: Name of the spec to delete.

    Returns:
        DeleteSpecResult with deleted=True on success, or deleted=False with
        error set (unknown name, or blocked by a dependent composite slice).
    """
    from aitaem.utils.exceptions import SpecNotFoundError, SpecValidationError

    try:
        ctx.deps.spec_cache.remove(spec_type, name)
    except (SpecNotFoundError, SpecValidationError) as exc:
        return DeleteSpecResult(
            spec_type=spec_type, spec_name=name, deleted=False, error=str(exc)
        )

    return DeleteSpecResult(spec_type=spec_type, spec_name=name, deleted=True)


# ── Step 8: date_range ───────────────────────────────────────────────────────


def _build_bounds_agg(table: ibis.Table, column: str) -> ibis.Table:
    """Build a bounds-only aggregate over column: count/null_count/min/max/distinct_count.

    Unlike common_tools._build_distribution_agg, there is no numeric branch in
    this function's code at all — no code path here can ever produce a
    percentile, mean, or std, regardless of the input column's dtype. This is
    what makes date_range structurally (not just conventionally) incapable of
    percentile-adjacent grounding.
    """
    col = table[column]
    return table.aggregate(
        count=col.count(),
        null_count=col.isnull().sum(),
        min_val=col.min(),
        max_val=col.max(),
        distinct_count=col.nunique(),
    )


def date_range(
    ctx: RunContext[DefinitionDeps],
    source: str,
    date_column: str,
    filter: str | None = None,
) -> DateRangeResult:
    """Summarize the real bounds of a temporal column on any raw table.

    Use this to ground a cohort/window boundary (e.g. "signups since March")
    in real data instead of inventing a date. Unlike column_distribution, this
    accepts any raw source: URI (as returned by list_tables()/describe_table())
    — not just a metric name — but is bounds-only: it never computes a
    percentile, mean, or std, and rejects non-temporal columns outright. For a
    value-based threshold (e.g. "75th percentile of revenue"), use
    column_distribution or compute_metrics + distribution_summary instead.

    Args:
        source: Source URI identifying the table, exactly as returned by a
            prior list_tables()/describe_table() call. Reuse it verbatim.
        date_column: Temporal column to summarize (e.g. a timestamp or date column).
        filter: Optional SQL boolean predicate (e.g. "country = 'US'"), applied
            as a WHERE clause on the source table.

    Returns:
        DateRangeResult with count/null_count/min_val/max_val/distinct_count,
        or error set on failure (including a non-temporal date_column).
    """
    try:
        connector = ctx.deps.connection_manager.get_connection_for_source(source)
        table_name, database = ctx.deps.connection_manager.resolve_table_reference(source)
        ibis_table = connector.get_table(table_name, database=database)
    except AitaemError as exc:
        return DateRangeResult(result_id="", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return DateRangeResult(result_id="", error=f"Unexpected error: {type(exc).__name__}: {exc}")

    if date_column not in ibis_table.columns:
        return DateRangeResult(
            result_id="",
            error=f"Column {date_column!r} not found. Available columns: {ibis_table.columns}",
        )

    if not ibis_table[date_column].type().is_temporal():
        return DateRangeResult(
            result_id="",
            error=(
                f"Column {date_column!r} has dtype {ibis_table[date_column].type()!r}, "
                "which is not temporal. date_range only summarizes temporal columns — "
                "use column_distribution for a value-based threshold instead."
            ),
        )

    filtered_table = ibis_table
    if filter is not None:
        try:
            safe_filter = _reject_unsafe_filter(filter, connector.backend_type)
        except ValueError as exc:
            return DateRangeResult(result_id="", error=str(exc))
        try:
            alias = f"_date_range_src_{uuid.uuid4().hex[:8]}"
            t_src = ibis_table.alias(alias)
            filtered_table = t_src.sql(f"SELECT * FROM {alias} WHERE {safe_filter}")
        except AitaemError as exc:
            return DateRangeResult(result_id="", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return DateRangeResult(result_id="", error=f"Unexpected error: {type(exc).__name__}: {exc}")

    try:
        agg = _build_bounds_agg(filtered_table, date_column)
        row = agg.to_pandas().iloc[0]
    except AitaemError as exc:
        return DateRangeResult(result_id="", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return DateRangeResult(result_id="", error=f"Unexpected error: {type(exc).__name__}: {exc}")

    min_val = _stringify_bound(row.get("min_val"))
    max_val = _stringify_bound(row.get("max_val"))
    result_id = ctx.deps.store.store_tabular(
        pa.Table.from_pylist([{
            "count": int(row["count"]),
            "null_count": int(row["null_count"]),
            "min_val": min_val,
            "max_val": max_val,
            "distinct_count": int(row["distinct_count"]),
        }]),
        agg,
        metadata={"source": source, "column": date_column, "min_val": min_val, "max_val": max_val},
    )

    return DateRangeResult(
        result_id=result_id,
        min_val=min_val,
        max_val=max_val,
        count=int(row["count"]),
        null_count=int(row["null_count"]),
        distinct_count=int(row["distinct_count"]),
    )


# ── Step 9: compute_metrics ──────────────────────────────────────────────────


def compute_metrics(
    ctx: RunContext[DefinitionDeps],
    metric_name: str,
    slices: list[str] | None = None,
    segment: str | None = None,
    by_entity: str | None = None,
    period_type: str = "all_time",
    time_window: tuple[str, str] | None = None,
) -> D_ComputeMetricsResult:
    """Validate and execute a catalog metric in one call, for aggregated-value
    thresholds (e.g. "p99 of per-store revenue" — a threshold over a computed
    metric_value, not a raw column). Feed the result_id to distribution_summary.

    Unlike QueryBot's compute_metrics, there is no spec_token indirection —
    this validates via SpecResolver.resolve() and executes immediately, since
    neither concern that split solves for QueryBot (anti-double-execution on
    parallel tool calls, a preceding record_intent NL-capture step) applies here.

    Args:
        metric_name: Proposed canonical metric name (must exactly match catalog).
        slices: Proposed slice spec names (for breakdowns). Defaults to no slices.
        segment: Proposed segment spec name. Defaults to no segment.
        by_entity: Entity column for entity-level grouping (e.g. per-store revenue).
        period_type: "all_time" | "hourly" | "daily" | "weekly" | "monthly" | "yearly".
        time_window: [start_iso, end_iso]. Required when period_type != "all_time".

    Returns:
        D_ComputeMetricsResult with result_id pointing to the stored artifact.
        near_misses is set (with no compute attempted) when metric_name/slices/
        segment/by_entity/period_type don't validate against the catalog.
    """
    intent = MetricIntent(
        metric_concept=metric_name,
        scope="subset" if (slices or segment) else "overall",
        period_type=period_type,
        time_window=time_window,
        by_entity=by_entity,
    )
    match_result = SpecResolver().resolve(
        intent=intent,
        proposed_metric_name=metric_name,
        proposed_slices=slices or [],
        proposed_segment=segment,
        spec_cache=ctx.deps.spec_cache,
    )

    if match_result.exact_match is None:
        return D_ComputeMetricsResult(
            result_id="", row_count=0, sample=[], columns=[],
            near_misses=match_result.near_misses,
            error="No exact catalog match — see near_misses.",
        )

    exact_match = match_result.exact_match
    try:
        result_id, row_count, sample, columns, _format_hints = _execute_metric_compute(
            ctx.deps.spec_cache,
            ctx.deps.connection_manager,
            ctx.deps.store,
            exact_match.metric_name,
            exact_match.slices,
            exact_match.segment,
            by_entity,
            period_type,
            time_window,
        )
    except AitaemError as exc:
        return D_ComputeMetricsResult(
            result_id="", row_count=0, sample=[], columns=[],
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        return D_ComputeMetricsResult(
            result_id="", row_count=0, sample=[], columns=[],
            error=f"Unexpected error: {type(exc).__name__}: {exc}",
        )

    dependent_metrics = ctx.deps.dependent_metrics
    if exact_match.metric_name not in dependent_metrics:
        dependent_metrics.append(exact_match.metric_name)

    return D_ComputeMetricsResult(
        result_id=result_id, row_count=row_count, sample=sample, columns=columns,
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


