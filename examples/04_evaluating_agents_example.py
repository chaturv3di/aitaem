"""
04_evaluating_agents_example.py — Evaluating QueryBot with pydantic_evals.

Demonstrates writing a pydantic_evals Dataset against a live QueryBot: a task
function that runs one question through a fresh bot, Evaluators that inspect
the response status and trace, and a pass_rate() helper that repeats the
dataset several times for confidence, since a live model is non-deterministic.

This is the live-model companion to tests/evals/ (this repo's CI-safe reference
harness, driven by scripted FunctionModels with no live API calls). Point this
example's pattern at your own spec catalog and questions to measure whether
your agent actually behaves the way you expect — tests/evals/ only proves the
trace/result-store/evaluator wiring is consumable, not that any given model
selects the right tool or refuses appropriately.

Prerequisites
-------------
1. Create the DuckDB database (one-time setup):
       python examples/data/setup_db.py

2. Set your Anthropic API key:
       export ANTHROPIC_API_KEY=sk-ant-...

3. Install the agent-evals extra:
       pip install aitaem[agent-evals]

Cost/runtime note
------------------
pass_rate(n=5) below runs the 2-case dataset 5 times against a live model —
10 bot invocations total. Each bot.ask() call is a multi-step tool loop
(record_intent -> resolve_intent -> compute_metrics, each a separate model
turn), so the actual number of model calls is meaningfully higher than 10.
Budget accordingly before running the pass_rate() section.

Run from the project root:
    python examples/04_evaluating_agents_example.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import QueryBot, QueryResponse, Status

MODEL = "anthropic:claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "       Export it before running:\n"
            "           export ANTHROPIC_API_KEY=sk-ant-...\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


# ---------------------------------------------------------------------------
# Task + I/O types
# ---------------------------------------------------------------------------

@dataclass
class QIn:
    question: str


@dataclass
class QOut:
    response: QueryResponse   # aitaem.agent.QueryResponse
    bot: QueryBot             # kept so evaluators can call get_result()


async def query_task(inputs: QIn) -> QOut:
    # Fresh bot per case — per-run state (intents, spec registry) must not leak
    # across cases. SPEC_CACHE/CONN_MGR are safe to share: read-only catalog
    # and connection, set up in main() before the dataset runs.
    bot = QueryBot(
        model=MODEL,
        spec_cache=SPEC_CACHE,
        connection_manager=CONN_MGR,
    )
    response = await bot.ask(inputs.question)  # single-turn, no history
    return QOut(response=response, bot=bot)


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------

@dataclass
class StatusIs(Evaluator[QIn, QOut, None]):
    expected: Status

    def evaluate(self, ctx: EvaluatorContext[QIn, QOut, None]) -> bool:
        return ctx.output.response.status is self.expected


@dataclass
class CalledTool(Evaluator[QIn, QOut, None]):
    tool_name: str

    def evaluate(self, ctx: EvaluatorContext[QIn, QOut, None]) -> bool:
        return any(
            tc.name == self.tool_name for tc in ctx.output.response.trace.tool_calls
        )


@dataclass
class ToolSequenceIs(Evaluator[QIn, QOut, None]):
    """Exact-sequence check — the one that actually tests the Metric Precision
    Rule: did the bot gate through record_intent -> resolve_intent before
    calling compute_metrics, in exactly that order?

    Deliberately strict, not a subsequence check. Against a live model, a run
    that self-corrects (e.g. calls resolve_intent twice after an initial
    near-miss) behaves correctly — it still gated before computing — but
    fails this exact-sequence assertion. A subsequence check would fix that,
    but would also let a model that calls compute_metrics before
    resolve_intent pass, as long as it calls resolve_intent *later* in the
    same run — defeating the one case that exercises the gate order at all.
    A single failed run on this assertion isn't necessarily a bug; read it
    through pass_rate() below, not a single run's pass/fail.

    Note the trailing "final_result" in the expected list below: pydantic-ai's
    default structured-output mechanism for a plain `output_type=` (as
    QueryBot uses) is a synthetic tool call named "final_result"
    (pydantic_ai._output.DEFAULT_OUTPUT_TOOL_NAME) — every successful run ends
    with one. Omitting it here would make this assertion fail on every real
    run, not just occasionally.
    """

    expected: list[str]

    def evaluate(self, ctx: EvaluatorContext[QIn, QOut, None]) -> bool:
        return [tc.name for tc in ctx.output.response.trace.tool_calls] == self.expected


# ---------------------------------------------------------------------------
# Dataset — two cases against the real examples/metrics/ catalog. Neither
# question names a metric in that catalog's "sales_velocity" form, so the
# refusal case is genuine, not contrived.
# ---------------------------------------------------------------------------

dataset: Dataset[QIn, QOut, None] = Dataset(
    name="query_bot_behavioral_eval",
    cases=[
        Case(
            name="in_catalog_metric",
            inputs=QIn("What was total revenue in Q1 2024?"),
            evaluators=(
                StatusIs(Status.ok),
                ToolSequenceIs(
                    ["record_intent", "resolve_intent", "compute_metrics", "final_result"]
                ),
            ),
        ),
        Case(
            name="out_of_catalog_metric",
            inputs=QIn("What was sales velocity last month?"),
            evaluators=(
                StatusIs(Status.refused),
                CalledTool("resolve_intent"),  # it must try, then refuse
            ),
        ),
    ]
)


# ---------------------------------------------------------------------------
# Repeated-run confidence — a live model is non-deterministic, so a single
# dataset.evaluate() run tells you much less than a distribution does.
# ---------------------------------------------------------------------------

async def pass_rate(
    dataset: Dataset[QIn, QOut, None], task, n: int = 5
) -> dict[str, dict[str, float]]:
    """Per-case, per-evaluator pass rates, plus an "overall" (all-assertions-passed)
    rate per case. A collapsed overall number can't tell you whether a low rate
    means the bot isn't reaching status=ok at all, or is reaching it via a tool
    sequence that doesn't match — those call for very different next steps. Runs
    with progress=False: pydantic_evals' default progress display depends on
    ipywidgets rendering correctly, which doesn't always hold in every notebook
    environment.
    """
    # Counter() returns 0 for a key that was never incremented, but an evaluator
    # that fails on *every* rep is never incremented at all — so its key would
    # silently be absent from the result rather than reported at 0%. Track the
    # full set of evaluator names seen per case so a 0% evaluator still shows up.
    tally: dict[str, Counter[str]] = defaultdict(Counter)
    evaluator_names: dict[str, set[str]] = defaultdict(set)
    for _ in range(n):
        report = await dataset.evaluate(task, progress=False)
        for case in report.cases:
            all_ok = True
            for evaluator_name, assertion in case.assertions.items():
                evaluator_names[case.name].add(evaluator_name)
                if assertion.value:
                    tally[case.name][evaluator_name] += 1
                else:
                    all_ok = False
            tally[case.name]["overall"] += all_ok
    return {
        case_name: {
            **{name: tally[case_name][name] / n for name in sorted(names)},
            "overall": tally[case_name]["overall"] / n,
        }
        for case_name, names in evaluator_names.items()
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SPEC_CACHE: SpecCache
CONN_MGR: ConnectionManager


async def main() -> None:
    global SPEC_CACHE, CONN_MGR
    _check_api_key()

    print("Loading specs …")
    SPEC_CACHE = SpecCache.from_yaml(
        metric_paths="examples/metrics/",
        slice_paths="examples/slices/",
    )
    print(f"  {len(SPEC_CACHE.metrics)} metrics: {', '.join(SPEC_CACHE.metrics)}")

    db_path = "examples/data/ad_campaigns.duckdb"
    if not os.path.exists(db_path):
        print("\nDuckDB file not found — creating from CSV …")
        from aitaem.helpers import load_csvs_to_duckdb
        load_csvs_to_duckdb("examples/data/ad_campaigns.csv", db_path)
        print(f"  Created {db_path}")

    print("\nConnecting to DuckDB …")
    CONN_MGR = ConnectionManager()
    CONN_MGR.add_connection("duckdb", path=db_path)

    print("\nRunning dataset once …")
    report = await dataset.evaluate(query_task, progress=False)
    report.print(include_input=True, include_output=False)

    print("\nRunning pass_rate(n=5) — 10 bot invocations, budget accordingly …")
    rates = await pass_rate(dataset, query_task, n=5)
    for case_name, breakdown in rates.items():
        print(f"  {case_name}:")
        print(f"    overall: {breakdown['overall']:.0%}")
        for evaluator_name, rate in breakdown.items():
            if evaluator_name != "overall":
                print(f"    {evaluator_name}: {rate:.0%}")


if __name__ == "__main__":
    asyncio.run(main())
