"""
01_definition_bot_example.py — DefinitionBot spec-definition workflow.

Demonstrates the four-step flow that DefinitionBot uses internally:
    record_definition_intent → list_tables / describe_table → draft_spec → validate_spec

Part 1 (no API key needed)
    Direct YAML spec parsing — shows what DefinitionBot validates before minting a
    spec_draft_token.  Each spec type (metric, slice, segment) is demonstrated with
    a valid YAML string and its parsed attributes.

Part 2 (requires ANTHROPIC_API_KEY)
    Two DefinitionBot.ask() calls using the ad campaign dataset:
    • avg_cpc — a ratio metric (ad_spend / clicks)
    • country  — a wildcard slice (auto-discover country values from the column)
    Each run's trace shows the four-step workflow and token-gating in action.

Part 3 (Anthropic only, same API key)
    Prompt-cache efficiency.  Layer A (workflow rules) and Layer B (existing catalog)
    are cached across ask() calls.  The second ask() should report cache_read_tokens > 0.

Prerequisites
-------------
1. Set your Anthropic API key:
       export ANTHROPIC_API_KEY=sk-ant-...

2. Install the agent extra:
       pip install aitaem[agent-anthropic]

Run from the project root:
    python examples/definition_bot_example.py
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from typing import Any

from aitaem.connectors import ConnectionManager
from aitaem.helpers import load_csvs_to_duckdb
from aitaem.specs import MetricSpec, SliceSpec, SpecCache
from aitaem.agent import (
    DefinitionBot,
    DefinitionResponse,
    RunTrace,
    Status,
)


# ── Pretty-printers ─────────────────────────────────────────────────────────

def _fmt_arg(v: Any) -> str:
    if isinstance(v, str) and len(v) > 14:
        return f"'{v[:10]}…'"
    if isinstance(v, list):
        return f"[{', '.join(repr(x) for x in v)}]"
    return repr(v)


def _print_spec_parse(label: str, yaml_string: str) -> None:
    """Parse a YAML string and print the resulting spec's attributes."""
    print(f"\n  {'─' * 62}")
    print(f"  {label}")
    print(f"  {'─' * 62}")

    # Detect spec type from the first key in the YAML.
    top_key = yaml_string.strip().splitlines()[0].split(":")[0].strip()

    try:
        if top_key == "metric":
            spec = MetricSpec.from_yaml(yaml_string)
            print(f"  ✓  Parsed MetricSpec")
            print(f"     name        : {spec.name}")
            print(f"     source      : {spec.source}")
            print(f"     numerator   : {spec.numerator}")
            denom = getattr(spec, "denominator", None)
            if denom:
                print(f"     denominator : {denom}")
            print(f"     timestamp   : {spec.timestamp_col}")
            result = spec.validate()
            if result.referenced_columns:
                for field, cols in result.referenced_columns.items():
                    print(f"     refs [{field}]  : {cols}")
        elif top_key == "slice":
            spec = SliceSpec.from_yaml(yaml_string)
            print(f"  ✓  Parsed SliceSpec")
            print(f"     name        : {spec.name}")
            if spec.is_composite:
                print(f"     subtype     : composite")
                print(f"     cross_product: {spec.cross_product}")
            elif spec.is_wildcard:
                print(f"     subtype     : wildcard")
                print(f"     column      : {spec.column}")
            else:
                print(f"     subtype     : leaf ({len(spec.values)} values)")
                for sv in spec.values[:3]:
                    print(f"       - {sv.name}: {sv.where}")
                if len(spec.values) > 3:
                    print(f"       … {len(spec.values) - 3} more")
        else:
            print(f"  ✗  Unknown top-level key: {top_key!r}")
    except Exception as exc:
        print(f"  ✗  Parse failed: {type(exc).__name__}: {exc}")


