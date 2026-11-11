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
from pydantic_ai.messages import CachePoint, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext

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
