"""aitaem group: connections, the spec catalog, and metric computes, through aitaem core."""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_ai import FunctionToolset, RunContext

from aitaem import (
    MetricCompute,
    MetricSpec,
    PeriodType,
    SegmentSpec,
    SliceSpec,
    SpecCache,
    SpecValidationError,
    TableNotFoundError,
)
from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status
from aitaem.agent.tools import ToolOutput, artifact_tool

SpecType = Literal["metric", "slice", "segment"]
AnySpec = MetricSpec | SliceSpec | SegmentSpec

_SPEC_CLASSES: dict[str, Any] = {"metric": MetricSpec, "slice": SliceSpec, "segment": SegmentSpec}


class ConnectionParams(BaseModel):
    """A backend connection to add."""

    backend_type: Literal["duckdb", "bigquery", "postgres"]
    config: dict[str, str | int | bool] = Field(
        default_factory=dict,
        description="ConnectionManager.add_connection keywords, e.g. path for DuckDB, "
        "project_id and dataset_id for BigQuery.",
    )


class MetricComputeParams(BaseModel):
    """The metrics to compute and how to break them down."""

    metrics: list[str]
    slices: list[str] = []
    segment: str | None = None
    time_window: tuple[str, str] | None = Field(
        default=None,
        description='(start, end) ISO dates; needed when period_type is not "all_time".',
    )
    period_type: PeriodType = "all_time"
    by_entity: str | None = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _bucket(cache: SpecCache, spec_type: str) -> Any:
    return {"metric": cache.metrics, "slice": cache.slices, "segment": cache.segments}[spec_type]


def _save(deps: SessionDeps) -> None:
    root = str(deps.spec_dir).rstrip("/")
    deps.spec_cache.save(
        metric_dir=f"{root}/metrics", slice_dir=f"{root}/slices", segment_dir=f"{root}/segments"
    )


def _normalize_bigquery_source(yaml_text: str) -> str:
    """Rewrite an all-slash ``bigquery://<project>/<dataset>/<table>`` source to the core form
    ``bigquery://<project>/<dataset>.<table>``; leave anything else unchanged."""
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return yaml_text
    if not isinstance(data, dict) or len(data) != 1:
        return yaml_text
    (body,) = data.values()
    source = body.get("source") if isinstance(body, dict) else None
    if not isinstance(source, str) or not source.startswith("bigquery://"):
        return yaml_text
    parts = source[len("bigquery://") :].split("/")
    if len(parts) != 3 or not all(parts):
        return yaml_text
    body["source"] = f"bigquery://{parts[0]}/{parts[1]}.{parts[2]}"
    return yaml.safe_dump(data, sort_keys=False)


def _issue(field: str, message: str, suggestion: str | None = None) -> dict[str, Any]:
    return {"field": field, "message": message, "suggestion": suggestion}


