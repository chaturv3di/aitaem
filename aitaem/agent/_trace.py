from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field


class Status(str, Enum):
    ok = "ok"
    empty = "empty"
    refused = "refused"
    error = "error"


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @computed_field
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

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
    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    name: str
    args: dict[str, Any]
    result_id: str | None = None
    llm_summary: str | None = None  # compact snippet only — never raw result data
    success: bool = True
    duration_ms: float | None = None


class RunTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    conversation_id: str
    timestamp: datetime
    tool_calls: list[ToolCall]
    usage: Usage
    traceparent: str | None = None
    duration_ms: float = 0.0


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
                    }

    for msg in result.new_messages():
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    tc = pending.get(part.tool_call_id)
                    if tc is not None:
                        tc["llm_summary"] = part.content
                        tc["success"] = part.outcome == "success"

    tool_calls = [
        ToolCall(
            tool_call_id=tc["tool_call_id"],
            name=tc["name"],
            args=tc["args"],
            llm_summary=tc.get("llm_summary"),
            success=tc.get("success", True),
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