def _print_response(label: str, response: DefinitionResponse) -> None:
    print(f"\n{'─' * 70}")
    print(f"{label}")
    print(f"{'─' * 70}")
    print(f"Status   : {response.status.value}")
    print(
        f"Narrative: {textwrap.fill(response.narrative, width=70, subsequent_indent='           ')}"
    )
    if response.status == Status.error and response.reason:
        print(f"Error    : {response.reason}")
        return
    if response.status == Status.refused:
        if response.reason:
            print(f"Reason   : {response.reason}")
        return

    p = response.payload
    if p is None or p.spec_type is None:
        return

    print(f"Spec     : {p.spec_type} / {p.spec_name}")
    print(f"Token    : {(p.spec_draft_token or '')[:16]}…")
    if p.referenced_columns:
        for src, cols in p.referenced_columns.items():
            print(f"Refs     : [{src}] {cols}")
    if p.validation_warnings:
        for w in p.validation_warnings:
            print(f"Warning  : {w}")


def _print_yaml(response: DefinitionResponse) -> None:
    p = response.payload
    if p is None or not p.yaml_string:
        return
    print(f"\nGenerated YAML:")
    for line in p.yaml_string.splitlines():
        print(f"  {line}")


def _print_trace(trace: RunTrace) -> None:
    print(
        f"\nTrace    : run={trace.run_id[:8]}  conv={trace.conversation_id[:8]}"
        f"  {trace.duration_ms:.0f}ms"
    )
    for tc in trace.tool_calls:
        non_null = {k: v for k, v in tc.args.items() if v is not None}
        args_str = ", ".join(f"{k}={_fmt_arg(v)}" for k, v in non_null.items())
        icon = "✓" if tc.success else "✗"
        print(f"  {icon} {tc.name}({args_str})")
    u = trace.usage
    cache_info = ""
    if u.cache_read_tokens:
        cache_info = f"  cache_read={u.cache_read_tokens}"
    print(f"Tokens   : {u.input_tokens} in / {u.output_tokens} out{cache_info}")


# ── Part 1: direct YAML spec parsing ────────────────────────────────────────

def run_part1(spec_cache: SpecCache) -> None:
    """
    MetricSpec.from_yaml() and SliceSpec.from_yaml() are the structural
    validators that DefinitionBot's validate_spec tool calls internally.
    This part shows what valid spec YAML looks like for each spec type and
    what attributes the parser extracts — no API call, no LLM.
    """
    print("\n" + "═" * 70)
    print("PART 1: Direct spec parsing (no API key needed)")
    print("═" * 70)

    print(
        f"\nExisting catalog:"
        f"\n  {len(spec_cache.metrics)} metrics  : {', '.join(spec_cache.metrics)}"
        f"\n  {len(spec_cache.slices)} slices   : {', '.join(spec_cache.slices)}"
        f"\n  {len(spec_cache.segments)} segments : {', '.join(spec_cache.segments) or '(none)'}"
    )
    print(
        "\nWe will define two new specs that are absent from the catalog above:"
        "\n  avg_cpc — a ratio metric (ad_spend ÷ clicks)"
        "\n  country — a wildcard slice (auto-discovers values from the column)"
    )

    # ── Scenario 1: ratio metric (MetricSpec) ────────────────────────────────
    _print_spec_parse(
        "Scenario 1 — MetricSpec: avg_cpc (ratio — ad_spend ÷ clicks)",
        """\
metric:
  name: avg_cpc
  description: Average cost per click — total ad spend divided by total clicks
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "SUM(ad_spend)"
  denominator: "SUM(clicks)"
  timestamp_col: date
  format: "currency:USD"
""",
    )

    # ── Scenario 2: wildcard slice (SliceSpec) ───────────────────────────────
    # Wildcard slices use a column name in `where`. At query time the engine
    # SELECTs DISTINCT values from that column and groups dynamically.
    _print_spec_parse(
        "Scenario 2 — SliceSpec (wildcard): country (auto-discovers values from column)",
        """\
slice:
  name: country
  description: Breakdown by country — values auto-discovered from the country column
  where: country
""",
    )

    # ── Scenario 3: structural parse failure ─────────────────────────────────
    # Missing `numerator` is a required field for MetricSpec.
    # validate_spec would catch this at check #2 and return errors=[...].
    _print_spec_parse(
        "Scenario 3 — MetricSpec parse failure: missing required 'numerator' field",
        """\
metric:
  name: bad_metric
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
""",
    )


# ── Parts 2 & 3: DefinitionBot ───────────────────────────────────────────────

