from __future__ import annotations

import json
from typing import Any, cast

from aitaem.agent.base import Bot
from aitaem.agent.response import BotResponse
from aitaem.agent.query_types import QueryDeps, QueryOutput, QueryPayload
from aitaem.agent.query_tools import (
    compute_metrics,
    rank_by_value,
    filter_by_threshold,
    distribution_summary,
    period_over_period,
    contribution_share,
)
from aitaem.agent.trace import Status


class QueryResponse(BotResponse[QueryPayload]):
    """Concrete response type for QueryBot — narrows BotResponse's generic payload."""


def _build_system_prompt(spec_cache: Any) -> str:
    """Build the QueryBot system prompt from a SpecCache instance.

    Includes: role, spec catalog, period types, Metric Precision Rule,
    format narration guidance, and QueryOutput filling instructions.
    Called once at _build_agent() time; result is a static string.
    """
    metric_lines = []
    for name, spec in spec_cache.metrics.items():
        parts = [f"- {name}: {spec.description or '(no description)'}"]
        if spec.entities:
            parts.append(f"  Entities: {', '.join(spec.entities)}")
        if spec.format:
            parts.append(f"  Format: {spec.format}")
        metric_lines.append("\n".join(parts))

    slice_lines = [
        f"- {name}: {spec.description or '(no description)'}"
        for name, spec in spec_cache.slices.items()
    ]

    segment_lines = [
        f"- {name}: {spec.description or '(no description)'}"
        for name, spec in spec_cache.segments.items()
    ]

    catalog_section = "\n".join([
        "## Available Metrics",
        "\n".join(metric_lines) or "(none)",
        "",
        "## Available Slices",
        "\n".join(slice_lines) or "(none)",
        "",
        "## Available Segments",
        "\n".join(segment_lines) or "(none)",
    ])

    return f"""You are a data analysis assistant for an AITAEM metrics platform.
You answer user questions by querying a defined metric catalog using the tools provided.

{catalog_section}

## Period Types
Valid values for period_type: "all_time", "hourly", "daily", "weekly", "monthly", "yearly".
Non-"all_time" values require time_window to be specified.

## Metric Precision Rule (CRITICAL)
Only call compute_metrics with metric names that EXACTLY match names in the Available Metrics \
catalog above. If the user asks for a metric that is not in the catalog, or if there is no metric \
that precisely answers the question:
- Set status to "refused"
- Explain clearly which metric is missing
- Do NOT substitute an approximate metric

Example: if "active_revenue" is not in the catalog but "revenue" is, refuse — do not compute \
"revenue" as a substitute. The user must rely on exact definitions.

## Format Narration
When a metric has a format hint (e.g. "percentage", "currency:USD"):
- Narrate values in that format (e.g. "42.5%" not "0.425"; "$1,234" not "1234")
- The format hint appears in the compute_metrics result under format_hints

## Tool Usage
1. Call compute_metrics to get metric data. Note the result_id in the response.
2. Optionally call analysis tools (rank_by_value, filter_by_threshold, etc.) passing the result_id.
3. Each analysis tool produces a new result_id — chain them if needed.
4. Collect the result_ids you want the user to receive.

## Filling Your Final Response
After tool calls, produce a QueryOutput:
- status: "ok" if data was returned, "empty" if zero rows, "refused" if out of scope, \
"error" if a tool returned an error field.
- narrative: plain-language explanation referencing the numbers from tool summaries.
- result_ids: list of result_id strings from the tools, primary/most relevant first. \
Empty if status is not "ok".
- reason: brief note when status is "refused" or "error". Null otherwise.
"""


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
    """

    def __init__(
        self,
        *,
        model: Any,
        spec_cache: Any,
        connection_manager: Any,
        tools: list[Any] | None = None,
    ) -> None:
        # Set bot-specific resources BEFORE super().__init__() — _build_agent()
        # is called inside super().__init__() and needs these attributes.
        self._spec_cache = spec_cache
        self._connection_manager = connection_manager
        super().__init__(model=model, tools=tools)
        # Retained across turns for trace correlation; None until the first run completes.
        self._conversation_id: str | None = None

    def _build_agent(self) -> Any:
        from pydantic_ai import Agent
        from pydantic_ai.toolsets import FunctionToolset
        from pydantic_ai.capabilities import ReinjectSystemPrompt

        toolset = FunctionToolset()
        toolset.add_function(compute_metrics)
        toolset.add_function(rank_by_value)
        toolset.add_function(filter_by_threshold)
        toolset.add_function(distribution_summary)
        toolset.add_function(period_over_period)
        toolset.add_function(contribution_share)

        system_prompt = _build_system_prompt(self._spec_cache)

        return Agent(
            model=self._model,
            deps_type=QueryDeps,
            output_type=QueryOutput,
            toolsets=[toolset],
            instructions=system_prompt,
            capabilities=[ReinjectSystemPrompt(replace_existing=True)],
        )

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
            result = await self._agent.run(
                message,
                message_history=self._message_history,
                deps=deps,
            )
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
            result = await self._agent.run(message, deps=deps)
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
        """Build a status=error QueryResponse when _agent.run() raises.

        conversation_id is the bot's retained ID from prior successful turns, so
        error traces correlate with the rest of the conversation. On the very first
        turn (no prior success), it is None and a fresh UUID is used.
        """
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
        metrics_used: list[str] = []
        slices_used: list[str] = []
        seen_metrics: set[str] = set()
        seen_slices: set[str] = set()
        segment_used: str | None = None
        time_window: tuple[str, str] | None = None
        period_type: str | None = None
        by_entity: str | None = None
        format_hints: dict[str, str] = {}

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
            # list fields: union with deduplication, order-preserving
            for m in ps.get("metrics_used") or []:
                if m not in seen_metrics:
                    seen_metrics.add(m)
                    metrics_used.append(m)
            for s in ps.get("slices_used") or []:
                if s not in seen_slices:
                    seen_slices.add(s)
                    slices_used.append(s)
            # scalar fields: first-write wins
            if segment_used is None and ps.get("segment_used"):
                segment_used = ps["segment_used"]
            if time_window is None and ps.get("time_window"):
                tw = ps["time_window"]
                time_window = (tw[0], tw[1]) if isinstance(tw, (list, tuple)) and len(tw) == 2 else None
            if period_type is None and ps.get("period_type"):
                period_type = ps["period_type"]
            if by_entity is None and ps.get("by_entity"):
                by_entity = ps["by_entity"]
            # dict field: union, first-write wins per metric name
            for metric, fmt in (ps.get("format_hints") or {}).items():
                if metric not in format_hints:
                    format_hints[metric] = fmt

        return QueryPayload(
            result_ids=output.result_ids,
            primary_result_id=output.result_ids[0] if output.result_ids else None,
            metrics_used=metrics_used,
            slices_used=slices_used,
            segment_used=segment_used,
            time_window=time_window,
            period_type=period_type or "all_time",
            by_entity=by_entity,
            format_hints=format_hints,
        )