def _check_draft(deps: SessionDeps, spec_type: str, yaml_text: str) -> dict[str, Any]:
    """Run v2's checks on a draft: structure, name, composite references, column existence."""
    draft: dict[str, Any] = {
        "spec_type": spec_type,
        "name": None,
        "yaml": _normalize_bigquery_source(yaml_text),
        "issues": [],
        "warnings": [],
        "referenced_columns": None,
    }
    try:
        spec = _SPEC_CLASSES[spec_type].from_yaml(draft["yaml"])
    except SpecValidationError as exc:
        draft["issues"] = [_issue(e.field, e.message, e.suggestion) for e in exc.errors]
        return draft
    except Exception as exc:  # noqa: BLE001
        draft["issues"] = [_issue("yaml", str(exc))]
        return draft
    draft["name"] = spec.name
    cache = deps.spec_cache

    if spec.name in _bucket(cache, spec_type):
        draft["warnings"].append(f"Committing replaces the existing {spec_type} {spec.name!r}.")

    if isinstance(spec, SliceSpec) and spec.is_composite:
        missing = [n for n in spec.cross_product if n not in cache.slices]
        if missing:
            draft["issues"].append(
                _issue("cross_product", f"Composite slice references unknown slice(s): {missing}.")
            )
            return draft
        nested = [n for n in spec.cross_product if cache.slices[n].is_composite]
        if nested:
            draft["issues"].append(
                _issue("cross_product", f"Referenced slice(s) {nested} are composite themselves.")
            )
            return draft

    referenced = spec.validate().referenced_columns
    draft["referenced_columns"] = referenced
    source = getattr(spec, "source", None)
    columns = sorted({c for cols in (referenced or {}).values() for c in cols})
    if source and columns:
        cm = deps.connection_manager
        try:
            with deps.compute_guard():
                connector = cm.get_connection_for_source(source)
                table_name, database = cm.resolve_table_reference(source)
                live = set(connector.get_table(table_name, database=database).columns)
            for col in columns:
                if col not in live:
                    draft["issues"].append(
                        _issue(
                            "source",
                            f"Column {col!r} not found in {table_name!r}. Available: {sorted(live)}",
                            f"describe_table({source!r}) shows the schema.",
                        )
                    )
        except TableNotFoundError as exc:
            draft["issues"].append(_issue("source", f"Source {source!r} not found: {exc}"))
        except Exception as exc:  # noqa: BLE001
            draft["warnings"].append(f"Column check skipped: {type(exc).__name__}: {exc}")
    return draft


def _spec_fields(spec: AnySpec) -> dict[str, str]:
    fields = {"name": spec.name, "description": spec.description or ""}
    if isinstance(spec, MetricSpec):
        fields["entities"] = " ".join(spec.entities or [])
        fields["source"] = spec.source
    elif isinstance(spec, SegmentSpec):
        fields["entities"] = spec.entity_id or ""
        fields["source"] = spec.source
    return fields


_FIELD_WEIGHTS = {"name": 3, "description": 2, "entities": 1, "source": 1}


# ── tools ───────────────────────────────────────────────────────────────────


@artifact_tool
def connect_source(ctx: RunContext[SessionDeps], params: ConnectionParams) -> ToolOutput:
    """Connect a DuckDB or BigQuery backend.

    Args:
        params: The backend type and its connection settings.
    """
    if params.backend_type == "postgres":
        message = (
            "Postgres needs a password, and the agent handles no credentials. Configure Postgres "
            "in the ConnectionManager passed to AitaemAgent."
        )
        return ToolOutput(
            {"backend_type": "postgres", "message": message},
            "object",
            "connection",
            message,
            status=Status.refused,
            facts={"message": message},
        )
    cm = ctx.deps.connection_manager
    with ctx.deps.compute_guard():
        config: dict[str, Any] = dict(params.config)
        cm.add_connection(params.backend_type, **config)
    payload = {"backend_type": params.backend_type, "backends": cm.backend_types}
    return ToolOutput(
        payload, "object", "connection", f"connected {params.backend_type}", facts=payload
    )


@artifact_tool
def list_specs(ctx: RunContext[SessionDeps], spec_type: SpecType | None = None) -> ToolOutput:
    """List the names and descriptions of the specs in the catalog.

    Args:
        spec_type: Only this type of spec; every type when omitted.
    """
    types = [spec_type] if spec_type else ["metric", "slice", "segment"]
    specs: dict[str, Any] = {
        t: [
            {"name": s.name, "description": s.description}
            for s in _bucket(ctx.deps.spec_cache, t).values()
        ]
        for t in types
    }
    summary = ", ".join(f"{len(v)} {t}s" for t, v in specs.items())
    return ToolOutput(specs, "object", "spec_list", summary, facts=specs)


