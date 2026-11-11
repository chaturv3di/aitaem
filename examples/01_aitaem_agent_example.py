"""AitaemAgent: a multi-turn analytics session over the ad campaigns dataset.

Run from the project root, after creating the DuckDB file once:

    python examples/data/setup_db.py
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/01_aitaem_agent_example.py

The session answers a question by launching the analysis coordinator, which finds the catalog
specs through its executors, computes, and runs statistics. The follow-up is answered from the
results the session already holds. Every tool call leaves an artifact you can inspect.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aitaem import ConnectionManager, SpecCache
from aitaem.agent import AitaemAgent, Response


def build_agent(
    connection_manager: ConnectionManager | None = None,
    model: Any = "anthropic:claude-sonnet-5-5",
    executor_model: Any = "anthropic:claude-haiku-5-5",
) -> AitaemAgent:
    """An agent over the example catalog and the ad campaigns DuckDB file."""
    if connection_manager is None:
        connection_manager = ConnectionManager.from_yaml("examples/connections.yaml")
    specs = SpecCache.from_yaml(
        metric_paths="examples/metrics/",
        slice_paths="examples/slices/",
    )
    return AitaemAgent(connection_manager, specs, model=model, executor_model=executor_model)


def show(response: Response) -> None:
    print(f"[{response.status.value}] {response.response}")
    for a in response.attachments:
        print(f"  attachment {a.artifact_id} ({a.type_tag}): {a.reason}")


async def main(agent: AitaemAgent) -> list[Response]:
    responses = []

    # 1. A question that needs a compute: the main session launches the analysis coordinator.
    first = await agent.chat("Which region has the highest click-through rate?")
    show(first)
    responses.append(first)

    # 2. A follow-up answered from the results the session already holds.
    second = await agent.chat("How far behind is the lowest region?")
    show(second)
    responses.append(second)

    # 3. Every run of a turn is in its trace: the main session, coordinators, and executors.
    trace = agent.trace(first.trace_id)
    for run in trace.runs:
        print(f"  {run.name}: {len(run.tool_calls)} tool calls, {run.usage.total_tokens} tokens")

    # 4. The artifact index: one line per tool call of the session.
    print("\n".join(agent.store.index_lines()))
    return responses


if __name__ == "__main__":
    session = build_agent()
    try:
        asyncio.run(main(session))
    finally:
        session.cleanup()
