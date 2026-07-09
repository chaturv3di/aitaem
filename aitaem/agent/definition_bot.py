"""DefinitionBot — single-turn spec-definition bot (SF-7 through SF-9).

Primary entry point is ask(). chat() is provided for cross-turn context but
multi-turn interactive refinement is deferred to v1.x (ND-10).
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from aitaem.agent.base import Bot
from aitaem.agent.response import BotResponse
from aitaem.agent.definition_types import (
    DefinitionDeps,
    DefinitionOutput,
    DefinitionPayload,
)
from aitaem.agent.definition_tools import (
    record_definition_intent,
    list_tables,
    describe_table,
    draft_spec,
    validate_spec,
    _parse_yaml_to_spec,
)
from aitaem.agent.store import ResultStore
from aitaem.agent.trace import Status


class DefinitionResponse(BotResponse[DefinitionPayload]):
    """Concrete response type for DefinitionBot — narrows BotResponse's generic payload."""


# ── System prompt builders ───────────────────────────────────────────────────

_LARGE_CATALOG_THRESHOLD = 32


def _build_layer_a_definition() -> str:
    """Layer A: stable workflow and rules (identical for all tenants)."""
    return """\
# ─── Layer A: workflow & rules ───────────────────────────────────────────────

You are a spec-definition assistant for an AITAEM metrics platform. You help
users define MetricSpec, SliceSpec, and SegmentSpec YAML definitions by
exploring the schema, drafting YAML, and validating it before returning.
Never invent column names or table names; all data must come from tool calls.

## 4-Step Workflow — follow in order

### Step 1 — record_definition_intent
Call first, once per spec. Fields:
- spec_type: "metric" | "slice" | "segment"
- description: natural-language description of what the spec should compute
- existing_yaml (optional): YAML string of an existing spec to update

When existing_yaml is provided, is_update=True and the spec name is locked —
validate_spec will reject any name change. Treat existing_yaml as the starting
point for the YAML draft.

One spec per turn. If the user asks for multiple specs, handle the first and
suggest calling ask() again for the remaining ones.

### Step 2 — list_tables and describe_table
Explore the schema before drafting.

- `list_tables()` — list all tables across backends; optionally filter by backend_type
- `describe_table(table_name, backend_type)` — get column names and types for a table

Always call describe_table on the primary source table before drafting. The
backend_type is always shown in list_tables results; pass it explicitly —
never guess or omit it.

### Step 3 — draft_spec
Call with the YAML string you intend to validate. No validation occurs here.
Each call creates a new draft entry; repeated calls for corrections produce
distinct draft_ids. Returns draft_id and yaml_preview.

The yaml_string must be a complete, top-level keyed YAML:
  metric: ...   OR   slice: ...   OR   segment: ...

### Step 4 — validate_spec
Call with the draft_id from draft_spec. Runs five checks:
1. Draft exists
2. YAML parses correctly and passes structural/SQL validation
3. Name does not conflict with existing specs (or, on is_update, name is unchanged)
4. Composite slice cross_product references exist (slice only)
5. Referenced columns exist in the live schema (best-effort; warnings only)

On errors: fix the YAML and call draft_spec again with the corrected version,
then call validate_spec with the new draft_id. Repeat until validation passes.

On full pass: returns spec_draft_token. Copy this token verbatim into the
DefinitionOutput.spec_draft_token field — this is the anti-hallucination gate.
Never set spec_draft_token without a valid validate_spec result.

## YAML Format Reference

### MetricSpec
```yaml
metric:
  name: <unique snake_case identifier>
  description: <optional human-readable description>
  source: <source URI, e.g. duckdb://mydb.db/events>
  numerator: <SQL expression, e.g. COUNT(*) or SUM(revenue)>
  denominator: <optional SQL expression for ratio metrics>
  timestamp_col: <column name for time-based aggregation>
  entities: [<entity_id_col>, ...]      # optional
  format: percent | currency:USD | integer  # optional
```

### SliceSpec — leaf (explicit values)
```yaml
slice:
  name: <unique snake_case identifier>
  description: <optional>
  values:
    - name: <member_label>
      where: <SQL predicate, e.g. "country = 'US'">
    - name: <another_label>
      where: <another SQL predicate>
```

### SliceSpec — wildcard (auto-discover from column)
```yaml
slice:
  name: <unique snake_case identifier>
  description: <optional>
  where: <column name, e.g. country>
```

### SliceSpec — composite (cross-product of other slices)
```yaml
slice:
  name: <unique snake_case identifier>
  description: <optional>
  cross_product:
    - <existing_slice_name>
    - <another_existing_slice_name>
```

### SegmentSpec
```yaml
segment:
  name: <unique snake_case identifier>
  description: <optional>
  source: <source URI>
  entity_id: <column that identifies the entity>
  values:
    - name: <segment_label>
      where: <SQL predicate>
  join_keys: [<col>, ...]  # optional; columns to join on
```

## Source URI Format

URIs encode the backend and table:

| Backend  | Format                               | Example                            |
|----------|--------------------------------------|------------------------------------|
| DuckDB   | duckdb://<db_path>/<table>           | duckdb://analytics.db/events       |
| BigQuery | bigquery://<project>/<dataset>.<tbl> | bigquery://myproject/ds.sales      |
| Postgres | postgres://<schema>/<table>          | postgres://public/events           |

Infer the URI format from the existing catalog in Layer B when unsure.

## Spec Precision Rule (CRITICAL)

- Never set spec_draft_token without a successful validate_spec result.
- Never invent column names; always call describe_table first.
- Never invent table names; always call list_tables first.
- If the requested spec cannot be defined from available data, set status="refused"
  and explain what data is missing.

## Final Response

After the validate_spec loop succeeds, produce a DefinitionOutput:
- status: "ok" on success; "refused" if spec cannot be defined; "error" on tool failure
- narrative: natural-language explanation of what was defined and any warnings
- spec_draft_token: copy verbatim from validate_spec result (null if status≠ok)
- reason: brief note when status is "refused" or "error"; null otherwise"""