@artifact_tool
def find_specs(
    ctx: RunContext[SessionDeps], spec_type: SpecType, query: str, limit: int = 10
) -> ToolOutput:
    """Find the specs whose name, description, entities, or source best match the query's keywords.

    Args:
        spec_type: The type of spec to search.
        query: Keywords describing the spec, such as "click rate".
        limit: The most matches to return.
    """
    keywords = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    scored = []
    for spec in _bucket(ctx.deps.spec_cache, spec_type).values():
        fields = {k: v.lower() for k, v in _spec_fields(spec).items()}
        score = sum(
            weight
            for kw in keywords
            for field, weight in _FIELD_WEIGHTS.items()
            if kw in fields.get(field, "")
        )
        if score:
            scored.append((score, spec.name, spec.description))
    scored.sort(key=lambda m: (-m[0], m[1]))
    matches = [{"name": n, "description": d} for _, n, d in scored[:limit]]
    return ToolOutput(
        matches,
        "object",
        "spec_matches",
        f"{len(matches)} {spec_type}s match {query!r}",
        status=Status.ok if matches else Status.empty,
        facts={"matches": matches},
    )


@artifact_tool
def read_spec(ctx: RunContext[SessionDeps], spec_type: SpecType, name: str) -> ToolOutput:
    """Return a spec in full, as YAML.

    Args:
        spec_type: The type of the spec.
        name: The spec's name.
    """
    cache = ctx.deps.spec_cache
    getter: dict[str, Any] = {
        "metric": cache.get_metric,
        "slice": cache.get_slice,
        "segment": cache.get_segment,
    }
    payload = {"spec_type": spec_type, "name": name, "yaml": getter[spec_type](name).to_yaml()}
    return ToolOutput(payload, "object", "spec", f"{spec_type} {name}", facts=payload)


@artifact_tool
def check_compatibility(
    ctx: RunContext[SessionDeps],
    metrics: list[str],
    slices: list[str] = [],  # noqa: B006
    segments: list[str] = [],  # noqa: B006
) -> ToolOutput:
    """Check which slices and segments each metric supports, against the metric's source table.

    Args:
        metrics: Metric names.
        slices: Slice names; a slice fits when its columns exist in the metric's source table.
        segments: Segment names; a segment fits when one of its join keys exists there.
    """
    cache = ctx.deps.spec_cache
    sub = SpecCache()
    for name in metrics:
        sub.add(cache.get_metric(name))
    named_slices = [cache.get_slice(n) for n in slices]
    # A composite slice's components go in first: SpecCache.add() checks its references.
    components = [c for s in named_slices if s.is_composite for c in s.cross_product]
    for name in dict.fromkeys([*components, *slices]):
        if name not in sub.slices:
            sub.add(cache.get_slice(name))
    for name in segments:
        sub.add(cache.get_segment(name))

    with ctx.deps.compute_guard():
        scan = MetricCompute(sub, ctx.deps.connection_manager).scan()
    named = set(slices) | set(segments)
    verdicts: list[dict[str, Any]] = [
        {
            "metric": r.metric_name,
            "spec": r.spec_name,
            "spec_type": r.spec_type,
            "compatible": r.compatible,
            "reason": r.reason,
        }
        for r in scan.results
        if r.spec_name in named
    ]
    skipped = sorted(set(metrics) - {r.metric_name for r in scan.results}) if named else []
    fits = sum(v["compatible"] for v in verdicts)
    summary = f"{fits} of {len(verdicts)} metric pairings compatible"
    if skipped:
        summary += f"; source unavailable for {skipped}"
    facts = {"verdicts": verdicts, "skipped_metrics": skipped}
    return ToolOutput(facts, "object", "compatibility", summary, facts=facts)


@artifact_tool
def validate_spec(ctx: RunContext[SessionDeps], spec_type: SpecType, yaml_text: str) -> ToolOutput:
    """Check a spec draft and store it; `commit_spec` saves a draft that passes.

    Args:
        spec_type: The type of the spec.
        yaml_text: The spec as YAML, under a top-level `metric:`, `slice:`, or `segment:` key.
    """
    draft = _check_draft(ctx.deps, spec_type, yaml_text)
    ok = not draft["issues"]
    summary = f"{spec_type} {draft['name']}: " + (
        "valid" if ok else f"{len(draft['issues'])} issues"
    )
    facts = {k: draft[k] for k in ("name", "issues", "warnings")}
    return ToolOutput(
        draft,
        "object",
        "spec_draft",
        summary,
        status=Status.ok if ok else Status.error,
        facts=facts,
    )


