"""
03_intent_resolution_example.py — QueryBot v0.2 intent-gated resolution.

Demonstrates the three-step flow that QueryBot v0.2 uses internally:
    record_intent → resolve_intent → compute_metrics

Part 1 (no API key needed)
    SpecResolver used directly to inspect catalog validation: exact match,
    typo correction, wrong dimension kind, and unknown metric.

Part 2 (requires ANTHROPIC_API_KEY)
    A multi-turn QueryBot conversation.  The trace for each turn shows the
    three required steps before any analysis or final answer.

Part 3 (Anthropic only, same API key)
    Prompt-cache efficiency.  After turn 1 warms Layers A and B, subsequent
    turns report cache_read_tokens > 0, confirming the static system prompt
    is served from cache.

This module is the single implementation for both the standalone script below
and 03_intent_resolution_example.ipynb — the notebook imports from here rather
than redefining any of this, so the two can't drift apart. If you're reading
the notebook, everything it calls is defined here.

Prerequisites
-------------
1. Set your Anthropic API key:
       export ANTHROPIC_API_KEY=sk-ant-...

2. Install the agent extra:
       pip install aitaem[agent-anthropic]

Run from the project root:
    python examples/03_intent_resolution_example.py
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from typing import Any

import pandas as pd

from aitaem.connectors import ConnectionManager
from aitaem.helpers import load_csvs_to_duckdb
from aitaem.specs import SpecCache
from aitaem.agent import (
    MetricIntent,
    QueryBot,
    QueryResponse,
    RunTrace,
    SpecMatchResult,
    SpecResolver,
    Status,
)

MODEL = "anthropic:claude-haiku-4-5-20251001"

QUESTIONS = [
    # Turn 1 — resolves two metrics; warms Layers A+B in Anthropic's cache.
    "What was total revenue and ROAS across all campaigns?",
    # Turn 2 — breakdown by industry; cache is warm from turn 1.
    "Which industry had the highest CTR, broken down by campaign type?",
    # Turn 3 — monthly trend; Layers A+B are served from cache.
    "How did total revenue change month-by-month in 2024?",
]


# ── Pretty-printers ─────────────────────────────────────────────────────────

def fmt_arg(v: Any) -> str:
    if isinstance(v, str) and len(v) > 14:
        return f"'{v[:10]}…'"
    if isinstance(v, list):
        return f"[{', '.join(repr(x) for x in v)}]"
    return repr(v)


def print_resolution(label: str, result: SpecMatchResult) -> None:
    print(f"\n  {'─' * 62}")
    print(f"  {label}")
    print(f"  {'─' * 62}")
    if result.exact_match is not None:
        m = result.exact_match
        slices = ", ".join(m.slices) if m.slices else "(none)"
        print("  ✓  Exact match")
        print(f"     metric  : {m.metric_name}")
        print(f"     slices  : {slices}")
        print(f"     segment : {m.segment or '(none)'}")
        print("     token   : (minted by resolve_intent tool — empty here)")
    else:
        print(f"  ✗  No exact match — {len(result.near_misses)} near miss(es)")
        for nm in result.near_misses:
            line = f"     {nm.name!r:30s}  why_not={nm.why_not}"
            if nm.suggestions:
                line += f"  →  did you mean: {nm.suggestions}"
            print(line)


def print_response(turn: int, question: str, response: QueryResponse) -> None:
    print(f"\n{'─' * 70}")
    print(f"Turn {turn}: {question}")
    print(f"{'─' * 70}")
    print(f"Status   : {response.status.value}")
    print(
        f"Narrative: {textwrap.fill(response.narrative, width=70, subsequent_indent='           ')}"
    )
    if response.status == Status.error and response.reason:
        print(f"Error    : {response.reason}")
        return
    if response.status == Status.refused:
        print(f"Reason   : {response.reason}")
        return

    p = response.payload
    if p is None:
        return

    print(f"Metrics  : {', '.join(p.metrics_used) or '—'}")
    print(f"Slices   : {', '.join(p.slices_used) or '—'}")
    print(
        f"Period   : {p.period_type}"
        + (f"  {p.time_window[0]} → {p.time_window[1]}" if p.time_window else "")
    )
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


def print_trace(trace: RunTrace) -> None:
    print(
        f"\nTrace    : run={trace.run_id[:8]}  conv={trace.conversation_id[:8]}"
        f"  {trace.duration_ms:.0f}ms"
    )
    for tc in trace.tool_calls:
        non_null = {k: v for k, v in tc.args.items() if v is not None}
        args_str = ", ".join(f"{k}={fmt_arg(v)}" for k, v in non_null.items())
        icon = "✓" if tc.success else "✗"
        print(f"  {icon} {tc.name}({args_str})")
    u = trace.usage
    cache_info = f"  cache_read={u.cache_read_tokens}" if u.cache_read_tokens else ""
    print(f"Tokens   : {u.input_tokens} in / {u.output_tokens} out{cache_info}")


def cache_summary(label: str, response: QueryResponse) -> None:
    u = response.trace.usage
    ct = u.cache_read_tokens or 0
    status = "✓  served from cache" if ct > 0 else "✗  no cache hit"
    print(f"{label}")
    print(f"  input_tokens       : {u.input_tokens:>6}")
    print(f"  cache_read_tokens  : {ct:>6}  {status}")
    print(f"  output_tokens      : {u.output_tokens:>6}")
    print()


# ── Setup ────────────────────────────────────────────────────────────────────

def setup(base_path: str = ".") -> tuple[SpecCache, SpecCache, str]:
    """Load both spec catalogs and ensure the bundled DuckDB exists, creating
    it from the CSV if needed. base_path is the aitaem repo root — "." when
    run as a script from the project root, or an explicit path from a
    notebook that may be running from a different working directory. Returns
    (spec_cache_full, spec_cache, db_path): spec_cache_full includes segments
    (for the Part 1 SpecResolver demo); spec_cache excludes the platform
    segment, since it references a dimension table (dim_platforms) absent
    from the example DuckDB, and QueryBot would hit compute errors with it."""
    spec_cache_full = SpecCache.from_yaml(
        metric_paths=os.path.join(base_path, "examples/metrics/"),
        slice_paths=os.path.join(base_path, "examples/slices/"),
        segment_paths=os.path.join(base_path, "examples/segments/"),
    )
    spec_cache = SpecCache.from_yaml(
        metric_paths=os.path.join(base_path, "examples/metrics/"),
        slice_paths=os.path.join(base_path, "examples/slices/"),
    )

    db_path = os.path.join(base_path, "examples/data/ad_campaigns.duckdb")
    if not os.path.exists(db_path):
        csv_path = os.path.join(base_path, "examples/data/ad_campaigns.csv")
        load_csvs_to_duckdb(csv_path, db_path)

    return spec_cache_full, spec_cache, db_path


# ── Part 1: SpecResolver — catalog validation without an LLM ────────────────

def run_part1(spec_cache_full: SpecCache) -> None:
    """
    SpecResolver is the deterministic core of resolve_intent.  It validates
    the LLM's proposed metric/slice/segment names against the catalog and
    returns either an ExactMatch or a list of NearMiss objects explaining
    what was wrong.

    This part runs entirely locally — no API call, no LLM.
    """
    print("\n" + "═" * 70)
    print("PART 1: SpecResolver — catalog validation (no API key needed)")
    print("═" * 70)

    resolver = SpecResolver()

    # ── Helper: build a minimal intent ──────────────────────────────────────
    def _intent(
        concept: str,
        period_type: str = "all_time",
        by_entity: str | None = None,
    ) -> MetricIntent:
        return MetricIntent(
            metric_concept=concept,
            scope="overall",
            period_type=period_type,
            by_entity=by_entity,
        )

    print(
        "\nCatalog loaded:"
        f"\n  {len(spec_cache_full.metrics)} metrics : {', '.join(spec_cache_full.metrics)}"
        f"\n  {len(spec_cache_full.slices)} slices  : {', '.join(spec_cache_full.slices)}"
        f"\n  {len(spec_cache_full.segments)} segments: {', '.join(spec_cache_full.segments)}"
    )

    # ── Scenario 1: Exact match ──────────────────────────────────────────────
    result = resolver.resolve(
        _intent("total revenue"),
        proposed_metric_name="total_revenue",
        proposed_slices=["campaign_type"],
        proposed_segment=None,
        spec_cache=spec_cache_full,
    )
    print_resolution(
        "Scenario 1 — exact match: total_revenue sliced by campaign_type",
        result,
    )

    # ── Scenario 2: Typo in metric name ─────────────────────────────────────
    # SpecResolver populates NearMiss.suggestions via difflib (cutoff=0.75)
    # so the LLM can include the hint in its refusal message:
    #   "I couldn't find 'total_revenu'. Did you mean 'total_revenue'?"
    # The LLM does NOT auto-substitute — the user must confirm on the next turn.
    result = resolver.resolve(
        _intent("total revenue"),
        proposed_metric_name="total_revenu",   # missing trailing 'e'
        proposed_slices=[],
        proposed_segment=None,
        spec_cache=spec_cache_full,
    )
    print_resolution(
        "Scenario 2 — typo: 'total_revenu' → refused with user-nudge via suggestions",
        result,
    )

    # ── Scenario 3: Wrong dimension kind ────────────────────────────────────
    # 'platform' is a segment, not a slice.  Passing it in the slices list
    # gives why_not="wrong_dimension_kind" so the LLM knows to move it to
    # the segment parameter instead.
    result = resolver.resolve(
        _intent("CTR"),
        proposed_metric_name="ctr",
        proposed_slices=["platform"],   # platform is a segment spec
        proposed_segment=None,
        spec_cache=spec_cache_full,
    )
    print_resolution(
        "Scenario 3 — wrong dimension kind: 'platform' is a segment, not a slice",
        result,
    )

    # ── Scenario 4: Unknown metric ───────────────────────────────────────────
    # 'profit_margin' is not defined in the catalog.  difflib finds no close
    # match (cutoff=0.75), so suggestions is empty.  The LLM must refuse.
    result = resolver.resolve(
        _intent("profit margin"),
        proposed_metric_name="profit_margin",
        proposed_slices=[],
        proposed_segment=None,
        spec_cache=spec_cache_full,
    )
    print_resolution(
        "Scenario 4 — unknown metric: 'profit_margin' (no close match in catalog)",
        result,
    )


# ── Part 2 & 3: QueryBot conversation ───────────────────────────────────────

async def run_parts2_and_3(spec_cache: SpecCache, db_path: str) -> None:
    """
    Part 2: three-turn QueryBot conversation.  Each trace shows the mandatory
    record_intent → resolve_intent → compute_metrics sequence before analysis.

    Part 3: after turns 1 and 2 warm Layers A (workflow rules) and B (metric
    catalog), subsequent turns should read both from the Anthropic prompt
    cache. cache_summary() reports cache_read_tokens for every turn.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "\n" + "═" * 70
            + "\nPARTS 2 & 3 skipped — set ANTHROPIC_API_KEY to run the LLM turns."
            + "\n" + "═" * 70
        )
        return

    conn_mgr = ConnectionManager()
    conn_mgr.add_connection("duckdb", path=db_path)

    print("\n" + "═" * 70)
    print("PARTS 2 & 3: QueryBot conversation (requires ANTHROPIC_API_KEY)")
    print("═" * 70)
    print(
        "\nThe three-step flow is enforced by the system prompt.  Each turn's"
        "\ntrace shows record_intent and resolve_intent before compute_metrics."
        "\nPart 3 measures prompt-cache efficiency across all turns (Anthropic only)."
    )

    bot = QueryBot(
        model=MODEL,
        spec_cache=spec_cache,
        connection_manager=conn_mgr,
    )

    total_tokens = 0
    conversation_id: str | None = None
    responses: list[QueryResponse] = []

    for i, question in enumerate(QUESTIONS, start=1):
        response = await bot.chat(question)
        print_response(i, question, response)
        print_sample(response)
        print_trace(response.trace)
        total_tokens += response.trace.usage.total_tokens
        conversation_id = response.trace.conversation_id
        responses.append(response)

    if "anthropic:" in bot._model:
        print(f"\n{'─' * 70}")
        print("Part 3 — Cache efficiency summary (Anthropic only)")
        print(f"{'─' * 70}")
        for i, response in enumerate(responses, start=1):
            label = (
                "Turn 1 (warms cache — no read expected)"
                if i == 1
                else f"Turn {i} (Layers A+B should be cached)"
            )
            cache_summary(label, response)

    print(f"\n{'─' * 70}")
    cid = (conversation_id or "")[:8] or "?"
    print(f"Session  : conv={cid}  {len(QUESTIONS)} turns  {total_tokens:,} tokens")
    print("Done.")


# ── Entry point ──────────────────────────────────────────────────────────────

async def main(base_path: str = ".") -> None:
    print("Loading specs …")
    spec_cache_full, spec_cache, db_path = setup(base_path)
    print(
        f"  {len(spec_cache_full.metrics)} metrics, "
        f"{len(spec_cache_full.slices)} slices, "
        f"{len(spec_cache_full.segments)} segment(s) loaded for Part 1"
    )
    print(
        f"  {len(spec_cache.metrics)} metrics, "
        f"{len(spec_cache.slices)} slices loaded for Part 2 (no segments)"
    )

    # Part 1 uses only YAML metadata — no DB connection needed.
    run_part1(spec_cache_full)
    # Parts 2+3 open the DuckDB connection only when ANTHROPIC_API_KEY is set.
    await run_parts2_and_3(spec_cache, db_path)


if __name__ == "__main__":
    asyncio.run(main())
