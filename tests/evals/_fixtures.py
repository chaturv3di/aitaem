"""Self-contained eval fixtures for tests/evals/.

tests/evals/ doubles as a reference for writing evals against `AitaemAgent`, so it defines its
own small dataset, catalog, and scripted models instead of importing tests/test_agent/'s fixtures.

Every scripted case drives each agent role with a `FunctionModel` that already knows which tools to
call. Those cases check that traces, artifacts, and responses are consumable by `pydantic_evals`
evaluators — the eval substrate — not that a model would choose well. Live-model cases, skipped
without ANTHROPIC_API_KEY, measure behavior.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import yaml
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem import ConnectionManager, IbisConnector, MetricCompute, SpecCache
from aitaem.agent import AitaemAgent

SOURCE = "duckdb://analytics.db/transactions"

TRANSACTIONS = pa.table(
    {
        "amount": [100.0, 250.0, 400.0, 999.0, 1500.0, 80.0],
        "country": ["US", "US", "DE", "DE", "IN", "IN"],
        "transaction_date": [datetime.date(2024, m, 1) for m in (1, 3, 6, 9, 12, 12)],
    }
)

REVENUE = f"""\
metric:
  name: revenue
  description: Total transaction revenue
  source: {SOURCE}
  numerator: SUM(amount)
  timestamp_col: transaction_date
"""
COUNTRY = """\
slice:
  name: country
  description: Breakdown by customer country
  values:
    - name: US
      where: "country = 'US'"
    - name: DE
      where: "country = 'DE'"
    - name: IN
      where: "country = 'IN'"
"""
DEVICE = """\
slice:
  name: device
  description: Breakdown by device type
  values:
    - name: mobile
      where: "device = 'mobile'"
"""


def make_agent(model: Any, executor_model: Any = None, spec_dir: Path | None = None) -> AitaemAgent:
    """An AitaemAgent over an in-memory DuckDB `transactions` table and a small catalog."""
    connector = IbisConnector("duckdb")
    connector.connect(":memory:")
    assert connector.connection is not None
    connector.connection.create_table("transactions", TRANSACTIONS)
    cm = ConnectionManager(tmp_dir=None)
    cm.add_connection("duckdb", connector=connector)
    specs = SpecCache.from_string(metric_yaml=REVENUE, slice_yaml=[COUNTRY, DEVICE])
    return AitaemAgent(
        cm, specs, model=model, executor_model=executor_model or model, spec_dir=spec_dir
    )


def ground_truth(agent: AitaemAgent, **compute: Any) -> pa.Table:
    """The compute result a direct MetricCompute call gives."""
    deps = agent.deps
    return MetricCompute(deps.spec_cache, deps.connection_manager).compute(**compute).to_pyarrow()


# ── scripted role models ────────────────────────────────────────────────────

Script = Callable[[list[ModelMessage]], ModelResponse]

# A tool only one role has tells the roles apart when one model backs them all.
_ROLE_MARKERS = {
    "run_analysis": "main",
    "run_analysis_executors": "analysis",
    "run_spec_executors": "spec",
    "connect_source": "connections",
    "find_specs": "analysis_executor",
    "date_range": "spec_executor",
}


def role_of(info: AgentInfo) -> str:
    names = {t.name for t in info.function_tools}
    return next(role for tool, role in _ROLE_MARKERS.items() if tool in names)


def role_model(**scripts: list[Script | ModelResponse]) -> FunctionModel:
    """One model for every role: each role answers with its own script, step by step.

    A role's script is consumed one step per model request across every run of that role.
    """
    queues = {role: list(steps) for role, steps in scripts.items()}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        steps = queues[role_of(info)]
        step = steps.pop(0)
        return step(messages) if callable(step) else step

    return FunctionModel(model)


def call(tool: str, **args: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool, args)])


def document(doc: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[TextPart(yaml.safe_dump(doc, sort_keys=False))])


def reply(response: str, *attachments: str, status: str = "ok") -> ModelResponse:
    return document(
        {
            "response": response,
            "status": status,
            "attachments": [
                {"artifact_id": a, "reason": "supports the answer"} for a in attachments
            ],
        }
    )


def returns(messages: list[ModelMessage]) -> list[Any]:
    """The tool results of the latest request."""
    last = messages[-1]
    if not isinstance(last, ModelRequest):
        return []
    return [p.content for p in last.parts if isinstance(p, ToolReturnPart)]


def prompt(messages: list[ModelMessage]) -> str:
    """The user prompt of a run: for coordinators and executors, their intake YAML."""
    first = messages[0]
    assert isinstance(first, ModelRequest)
    return next(str(p.content) for p in first.parts if isinstance(p, UserPromptPart))
