"""`AitaemAgent`: the session facade that routes each user message to the active agent role."""

from __future__ import annotations

import dataclasses
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.conversation import Conversation
from pydantic_ai.models import Model

from aitaem import ConnectionManager, SpecCache
from aitaem.agent.agent_roles.analysis_coordinator.agent import build_analysis_coordinator
from aitaem.agent.agent_roles.connections_coordinator.agent import build_connections_coordinator
from aitaem.agent.agent_roles.documents import (
    is_handoff,
    parse_handoff,
    parse_reply,
    store_handoff,
)
from aitaem.agent.agent_roles.main_session.agent import build_main_session
from aitaem.agent.agent_roles.spec_coordinator.agent import build_spec_coordinator
from aitaem.agent.artifacts import ArtifactStore
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolResult
from aitaem.agent.trace import RunTrace, Status, Usage, assemble_trace

Route = Literal["main", "spec"]

DEFAULT_MODEL = "anthropic:claude-sonnet-5-5"
DEFAULT_EXECUTOR_MODEL = "anthropic:claude-haiku-5-5"


class ResponseAttachment(BaseModel):
    """An artifact that supports a response."""

    artifact_id: str
    type_tag: str
    reason: str


class Response(BaseModel):
    """What `AitaemAgent.chat()` returns."""

    response: str
    status: Status
    attachments: list[ResponseAttachment] = []
    trace_id: str


class AitaemAgent:
    """One multi-turn agent session over a ConnectionManager and a SpecCache.

    The main session talks to the user and launches coordinators; during spec design, user
    messages go to the spec coordinator until it hands off.
    """

    def __init__(
        self,
        connection_manager: ConnectionManager | None = None,
        spec_cache: SpecCache | None = None,
        *,
        model: str | Model = DEFAULT_MODEL,
        executor_model: str | Model = DEFAULT_EXECUTOR_MODEL,
        spec_dir: str | Path | None = None,
    ) -> None:
        self._setup(
            ArtifactStore(), connection_manager, spec_cache, model, executor_model, spec_dir
        )

    def _setup(
        self,
        store: ArtifactStore,
        connection_manager: ConnectionManager | None,
        spec_cache: SpecCache | None,
        model: str | Model,
        executor_model: str | Model,
        spec_dir: str | Path | None,
    ) -> None:
        if connection_manager is None:
            connection_manager = ConnectionManager(tmp_dir=str(store.session_dir))
        self.deps = SessionDeps(
            store=store,
            connection_manager=connection_manager,
            spec_cache=spec_cache if spec_cache is not None else SpecCache(),
            spec_dir=spec_dir if spec_dir is not None else store.spec_dir,
        )
        self.connections_coordinator = build_connections_coordinator(model)
        self.analysis_coordinator = build_analysis_coordinator(model, executor_model)
        self.spec_coordinator = build_spec_coordinator(model, executor_model)
        self.main_session = build_main_session(
            model, self.connections_coordinator, self.analysis_coordinator
        )
        self.route: Route = "main"
        self.main_conversation = Conversation()
        self.spec_conversation: Conversation | None = None
        self.pending_call_id: str | None = None
        self.pending_intake_id: str | None = None
        self.pending_others: list[str] = []
        self.traces: dict[str, RunTrace] = {}
        self._runs: list[Any] = []

    @property
    def store(self) -> ArtifactStore:
        return self.deps.store

    # ── chat ────────────────────────────────────────────────────────────────

    async def chat(self, message: str) -> Response:
        """Send a user message to the active role and return its reply."""
        self.deps = dataclasses.replace(self.deps, turn=self.deps.turn + 1)
        trace_id = uuid.uuid4().hex
        start = datetime.now(timezone.utc)
        self._runs = []
        try:
            if self.route == "spec":
                text = await self._spec_turn(message)
            else:
                text = await self._main_turn(message)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self.traces[trace_id] = self._trace(trace_id, start, error)
            return Response(response=error, status=Status.error, trace_id=trace_id)
        self.traces[trace_id] = self._trace(trace_id, start, None)
        reply = parse_reply(text)
        attachments = [
            ResponseAttachment(
                artifact_id=a.artifact_id,
                type_tag=self.store.get(a.artifact_id).type_tag,
                reason=a.reason,
            )
            for a in reply.attachments
        ]
        return Response(
            response=reply.response, status=reply.status, attachments=attachments, trace_id=trace_id
        )

    async def _run(
        self, agent: Agent[SessionDeps, Any], role: str, *args: Any, **kwargs: Any
    ) -> Any:
        deps = dataclasses.replace(self.deps, role=role)
        result = await agent.run(*args, deps=deps, **kwargs)
        self._runs.append(result)
        return result

    async def _main_turn(
        self, message: str | None, results: DeferredToolResults | None = None
    ) -> str:
        """Run the main session; when it starts spec design, run the spec coordinator."""
        result = await self._run(
            self.main_session,
            "main_session",
            message,
            conversation=self.main_conversation,
            deferred_tool_results=results,
        )
        self.main_conversation = result.conversation
        if not isinstance(result.output, DeferredToolRequests):
            return result.output
        first, *others = result.output.calls
        self.pending_call_id = first.tool_call_id
        self.pending_intake_id = result.output.metadata[first.tool_call_id]["intake_id"]
        self.pending_others = [c.tool_call_id for c in others]
        self.spec_conversation = Conversation()
        return await self._spec_turn(self.store.read(self.pending_intake_id))

    async def _spec_turn(self, message: str) -> str:
        """Run the spec coordinator; a handoff returns control to the main session."""
        result = await self._run(
            self.spec_coordinator,
            "spec_coordinator",
            message,
            conversation=self.spec_conversation,
        )
        self.spec_conversation = result.conversation
        if not is_handoff(result.output):
            self.route = "spec"
            return result.output
        assert self.pending_call_id is not None and self.pending_intake_id is not None
        handoff = parse_handoff(result.output)
        handoff_id = store_handoff(
            self.deps, handoff, self.pending_intake_id, "start_spec_design", "spec_coordinator"
        )
        tool_result = ToolResult(
            artifact_id=handoff_id,
            status=Status.ok,
            summary=self.store.get(handoff_id).summary,
            facts=handoff.model_dump(),
        )
        calls: dict[str, Any] = {self.pending_call_id: tool_result}
        calls.update(
            {
                c: "Run one spec design at a time; launch this one again."
                for c in self.pending_others
            }
        )
        self.route = "main"
        self.spec_conversation = None
        self.pending_call_id = self.pending_intake_id = None
        self.pending_others = []
        return await self._main_turn(None, DeferredToolResults(calls=calls))

    # ── traces ──────────────────────────────────────────────────────────────

    def _trace(self, trace_id: str, start: datetime, error: str | None) -> RunTrace:
        if self._runs:
            trace = assemble_trace(self._runs[-1], start)
            return trace.model_copy(update={"run_id": trace_id, "error": error})
        return RunTrace(
            run_id=trace_id,
            conversation_id=self.main_conversation.conversation_id or "",
            timestamp=start,
            tool_calls=[],
            usage=Usage(),
            duration_ms=(datetime.now(timezone.utc) - start).total_seconds() * 1000,
            error=error,
        )

    def trace(self, trace_id: str) -> RunTrace:
        """Return the trace of one `chat()` turn."""
        return self.traces[trace_id]

    # ── lifecycle ───────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Remove the session directory."""
        shutil.rmtree(self.store.session_dir, ignore_errors=True)