@artifact_tool
def commit_spec(ctx: RunContext[SessionDeps], draft_id: str) -> ToolOutput:
    """Save a validated spec draft to the catalog, adding it or replacing the spec of that name.

    Args:
        draft_id: The artifact ID `validate_spec` returned.
    """
    deps = ctx.deps
    ref = deps.store.get(draft_id)
    if ref.type_tag != "spec_draft":
        raise ValueError(f"{draft_id} is a {ref.type_tag}; commit_spec takes a spec_draft.")
    stored = deps.store.read(draft_id)
    draft = _check_draft(deps, stored["spec_type"], stored["yaml"])
    if draft["issues"]:
        raise ValueError(f"The draft no longer validates: {draft['issues']}")
    spec_type = draft["spec_type"]
    spec = _SPEC_CLASSES[spec_type].from_yaml(draft["yaml"])
    bucket = _bucket(deps.spec_cache, spec_type)
    previous = bucket.get(spec.name)
    if previous is None:
        deps.spec_cache.add(spec)
    else:
        deps.spec_cache.update(spec)
    try:
        _save(deps)
    except Exception:
        if previous is None:
            deps.spec_cache.remove(spec_type, spec.name)
        else:
            deps.spec_cache.update(previous)
        raise
    action = "added" if previous is None else "updated"
    payload = {"spec_type": spec_type, "name": spec.name, "action": action}
    return ToolOutput(
        payload,
        "object",
        "spec_commit",
        f"{action} {spec_type} {spec.name}",
        [draft_id],
        facts=payload,
    )


@artifact_tool
def delete_spec(ctx: RunContext[SessionDeps], spec_type: SpecType, name: str) -> ToolOutput:
    """Delete a spec from the catalog and its saved file.

    Args:
        spec_type: The type of the spec.
        name: The spec's name.
    """
    deps = ctx.deps
    previous = _bucket(deps.spec_cache, spec_type).get(name)
    deps.spec_cache.remove(spec_type, name)
    try:
        _save(deps)
    except Exception:
        deps.spec_cache.add(previous)
        raise
    payload = {"spec_type": spec_type, "name": name}
    return ToolOutput(
        payload, "object", "spec_delete", f"deleted {spec_type} {name}", facts=payload
    )


@artifact_tool
def compute_metrics(ctx: RunContext[SessionDeps], params: MetricComputeParams) -> ToolOutput:
    """Compute catalog metrics as a lazy result on the source backend; stats tools take its ID.

    Args:
        params: The metrics, their slices and segment, and the time window.
    """
    deps = ctx.deps
    with deps.compute_guard():
        table = MetricCompute(deps.spec_cache, deps.connection_manager).compute(
            metrics=params.metrics,
            slices=params.slices or None,
            segments=params.segment,
            time_window=params.time_window,
            period_type=params.period_type,
            by_entity=params.by_entity,
        )
    by = [*params.slices, *([params.segment] if params.segment else [])]
    summary = ", ".join(params.metrics) + (f" by {', '.join(by)}" if by else "")
    summary += f", {params.period_type}"
    formats = {
        m: f for m in params.metrics if (f := deps.spec_cache.get_metric(m).format) is not None
    }
    facts = {**params.model_dump(mode="json", exclude_defaults=True), "formats": formats}
    return ToolOutput(table, "ibis", "metric_result", summary, facts=facts)


aitaem_toolset: FunctionToolset[SessionDeps] = FunctionToolset(
    [
        connect_source,
        list_specs,
        find_specs,
        read_spec,
        check_compatibility,
        validate_spec,
        commit_spec,
        delete_spec,
        compute_metrics,
    ]
)
