"""Context management for the roles that talk to the user: the tail block and history stubbing.

The tail block carries the session state and the artifact index on every model request, behind a
cache point, without entering the stored history: it is rebuilt from the store each time, so the
cached prefix (tools, instructions, history) stays byte-stable.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.profiles import prompt_cache_outlook

from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status

STATE_MAX_CHARS = 2000


def session_state(deps: SessionDeps) -> dict[str, Any]:
    """The session state, derived from the store and the aitaem handles (never from an LLM)."""
    store = deps.store
    intakes = store.refs("intake")
    goal = yaml.safe_load(store.read(intakes[-1].id))["instructions"] if intakes else None
    questions: list[str] = []
    handoffs = [r for r in store.refs("handoff") if not r.lost]
    if handoffs:
        latest = yaml.safe_load(store.read(handoffs[-1].id))
        if latest.get("outcome") == "needs-input":
            questions = latest.get("questions", [])
    last_ok: dict[str, int] = {}
    for ref in store.refs():
        if ref.status == Status.ok:
            last_ok[ref.produced_by.tool] = int(ref.id[1:])
    errors = [
        f"{r.id} {r.produced_by.tool}: {r.summary}"
        for r in store.refs()
        if r.status == Status.error and int(r.id[1:]) > last_ok.get(r.produced_by.tool, 0)
    ]
    cache = deps.spec_cache
    return {
        "goal": goal,
        "connected_backends": deps.connection_manager.backend_types,
        "specs": {
            "metrics": sorted(cache.metrics),
            "slices": sorted(cache.slices),
            "segments": sorted(cache.segments),
        },
        "route": "spec" if deps.role == "spec_coordinator" else "main",
        "open_questions": questions,
        "unresolved_errors": errors,
    }


def render_state(state: dict[str, Any], max_chars: int = STATE_MAX_CHARS) -> str:
    """Compact JSON of the state, at most `max_chars`; spec names become counts when too long."""
    text = json.dumps(state, separators=(",", ":"))
    if len(text) > max_chars:
        state = {**state, "specs": {k: len(v) for k, v in state["specs"].items()}}
        text = json.dumps(state, separators=(",", ":"))
    return text[:max_chars]


def tail_block(deps: SessionDeps) -> str:
    index = "\n".join(deps.store.index_lines())
    return (
        f"<session-state>\n{render_state(session_state(deps))}\n</session-state>\n"
        f"<artifact-index>\n{index}\n</artifact-index>"
    )


@dataclass
class TailBlock(AbstractCapability[SessionDeps]):
    """Appends the session state and the artifact index to each request, behind a cache point.

    The block is request-only: it goes into a new message list for this request, so the stored
    history never contains it. List this capability after `ProcessHistory`, which saves the
    messages it receives into the history.
    """

    ttl: Literal["5m", "1h"] = "1h"

    async def before_model_request(
        self, ctx: RunContext[SessionDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        part = UserPromptPart(content=[CachePoint(ttl=self.ttl), tail_block(ctx.deps)])
        messages = list(request_context.messages)
        last = messages[-1] if messages else None
        if isinstance(last, ModelRequest):
            messages[-1] = dataclasses.replace(last, parts=[*last.parts, part])
        else:
            messages.append(ModelRequest(parts=[part]))
        return dataclasses.replace(request_context, messages=messages)


# ── history stubbing ────────────────────────────────────────────────────────

STUB_AT = 0.65
"""Fraction of the context window at which old tool returns are stubbed."""
STUB_AT_WHEN_COLD = 0.35
"""The lower fraction that applies when the prompt cache is cold, so the rewrite costs nothing."""
MIN_CLEAR_TOKENS = 2000
KEEP_PAIRS = 4


def _tokens(value: Any) -> int:
    """A rough token count: four characters per token."""
    return len(str(value)) // 4


def _stub(deps: SessionDeps, part: ToolReturnPart) -> str | None:
    content = part.content
    artifact_id = (
        content.get("artifact_id")
        if isinstance(content, dict)
        else getattr(content, "artifact_id", None)
    )
    if not artifact_id or artifact_id not in deps.store:
        return None
    ref = deps.store.get(artifact_id)
    return f"[artifact {ref.id} · {ref.kind} · {ref.summary} · read_artifact or describe_artifact]"


def _is_turn_start(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in message.parts
    )


def stub_history(ctx: RunContext[SessionDeps], messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor: stub old tool returns once the context fills, then drop old turns if needed.

    Runs when the context window is at least 65% full, or 35% with a cold prompt cache. Tool
    returns become one-line references to their artifacts; every call/return pair, the last four
    pairs, and the current run's messages stay intact. Nothing changes unless at least 2,000 tokens
    are cleared. When stubbing is not enough, whole turns are dropped from the oldest, at user-prompt
    boundaries, until the estimate falls under 65%.
    """
    used = ctx.context_window_used
    if used is None:
        return messages
    profile = ctx.model.profile if isinstance(ctx.model, Model) else None
    cold = prompt_cache_outlook(messages, profile=profile) == "cold"
    if used < STUB_AT and not (cold and used >= STUB_AT_WHEN_COLD):
        return messages

    current = next((i for i, m in enumerate(messages) if m.run_id == ctx.run_id), len(messages))
    candidates: list[tuple[int, int, ToolReturnPart]] = [
        (i, j, p)
        for i, m in enumerate(messages[:current])
        if isinstance(m, ModelRequest)
        for j, p in enumerate(m.parts)
        if isinstance(p, ToolReturnPart) and not str(p.content).startswith("[artifact ")
    ]
    stubs: dict[tuple[int, int], ToolReturnPart] = {}
    for i, j, part in candidates[: max(0, len(candidates) - KEEP_PAIRS)]:
        if (stub := _stub(ctx.deps, part)) is not None:
            stubs[(i, j)] = dataclasses.replace(part, content=stub)
    cleared = sum(_tokens(messages[i].parts[j]) - _tokens(stub) for (i, j), stub in stubs.items())

    result = list(messages)
    if cleared >= MIN_CLEAR_TOKENS:
        for i in {i for i, _ in stubs}:
            request = result[i]
            assert isinstance(request, ModelRequest)
            parts = [stubs.get((i, j), p) for j, p in enumerate(request.parts)]
            result[i] = dataclasses.replace(request, parts=parts)

    # Escalation: estimate the new fraction by how much the history shrank, then drop the oldest
    # turns, one user prompt to the next, keeping the current run.
    before = sum(_tokens(m.parts) for m in messages)

    def estimate(history: list[ModelMessage]) -> float:
        return used * sum(_tokens(m.parts) for m in history) / max(before, 1)

    boundaries = [i for i, m in enumerate(result[:current]) if i and _is_turn_start(m)]
    boundaries.append(current)
    while estimate(result) >= STUB_AT and len(boundaries) > 1:
        cut = boundaries.pop(0)
        result = result[cut:]
        boundaries = [b - cut for b in boundaries]
    return result
