"""
QueryBot example — Global Ads Performance dataset.

Demonstrates a multi-turn conversation with QueryBot using the ad campaign
dataset bundled in examples/data/.  Each turn shows the agent's narrative,
the assembled payload, and a preview of the underlying Arrow result.

Prerequisites
-------------
1. Create the DuckDB database (one-time setup):
       python examples/data/setup_db.py

2. Set your Anthropic API key:
       export ANTHROPIC_API_KEY=sk-ant-...

3. Install the agent extra:
       pip install aitaem[agent-anthropic]

Run from the project root:
    python examples/query_bot_example.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

import pandas as pd

from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import QueryBot
from aitaem.agent.trace import Status


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


def _print_response(turn: int, question: str, response) -> None:
    print(f"\n{'─' * 70}")
    print(f"Turn {turn}: {question}")
    print(f"{'─' * 70}")
    print(f"Status   : {response.status.value}")
    print(f"Narrative: {textwrap.fill(response.narrative, width=70, subsequent_indent='           ')}")

    if response.status == Status.error and response.reason:
        print(f"Error    : {response.reason}")
        return

    p = response.payload
    if p is None:
        return

    print(f"Metrics  : {', '.join(p.metrics_used) or '—'}")
    print(f"Slices   : {', '.join(p.slices_used) or '—'}")
    print(f"Period   : {p.period_type}"
          + (f"  {p.time_window[0]} → {p.time_window[1]}" if p.time_window else ""))
    if p.format_hints:
        hints = ", ".join(f"{k}={v}" for k, v in p.format_hints.items())
        print(f"Formats  : {hints}")
    print(f"Results  : {len(p.result_ids)} result(s)  primary={p.primary_result_id}")


def _print_sample(response) -> None:
    """Print the sample rows from response.payload.sample."""
    p = response.payload
    if p is None or not p.sample:
        return
    df = pd.DataFrame(p.sample).dropna(axis=1, how="all")
    print(f"\nSample ({len(p.sample)} row(s)):")
    print(df.to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    _check_api_key()

    # ------------------------------------------------------------------
    # 1. Load specs — metrics and slices only.
    #    The platform segment is excluded because it references a dimension
    #    table (dim_platforms) that is not present in the example DuckDB.
    # ------------------------------------------------------------------
    print("Loading specs …")
    spec_cache = SpecCache.from_yaml(
        metric_paths="examples/metrics/",
        slice_paths="examples/slices/",
    )
    print(f"  {len(spec_cache.metrics)} metrics: {', '.join(spec_cache.metrics)}")
    print(f"  {len(spec_cache.slices)} slices : {', '.join(spec_cache.slices)}")

    # ------------------------------------------------------------------
    # 2. Connect to the DuckDB database, creating it from the CSV if needed.
    #    Using add_connection() directly because connections.yaml also
    #    contains a BigQuery entry that requires GCP_PROJECT_ID.
    # ------------------------------------------------------------------
    db_path = "examples/data/ad_campaigns.duckdb"
    if not os.path.exists(db_path):
        print("\nDuckDB file not found — creating from CSV …")
        from aitaem.helpers import load_csvs_to_duckdb
        load_csvs_to_duckdb("examples/data/ad_campaigns.csv", db_path)
        print(f"  Created {db_path}")

    print("\nConnecting to DuckDB …")
    conn_mgr = ConnectionManager()
    conn_mgr.add_connection("duckdb", path=db_path)

    # ------------------------------------------------------------------
    # 3. Create the QueryBot (one instance — stateful conversation).
    # ------------------------------------------------------------------
    bot = QueryBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=spec_cache,
        connection_manager=conn_mgr,
    )

    # ------------------------------------------------------------------
    # 4. Multi-turn conversation.
    # ------------------------------------------------------------------
    questions = [
        "What was the total revenue and ROAS across all campaigns?",
        "Which campaign type had the highest CTR?",
        (
            "How did total revenue change between H1 2024 (Jan–Jun) "
            "and H2 2024 (Jul–Dec)?"
        ),
    ]

    print("\nStarting conversation …")
    for i, question in enumerate(questions, start=1):
        response = await bot.chat(question)
        _print_response(i, question, response)
        _print_sample(response)

    print(f"\n{'─' * 70}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
