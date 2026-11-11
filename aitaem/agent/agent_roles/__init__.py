"""Agent roles: one package per role, each with its `instructions.md` and `agent.py`."""

from __future__ import annotations

import asyncio
import dataclasses
from importlib.resources import files
from typing import Any

from pydantic_ai import Agent, RunContext

from aitaem.agent.agent_roles.documents import (
    Intake,
    parse_handoff,
    store_handoff,
    store_intake,
    to_yaml,
)
from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput
from aitaem.agent.trace import Status, record_run

EXECUTOR_CONCURRENCY = 4


def load_instructions(package: str) -> str:
    """Return the `instructions.md` packaged with a role."""
    return files(package).joinpath("instructions.md").read_text(encoding="utf-8")


async def run_executors(
    ctx: RunContext[SessionDeps],
    executor: Agent[SessionDeps, str],
    intakes: list[Intake[Any]],
    *,
    tool: str,
    role: str,
) -> ToolOutput:
    """Run one executor per intake, at most four at a time, and await them all.

    Each intake and handoff is stored as an artifact; a failed executor yields an `error`
    artifact and leaves its siblings running. Executors share the coordinator's usage, so its
    usage limits bound the whole tree.
    """
    deps = ctx.deps
    child = dataclasses.replace(deps, role=role)
    semaphore = asyncio.Semaphore(EXECUTOR_CONCURRENCY)
    intake_ids = [store_intake(deps, intake, tool, ctx.tool_call_id) for intake in intakes]

    async def one(intake: Intake[Any]) -> str:
        async with semaphore:
            result = await executor.run(to_yaml(intake), deps=child, usage=ctx.usage)
        record_run(executor.name, result)
        return result.output

    outputs = await asyncio.gather(*(one(i) for i in intakes), return_exceptions=True)
    entries: list[dict[str, Any]] = []
    for intake_id, output in zip(intake_ids, outputs):
        if isinstance(output, BaseException):
            message = f"{type(output).__name__}: {output}"
            ref = deps.store.put(
                "object",
                {"error": message},
                type_tag="error",
                summary=f"{role} failed: {message}",
                produced_by=ProducedBy(
                    tool=tool, call_id=ctx.tool_call_id, turn=deps.turn, role=role
                ),
                parents=[intake_id],
                status=Status.error,
            )
            entries.append({"error_id": ref.id, "outcome": "error", "response": message})
            continue
        handoff = parse_handoff(output)
        handoff_id = store_handoff(deps, handoff, intake_id, tool, role)
        entries.append(
            {"handoff_id": handoff_id, "outcome": handoff.outcome, "response": handoff.response}
        )
    failed = sum(e["outcome"] == "error" for e in entries)
    summary = f"{len(entries)} {role} runs" + (f", {failed} failed" if failed else "")
    parents = [e.get("handoff_id") or e["error_id"] for e in entries]
    return ToolOutput(entries, "object", "fan_out", summary, parents, facts={"executors": entries})


async def launch(
    ctx: RunContext[SessionDeps],
    coordinator: Agent[SessionDeps, str],
    intake: Intake[Any],
    *,
    tool: str,
    role: str,
) -> ToolOutput:
    """Store the intake, run the coordinator on it, and return its handoff as the tool's artifact.

    The coordinator shares the launcher's usage, so the launcher's usage limits bound the whole
    tree.
    """
    intake_id = store_intake(ctx.deps, intake, tool, ctx.tool_call_id)
    child = dataclasses.replace(ctx.deps, role=role)
    result = await coordinator.run(to_yaml(intake), deps=child, usage=ctx.usage)
    record_run(coordinator.name, result)
    handoff = parse_handoff(result.output)
    return ToolOutput(
        to_yaml(handoff),
        "file",
        "handoff",
        f"{handoff.outcome}: {handoff.response}",
        [intake_id, *handoff.result_ids],
        facts=handoff.model_dump(),
    )