async def run_parts2_and_3(spec_cache: SpecCache, db_path: str) -> None:
    """
    Part 2: two DefinitionBot.ask() calls.  Each trace shows the four mandatory
    steps before the spec_draft_token is minted and returned.

    Part 3: the second ask() demonstrates Layer A+B prompt-cache efficiency.
    After the first call warms Layers A (workflow rules) and B (existing catalog),
    the second call should report cache_read_tokens > 0 for Anthropic models.
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
    print("PARTS 2 & 3: DefinitionBot.ask() (requires ANTHROPIC_API_KEY)")
    print("═" * 70)
    print(
        "\nThe four-step flow is enforced by the system prompt.  Each ask()'s"
        "\ntrace shows record_definition_intent, list_tables, describe_table,"
        "\ndraft_spec, and validate_spec before the spec_draft_token is returned."
        "\nThe second ask() measures prompt-cache efficiency (Anthropic only)."
    )

    bot = DefinitionBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=spec_cache,
        connection_manager=conn_mgr,
    )

    # ── Ask 1: ratio metric ───────────────────────────────────────────────────
    # avg_cpc = total ad_spend / total clicks.  The bot must call list_tables,
    # describe_table, draft_spec, and validate_spec before returning the token.
    # This call also warms Layers A+B in Anthropic's prompt cache.
    response1 = await bot.ask(
        "Define a metric called avg_cpc for average cost per click — "
        "total ad spend divided by total clicks."
    )
    _print_response("Ask 1: avg_cpc — average cost per click", response1)
    _print_yaml(response1)
    _print_trace(response1.trace)

    # ── Ask 2: wildcard slice ─────────────────────────────────────────────────
    # A wildcard slice delegates value discovery to the query engine at runtime.
    # Layer A+B should now be read from Anthropic's cache.
    response2 = await bot.ask(
        "Define a wildcard slice called country that auto-discovers "
        "all country values from the country column in the campaigns table."
    )
    _print_response("Ask 2: country — wildcard slice", response2)
    _print_yaml(response2)
    _print_trace(response2.trace)

    # ── Part 3: cache summary ─────────────────────────────────────────────────
    if "anthropic:" in bot._model:
        print(f"\n{'─' * 70}")
        print("Part 3 — Cache efficiency summary (Anthropic only)")
        print(f"{'─' * 70}")

        def _cache_row(label: str, response: DefinitionResponse) -> None:
            u = response.trace.usage
            ct = u.cache_read_tokens or 0
            status = "✓  Layers A+B from cache" if ct > 0 else "✗  no cache hit"
            print(f"{label}")
            print(f"  input_tokens       : {u.input_tokens:>6}")
            print(f"  cache_read_tokens  : {ct:>6}  {status}")
            print(f"  output_tokens      : {u.output_tokens:>6}")
            print()

        _cache_row("Ask 1 (warms cache)", response1)
        _cache_row("Ask 2 (Layers A+B should be cached)", response2)

    total = sum(
        r.trace.usage.total_tokens
        for r in (response1, response2)
        if r.trace.usage.total_tokens
    )
    print(f"\n{'─' * 70}")
    conv = (response2.trace.conversation_id or "")[:8] or "?"
    print(f"Session  : conv={conv}  2 specs  {total:,} tokens")
    print("Done.")


# ── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    # ── 1. Load specs ────────────────────────────────────────────────────────
    print("Loading specs …")

    # Full catalog for Part 1 — SpecResolver reads YAML metadata only.
    spec_cache = SpecCache.from_yaml(
        metric_paths="examples/metrics/",
        slice_paths="examples/slices/",
        segment_paths="examples/segments/",
    )
    print(
        f"  {len(spec_cache.metrics)} metrics, "
        f"{len(spec_cache.slices)} slices, "
        f"{len(spec_cache.segments)} segment(s) loaded"
    )

    # ── 2. Ensure DuckDB exists ───────────────────────────────────────────────
    db_path = "examples/data/ad_campaigns.duckdb"
    if not os.path.exists(db_path):
        print("\nDuckDB file not found — creating from CSV …")
        load_csvs_to_duckdb("examples/data/ad_campaigns.csv", db_path)
        print(f"  Created {db_path}")

    # ── 3. Run the example sections ───────────────────────────────────────────
    run_part1(spec_cache)
    await run_parts2_and_3(spec_cache, db_path)


if __name__ == "__main__":
    asyncio.run(main())
