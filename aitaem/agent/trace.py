from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field


class Status(str, Enum):
    """Outcome of a bot response: ok, empty, refused, or error.

    `refused` is the general "no spec precisely answers — don't substitute an
    approximation" outcome (the Metric Precision Rule), distinct from `error`
    (a tool or the run itself failed) and `empty` (ran fine, no results).
    """

    ok = "ok"
    empty = "empty"
    refused = "refused"
    error = "error"


class Usage(BaseModel):
    """Token and tool-call usage counters for one bot run."""

    model_config = ConfigDict(frozen=True)

    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_messages(cls, messages: list[Any]) -> Usage:
        """Sum the usage of the model responses among `messages`: one run's own usage."""
        from pydantic_ai.messages import ModelRequest, ModelResponse, ToolReturnPart

        responses = [m for m in messages if isinstance(m, ModelResponse)]
        return cls(
            requests=len(responses),
            tool_calls=sum(
                isinstance(p, ToolReturnPart)
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
            ),
            input_tokens=sum(r.usage.input_tokens for r in responses),
            output_tokens=sum(r.usage.output_tokens for r in responses),
            cache_read_tokens=sum(r.usage.cache_read_tokens for r in responses),
            cache_write_tokens=sum(r.usage.cache_write_tokens for r in responses),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            requests=self.requests + other.requests,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    @classmethod
    def from_run_usage(cls, ru: Any) -> Usage:
        return cls(
            requests=ru.requests,
            tool_calls=ru.tool_calls,
            input_tokens=ru.input_tokens,
            output_tokens=ru.output_tokens,
            cache_read_tokens=ru.cache_read_tokens,
            cache_write_tokens=ru.cache_write_tokens,
        )


class ToolCall(BaseModel):
    """One tool invocation within a run, with its arguments, outcome, and an
    LLM-facing summary (never the raw result — see `artifact_id` for that)."""

    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    name: str
    args: dict[str, Any]
    artifact_id: str | None = None
    llm_summary: str | None = None  # compact snippet only — never raw result data
    success: bool = True
    duration_ms: float | None = None


class AgentRun(BaseModel):
    """One agent run within a turn: the main session, a coordinator, or an executor."""

    model_config = ConfigDict(frozen=True)

    name: str | None
    run_id: str
    tool_calls: list[ToolCall]
    usage: Usage
    """This run's own requests and tokens, cache reads and writes included."""


class RunTrace(BaseModel):
    """Aggregated, eval-friendly trace of one turn — tool calls, usage, and timing of every run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    conversation_id: str
    timestamp: datetime
    tool_calls: list[ToolCall]
    """Every tool call of the turn, across its runs, in run order."""
    usage: Usage
    """The turn's total usage, across its runs."""
    runs: list[AgentRun] = []
    traceparent: str | None = None
    duration_ms: float = 0.0
    error: str | None = None


# ── run collection ──────────────────────────────────────────────────────────

_RUNS: ContextVar[list[tuple[str | None, Any]] | None] = ContextVar(
    "aitaem_agent_runs", default=None
)


@contextmanager
def collect_runs() -> Iterator[list[tuple[str | None, Any]]]:
    """Collect (agent name, result) for every run recorded with `record_run` inside the block."""
    runs: list[tuple[str | None, Any]] = []
    token = _RUNS.set(runs)
    try:
        yield runs
    finally:
        _RUNS.reset(token)


def record_run(name: str | None, result: Any) -> None:
    """Add a finished run to the current `collect_runs` block, if any."""
    runs = _RUNS.get()
    if runs is not None:
        runs.append((name, result))


def _extract_artifact_id(content: Any) -> str | None:
    """The artifact ID of a tool's `ToolResult`, or None for a return without one."""
    return getattr(content, "artifact_id", None) or None


def assemble_trace(result: Any, run_start: datetime) -> RunTrace:
    """Assemble a RunTrace from a completed pydantic-ai AgentRunResult.

    Args:
        result: A pydantic_ai.AgentRunResult (typed as Any to avoid hard
            import at module level; pydantic-ai is an optional dependency).
        run_start: datetime.now(timezone.utc) captured before agent.run().
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    duration_ms = (datetime.now(timezone.utc) - run_start).total_seconds() * 1000

    pending: dict[str, dict[str, Any]] = {}
    for msg in result.new_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    args = part.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {"_raw": args}
                    elif args is None:
                        args = {}
                    pending[part.tool_call_id] = {
                        "tool_call_id": part.tool_call_id,
                        "name": part.tool_name,
                        "args": args,
                        "start_time": msg.timestamp,
                    }

    for msg in result.new_messages():
        if isinstance(msg, ModelRequest):
            for return_part in msg.parts:
                if isinstance(return_part, ToolReturnPart):
                    tc = pending.get(return_part.tool_call_id)
                    if tc is not None:
                        content = return_part.content
                        if isinstance(content, str):
                            tc["llm_summary"] = content
                        else:
                            tc["llm_summary"] = return_part.model_response_str()
                            tc["artifact_id"] = _extract_artifact_id(content)
                        tc["success"] = return_part.outcome == "success"
                        tc["duration_ms"] = (
                            return_part.timestamp - tc["start_time"]
                        ).total_seconds() * 1000

    tool_calls = [
        ToolCall(
            tool_call_id=tc["tool_call_id"],
            name=tc["name"],
            args=tc["args"],
            artifact_id=tc.get("artifact_id"),
            llm_summary=tc.get("llm_summary"),
            success=tc.get("success", True),
            duration_ms=tc.get("duration_ms"),
        )
        for tc in pending.values()
    ]

    return RunTrace(
        run_id=result.run_id,
        conversation_id=result.conversation_id,
        timestamp=result.timestamp,
        tool_calls=tool_calls,
        usage=Usage.from_run_usage(result.usage),
        traceparent=result._traceparent_value,
        duration_ms=duration_ms,
    )


def assemble_turn_trace(
    trace_id: str, results: list[tuple[str | None, Any]], start: datetime, error: str | None = None
) -> RunTrace:
    """Assemble one turn's RunTrace from every run's result, in the order they finished."""
    runs = []
    for name, result in results:
        single = assemble_trace(result, start)
        runs.append(
            AgentRun(
                name=name,
                run_id=result.run_id,
                tool_calls=single.tool_calls,
                usage=Usage.from_messages(result.new_messages()),
            )
        )
    last = results[-1][1] if results else None
    total = Usage()
    for run in runs:
        total = total + run.usage
    return RunTrace(
        run_id=trace_id,
        conversation_id=last.conversation_id if last is not None else "",
        timestamp=start,
        tool_calls=[c for run in runs for c in run.tool_calls],
        usage=total,
        runs=runs,
        traceparent=last._traceparent_value if last is not None else None,
        duration_ms=(datetime.now(timezone.utc) - start).total_seconds() * 1000,
        error=error,
    )
