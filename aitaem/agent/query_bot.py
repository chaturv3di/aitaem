from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from aitaem.agent.base import Bot, _register_tool
from aitaem.agent.response import BotResponse
from aitaem.agent.query_types import QueryDeps, QueryOutput, QueryPayload
from aitaem.agent.query_tools import (
    record_intent,
    resolve_intent,
    compute_metrics,
    rank_by_value,
    filter_by_threshold,
    distribution_summary,
    period_over_period,
    contribution_share,
)
from aitaem.agent.trace import Status


def _build_extra_toolset(extra_tools: list[Any] | None) -> Any | None:
    """Build an ephemeral FunctionToolset from extra_tools, or None if empty.

    Passed to agent.run(toolsets=[...]), which is additive to the bot's
    persistent toolset — never touches self._toolset.
    """
    if not extra_tools:
        return None
    from pydantic_ai.toolsets import FunctionToolset

    toolset = FunctionToolset()
    for tool in extra_tools:
        _register_tool(toolset, tool)
    return toolset


class QueryResponse(BotResponse[QueryPayload]):
    """Concrete response type for QueryBot — narrows BotResponse's generic payload."""


# ── System prompt builders ───────────────────────────────────────────────────

def _build_layer_a() -> str:
    """Layer A: stable workflow and rules (identical for all tenants)."""
    return """\
# ─── Layer A: workflow & rules ───────────────────────────────────────────────

You are a data analysis assistant for an AITAEM metrics platform. You answer
questions by resolving them against a defined catalog and calling tools. Never
invent values; every number must come from a tool call.

## Workflow — three required steps, in order

### Step 1 — record_intent
Call `record_intent` once per metric the user is asking about. Fields:
- metric_concept: free-text name (e.g. "click-through rate")
- scope: "overall" (unfiltered) or "subset" (filter specified)
- slice_type / slice_value: for breakdowns or a specific slice member
- segment_name / segment_value: for entity-level filters
- period_type: "all_time" | "hourly" | "daily" | "weekly" | "monthly" | "yearly"
  (default: all_time). Non-"all_time" requires time_window.
- time_window: [start, end] ISO dates. Hourly uses YYYY-MM-DDTHH:MM:SS,
  floored to the hour.
- by_entity: only for entity-level questions ("which user", "top 10 advertisers").

Returns: intent_id (integer).

### Step 2 — resolve_intent
Call `resolve_intent` with the intent_id and your proposed canonical names
(metric_name, slices, segment) drawn from the catalog.

Returns:
- exact_match: {spec_token, metric_name, slices, segment} if the proposal is
  valid — proceed to Step 3.
- near_misses: [{name, why_not}] — specs that came close but did not match.

If exact_match is null: STOP. Do not call compute_metrics. Set status="refused"
and cite near_misses in the reason. See Metric Precision Rule below.

### Step 3 — compute_metrics
Call `compute_metrics(spec_token=...)`. All compute parameters are encoded in
the token; pass nothing else. Returns result_id and optional warnings.

Each spec_token is single-use. If you receive an "already-consumed" error,
check for a concurrent compute_metrics result carrying the same token and use
its result_id — do not call resolve_intent again.

## Analysis tools

Use these on a result_id from compute_metrics (or from a prior analysis call).
Each produces a new result_id.

| If the question asks for…                                  | Tool                        |
|------------------------------------------------------------|-----------------------------|
| Top / bottom N entities, slice members, or periods         | rank_by_value               |
| Rows above / below a value; who exceeds a target           | filter_by_threshold         |
| Distribution, percentile rank, count above/below median    | distribution_summary        |
| Ranking all members of one dimension                       | rank_by_value (n=None)      |
| Growth or decline across periods; period deltas            | period_over_period          |
| Share of total; concentration in top X% or top N           | contribution_share          |

Rules:
- Complete Steps 1–3 first. Pass the result_id to analysis tools.
- With ≤ 20 rows and no analytical intent, skip analysis tools; narrate directly.
- Time-series questions (period_type ≠ all_time) always narrate from compute_metrics.
- For "which entities are above/below the median": call distribution_summary
  to get p50, then call filter_by_threshold with that value.

## Metric Precision Rule (CRITICAL)

- Never substitute an approximate metric. CTR ≠ conversion rate.
  Revenue ≠ profit. Sessions ≠ unique users.
- If resolve_intent returns exact_match=null: set status="refused",
  cite near_misses[].name + why_not in the reason, and prompt the user to
  refine or define a new spec.

## Final Response

After tool calls, produce a QueryOutput:
- status: "ok" if data was returned; "empty" if zero rows; "refused" if
  resolution failed or out of scope; "error" if a tool returned an error.
- narrative: plain-language explanation referencing tool-returned values.
- result_ids: list of result_ids, primary/most relevant first. Empty unless
  status="ok".
- reason: brief note when status is "refused" or "error". Null otherwise.

## Value Formatting

Read format_hints from the compute_metrics result and apply them yourself
when writing the narrative. Common values: "percent" → format as a percentage
(e.g. 4.2%); "currency" → add currency symbol (e.g. $1,234); "integer" →
round to whole number. If format_hints is empty, use plain numeric values.

## Multi-Metric Questions

If the user asks about multiple metrics, run Steps 1–3 independently for each."""


