"""
02_query_bot_example.py — QueryBot example, Global Ads Performance dataset.

Demonstrates a multi-turn conversation with QueryBot using the ad campaign
dataset bundled in examples/data/.  Each turn shows the agent's narrative,
the assembled payload, a data sample, and the run trace.

This module is the single implementation for both the standalone script below
and 02_query_bot_example.ipynb — the notebook imports from here rather than
redefining any of this, so the two can't drift apart. If you're reading the
notebook, everything it calls is defined here.

Prerequisites
-------------
1. Set your Anthropic API key:
       export ANTHROPIC_API_KEY=sk-ant-...

2. Install the agent extra:
       pip install aitaem[agent-anthropic]

Run from the project root:
    python examples/02_query_bot_example.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from typing import Any

import pandas as pd

from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import QueryBot, QueryResponse, RunTrace, Status

MODEL = "anthropic:claude-haiku-4-5-20251001"

QUESTIONS = [
    "What was the total revenue and ROAS across all campaigns?",
    "Which campaign type had the highest CTR?",
    (
        "How did total revenue change between H1 2024 (Jan–Jun) "
        "and H2 2024 (Jul–Dec)?"
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_api_key(exit_on_missing: bool = True) -> str:
    """Read ANTHROPIC_API_KEY. exit_on_missing=True (script default) prints to
    stderr and calls sys.exit(1); pass False (notebook use) to raise
    RuntimeError instead, since sys.exit() would kill a Jupyter kernel."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        message = (
            "ANTHROPIC_API_KEY is not set.\n"
            "       Export it before running:\n"
            "           export ANTHROPIC_API_KEY=sk-ant-...\n"
        )
        if exit_on_missing:
            print(f"ERROR: {message}", file=sys.stderr)
            sys.exit(1)
        raise RuntimeError(message)
    return key


def print_response(turn: int, question: str, response: QueryResponse) -> None:
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


def print_sample(response: QueryResponse) -> None:
    p = response.payload
    if p is None or not p.sample:
        return
    df = pd.DataFrame(p.sample).dropna(axis=1, how="all")
    print(f"\nSample ({len(p.sample)} row(s)):")
    print(df.to_string(index=False))


def fmt_arg(v: Any) -> str:
    """Format a tool argument value — truncate long strings (e.g. result UUIDs)."""
    if isinstance(v, str) and len(v) > 12:
        return f"'{v[:8]}…'"
    return repr(v)


def print_trace(trace: RunTrace) -> None:
    print(f"\nTrace    : run={trace.run_id[:8]}  conv={trace.conversation_id[:8]}  {trace.duration_ms:.0f}ms")
    for tc in trace.tool_calls:
        non_null = {k: v for k, v in tc.args.items() if v is not None}
        args_str = ", ".join(f"{k}={fmt_arg(v)}" for k, v in non_null.items())
        icon = "✓" if tc.success else "✗"
        print(f"  {icon} {tc.name}({args_str})")
    u = trace.usage
    print(f"Tokens   : {u.input_tokens} in / {u.output_tokens} out  ({u.total_tokens} total)")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup(base_path: str = ".") -> tuple[SpecCache, ConnectionManager]:
    """Load the spec catalog and connect to DuckDB, creating it from the
    bundled CSV if needed. base_path is the aitaem repo root — "." when run
    as a script from the project root, or an explicit path from a notebook
    that may be running from a different working directory."""
    # Metrics and slices only — the platform segment references a dimension
    # table (dim_platforms) that is not present in the example DuckDB.
    spec_cache = SpecCache.from_yaml(
        metric_paths=os.path.join(base_path, "examples/metrics/"),
        slice_paths=os.path.join(base_path, "examples/slices/"),
    )

    db_path = os.path.join(base_path, "examples/data/ad_campaigns.duckdb")
    if not os.path.exists(db_path):
        from aitaem.helpers import load_csvs_to_duckdb
        csv_path = os.path.join(base_path, "examples/data/ad_campaigns.csv")
        load_csvs_to_duckdb(csv_path, db_path)

    # Using add_connection() directly because connections.yaml also contains
    # a BigQuery entry that requires GCP_PROJECT_ID.
    conn_mgr = ConnectionManager()
    conn_mgr.add_connection("duckdb", path=db_path)
    return spec_cache, conn_mgr


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(base_path: str = ".", exit_on_missing_key: bool = True) -> None:
    check_api_key(exit_on_missing=exit_on_missing_key)

    print("Loading specs …")
    spec_cache, conn_mgr = setup(base_path)
    print(f"  {len(spec_cache.metrics)} metrics: {', '.join(spec_cache.metrics)}")
    print(f"  {len(spec_cache.slices)} slices : {', '.join(spec_cache.slices)}")

    bot = QueryBot(
        model=MODEL,
        spec_cache=spec_cache,
        connection_manager=conn_mgr,
    )

    print("\nStarting conversation …")
    total_tokens = 0
    conversation_id = None
    for i, question in enumerate(QUESTIONS, start=1):
        response = await bot.chat(question)
        print_response(i, question, response)
        print_sample(response)
        print_trace(response.trace)
        total_tokens += response.trace.usage.total_tokens
        conversation_id = response.trace.conversation_id

    print(f"\n{'─' * 70}")
    cid = conversation_id[:8] if conversation_id else "?"
    print(f"Session  : conv={cid}  {len(QUESTIONS)} turns  {total_tokens} tokens")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