def _build_layer_b_definition(spec_cache: Any) -> str:
    """Layer B: per-tenant existing catalog (session-stable).

    All spec names are always listed to avoid name-conflict round-trips.
    Slice subtype (leaf/wildcard/composite) is always shown alongside the name.
    Source URI and description are shown only below _LARGE_CATALOG_THRESHOLD.
    """
    n_total = len(spec_cache.metrics) + len(spec_cache.slices) + len(spec_cache.segments)
    show_details = n_total <= _LARGE_CATALOG_THRESHOLD

    # ── Metrics ──
    metric_lines: list[str] = []
    for name, spec in spec_cache.metrics.items():
        if show_details:
            desc = spec.description or "(no description)"
            source = getattr(spec, "source", "(unknown source)")
            metric_lines.append(f"- **{name}**: {desc}\n  source: {source}")
        else:
            metric_lines.append(f"- {name}")

    # ── Slices ──
    slice_lines: list[str] = []
    for name, spec in spec_cache.slices.items():
        if spec.is_composite:
            subtype = "(composite)"
        elif spec.is_wildcard:
            subtype = "(wildcard)"
        else:
            subtype = "(leaf)"

        if show_details:
            desc = spec.description or "(no description)"
            slice_lines.append(f"- **{name}** {subtype}: {desc}")
        else:
            slice_lines.append(f"- {name} {subtype}")

    # ── Segments ──
    segment_lines: list[str] = []
    for name, spec in spec_cache.segments.items():
        if show_details:
            desc = spec.description or "(no description)"
            source = getattr(spec, "source", "(unknown source)")
            segment_lines.append(f"- **{name}**: {desc}\n  source: {source}")
        else:
            segment_lines.append(f"- {name}")

    detail_note = "" if show_details else "\n(Details omitted — catalog exceeds display threshold. Call list_tables to explore schema.)\n"

    catalog = "\n".join([
        "## Existing Metrics",
        "\n".join(metric_lines) or "(none)",
        "",
        "## Existing Slices",
        "\n".join(slice_lines) or "(none)",
        "",
        "## Existing Segments",
        "\n".join(segment_lines) or "(none)",
        detail_note,
    ])

    return (
        "# ─── Layer B: existing catalog (per-tenant, session-stable) ────────────────\n\n"
        "## SPEC CATALOG\n"
        + catalog
    )