_LARGE_CATALOG_THRESHOLD = 32


def _build_layer_b(spec_cache: Any) -> str:
    """Layer B: per-tenant spec catalog (session-stable).

    Above 32 metrics, the catalog is replaced with a one-liner directing the
    LLM to rely on resolve_intent for catalog search (v1 RAG path).
    """
    n_metrics = len(spec_cache.metrics)
    if n_metrics > _LARGE_CATALOG_THRESHOLD:
        return (
            "# ─── Layer B: catalog ──────────────────────────────────────────────────────\n\n"
            "## SPEC CATALOG\n"
            "Call resolve_intent to search the catalog. Do not enumerate metrics from memory."
        )

    metric_lines = []
    for name, spec in spec_cache.metrics.items():
        parts = [f"- **{name}**: {spec.description or '(no description)'}"]
        if spec.entities:
            parts.append(f"  Entities: {', '.join(spec.entities)}")
        metric_lines.append("\n".join(parts))
    # Note: spec.format intentionally omitted — format_hints are returned by
    # compute_metrics at narrative time; they are not needed for metric selection.

    slice_lines = [
        f"- **{name}**: {spec.description or '(no description)'}"
        for name, spec in spec_cache.slices.items()
    ]
    segment_lines = [
        f"- **{name}**: {spec.description or '(no description)'}"
        for name, spec in spec_cache.segments.items()
    ]

    catalog = "\n".join([
        "## Metrics",
        "\n".join(metric_lines) or "(none)",
        "",
        "## Slices",
        "\n".join(slice_lines) or "(none)",
        "",
        "## Segments",
        "\n".join(segment_lines) or "(none)",
    ])

    return (
        "# ─── Layer B: catalog (per-tenant, session-stable) ─────────────────────────\n\n"
        "## SPEC CATALOG\n"
        + catalog
    )


def _permission_fingerprint(spec_cache: Any) -> str:
    """8-char hex fingerprint of the visible catalog (metrics + slices + segments).

    Two spec_caches with the same visible keys produce the same fingerprint and
    share an OpenAI routing lane. Different keys (e.g. post-RBAC) produce
    different fingerprints and get separate lanes with no cross-eviction.

    "|" separates categories; "," separates names within a category.
    Both characters are illegal in YAML keys, so no collision is possible.
    """
    parts = (
        sorted(spec_cache.metrics.keys()),
        sorted(spec_cache.slices.keys()),
        sorted(spec_cache.segments.keys()),
    )
    payload = "|".join(",".join(p) for p in parts)
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def _provider_cache_config(model_str: str, tenant_id: str | None) -> dict:
    """Return model_settings for prompt caching, keyed by provider prefix.

    Anthropic: cache_control breakpoints are explicit — requires
    anthropic_cache_instructions to mark where the static prefix ends.
    Verified against pydantic-ai 2.2.0's anthropic adapter.

    OpenAI: prompt_cache_key is a routing hint — the server routes requests
    sharing the same key to the same backend pool for prefix-cache hits. A
    shared key across tenants concentrates routing to a smaller pool and causes
    cross-tenant eviction contention. Key on tenant instead.

    tenant_id is used directly when provided. When None (single-tenant /
    self-hosted), a short hash of the sorted metric/slice/segment names serves
    as a stable per-permission-set key. Two users with the same catalog see the
    same routing lane, which is correct — their Layer B is identical.

    prompt_cache_retention="24h" keeps the cached prefix warm across sessions.
    Verified against pydantic-ai 2.2.0's OpenAI model settings.

    Other providers: return {} (no-op; stable-content-first ordering still
    helps server-side caching where it exists).
    """
    if not isinstance(model_str, str):
        return {}
    provider = model_str.split(":")[0] if ":" in model_str else ""
    if provider == "anthropic":
        return {"anthropic_cache_instructions": "5m"}
    if provider == "openai":
        return {
            "openai_prompt_cache_key": f"aitaem-{tenant_id}",
            "openai_prompt_cache_retention": "24h",
        }
    return {}


# ── QueryBot ─────────────────────────────────────────────────────────────────

