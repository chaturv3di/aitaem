"""Tool groups, and the `artifact_tool` wrapper every tool goes through.

A tool body returns a `ToolOutput` describing its payload; `artifact_tool` writes the artifact
and hands the model a small `ToolResult` (artifact ID, status, summary, and a few facts).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    RunCancelled,
    UsageLimitExceeded,
)

from aitaem.agent.artifacts import Kind, ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status

# pydantic-ai control flow passes through the wrapper untouched.
_PASS_THROUGH = (ModelRetry, CallDeferred, ApprovalRequired, RunCancelled, UsageLimitExceeded)


class ToolResult(BaseModel):
    """What a tool returns to the model: never rows, only the artifact ID and a few facts."""

    artifact_id: str
    status: Status
    summary: str
    facts: dict[str, Any] = {}


@dataclass
class ToolOutput:
    """What a tool body returns: the payload to store and the facts to show the model."""

    payload: Any
    kind: Kind
    type_tag: str
    summary: str
    parents: list[str] = field(default_factory=list)
    status: Status = Status.ok
    facts: dict[str, Any] = field(default_factory=dict)


def _store(ctx: RunContext[SessionDeps], name: str, out: ToolOutput) -> ToolResult:
    by = ProducedBy(tool=name, call_id=ctx.tool_call_id, turn=ctx.deps.turn, role=ctx.deps.role)
    ref = ctx.deps.store.put(
        out.kind,
        out.payload,
        type_tag=out.type_tag,
        summary=out.summary,
        produced_by=by,
        parents=out.parents,
        status=out.status,
    )
    return ToolResult(artifact_id=ref.id, status=out.status, summary=ref.summary, facts=out.facts)


def _store_error(ctx: RunContext[SessionDeps], name: str, exc: Exception) -> ToolResult:
    message = f"{type(exc).__name__}: {exc}"
    out = ToolOutput({"error": message}, "object", "error", message, status=Status.error)
    return _store(ctx, name, out)


def artifact_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool body so each call writes one artifact and returns a `ToolResult`.

    The body takes `RunContext[SessionDeps]` first and returns a `ToolOutput`. When it raises,
    the wrapper writes an `error` artifact with the message and returns an `error` result.
    """
    name = fn.__name__

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(
            ctx: RunContext[SessionDeps], *args: Any, **kwargs: Any
        ) -> ToolResult:
            try:
                out = await fn(ctx, *args, **kwargs)
            except _PASS_THROUGH:
                raise
            except Exception as e:  # noqa: BLE001
                return _store_error(ctx, name, e)
            return _store(ctx, name, out)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(ctx: RunContext[SessionDeps], *args: Any, **kwargs: Any) -> ToolResult:
        try:
            out = fn(ctx, *args, **kwargs)
        except _PASS_THROUGH:
            raise
        except Exception as e:  # noqa: BLE001
            return _store_error(ctx, name, e)
        return _store(ctx, name, out)

    return wrapper


__all__ = ["ToolOutput", "ToolResult", "artifact_tool"]