def _definition_permission_fingerprint(spec_cache: Any) -> str:
    """8-char MD5 hex of sorted metric + slice + segment key sets."""
    parts = (
        sorted(spec_cache.metrics.keys()),
        sorted(spec_cache.slices.keys()),
        sorted(spec_cache.segments.keys()),
    )
    payload = "|".join(",".join(p) for p in parts)
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def _provider_cache_config_definition(model_str: str, tenant_id: str | None) -> dict:
    """Return model_settings for prompt caching — mirrors QueryBot's _provider_cache_config."""
    if not isinstance(model_str, str):
        return {}
    provider = model_str.split(":")[0] if ":" in model_str else ""
    if provider == "anthropic":
        return {"anthropic_cache": "5m"}
    if provider == "openai":
        return {
            "openai_prompt_cache_key": f"aitaem-def-{tenant_id}",
            "openai_prompt_cache_retention": "24h",
        }
    return {}


# ── DefinitionBot ─────────────────────────────────────────────────────────────

class DefinitionBot(Bot):
    """Convenience bot for defining MetricSpec, SliceSpec, and SegmentSpec.

    Uses a 4-step token-gated workflow: record_definition_intent → list_tables /
    describe_table → draft_spec → validate_spec loop → DefinitionOutput.

    The bot is primarily a single-turn bot — use ask() for each new spec.
    chat() is provided for cross-turn context but multi-turn revision re-drafts
    from scratch (drafts are ephemeral per run(); see ND-10).

    Construction:
        bot = DefinitionBot(
            model="anthropic:claude-sonnet-4-6",
            connection_manager=my_connection_manager,
            spec_cache=my_spec_cache,
        )
        response = await bot.ask("Define a metric for weekly active users on the events table.")
    """

    def __init__(
        self,
        *,
        model: Any,
        connection_manager: Any,
        spec_cache: Any,
        tenant_id: str | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        # Set bot-specific resources BEFORE super().__init__() — _build_agent()
        # is called inside super().__init__() and needs these attributes.
        self._connection_manager = connection_manager
        self._spec_cache = spec_cache
        self._tenant_id = tenant_id
        super().__init__(model=model, tools=tools)
        self._conversation_id: str | None = None

    def _build_agent(self) -> Any:
        from pydantic_ai import Agent
        from pydantic_ai.toolsets import FunctionToolset
        from pydantic_ai.capabilities import ReinjectSystemPrompt

        toolset = FunctionToolset()
        toolset.add_function(record_definition_intent)  # Step 1
        toolset.add_function(list_tables)               # Step 2a
        toolset.add_function(describe_table)            # Step 2b
        toolset.add_function(draft_spec)                # Step 3
        toolset.add_function(validate_spec)             # Step 4

        static_instructions = (
            _build_layer_a_definition()
            + "\n\n"
            + _build_layer_b_definition(self._spec_cache)
        )

        tenant_id = self._tenant_id or _definition_permission_fingerprint(self._spec_cache)

        agent = Agent(  # type: ignore[call-overload]
            model=self._model,
            deps_type=DefinitionDeps,
            output_type=DefinitionOutput,
            toolsets=[toolset],
            instructions=static_instructions,
            model_settings=_provider_cache_config_definition(self._model, tenant_id),
            capabilities=[ReinjectSystemPrompt(replace_existing=True)],
        )

        @agent.instructions
        def _layer_c() -> str:
            from datetime import date

            return (
                "# ─── Layer C: per-turn context ─────────────────────────────────────────────\n\n"
                f"Today is {date.today().isoformat()}."
            )

        return agent

    async def ask(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> DefinitionResponse:
        """Send a single-turn message. Does NOT accumulate history.

        Each ask() call is fully independent — drafts from prior ask() calls
        are not accessible. Use chat() for cross-turn context.
        """
        from datetime import datetime, timezone
        from aitaem.agent.trace import assemble_trace

        run_start = datetime.now(timezone.utc)
        deps = DefinitionDeps(
            connection_manager=self._connection_manager,
            spec_cache=self._spec_cache,
            store=self._store,
        )
        try:
            result = await self._agent.run(message, deps=deps)
            output = cast(DefinitionOutput, result.output)
            trace = assemble_trace(result, run_start)
            self._conversation_id = trace.conversation_id
            payload = DefinitionBot._assemble_payload(output, self._store)
            return DefinitionResponse(
                status=output.status,
                narrative=output.narrative,
                trace=trace,
                reason=output.reason,
                payload=payload,
            )
        except Exception as exc:
            return DefinitionBot._error_response(exc, run_start, self._conversation_id)

    async def chat(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> DefinitionResponse:
        """Send a message in multi-turn mode. Accumulates history on the bot.

        Drafts from prior turns are not accessible across runs — cross-turn
        revision re-drafts from scratch. For specs whose YAML exceeds 800 chars,
        cross-turn recovery may be lossy (ND-10).
        """
        from datetime import datetime, timezone
        from aitaem.agent.trace import assemble_trace

        run_start = datetime.now(timezone.utc)
        deps = DefinitionDeps(
            connection_manager=self._connection_manager,
            spec_cache=self._spec_cache,
            store=self._store,
        )
        try:
            result = await self._agent.run(
                message,
                message_history=self._message_history,
                deps=deps,
            )
            self._message_history = result.all_messages()
            output = cast(DefinitionOutput, result.output)
            trace = assemble_trace(result, run_start)
            self._conversation_id = trace.conversation_id
            payload = DefinitionBot._assemble_payload(output, self._store)
            return DefinitionResponse(
                status=output.status,
                narrative=output.narrative,
                trace=trace,
                reason=output.reason,
                payload=payload,
            )
        except Exception as exc:
            return DefinitionBot._error_response(exc, run_start, self._conversation_id)

    @staticmethod
    def _assemble_payload(
        output: DefinitionOutput, store: ResultStore
    ) -> DefinitionPayload:
        """Assemble DefinitionPayload from the LLM's DefinitionOutput.

        When status is not ok or spec_draft_token is None, returns an empty payload.
        Otherwise retrieves the validated YAML from the store, re-parses it, and
        populates the appropriate spec field.
        """
        if output.status != Status.ok or output.spec_draft_token is None:
            return DefinitionPayload()

        try:
            entry = store.get_text(output.spec_draft_token)
        except Exception:
            return DefinitionPayload()

        yaml_string = entry.text
        metadata = entry.metadata
        spec_type = metadata.get("spec_type")
        spec_name = metadata.get("spec_name")

        import json as _json
        raw_rc = metadata.get("referenced_columns")
        referenced_columns: dict[str, list[str]] | None = None
        if raw_rc:
            try:
                referenced_columns = _json.loads(raw_rc)
            except Exception:
                pass

        raw_warnings = metadata.get("warnings")
        warnings: list[str] = []
        if raw_warnings:
            try:
                warnings = _json.loads(raw_warnings)
            except Exception:
                pass

        metric_spec = None
        slice_spec = None
        segment_spec = None

        try:
            if spec_type == "metric":
                metric_spec = _parse_yaml_to_spec("metric", yaml_string)
            elif spec_type == "slice":
                slice_spec = _parse_yaml_to_spec("slice", yaml_string)
            elif spec_type == "segment":
                segment_spec = _parse_yaml_to_spec("segment", yaml_string)
        except Exception:
            # Re-parse exceptions are swallowed — YAML was already validated.
            pass

        return DefinitionPayload(
            spec_type=spec_type,  # type: ignore[arg-type]
            spec_name=spec_name,
            yaml_string=yaml_string,
            spec_draft_token=output.spec_draft_token,
            validation_warnings=warnings,
            referenced_columns=referenced_columns,
            metric_spec=metric_spec,
            slice_spec=slice_spec,
            segment_spec=segment_spec,
        )

    @staticmethod
    def _error_response(
        exc: Exception, run_start: Any, conversation_id: str | None
    ) -> DefinitionResponse:
        """Build a status=error DefinitionResponse when _agent.run() raises."""
        import uuid
        from aitaem.agent.trace import RunTrace, Usage

        trace = RunTrace(
            run_id=str(uuid.uuid4()),
            conversation_id=conversation_id or str(uuid.uuid4()),
            timestamp=run_start,
            tool_calls=[],
            usage=Usage(),
            error=f"{type(exc).__name__}: {exc}",
        )
        return DefinitionResponse(
            status=Status.error,
            narrative="The request could not be completed due to an unexpected error.",
            trace=trace,
            reason=str(exc),
            payload=DefinitionPayload(),
        )