class QueryBot(Bot):
    """Convenience bot for answering natural-language questions against a metric catalog.

    Tools create a MetricCompute instance per call from the held spec_cache and
    connection_manager. Artifacts are written to the bot's ResultStore; callers
    dereference via get_result(result_id).

    Construction:
        bot = QueryBot(
            model="anthropic:claude-sonnet-4-6",
            spec_cache=my_spec_cache,
            connection_manager=my_connection_manager,
        )
        response = await bot.chat("What was Q4 revenue by region?")

    Multi-provider:
        Use model strings supported by pydantic-ai, e.g. "openai:gpt-4o".
        For testing, pass a FunctionModel or TestModel instance directly.

    tenant_id:
        Optional per-tenant identifier for OpenAI prompt-cache routing.
        When omitted, a fingerprint of the spec_cache's visible catalog is used,
        which naturally separates RBAC-differentiated permission sets.
    """

    def __init__(
        self,
        *,
        model: Any,
        spec_cache: Any,
        connection_manager: Any,
        tenant_id: str | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        # Set bot-specific resources BEFORE super().__init__() — _build_agent()
        # is called inside super().__init__() and needs these attributes.
        self._spec_cache = spec_cache
        self._connection_manager = connection_manager
        self._tenant_id = tenant_id
        super().__init__(model=model, tools=tools)
        self._conversation_id: str | None = None

    def _build_agent(self) -> Any:
        from pydantic_ai import Agent
        from pydantic_ai.toolsets import FunctionToolset
        from pydantic_ai.capabilities import ReinjectSystemPrompt

        toolset = FunctionToolset()
        toolset.add_function(record_intent)        # Step 1
        toolset.add_function(resolve_intent)       # Step 2
        toolset.add_function(compute_metrics)      # Step 3
        toolset.add_function(rank_by_value)
        toolset.add_function(filter_by_threshold)
        toolset.add_function(distribution_summary)
        toolset.add_function(period_over_period)
        toolset.add_function(contribution_share)

        for tool in self._tools:
            _register_tool(toolset, tool)
        self._toolset = toolset

        # Static instructions: Layers A + B combined.
        # These become InstructionPart(dynamic=False) and are cached at the
        # provider-appropriate breakpoint (see _provider_cache_config above).
        static_instructions = _build_layer_a() + "\n\n" + _build_layer_b(self._spec_cache)

        # Derive a stable routing key for OpenAI. Explicit tenant_id wins; fall back
        # to _permission_fingerprint so single-tenant installs require zero config and
        # RBAC-differentiated users naturally land in separate routing lanes.
        tenant_id = self._tenant_id or _permission_fingerprint(self._spec_cache)

        agent = Agent(  # type: ignore[call-overload]
            model=self._model,
            deps_type=QueryDeps,
            output_type=QueryOutput,
            toolsets=[toolset],
            instructions=static_instructions,
            # anthropic_cache_instructions: verified against pydantic-ai 2.2.0's
            # anthropic adapter. InstructionPart.sorted() sorts static (dynamic=False)
            # before dynamic parts; the adapter then sets
            # cache_block_idx = num_prefix_blocks + num_static - 1, placing the
            # cache_control breakpoint after the last static block (Layer B). Layer C
            # follows as a dynamic block and is NOT cached. Other providers ignore this.
            model_settings=_provider_cache_config(self._model, tenant_id),
            capabilities=[ReinjectSystemPrompt(replace_existing=True)],
        )

        # Layer C: per-turn date context (dynamic=True → NOT cached).
        # Registered here, after the agent is built, so it captures today's date on each run.
        @agent.instructions
        def _layer_c() -> str:
            from datetime import date
            return (
                f"# ─── Layer C: per-turn context ─────────────────────────────────────────────\n\n"
                f"Today is {date.today().isoformat()}. Use it to resolve relative time references "
                f'("last month", "recently", "May") into concrete time_window values before '
                f"calling record_intent."
            )

        return agent

    async def chat(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> QueryResponse:
        """Send a message in multi-turn mode. Accumulates history on the bot.

        Always returns a QueryResponse — exceptions from the agent run are caught
        and surfaced as status=error rather than propagated raw.
        """
        from datetime import datetime, timezone
        from aitaem.agent.trace import assemble_trace

        run_start = datetime.now(timezone.utc)
        deps = QueryDeps(
            spec_cache=self._spec_cache,
            connection_manager=self._connection_manager,
            store=self._store,
        )
        try:
            run_kwargs: dict[str, Any] = {
                "message_history": self._message_history,
                "deps": deps,
            }
            extra_toolset = _build_extra_toolset(extra_tools)
            if extra_toolset is not None:
                run_kwargs["toolsets"] = [extra_toolset]
            result = await self._agent.run(message, **run_kwargs)
            self._message_history = result.all_messages()
            output = cast(QueryOutput, result.output)
            trace = assemble_trace(result, run_start)
            self._conversation_id = trace.conversation_id
            payload = QueryBot._assemble_payload(output, trace)
            return QueryResponse(
                status=output.status,
                narrative=output.narrative,
                trace=trace,
                reason=output.reason,
                payload=payload,
            )
        except Exception as exc:
            return QueryBot._error_response(exc, run_start, self._conversation_id)

    async def ask(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> QueryResponse:
        """Send a single-turn message. Does NOT accumulate history.

        Always returns a QueryResponse — exceptions from the agent run are caught
        and surfaced as status=error rather than propagated raw.
        """
        from datetime import datetime, timezone
        from aitaem.agent.trace import assemble_trace

        run_start = datetime.now(timezone.utc)
        deps = QueryDeps(
            spec_cache=self._spec_cache,
            connection_manager=self._connection_manager,
            store=self._store,
        )
        try:
            run_kwargs: dict[str, Any] = {"deps": deps}
            extra_toolset = _build_extra_toolset(extra_tools)
            if extra_toolset is not None:
                run_kwargs["toolsets"] = [extra_toolset]
            result = await self._agent.run(message, **run_kwargs)
            output = cast(QueryOutput, result.output)
            trace = assemble_trace(result, run_start)
            self._conversation_id = trace.conversation_id
            payload = QueryBot._assemble_payload(output, trace)
            return QueryResponse(
                status=output.status,
                narrative=output.narrative,
                trace=trace,
                reason=output.reason,
                payload=payload,
            )
        except Exception as exc:
            return QueryBot._error_response(exc, run_start, self._conversation_id)

    @staticmethod
    def _error_response(
        exc: Exception, run_start: Any, conversation_id: str | None
    ) -> QueryResponse:
        """Build a status=error QueryResponse when _agent.run() raises."""
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
        return QueryResponse(
            status=Status.error,
            narrative="The request could not be completed due to an unexpected error.",
            trace=trace,
            reason=str(exc),
            payload=QueryPayload(
                result_ids=[], primary_result_id=None,
                metrics_used=[], slices_used=[], segment_used=None,
                time_window=None, period_type="all_time", by_entity=None,
            ),
        )

    @staticmethod
    def _assemble_payload(output: QueryOutput, trace: Any) -> QueryPayload:
        """Assemble QueryPayload from the LLM's QueryOutput and the turn trace.

        Reads payload_summary from each tool's llm_summary (JSON-serialized
        ToolResult). Tool-agnostic: no per-tool field access needed.

        Aggregation rules across multiple tool calls:
          list fields  — union with deduplication, order of first appearance
          scalar fields — first-write wins (first call that sets a field governs)
        """
        primary_result_id = output.result_ids[0] if output.result_ids else None
        metrics_used: list[str] = []
        slices_used: list[str] = []
        seen_metrics: set[str] = set()
        seen_slices: set[str] = set()
        segment_used: str | None = None
        time_window: tuple[str, str] | None = None
        period_type: str | None = None
        by_entity: str | None = None
        format_hints: dict[str, str] = {}
        sample: list[dict[str, Any]] | None = None

        for tc in trace.tool_calls:
            if not tc.llm_summary:
                continue
            try:
                summary = json.loads(tc.llm_summary)
            except (ValueError, TypeError):
                continue
            ps = summary.get("payload_summary")
            if not ps:
                continue
            for m in ps.get("metrics_used") or []:
                if m not in seen_metrics:
                    seen_metrics.add(m)
                    metrics_used.append(m)
            for s in ps.get("slices_used") or []:
                if s not in seen_slices:
                    seen_slices.add(s)
                    slices_used.append(s)
            if segment_used is None and ps.get("segment_used"):
                segment_used = ps["segment_used"]
            if time_window is None and ps.get("time_window"):
                tw = ps["time_window"]
                time_window = (tw[0], tw[1]) if isinstance(tw, (list, tuple)) and len(tw) == 2 else None
            if period_type is None and ps.get("period_type"):
                period_type = ps["period_type"]
            if by_entity is None and ps.get("by_entity"):
                by_entity = ps["by_entity"]
            for metric, fmt in (ps.get("format_hints") or {}).items():
                if metric not in format_hints:
                    format_hints[metric] = fmt
            if sample is None and primary_result_id and ps.get("result_id") == primary_result_id:
                raw = ps.get("sample")
                sample = raw if isinstance(raw, list) else None

        return QueryPayload(
            result_ids=output.result_ids,
            primary_result_id=primary_result_id,
            metrics_used=metrics_used,
            slices_used=slices_used,
            segment_used=segment_used,
            time_window=time_window,
            period_type=period_type or "all_time",
            by_entity=by_entity,
            format_hints=format_hints,
            sample=sample,
        )
