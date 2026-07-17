# Phase 2 — QueryBot: `aitaem.agent`

Delivers the first working convenience bot. A caller with a `SpecCache` and a `ConnectionManager` gets a bot that answers natural-language questions against their metric catalog, calls `MetricCompute` under the hood, and returns structured responses with dereferenced artifacts. Phase 1 foundations are the prerequisite.

---

## Decisions Resolved (Pre-flight)

| Question | Decision |
|---|---|
| **Q1: LLM–bot contract** | Pydantic models throughout. Tool return types are compact Pydantic models (serialized to JSON; the LLM reads them). The final agent output type is `QueryOutput` (pydantic-ai `output_type`). The bot assembles `QueryPayload` + `QueryResponse` from `QueryOutput` and the trace. |
| **Q2: Tool state access** | `deps_type=QueryDeps` pattern. `QueryDeps` carries `spec_cache`, `connection_manager`, and `store`. Tools are `RunContext[QueryDeps]`-aware. `MetricCompute` is constructed per-call inside `compute_metrics` from `spec_cache` and `connection_manager`. |
| **Q3: Segment parameter** | `MetricCompute.compute()` accepts `segments` as either a `str` (segment name; uses the spec's default join key) or a `dict[str, str]` (segment name → explicit join key override). The `compute_metrics` tool exposes only the string form: `segment: str \| None`. One segment per call, no join-key override. The `dict` form and multi-segment handling are deferred; tracked as OQ-A2. |
| **Q4: Analysis tool count** | All 5 ship in Phase 2: `rank_by_value`, `filter_by_threshold`, `distribution_summary`, `period_over_period`, `contribution_share`. |
| **Q5: Test strategy** | `FunctionModel`-based unit/integration tests first. Real-LLM integration tests as a fast-follow. `pytest-asyncio` added to `[dev]`. |
| **Docs** | Public API docs for `QueryBot`, `QueryResponse`, `QueryPayload` deferred to Phase 7 (all agent docs bundled). |

---

## Terminology

Reuses Phase 1 terminology (Run, Turn, Conversation, Trace) without change.

**New term — QueryDeps:** The `@dataclass` that carries the bot's session-scoped resources into every tool call. Analogous to a "context object" for the tool layer.

---

## Background: pydantic-ai v2 Patterns Used in Phase 2

### `output_type` for structured final answers

Setting `output_type=QueryOutput` on the `Agent` tells pydantic-ai that the LLM must produce a `QueryOutput`-shaped JSON as its terminal response. Tool calling happens in the intermediate steps; the structured output is only produced after the tool loop completes. pydantic-ai injects schema instructions appropriate to the provider (JSON mode for OpenAI, system-prompt schema for Anthropic).

Consequence: `result.output` on the `AgentRunResult` is a validated `QueryOutput` instance, not a string.

### `deps_type` for tool state sharing

```python
from dataclasses import dataclass

@dataclass
class QueryDeps:
    spec_cache: SpecCache
    connection_manager: ConnectionManager
    store: ResultStore

# In QueryBot._build_agent():
agent = Agent(model=self._model, deps_type=QueryDeps, output_type=QueryOutput, ...)

# In each tool:
def compute_metrics(ctx: RunContext[QueryDeps], metrics: list[str], ...) -> ComputeMetricsResult:
    from aitaem import MetricCompute
    mc = MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)
    ibis_table = mc.compute(metrics=metrics, ...)
    result_id = ctx.deps.store.store(arrow_table, ibis_table)
    ...
```

### `FunctionModel` for deterministic tests

```python
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, TextPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, AgentInfo

def _make_model_that_computes_revenue():
    def fn(messages, info: AgentInfo) -> ModelResponse:
        # Find any tool return parts
        tool_returns = [
            p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not tool_returns:
            # Step 1: call compute_metrics
            return ModelResponse(parts=[ToolCallPart(
                tool_name="compute_metrics",
                args=json.dumps({"metrics": ["revenue"], "period_type": "all_time"}),
                tool_call_id="tc-1",
            )])
        else:
            # Step 2: extract result_id from tool return, produce QueryOutput
            payload = json.loads(tool_returns[0].content)
            output = QueryOutput(
                status=Status.ok,
                narrative="Revenue was computed.",
                result_ids=[payload["result_id"]],
            )
            return ModelResponse(parts=[TextPart(content=output.model_dump_json())])
    return FunctionModel(fn)
```

Key: `ToolReturnPart.content` is the JSON-serialized `ComputeMetricsResult`. The FunctionModel parses it to extract `result_id`. The final `TextPart` contains a `QueryOutput` JSON string — pydantic-ai parses it against the `output_type` schema.

### `ReinjectSystemPrompt` capability

Every `QueryBot._build_agent()` call includes `ReinjectSystemPrompt(replace_existing=True)`. This ensures the current bot's system prompt wins over any stale stored prompt when a history bundle is reloaded (Phase 1 design note, now activated).

### Lazy vs. eager mode in analysis tools

A private helper `_get_ibis_table(entry: ResultEntry) -> ibis.Table` (in `query_tools.py`) handles both modes:

```python
def _get_ibis_table(entry: ResultEntry) -> ibis.Table:
    if entry.ibis_ref is not None:
        return entry.ibis_ref
    if entry.arrow is not None:
        return ibis.memtable(entry.arrow)   # in-memory DuckDB expression
    raise ValueError(f"Result entry {entry.id!r} has no data.")
```

**Lazy mode** (`ibis_ref` alive): operations are pushed down to the original backend.  
**Eager mode** (`ibis_ref` is `None`, e.g., after history reload): `ibis.memtable` wraps the Arrow table so all tools use a single code path.

`rank_by_value` and `filter_by_threshold` use the Ibis path throughout. `distribution_summary`, `period_over_period`, and `contribution_share` materialize to pandas (`table.to_pandas()`) since they produce derived schemas with window functions where pandas is cleaner.

---

## File Structure

### New files

```
aitaem/agent/
├── query_types.py        # QueryDeps, QueryOutput, QueryPayload, all tool result models
├── query_tools.py        # compute_metrics + 5 analysis tools + _get_ibis_table helper
└── query_bot.py          # QueryBot, QueryResponse, _build_system_prompt, _assemble_payload

tests/test_agent/
├── test_query_tools.py      # SF-2/3: tool unit tests
├── test_query_bot.py        # SF-4 through SF-10: bot-level and history tests
└── test_query_bot_smoke.py  # SF-10b: real-LLM smoke test (skipped without ANTHROPIC_API_KEY)
```

### Modified files

```
aitaem/agent/__init__.py                         # add QueryBot, QueryResponse, QueryPayload
plans/agent_module/08-implementation-order.md    # add OQ-A2 in appendix
pyproject.toml                                   # add pytest-asyncio to [dev]
```

No changes to existing Phase 1 files beyond `__init__.py`.

---

## Implementation Sub-Features

Implement in order. Each SF is independently testable before moving to the next.

---

### SF-1: Pydantic contract models (`aitaem/agent/query_types.py`)

All types that define the LLM–bot–tool contract live here. No tool logic; no bot logic.

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aitaem.agent.trace import Status


# ── Deps (passed to every tool via RunContext) ──────────────────────────────

@dataclass
class QueryDeps:
    """Session-scoped resources available to every QueryBot tool."""
    spec_cache: Any           # aitaem.SpecCache; for spec lookups and format hints
    connection_manager: Any   # aitaem.ConnectionManager; for backend access
    store: ResultStore        # aitaem.agent.store.ResultStore


# ── Final agent output (output_type — LLM fills this last) ──────────────────

class QueryOutput(BaseModel):
    """Structured final answer from the LLM after completing a QueryBot turn.

    The LLM produces exactly one QueryOutput per turn, after all tool calls.
    result_ids must reference result_id values from tool outputs in this turn.
    """
    model_config = ConfigDict(frozen=True)

    status: Status = Field(
        description=(
            "ok = data returned; empty = no rows matched; "
            "refused = question out of scope or no exact metric match; "
            "error = a tool failed."
        )
    )
    narrative: str = Field(
        description="Plain-language explanation for the user. Narrate from the tool summaries."
    )
    result_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Result store IDs to surface to the caller, ordered with the primary result first. "
            "Read result_id from each tool output and include the ones relevant to the answer. "
            "Empty when status is refused, empty, or error."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="Brief explanation when status is refused or error. Null otherwise.",
    )


# ── Bot-assembled response types (never seen by LLM) ────────────────────────

class QueryPayload(BaseModel):
    """Metadata assembled by QueryBot from QueryOutput and the turn trace."""
    model_config = ConfigDict(frozen=True)

    result_ids: list[str]
    primary_result_id: str | None   # first entry of result_ids, or None
    metrics_used: list[str]
    slices_used: list[str]
    segment_used: str | None
    time_window: tuple[str, str] | None
    period_type: str
    by_entity: str | None
    format_hints: dict[str, str] = Field(
        default_factory=dict,
        description="metric_name → format string (e.g. 'percentage', 'currency:USD'). "
                    "Callers use this to render metric values correctly.",
    )


# ── Tool result models (LLM reads these after each tool call) ────────────────

class ToolResult(BaseModel):
    """Base for all tool result models returned to the LLM.

    If this tool contributes to QueryPayload, populate payload_summary with
    any of the standard keys (all optional — omit inapplicable ones):
      metrics_used : list[str]             — metric names computed this call
      slices_used  : list[str]             — slice names applied
      segment_used : str | None            — segment name applied
      period_type  : str                   — granularity ("all_time", "monthly", …)
      time_window  : list[str] | None      — [start, end] ISO-8601 dates
      by_entity    : str | None            — entity grouping column
      format_hints : dict[str, str]        — metric_name → format string (e.g. "percentage")

    Leave payload_summary=None if the tool contributes nothing to the payload
    (analysis tools that only transform a prior result should do this).

    Aggregation when multiple tool calls contribute in one turn:
      - list fields  : union with deduplication, order of first appearance
      - scalar fields: first-write wins (first call that sets a field governs)
    """
    payload_summary: dict[str, Any] | None = None
    error: str | None = None   # populated on failure; result_id will be "" when set


class ComputeMetricsResult(ToolResult):
    """Summary returned by compute_metrics. Full data is in ResultStore."""
    result_id: str
    metrics: list[str]
    slices: list[str] | None
    segment: str | None
    row_count: int
    sample: list[dict[str, Any]]    # up to 5 rows, metric_value included
    columns: list[str]
    period_type: str
    time_window: tuple[str, str] | None
    by_entity: str | None
    format_hints: dict[str, str]    # metric_name → format string (e.g. "percentage")


class RankByValueResult(ToolResult):
    """Summary returned by rank_by_value."""
    result_id: str
    top_rows: list[dict[str, Any]]  # up to top_n rows
    total_rows: int
    ascending: bool


class FilterByThresholdResult(ToolResult):
    """Summary returned by filter_by_threshold."""
    result_id: str
    matching_rows: int
    total_rows: int
    sample: list[dict[str, Any]]    # up to 5 matching rows
    predicate: str                  # human-readable: "metric_value > 100.0"


class MetricDistribution(BaseModel):
    """Per-metric distribution statistics."""
    metric_name: str
    count: int
    mean: float | None = None
    std: float | None = None
    min_val: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    max_val: float | None = None


class DistributionSummaryResult(ToolResult):
    """Summary returned by distribution_summary. One entry per unique metric_name."""
    result_id: str
    distributions: list[MetricDistribution]


class PeriodOverPeriodResult(ToolResult):
    """Summary returned by period_over_period."""
    result_id: str
    periods_found: int
    rows_computed: int
    sample: list[dict[str, Any]]    # up to 5 rows; includes delta and pct_change columns


class ContributionShareResult(ToolResult):
    """Summary returned by contribution_share."""
    result_id: str
    total_value: float              # sum of metric_value across all rows
    rows: int
    sample: list[dict[str, Any]]    # up to 5 rows by descending share; includes share and cumulative_share
```

**Validation:** All models are pure Pydantic with no side effects; instantiation is the test.

```python
# tests/test_agent/test_query_tools.py

from aitaem.agent.query_types import (
    QueryOutput, QueryPayload, ComputeMetricsResult,
    RankByValueResult, FilterByThresholdResult, DistributionSummaryResult,
    PeriodOverPeriodResult, ContributionShareResult, MetricDistribution,
    QueryDeps,
)
from aitaem.agent.trace import Status


def test_tool_result_base_payload_summary_defaults_none():
    from aitaem.agent.query_types import ToolResult
    assert ToolResult().payload_summary is None


def test_compute_metrics_result_is_tool_result():
    from aitaem.agent.query_types import ComputeMetricsResult, ToolResult
    assert issubclass(ComputeMetricsResult, ToolResult)


def test_all_result_models_are_tool_results():
    from aitaem.agent.query_types import (
        ToolResult, RankByValueResult, FilterByThresholdResult,
        DistributionSummaryResult, PeriodOverPeriodResult, ContributionShareResult,
    )
    for cls in [RankByValueResult, FilterByThresholdResult, DistributionSummaryResult,
                PeriodOverPeriodResult, ContributionShareResult]:
        assert issubclass(cls, ToolResult), f"{cls.__name__} must inherit ToolResult"


def test_query_output_frozen():
    from pydantic import ValidationError
    out = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    with pytest.raises(ValidationError):
        out.status = Status.error


def test_query_output_refused_has_reason():
    out = QueryOutput(status=Status.refused, narrative="N/A.", reason="No exact match.")
    assert out.reason == "No exact match."
    assert out.result_ids == []


def test_compute_metrics_result_error_case():
    r = ComputeMetricsResult(
        result_id="", metrics=["revenue"], slices=None, segment=None,
        row_count=0, sample=[], columns=[], period_type="all_time",
        time_window=None, by_entity=None, format_hints={}, error="SpecNotFoundError: revenue",
    )
    assert r.error is not None
    assert r.result_id == ""


def test_metric_distribution_optional_stats():
    d = MetricDistribution(metric_name="ctr", count=0)
    assert d.mean is None
```

---

### SF-2: `compute_metrics` tool (`aitaem/agent/query_tools.py`)

```python
from __future__ import annotations

import json
import operator
from typing import Any

import ibis
import pyarrow as pa
from pydantic_ai import RunContext

from aitaem.agent.query_types import (
    QueryDeps,
    ComputeMetricsResult,
    RankByValueResult,
    FilterByThresholdResult,
    DistributionSummaryResult,
    MetricDistribution,
    PeriodOverPeriodResult,
    ContributionShareResult,
)
from aitaem.agent.store import ResultEntry

# Columns that identify a row's grouping in STANDARD_COLUMNS.
# Used by analysis tools to find meaningful group-by keys without using index.
_STANDARD_COLS = [
    "period_type", "period_start_date", "period_end_date", "entity_id",
    "metric_name", "metric_format", "slice_type", "slice_value",
    "segment_name", "segment_value", "metric_value",
]
_NON_VALUE_COLS = frozenset(_STANDARD_COLS) - {"metric_value"}


def _get_ibis_table(entry: ResultEntry) -> ibis.Table:
    """Return an ibis.Table: lazy from ibis_ref if alive, else memtable over Arrow."""
    if entry.ibis_ref is not None:
        return entry.ibis_ref
    if entry.arrow is not None:
        return ibis.memtable(entry.arrow)
    raise ValueError(f"Result entry {entry.id!r} has no data.")


def _sample_arrow(table: pa.Table, n: int = 5) -> list[dict[str, Any]]:
    """Return up to n rows as a list of dicts with JSON-safe values."""
    sliced = table.slice(0, n)
    return [
        {col: (v if not hasattr(v, "as_py") else v.as_py()) for col, v in zip(sliced.column_names, row)}
        for row in zip(*[sliced.column(c) for c in sliced.column_names])
    ]


def compute_metrics(
    ctx: RunContext[QueryDeps],
    metrics: list[str],
    slices: list[str] | None = None,
    segment: str | None = None,
    time_window: tuple[str, str] | None = None,
    period_type: str = "all_time",
    by_entity: str | None = None,
) -> ComputeMetricsResult:
    """Compute one or more metrics from the spec catalog.

    Args:
        metrics: One or more metric names. Must exactly match names in the catalog.
        slices: Optional slice names to break the metric down by. Each slice
            produces additional rows in the result.
        segment: Optional segment name for entity-level segmentation. Only one
            segment per call is supported.
        time_window: (start_date, end_date) as ISO-8601 strings (e.g.
            ("2024-01-01", "2024-03-31")). Required when period_type is not
            "all_time". Requires timestamp_col to be set on each metric spec.
        period_type: Granularity for time grouping. One of: "all_time",
            "hourly", "daily", "weekly", "monthly", "yearly".
        by_entity: Column name for entity-level grouping. Each metric must
            list this column in its entities field.

    Returns:
        ComputeMetricsResult with result_id pointing to the stored artifact.
        On failure, result_id is "" and error contains the exception message.
    """
    from aitaem import MetricCompute
    from aitaem.utils.exceptions import (
        AitaemConnectionError,
        QueryBuildError,
        QueryExecutionError,
        SpecNotFoundError,
    )

    try:
        mc = MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)
        ibis_table = mc.compute(
            metrics=metrics,
            slices=slices,
            segments=segment,
            time_window=tuple(time_window) if time_window else None,
            period_type=period_type,
            by_entity=by_entity,
        )
        arrow_table = ibis_table.to_pyarrow()
        result_id = ctx.deps.store.store(arrow_table, ibis_table)

        # Build format hints from spec cache
        format_hints: dict[str, str] = {}
        for m in metrics:
            spec = ctx.deps.spec_cache.metrics.get(m)
            if spec and spec.format:
                format_hints[m] = spec.format

        return ComputeMetricsResult(
            result_id=result_id,
            metrics=metrics,
            slices=slices,
            segment=segment,
            row_count=len(arrow_table),
            sample=_sample_arrow(arrow_table),
            columns=arrow_table.schema.names,
            period_type=period_type,
            time_window=tuple(time_window) if time_window else None,
            by_entity=by_entity,
            format_hints=format_hints,
            payload_summary={
                "metrics_used": metrics,
                "slices_used": slices or [],
                "segment_used": segment,
                "period_type": period_type,
                "time_window": list(time_window) if time_window else None,
                "by_entity": by_entity,
                "format_hints": format_hints,
            },
        )

    except (SpecNotFoundError, QueryBuildError, QueryExecutionError, AitaemConnectionError) as exc:
        return ComputeMetricsResult(
            result_id="",
            metrics=metrics,
            slices=slices,
            segment=segment,
            row_count=0,
            sample=[],
            columns=[],
            period_type=period_type,
            time_window=tuple(time_window) if time_window else None,
            by_entity=by_entity,
            format_hints={},
            error=f"{type(exc).__name__}: {exc}",
        )
```

**Validation (SF-2 tests):**

```python
# tests/test_agent/test_query_tools.py

import asyncio
from unittest.mock import MagicMock, patch
import pyarrow as pa
import pytest

from aitaem.agent.store import ResultStore
from aitaem.agent.query_types import QueryDeps
from aitaem.agent.query_tools import compute_metrics


def _make_ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _sample_table():
    return pa.table({
        "metric_name": ["revenue"],
        "metric_value": [1000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": [None],
        "slice_type": [None],
        "slice_value": [None],
        "segment_name": [None],
        "segment_value": [None],
    })


def _make_deps():
    mock_spec = MagicMock()
    mock_spec.format = None
    mock_sc = MagicMock()
    mock_sc.metrics = {"revenue": mock_spec}
    mock_cm = MagicMock()
    store = ResultStore()
    return QueryDeps(spec_cache=mock_sc, connection_manager=mock_cm, store=store), store


def _make_mock_mc(arrow_table=None, raise_exc=None):
    """Return a mock MetricCompute; patch into query_tools.MetricCompute at call site."""
    mc = MagicMock()
    if raise_exc:
        mc.compute.side_effect = raise_exc
    else:
        mock_ibis = MagicMock()
        mock_ibis.to_pyarrow.return_value = arrow_table or _sample_table()
        mc.compute.return_value = mock_ibis
    return mc


def test_compute_metrics_success_stores_result():
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"], period_type="all_time")
    assert result.error is None
    assert result.row_count == 1
    assert result.result_id != ""
    assert result.result_id in store.ids()
    entry = store.get(result.result_id)
    assert entry.arrow is not None
    assert entry.ibis_ref is not None


def test_compute_metrics_stores_ibis_ref():
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"], period_type="all_time")
    entry = store.get(result.result_id)
    assert entry.ibis_ref is not None


def test_compute_metrics_spec_not_found():
    from aitaem.utils.exceptions import SpecNotFoundError
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc(raise_exc=SpecNotFoundError("revenue not found"))
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"])
    assert result.error is not None
    assert "SpecNotFoundError" in result.error
    assert result.result_id == ""
    assert len(store.ids()) == 0


def test_compute_metrics_format_hints():
    mock_spec = MagicMock()
    mock_spec.format = "percentage"
    mock_sc = MagicMock()
    mock_sc.metrics = {"ctr": mock_spec}
    store = ResultStore()
    deps = QueryDeps(spec_cache=mock_sc, connection_manager=MagicMock(), store=store)
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["ctr"])
    assert result.format_hints == {"ctr": "percentage"}


def test_compute_metrics_populates_payload_summary():
    deps, store = _make_deps()
    ctx = _make_ctx(deps)
    mock_mc = _make_mock_mc()
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mock_mc):
        result = compute_metrics(ctx, metrics=["revenue"], slices=["by_country"], period_type="monthly")
    assert result.payload_summary is not None
    assert result.payload_summary["metrics_used"] == ["revenue"]
    assert result.payload_summary["slices_used"] == ["by_country"]
    assert result.payload_summary["period_type"] == "monthly"
    assert result.payload_summary["segment_used"] is None


def test_compute_metrics_sample_max_five_rows():
    big_table = pa.table({
        "metric_name": ["revenue"] * 10,
        "metric_value": list(range(10)),
        "period_type": ["monthly"] * 10,
        "period_start_date": [None] * 10,
        "period_end_date": [None] * 10,
        "entity_id": [None] * 10,
        "metric_format": [None] * 10,
        "slice_type": [None] * 10,
        "slice_value": [None] * 10,
        "segment_name": [None] * 10,
        "segment_value": [None] * 10,
    })
    deps, store = _make_deps(arrow_table=big_table)
    ctx = _make_ctx(deps)
    result = compute_metrics(ctx, metrics=["revenue"])
    assert len(result.sample) <= 5
```

---

### SF-3: Analysis tools (`aitaem/agent/query_tools.py`, continued)

All five tools follow the same pattern: read from `ResultStore`, compute, write a new entry (Arrow only; no ibis_ref), return a compact result model.

```python
# ── Operator map for filter_by_threshold ────────────────────────────────────

_FILTER_OPS: dict[str, Any] = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq, "!=": operator.ne,
}


# ── rank_by_value ────────────────────────────────────────────────────────────

def rank_by_value(
    ctx: RunContext[QueryDeps],
    result_id: str,
    top_n: int = 10,
    ascending: bool = False,
) -> RankByValueResult:
    """Rank rows in a prior result by metric_value and return the top N.

    Args:
        result_id: ID of the result store entry to rank.
        top_n: Number of rows to return.
        ascending: If True, return lowest values first (default: highest first).

    Returns:
        RankByValueResult with a new result_id for the ranked slice.
    """
    entry = ctx.deps.store.get(result_id)
    ibis_table = _get_ibis_table(entry)

    order_fn = ibis.asc if ascending else ibis.desc
    ranked = ibis_table.order_by(order_fn("metric_value")).limit(top_n)
    result_arrow = ranked.to_pyarrow()
    new_id = ctx.deps.store.store(result_arrow, None)

    return RankByValueResult(
        result_id=new_id,
        top_rows=_sample_arrow(result_arrow, n=top_n),
        total_rows=len(result_arrow),
        ascending=ascending,
    )


# ── filter_by_threshold ──────────────────────────────────────────────────────

def filter_by_threshold(
    ctx: RunContext[QueryDeps],
    result_id: str,
    threshold: float,
    op: str = ">",
    column: str = "metric_value",
) -> FilterByThresholdResult:
    """Filter rows in a prior result by a threshold on a numeric column.

    Args:
        result_id: ID of the result store entry to filter.
        threshold: Numeric threshold for the comparison.
        op: Comparison operator. One of: ">", ">=", "<", "<=", "==", "!=".
        column: Column to apply the threshold to. Defaults to "metric_value".
            Use "pct_change" or "delta" to filter on period_over_period output.

    Returns:
        FilterByThresholdResult with a new result_id for the filtered rows.
    """
    if op not in _FILTER_OPS:
        raise ValueError(f"op must be one of {list(_FILTER_OPS)}; got {op!r}")

    entry = ctx.deps.store.get(result_id)
    ibis_table = _get_ibis_table(entry)

    if column not in ibis_table.columns:
        return FilterByThresholdResult(
            result_id="",
            matching_rows=0,
            total_rows=entry.arrow.num_rows if entry.arrow is not None else 0,
            sample=[],
            predicate=f"{column} {op} {threshold}",
            error=f"Column {column!r} not found. Available columns: {ibis_table.columns}",
        )

    filtered = ibis_table.filter(_FILTER_OPS[op](ibis_table[column], threshold))
    result_arrow = filtered.to_pyarrow()
    new_id = ctx.deps.store.store(result_arrow, None)

    return FilterByThresholdResult(
        result_id=new_id,
        matching_rows=len(result_arrow),
        total_rows=entry.arrow.num_rows if entry.arrow is not None else 0,
        sample=_sample_arrow(result_arrow),
        predicate=f"{column} {op} {threshold}",
    )


# ── distribution_summary ─────────────────────────────────────────────────────

def distribution_summary(
    ctx: RunContext[QueryDeps],
    result_id: str,
) -> DistributionSummaryResult:
    """Compute distribution statistics (mean, std, percentiles) over metric_value.

    Statistics are computed per unique metric_name in the result.
    The result store entry contains one row per metric_name with the stats columns.

    Args:
        result_id: ID of the result store entry to summarize.

    Returns:
        DistributionSummaryResult with per-metric statistics.
    """
    import pandas as pd

    entry = ctx.deps.store.get(result_id)
    df = _get_ibis_table(entry).to_pandas()

    distributions: list[MetricDistribution] = []
    for metric_name, group in df.groupby("metric_name"):
        vals = group["metric_value"].dropna()
        if vals.empty:
            distributions.append(MetricDistribution(metric_name=str(metric_name), count=0))
            continue
        distributions.append(MetricDistribution(
            metric_name=str(metric_name),
            count=int(len(vals)),
            mean=float(vals.mean()),
            std=float(vals.std()) if len(vals) > 1 else 0.0,
            min_val=float(vals.min()),
            p25=float(vals.quantile(0.25)),
            median=float(vals.median()),
            p75=float(vals.quantile(0.75)),
            max_val=float(vals.max()),
        ))

    # Store a tidy stats table
    stats_rows = [d.model_dump() for d in distributions]
    stats_arrow = pa.Table.from_pylist(stats_rows) if stats_rows else pa.table({})
    new_id = ctx.deps.store.store(stats_arrow, None)

    return DistributionSummaryResult(result_id=new_id, distributions=distributions)


# ── period_over_period ───────────────────────────────────────────────────────

def period_over_period(
    ctx: RunContext[QueryDeps],
    result_id: str,
) -> PeriodOverPeriodResult:
    """Compute period-over-period delta and percentage change.

    For each group (metric_name + entity/slice/segment keys), rows are sorted
    by period_start_date and the change from the preceding period is computed.
    Rows with no preceding period (first in each group) get NaN delta/pct_change.

    Result store entry retains all STANDARD_COLUMNS plus delta and pct_change.

    Args:
        result_id: ID of the result store entry. Must contain time-series data
            (period_type != "all_time" or multiple period_start_date values).

    Returns:
        PeriodOverPeriodResult with a new result_id.
    """
    import pandas as pd

    entry = ctx.deps.store.get(result_id)
    df = _get_ibis_table(entry).to_pandas()

    # Group by all standard dimension columns present in this result
    group_keys = [
        c for c in ["metric_name", "entity_id", "slice_type", "slice_value",
                     "segment_name", "segment_value"]
        if c in df.columns
    ]

    # Guard: period-over-period requires ≥2 distinct periods per group.
    if "period_start_date" in df.columns:
        periods_per_group = (
            df.groupby(group_keys)["period_start_date"].nunique()
            if group_keys else df["period_start_date"].nunique()
        )
        max_periods = int(periods_per_group.max()) if hasattr(periods_per_group, "max") else int(periods_per_group)
    else:
        max_periods = 0

    if max_periods <= 1:
        return PeriodOverPeriodResult(
            result_id="",
            periods_found=max_periods,
            rows_computed=0,
            sample=[],
            error=(
                "period_over_period requires at least 2 distinct periods per group. "
                "The result contains only 1 unique period_start_date (or none). "
                "Re-run compute_metrics with a non-'all_time' period_type."
            ),
        )

    df = df.sort_values(group_keys + ["period_start_date"])
    df["prior_value"] = df.groupby(group_keys)["metric_value"].shift(1)
    df["delta"] = df["metric_value"] - df["prior_value"]
    df["pct_change"] = (df["delta"] / df["prior_value"].abs()) * 100
    df = df.drop(columns=["prior_value"])

    result_arrow = pa.Table.from_pandas(df, preserve_index=False)
    new_id = ctx.deps.store.store(result_arrow, None)

    return PeriodOverPeriodResult(
        result_id=new_id,
        periods_found=df["period_start_date"].nunique() if "period_start_date" in df.columns else 0,
        rows_computed=len(df),
        sample=_sample_arrow(result_arrow),
    )


# ── contribution_share ───────────────────────────────────────────────────────

def contribution_share(
    ctx: RunContext[QueryDeps],
    result_id: str,
) -> ContributionShareResult:
    """Compute each row's share of total metric_value and cumulative share.

    Share is computed as metric_value / sum(metric_value) within each metric_name.
    Rows are ordered by descending share. Cumulative share is computed within
    each metric_name group.

    Result store entry retains all STANDARD_COLUMNS plus share and cumulative_share.

    Args:
        result_id: ID of the result store entry.

    Returns:
        ContributionShareResult with a new result_id.
    """
    import pandas as pd

    entry = ctx.deps.store.get(result_id)
    df = _get_ibis_table(entry).to_pandas()

    # Guard: all-zero sums make shares undefined.
    metric_totals = df.groupby("metric_name")["metric_value"].sum()
    if (metric_totals == 0).all():
        return ContributionShareResult(
            result_id="",
            total_value=0.0,
            rows=len(df),
            sample=[],
            error=(
                "contribution_share: all metric_value entries sum to zero. "
                "Shares are undefined when the total is zero."
            ),
        )

    total_by_metric = df.groupby("metric_name")["metric_value"].transform("sum")
    df["share"] = df["metric_value"] / total_by_metric.replace(0, float("nan"))
    df = df.sort_values(["metric_name", "share"], ascending=[True, False])
    df["cumulative_share"] = df.groupby("metric_name")["share"].cumsum()

    result_arrow = pa.Table.from_pandas(df, preserve_index=False)
    new_id = ctx.deps.store.store(result_arrow, None)

    total_value = float(df["metric_value"].sum()) if not df["metric_value"].isna().all() else 0.0
    return ContributionShareResult(
        result_id=new_id,
        total_value=total_value,
        rows=len(df),
        sample=_sample_arrow(result_arrow),
    )
```

**Validation (SF-3 tests):**

```python
# tests/test_agent/test_query_tools.py (continued)

from aitaem.agent.query_tools import (
    rank_by_value, filter_by_threshold, distribution_summary,
    period_over_period, contribution_share,
)
from aitaem.agent.store import ResultStore
from aitaem.agent.query_types import QueryDeps
from datetime import date


def _make_deps_with_table(table: pa.Table):
    store = ResultStore()
    rid = store.store(table, None)
    mock_sc = MagicMock()
    mock_sc.metrics = {}
    deps = QueryDeps(spec_cache=mock_sc, connection_manager=MagicMock(), store=store)
    return deps, rid


def _multi_row_table():
    return pa.table({
        "metric_name": ["revenue"] * 5,
        "metric_value": [100.0, 300.0, 50.0, 200.0, 150.0],
        "period_type": ["all_time"] * 5,
        "period_start_date": [None] * 5,
        "period_end_date": [None] * 5,
        "entity_id": ["A", "B", "C", "D", "E"],
        "metric_format": [None] * 5,
        "slice_type": [None] * 5,
        "slice_value": [None] * 5,
        "segment_name": [None] * 5,
        "segment_value": [None] * 5,
    })


def test_rank_by_value_top2_descending():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=2, ascending=False)
    assert result.total_rows == 2
    assert result.result_id in deps.store.ids()
    ranked_arrow = deps.store.get_arrow(result.result_id)
    values = ranked_arrow.column("metric_value").to_pylist()
    assert values == sorted(values, reverse=True)


def test_rank_by_value_ascending():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=3, ascending=True)
    ranked_arrow = deps.store.get_arrow(result.result_id)
    values = ranked_arrow.column("metric_value").to_pylist()
    assert values == sorted(values)


def test_filter_by_threshold_greater_than():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = filter_by_threshold(ctx, result_id=rid, threshold=150.0, op=">")
    assert result.matching_rows == 2  # 300 and 200
    assert result.predicate == "metric_value > 150.0"
    filtered_arrow = deps.store.get_arrow(result.result_id)
    values = filtered_arrow.column("metric_value").to_pylist()
    assert all(v > 150.0 for v in values)


def test_filter_by_threshold_invalid_op():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    with pytest.raises(ValueError, match="op must be one of"):
        filter_by_threshold(ctx, result_id=rid, threshold=100.0, op="between")


def test_filter_by_threshold_custom_column():
    """Filter on pct_change after period_over_period — the chaining use case."""
    import datetime
    pop_table = pa.table({
        "metric_name": ["revenue", "revenue"],
        "metric_value": [100.0, 120.0],
        "period_type": ["monthly", "monthly"],
        "period_start_date": [datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)],
        "period_end_date": [datetime.date(2024, 1, 31), datetime.date(2024, 2, 29)],
        "entity_id": [None, None],
        "metric_format": [None, None],
        "slice_type": [None, None],
        "slice_value": [None, None],
        "segment_name": [None, None],
        "segment_value": [None, None],
        "delta": [None, 20.0],
        "pct_change": [None, 20.0],
    })
    deps, rid = _make_deps_with_table(pop_table)
    ctx = _make_ctx(deps)
    result = filter_by_threshold(ctx, result_id=rid, threshold=10.0, op=">", column="pct_change")
    assert result.error is None
    assert result.matching_rows == 1
    assert result.predicate == "pct_change > 10.0"


def test_filter_by_threshold_missing_column_returns_error():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = filter_by_threshold(ctx, result_id=rid, threshold=0.0, column="pct_change")
    assert result.error is not None
    assert result.result_id == ""
    assert "pct_change" in result.error


def test_distribution_summary_stats():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = distribution_summary(ctx, result_id=rid)
    assert len(result.distributions) == 1
    dist = result.distributions[0]
    assert dist.metric_name == "revenue"
    assert dist.count == 5
    assert dist.min_val == pytest.approx(50.0)
    assert dist.max_val == pytest.approx(300.0)
    assert dist.result_id in deps.store.ids()


def _time_series_table():
    import datetime
    return pa.table({
        "metric_name": ["revenue"] * 3,
        "metric_value": [100.0, 120.0, 150.0],
        "period_type": ["monthly"] * 3,
        "period_start_date": [
            datetime.date(2024, 1, 1),
            datetime.date(2024, 2, 1),
            datetime.date(2024, 3, 1),
        ],
        "period_end_date": [
            datetime.date(2024, 1, 31),
            datetime.date(2024, 2, 29),
            datetime.date(2024, 3, 31),
        ],
        "entity_id": [None] * 3,
        "metric_format": [None] * 3,
        "slice_type": [None] * 3,
        "slice_value": [None] * 3,
        "segment_name": [None] * 3,
        "segment_value": [None] * 3,
    })


def test_period_over_period_computes_delta():
    deps, rid = _make_deps_with_table(_time_series_table())
    ctx = _make_ctx(deps)
    result = period_over_period(ctx, result_id=rid)
    assert result.rows_computed == 3
    arrow = deps.store.get_arrow(result.result_id)
    assert "delta" in arrow.schema.names
    assert "pct_change" in arrow.schema.names
    deltas = arrow.column("delta").to_pylist()
    # First row has no prior → NaN/None; last row: 150 - 120 = 30
    non_null_deltas = [d for d in deltas if d is not None]
    assert 30.0 in non_null_deltas


def test_contribution_share_sums_to_one():
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = contribution_share(ctx, result_id=rid)
    arrow = deps.store.get_arrow(result.result_id)
    shares = [v for v in arrow.column("share").to_pylist() if v is not None]
    assert abs(sum(shares) - 1.0) < 1e-9
    assert result.total_rows == 5


def test_period_over_period_single_period_returns_error():
    """1-row all_time result has only 1 period — must return error, not NaN garbage."""
    single_row = pa.table({
        "metric_name": ["revenue"],
        "metric_value": [1000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": [None],
        "slice_type": [None],
        "slice_value": [None],
        "segment_name": [None],
        "segment_value": [None],
    })
    deps, rid = _make_deps_with_table(single_row)
    ctx = _make_ctx(deps)
    result = period_over_period(ctx, result_id=rid)
    assert result.error is not None
    assert result.result_id == ""
    assert "period" in result.error.lower()


def test_contribution_share_all_zero_returns_error():
    """All-zero metric values make shares undefined — must return error, not NaN."""
    zero_table = pa.table({
        "metric_name": ["revenue"] * 3,
        "metric_value": [0.0, 0.0, 0.0],
        "period_type": ["all_time"] * 3,
        "period_start_date": [None] * 3,
        "period_end_date": [None] * 3,
        "entity_id": ["A", "B", "C"],
        "metric_format": [None] * 3,
        "slice_type": [None] * 3,
        "slice_value": [None] * 3,
        "segment_name": [None] * 3,
        "segment_value": [None] * 3,
    })
    deps, rid = _make_deps_with_table(zero_table)
    ctx = _make_ctx(deps)
    result = contribution_share(ctx, result_id=rid)
    assert result.error is not None
    assert result.result_id == ""


def test_analysis_tool_result_id_not_source():
    """Analysis tools must create a NEW result_id, not reuse the source."""
    deps, rid = _make_deps_with_table(_multi_row_table())
    ctx = _make_ctx(deps)
    result = rank_by_value(ctx, result_id=rid, top_n=3)
    assert result.result_id != rid
```

---

### SF-4: System prompt builder (`aitaem/agent/query_bot.py`)

The spec catalog is built once at `_build_agent()` time — the `SpecCache` is frozen for the bot's lifetime. The prompt is a static string, avoiding per-run assembly overhead.

```python
from __future__ import annotations

from typing import Any

from aitaem.agent.query_types import QueryDeps, QueryOutput, QueryPayload


def _build_system_prompt(spec_cache: Any) -> str:
    """Build the QueryBot system prompt from a SpecCache instance.

    Includes: role, spec catalog, period types, Metric Precision Rule,
    format narration guidance, and QueryOutput filling instructions.
    Called once at _build_agent() time; result is a static string.
    """
    # ── Spec catalog ──────────────────────────────────────────────────────────
    metric_lines = []
    for name, spec in spec_cache.metrics.items():
        parts = [f"- {name}: {spec.description or '(no description)'}"]
        if spec.entities:
            parts.append(f"  Entities: {', '.join(spec.entities)}")
        if spec.format:
            parts.append(f"  Format: {spec.format}")
        metric_lines.append("\n".join(parts))

    slice_lines = [
        f"- {name}: {spec.description or '(no description)'}"
        for name, spec in spec_cache.slices.items()
    ]

    segment_lines = [
        f"- {name}: {spec.description or '(no description)'}"
        for name, spec in spec_cache.segments.items()
    ]

    catalog_section = "\n".join([
        "## Available Metrics",
        "\n".join(metric_lines) or "(none)",
        "",
        "## Available Slices",
        "\n".join(slice_lines) or "(none)",
        "",
        "## Available Segments",
        "\n".join(segment_lines) or "(none)",
    ])

    return f"""You are a data analysis assistant for an AITAEM metrics platform.
You answer user questions by querying a defined metric catalog using the tools provided.

{catalog_section}

## Period Types
Valid values for period_type: "all_time", "hourly", "daily", "weekly", "monthly", "yearly".
Non-"all_time" values require time_window to be specified.

## Metric Precision Rule (CRITICAL)
Only call compute_metrics with metric names that EXACTLY match names in the Available Metrics \
catalog above. If the user asks for a metric that is not in the catalog, or if there is no metric \
that precisely answers the question:
- Set status to "refused"
- Explain clearly which metric is missing
- Do NOT substitute an approximate metric

Example: if "active_revenue" is not in the catalog but "revenue" is, refuse — do not compute \
"revenue" as a substitute. The user must rely on exact definitions.

## Format Narration
When a metric has a format hint (e.g. "percentage", "currency:USD"):
- Narrate values in that format (e.g. "42.5%" not "0.425"; "$1,234" not "1234")
- The format hint appears in the compute_metrics result under format_hints

## Tool Usage
1. Call compute_metrics to get metric data. Note the result_id in the response.
2. Optionally call analysis tools (rank_by_value, filter_by_threshold, etc.) passing the result_id.
3. Each analysis tool produces a new result_id — chain them if needed.
4. Collect the result_ids you want the user to receive.

## Filling Your Final Response
After tool calls, produce a QueryOutput:
- status: "ok" if data was returned, "empty" if zero rows, "refused" if out of scope, \
"error" if a tool returned an error field.
- narrative: plain-language explanation referencing the numbers from tool summaries.
- result_ids: list of result_id strings from the tools, primary/most relevant first. \
Empty if status is not "ok".
- reason: brief note when status is "refused" or "error". Null otherwise.
"""
```

**Validation (SF-4 test):**

```python
# tests/test_agent/test_query_bot.py

from unittest.mock import MagicMock
from aitaem.agent.query_bot import _build_system_prompt


def _make_spec_cache(metric_names=("revenue", "ctr"), slice_names=("by_country",), segment_names=()):
    sc = MagicMock()
    sc.metrics = {
        "revenue": MagicMock(description="Total revenue", entities=["user_id"], format="currency:USD"),
        "ctr": MagicMock(description="Click-through rate", entities=None, format="percentage"),
    }
    sc.slices = {"by_country": MagicMock(description="By country")}
    sc.segments = {}
    return sc


def test_system_prompt_contains_metric_names():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "revenue" in prompt
    assert "ctr" in prompt


def test_system_prompt_contains_metric_precision_rule():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "Metric Precision Rule" in prompt
    assert "refused" in prompt


def test_system_prompt_contains_slice_names():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "by_country" in prompt


def test_system_prompt_contains_format_hints():
    prompt = _build_system_prompt(_make_spec_cache())
    assert "currency:USD" in prompt
    assert "percentage" in prompt


def test_system_prompt_empty_catalog():
    sc = MagicMock()
    sc.metrics = {}
    sc.slices = {}
    sc.segments = {}
    prompt = _build_system_prompt(sc)
    assert "(none)" in prompt
```

---

### SF-5: `QueryBot` class + `_build_agent()` (`aitaem/agent/query_bot.py`)

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.capabilities import ReinjectSystemPrompt

from aitaem import MetricCompute, SpecCache, ConnectionManager
from aitaem.agent.base import Bot
from aitaem.agent.response import BotResponse
from aitaem.agent.query_types import QueryDeps, QueryOutput, QueryPayload
from aitaem.agent.query_tools import (
    compute_metrics,
    rank_by_value,
    filter_by_threshold,
    distribution_summary,
    period_over_period,
    contribution_share,
)


class QueryResponse(BotResponse[QueryPayload]):
    """Concrete response type for QueryBot — narrows BotResponse's generic payload."""


class QueryBot(Bot):
    """Convenience bot for answering natural-language questions against a metric catalog.

    Tools create a MetricCompute instance per call from the held spec_cache and
    connection_manager. Artifacts are written to the bot's ResultStore; callers
    dereference via get_result(result_id).

    Construction:
        bot = QueryBot(
            model="anthropic:claude-sonnet-4-6",
            spec_cache=my_spec_cache,
            connection_manager=my_connection_manager,
        )
        response = await bot.chat("What was Q4 revenue by region?")

    Multi-provider:
        Use model strings supported by pydantic-ai, e.g. "openai:gpt-4o".
        For testing, pass a FunctionModel or TestModel instance directly.
    """

    def __init__(
        self,
        *,
        model: str | Any,
        spec_cache: SpecCache,
        connection_manager: ConnectionManager,
        tools: list[Any] | None = None,
    ) -> None:
        # Set bot-specific resources BEFORE super().__init__() — _build_agent()
        # is called inside super().__init__() and needs these attributes.
        self._spec_cache = spec_cache
        self._connection_manager = connection_manager
        super().__init__(model=model, tools=tools)
        # Retained across turns for trace correlation; None until the first run completes.
        self._conversation_id: str | None = None

    def _build_agent(self) -> Agent:
        from pydantic_ai import Agent
        from pydantic_ai.toolsets import FunctionToolset

        toolset = FunctionToolset()
        toolset.add_function(compute_metrics)
        toolset.add_function(rank_by_value)
        toolset.add_function(filter_by_threshold)
        toolset.add_function(distribution_summary)
        toolset.add_function(period_over_period)
        toolset.add_function(contribution_share)

        # Attach any extra tools passed at construction (EP1 / Phase 5 setup)
        # self._extra_tools is set by Bot.__init__ before _build_agent() is called
        # (Phase 5 will add proper add_tool() support; for now, no-op)

        system_prompt = _build_system_prompt(self._spec_cache)

        return Agent(
            model=self._model,
            deps_type=QueryDeps,
            output_type=QueryOutput,
            toolsets=[toolset],
            instructions=system_prompt,
            capabilities=[ReinjectSystemPrompt(replace_existing=True)],
        )
```

**Validation (SF-5 tests):**

```python
# tests/test_agent/test_query_bot.py (continued)

from aitaem.agent.query_bot import QueryBot, QueryResponse
from aitaem.agent.query_types import QueryPayload
from unittest.mock import MagicMock, patch


def _make_bot(model="anthropic:claude-sonnet-4-6"):
    sc = _make_spec_cache()
    cm = MagicMock()
    return QueryBot(model=model, spec_cache=sc, connection_manager=cm)


def test_query_bot_has_result_store():
    bot = _make_bot()
    assert bot.store is not None


def test_query_bot_is_concrete():
    # QueryBot is not abstract — can be instantiated
    bot = _make_bot()
    assert isinstance(bot, QueryBot)


def test_query_response_is_bot_response_subtype():
    from aitaem.agent.response import BotResponse
    assert issubclass(QueryResponse, BotResponse)
```

---

### SF-6: `chat()`, `ask()`, and `_assemble_payload()` (`aitaem/agent/query_bot.py`)

```python
# (continued in QueryBot class)
# Prerequisite: add `error: str | None = None` to RunTrace in aitaem/agent/trace.py.
# The error field is populated on the fallback trace when _agent.run() raises.

    async def chat(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> QueryResponse:
        """Send a message in multi-turn mode. Accumulates history on the bot.

        Always returns a QueryResponse — exceptions from the agent run are caught
        and surfaced as status=error rather than propagated raw.
        """
        from datetime import datetime, timezone
        from aitaem.agent.trace import assemble_trace

        run_start = datetime.now(timezone.utc)
        deps = QueryDeps(
            spec_cache=self._spec_cache,
            connection_manager=self._connection_manager,
            store=self._store,
        )
        try:
            result = await self._agent.run(
                message,
                message_history=self._message_history,
                deps=deps,
            )
            self._message_history = result.all_messages()
            output: QueryOutput = result.output
            trace = assemble_trace(result, run_start)
            self._conversation_id = trace.conversation_id
            payload = QueryBot._assemble_payload(output, trace)
            return QueryResponse(
                status=output.status,
                narrative=output.narrative,
                trace=trace,
                reason=output.reason,
                payload=payload,
            )
        except Exception as exc:
            return QueryBot._error_response(exc, run_start, self._conversation_id)

    async def ask(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> QueryResponse:
        """Send a single-turn message. Does NOT accumulate history.

        Always returns a QueryResponse — exceptions from the agent run are caught
        and surfaced as status=error rather than propagated raw.
        """
        from datetime import datetime, timezone
        from aitaem.agent.trace import assemble_trace

        run_start = datetime.now(timezone.utc)
        deps = QueryDeps(
            spec_cache=self._spec_cache,
            connection_manager=self._connection_manager,
            store=self._store,
        )
        try:
            result = await self._agent.run(message, deps=deps)
            output: QueryOutput = result.output
            trace = assemble_trace(result, run_start)
            self._conversation_id = trace.conversation_id
            payload = QueryBot._assemble_payload(output, trace)
            return QueryResponse(
                status=output.status,
                narrative=output.narrative,
                trace=trace,
                reason=output.reason,
                payload=payload,
            )
        except Exception as exc:
            return QueryBot._error_response(exc, run_start, self._conversation_id)

    @staticmethod
    def _error_response(
        exc: Exception, run_start: Any, conversation_id: str | None
    ) -> QueryResponse:
        """Build a status=error QueryResponse when _agent.run() raises.

        conversation_id is the bot's retained ID from prior successful turns, so
        error traces correlate with the rest of the conversation. On the very first
        turn (no prior success), it is None and a fresh UUID is used.
        """
        import uuid
        from aitaem.agent.trace import RunTrace, Usage

        trace = RunTrace(
            run_id=str(uuid.uuid4()),
            conversation_id=conversation_id or str(uuid.uuid4()),
            timestamp=run_start,
            tool_calls=[],
            usage=Usage(),
            error=f"{type(exc).__name__}: {exc}",
        )
        return QueryResponse(
            status=Status.error,
            narrative="The request could not be completed due to an unexpected error.",
            trace=trace,
            reason=str(exc),
            payload=QueryPayload(
                result_ids=[], primary_result_id=None,
                metrics_used=[], slices_used=[], segment_used=None,
                time_window=None, period_type="all_time", by_entity=None,
            ),
        )

    @staticmethod
    def _assemble_payload(output: QueryOutput, trace: Any) -> QueryPayload:
        """Assemble QueryPayload from the LLM's QueryOutput and the turn trace.

        Reads payload_summary from each tool's llm_summary (JSON-serialized
        ToolResult). Tool-agnostic: no per-tool field access needed.

        Aggregation rules across multiple tool calls:
          list fields  — union with deduplication, order of first appearance
          scalar fields — first-write wins (first call that sets a field governs)
        """
        import json

        metrics_used: list[str] = []
        slices_used: list[str] = []
        seen_metrics: set[str] = set()
        seen_slices: set[str] = set()
        segment_used: str | None = None
        time_window: tuple[str, str] | None = None
        period_type: str | None = None
        by_entity: str | None = None
        format_hints: dict[str, str] = {}

        for tc in trace.tool_calls:
            if not tc.llm_summary:
                continue
            try:
                summary = json.loads(tc.llm_summary)
            except (ValueError, TypeError):
                continue
            ps = summary.get("payload_summary")
            if not ps:
                continue
            # list fields: union with deduplication, order-preserving
            for m in ps.get("metrics_used") or []:
                if m not in seen_metrics:
                    seen_metrics.add(m)
                    metrics_used.append(m)
            for s in ps.get("slices_used") or []:
                if s not in seen_slices:
                    seen_slices.add(s)
                    slices_used.append(s)
            # scalar fields: first-write wins
            if segment_used is None and ps.get("segment_used"):
                segment_used = ps["segment_used"]
            if time_window is None and ps.get("time_window"):
                tw = ps["time_window"]
                time_window = (tw[0], tw[1]) if isinstance(tw, (list, tuple)) and len(tw) == 2 else None
            if period_type is None and ps.get("period_type"):
                period_type = ps["period_type"]
            if by_entity is None and ps.get("by_entity"):
                by_entity = ps["by_entity"]
            # dict field: union, first-write wins per metric name
            for metric, fmt in (ps.get("format_hints") or {}).items():
                if metric not in format_hints:
                    format_hints[metric] = fmt

        return QueryPayload(
            result_ids=output.result_ids,
            primary_result_id=output.result_ids[0] if output.result_ids else None,
            metrics_used=metrics_used,
            slices_used=slices_used,
            segment_used=segment_used,
            time_window=time_window,
            period_type=period_type or "all_time",
            by_entity=by_entity,
            format_hints=format_hints,
        )
```

**Validation (SF-6 tests — `_assemble_payload` unit tests):**

```python
# tests/test_agent/test_query_bot.py (continued)

from datetime import datetime, timezone
from aitaem.agent.query_bot import QueryBot
from aitaem.agent.query_types import QueryOutput
from aitaem.agent.trace import RunTrace, ToolCall, Usage, Status


def _minimal_trace(tool_calls=None):
    return RunTrace(
        run_id="r",
        conversation_id="c",
        timestamp=datetime.now(timezone.utc),
        tool_calls=tool_calls or [],
        usage=Usage(),
    )


import json as _json


def _tc_with_payload(tc_id: str, name: str, payload_summary: dict | None = None) -> ToolCall:
    """Build a ToolCall whose llm_summary is a JSON-serialized ToolResult."""
    body: dict = {}
    if payload_summary is not None:
        body["payload_summary"] = payload_summary
    return ToolCall(tool_call_id=tc_id, name=name, args={}, llm_summary=_json.dumps(body))


def test_assemble_payload_ok_with_result_ids():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1", "r2"])
    trace = _minimal_trace()
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.result_ids == ["r1", "r2"]
    assert payload.primary_result_id == "r1"


def test_assemble_payload_refused_empty_result_ids():
    output = QueryOutput(status=Status.refused, narrative="N/A.", reason="No match.")
    trace = _minimal_trace()
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.result_ids == []
    assert payload.primary_result_id is None


def test_assemble_payload_extracts_from_payload_summary():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "slices_used": ["by_country"],
            "period_type": "monthly",
            "time_window": ["2024-01-01", "2024-03-31"],
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == ["revenue"]
    assert payload.slices_used == ["by_country"]
    assert payload.period_type == "monthly"
    assert payload.time_window == ("2024-01-01", "2024-03-31")


def test_assemble_payload_list_fields_union_dedup():
    """Two compute_metrics calls — metrics_used is a deduped union; scalars use first-write wins."""
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue", "ctr"],
            "period_type": "monthly",
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],   # duplicate — must be dropped
            "period_type": "weekly",       # scalar conflict — first-write wins
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == ["revenue", "ctr"]
    assert payload.metrics_used.count("revenue") == 1
    assert payload.period_type == "monthly"            # first-write wins


def test_assemble_payload_scalar_first_write_wins():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "period_type": "monthly",
            "time_window": ["2024-01-01", "2024-03-31"],
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["refund_rate"],
            "period_type": "weekly",      # ignored — period_type already set
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.period_type == "monthly"
    assert set(payload.metrics_used) == {"revenue", "refund_rate"}


def test_assemble_payload_propagates_format_hints():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue", "ctr"],
            "format_hints": {"revenue": "currency:USD", "ctr": "percentage"},
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.format_hints == {"revenue": "currency:USD", "ctr": "percentage"}


def test_assemble_payload_format_hints_first_write_wins():
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r1"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "format_hints": {"revenue": "currency:USD"},
        }),
        _tc_with_payload("tc2", "compute_metrics", payload_summary={
            "metrics_used": ["revenue"],
            "format_hints": {"revenue": "currency:EUR"},  # ignored — first-write wins
        }),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.format_hints["revenue"] == "currency:USD"


def test_assemble_payload_ignores_tools_without_payload_summary():
    """Analysis tools with no payload_summary don't affect QueryPayload metadata."""
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=["r2"])
    trace = _minimal_trace(tool_calls=[
        _tc_with_payload("tc1", "rank_by_value", payload_summary=None),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == []


def test_assemble_payload_ignores_non_json_llm_summary():
    """Plain-string llm_summary (not JSON) is skipped gracefully."""
    output = QueryOutput(status=Status.ok, narrative="Done.", result_ids=[])
    trace = _minimal_trace(tool_calls=[
        ToolCall(tool_call_id="tc1", name="compute_metrics",
                 args={}, llm_summary="Computed 1 metric."),
    ])
    payload = QueryBot._assemble_payload(output, trace)
    assert payload.metrics_used == []
```

---

### SF-7: Update `aitaem/agent/__init__.py`

```python
# aitaem/agent/__init__.py  (full replacement)
from aitaem.agent.response import BotResponse, Status
from aitaem.agent.store import ResultEntry, ResultStore
from aitaem.agent.trace import RunTrace, ToolCall, Usage
from aitaem.agent.base import Bot
from aitaem.agent.query_bot import QueryBot, QueryResponse
from aitaem.agent.query_types import QueryPayload

__all__ = [
    # Phase 1 primitives
    "Bot",
    "BotResponse",
    "Status",
    "RunTrace",
    "ToolCall",
    "Usage",
    "ResultEntry",
    "ResultStore",
    # Phase 2 — QueryBot
    "QueryBot",
    "QueryResponse",
    "QueryPayload",
]
```

**Validation:**

```python
def test_public_exports_include_query_bot():
    from aitaem.agent import QueryBot, QueryResponse, QueryPayload
    assert all(x is not None for x in [QueryBot, QueryResponse, QueryPayload])
```

---

### SF-8 & SF-9: FunctionModel integration tests (`tests/test_agent/test_query_bot.py`)

These tests verify the full `QueryBot.chat()` / `ask()` pipeline without a real LLM. The `FunctionModel` drives deterministic tool-calling sequences.

```python
# tests/test_agent/test_query_bot.py (continued)

import asyncio
import json
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, AgentInfo

from aitaem.agent.query_bot import QueryBot, QueryResponse
from aitaem.agent.query_types import QueryOutput
from aitaem.agent.trace import Status


# ── Shared fixtures ──────────────────────────────────────────────────────────

def _revenue_mock_mc():
    """Mock MetricCompute that returns a simple 1-row revenue table."""
    mc = MagicMock()
    table = pa.table({
        "metric_name": ["revenue"],
        "metric_value": [1000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": [None],
        "slice_type": [None],
        "slice_value": [None],
        "segment_name": [None],
        "segment_value": [None],
    })
    mock_ibis = MagicMock()
    mock_ibis.to_pyarrow.return_value = table
    mc.compute.return_value = mock_ibis
    return mc


def _make_bot_with_model(model):
    """Create a QueryBot. MetricCompute is patched at the tool call site in tests."""
    sc = _make_spec_cache()
    cm = MagicMock()
    return QueryBot(model=model, spec_cache=sc, connection_manager=cm)


# ── FunctionModel helpers ────────────────────────────────────────────────────

def _make_compute_then_answer_model(metric: str = "revenue"):
    """FunctionModel: calls compute_metrics, then produces QueryOutput."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        tool_returns = [
            p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not tool_returns:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="compute_metrics",
                args=json.dumps({"metrics": [metric], "period_type": "all_time"}),
                tool_call_id="tc-1",
            )])
        else:
            tool_data = json.loads(tool_returns[0].content)
            result_id = tool_data.get("result_id", "")
            output = QueryOutput(
                status=Status.ok,
                narrative=f"{metric.capitalize()} computed: {tool_data.get('row_count', 0)} rows.",
                result_ids=[result_id] if result_id else [],
            )
            return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _make_refused_model():
    """FunctionModel: immediately refuses without calling any tool."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        output = QueryOutput(
            status=Status.refused,
            narrative="That metric is not in the catalog.",
            result_ids=[],
            reason="No exact match for 'sales_velocity'.",
        )
        return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


# ── Autouse fixture: patch MetricCompute for all integration tests ────────────
# All tests in this module that trigger compute_metrics need MetricCompute patched
# at the tool module level. This fixture applies automatically to every test.

@pytest.fixture(autouse=True)
def patch_metric_compute():
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_revenue_mock_mc()):
        yield


# ── SF-8: chat() and ask() tests ─────────────────────────────────────────────

def test_chat_returns_query_response():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert isinstance(response, QueryResponse)


def test_chat_status_ok_on_successful_compute():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert response.status == Status.ok


def test_chat_payload_has_result_id():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert len(response.payload.result_ids) == 1
    rid = response.payload.primary_result_id
    assert rid is not None
    # Verify we can retrieve the actual data
    entry = bot.get_result(rid)
    assert entry.arrow is not None


def test_chat_refused_status():
    bot = _make_bot_with_model(_make_refused_model())
    response = asyncio.run(bot.chat("What was sales velocity?"))
    assert response.status == Status.refused
    assert response.reason is not None
    assert response.payload.result_ids == []


def test_ask_does_not_accumulate_history():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    assert bot._message_history == []
    asyncio.run(bot.ask("What was revenue?"))
    assert bot._message_history == []    # ask() must not mutate history


def test_chat_accumulates_history():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    assert bot._message_history == []
    asyncio.run(bot.chat("What was revenue?"))
    assert len(bot._message_history) > 0


def test_chat_multi_turn_history_grows():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    asyncio.run(bot.chat("First question."))
    after_turn_1 = len(bot._message_history)
    asyncio.run(bot.chat("Second question."))
    after_turn_2 = len(bot._message_history)
    assert after_turn_2 > after_turn_1


def test_trace_contains_tool_call():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert len(response.trace.tool_calls) >= 1
    assert response.trace.tool_calls[0].name == "compute_metrics"


def test_trace_usage_populated():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    # FunctionModel does not count tokens, but Usage object must be present
    assert response.trace.usage is not None
```

---

### SF-10: History round-trip tests for `QueryBot`

```python
# tests/test_agent/test_query_bot.py (continued)

def test_dump_history_captures_result_store():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id

    bundle = bot.dump_history()
    assert rid in bundle["artifacts"]


def test_load_history_restores_result():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id
    original_arrow = bot.get_result(rid).arrow

    bundle = bot.dump_history()

    restored = QueryBot.load_history(
        bundle,
        model=_make_compute_then_answer_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
    )

    entry = restored.get_result(rid)
    assert entry.arrow.equals(original_arrow)
    assert entry.ibis_ref is None   # ibis refs are not serialized


def test_load_history_restores_message_history():
    bot = _make_bot_with_model(_make_compute_then_answer_model())
    asyncio.run(bot.chat("What was revenue?"))
    n_messages = len(bot._message_history)

    bundle = bot.dump_history()

    restored = QueryBot.load_history(
        bundle,
        model=_make_compute_then_answer_model(),
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
    )

    assert len(restored._message_history) == n_messages
```

---

### SF-10b: Smoke test (`tests/test_agent/test_query_bot_smoke.py`)

One end-to-end `chat()` turn with a real LLM and a mocked `MetricCompute`. Skipped automatically when `ANTHROPIC_API_KEY` is unset; the regular CI job never sets that secret, so the test is collected but skipped there. A dedicated CI job sets the secret and runs this file explicitly.

Uses `claude-haiku-4-5-20251001` — cheapest model, enough to validate that the LLM calls `compute_metrics` correctly, reads the result, and produces a well-formed `QueryOutput`.

```python
# tests/test_agent/test_query_bot_smoke.py
"""
Smoke test: one real LLM chat() turn against a mocked MetricCompute.
Skipped automatically when ANTHROPIC_API_KEY is unset.

Regular CI  : collected, skipped (no secret).
Dedicated CI: pytest tests/test_agent/test_query_bot_smoke.py -v
"""
import asyncio
import os
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — smoke test requires a real LLM",
)

from aitaem.agent.query_bot import QueryBot
from aitaem.agent.trace import Status


def _smoke_spec_cache():
    sc = MagicMock()
    rev = MagicMock()
    rev.description = "Total revenue in USD"
    rev.format = "currency:USD"
    rev.entities = None
    sc.metrics = {"revenue": rev}
    sc.slices = {}
    sc.segments = {}
    return sc


def _smoke_mc():
    mc = MagicMock()
    table = pa.table({
        "metric_name": ["revenue"],
        "metric_value": [125_000.0],
        "period_type": ["all_time"],
        "period_start_date": [None],
        "period_end_date": [None],
        "entity_id": [None],
        "metric_format": ["currency:USD"],
        "slice_type": ["none"],
        "slice_value": ["all"],
        "segment_name": ["none"],
        "segment_value": ["all"],
    })
    mock_ibis = MagicMock()
    mock_ibis.to_pyarrow.return_value = table
    mc.compute.return_value = mock_ibis
    return mc


def test_query_bot_smoke_single_turn():
    """One real-LLM chat() turn. MetricCompute is mocked; no database required."""
    bot = QueryBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=_smoke_spec_cache(),
        connection_manager=MagicMock(),
    )
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_smoke_mc()):
        response = asyncio.run(bot.chat("What was total revenue?"))

    assert response.status == Status.ok, (
        f"Expected status=ok, got {response.status!r}. reason={response.reason!r}"
    )
    rid = response.payload.primary_result_id
    assert rid is not None, "primary_result_id must be set on ok response"

    entry = bot.get_result(rid)
    assert entry.arrow is not None
    assert entry.arrow.num_rows == 1

    assert "revenue" in response.payload.metrics_used
    assert response.payload.format_hints.get("revenue") == "currency:USD", (
        f"format_hints missing or wrong: {response.payload.format_hints}"
    )
```

---

### SF-11: Architecture doc update

Add the following to the appendix of `plans/agent_module/08-implementation-order.md` (the existing OQ-A1 section is already present):

```markdown
### OQ-A2: `compute_metrics` segment join-key override

**Problem:** `MetricCompute.compute()` accepts `segments` as either `str` (segment name,
uses spec's default join key) or `dict[str, str]` (name → custom join key). The Phase 2
`compute_metrics` tool only exposes the string form.

**Impact:** Users who need to override the join key via the LLM interface cannot do so
with the default QueryBot. They would need to call `MetricCompute.compute()` directly
or build a custom tool.

**Decision trigger:** When a user reports needing non-default join key selection
through the LLM interface (likely rare; most specs have a single natural join key).

**Implementation path when triggered:** Add an optional `segment_join_key: str | None`
parameter to the `compute_metrics` tool. Construct `segments={segment: segment_join_key}`
when both are provided.
```

---

## Files Changed Summary

| File | Change |
|---|---|
| `aitaem/agent/query_types.py` | New — `QueryDeps`, `QueryOutput`, `QueryPayload`, all tool result models |
| `aitaem/agent/query_tools.py` | New — `compute_metrics` + 5 analysis tools + `_get_ibis_table` helper |
| `aitaem/agent/query_bot.py` | New — `QueryBot`, `QueryResponse`, `_build_system_prompt`, `_assemble_payload` |
| `aitaem/agent/__init__.py` | Modified — add `QueryBot`, `QueryResponse`, `QueryPayload` exports |
| `tests/test_agent/test_query_tools.py` | New — SF-2 and SF-3 tool unit tests |
| `tests/test_agent/test_query_bot.py` | New — SF-4 through SF-10 bot-level and history tests |
| `tests/test_agent/test_query_bot_smoke.py` | New — SF-10b real-LLM smoke test; skipped without `ANTHROPIC_API_KEY` |
| `plans/agent_module/08-implementation-order.md` | Modified — add OQ-A2 to appendix |
| `pyproject.toml` | Modified — add `pytest-asyncio>=0.23.0` to `[dev]` |
| `aitaem/agent/trace.py` | Modified — add `error: str | None = None` field to `RunTrace` |

No changes to any existing Phase 1 agent files (beyond `__init__.py`, `trace.py`). No changes to `aitaem` core.

---

## Testing Strategy

1. **Before starting:** Run `python -m pytest tests/test_agent/ --cov=aitaem/agent` to confirm Phase 1 baseline is green.

2. **After SF-1 (contract models):** `python -m pytest tests/test_agent/test_query_tools.py -k "query_output or compute_metrics_result or metric_distribution"` — model construction and validation.

3. **After SF-2 (`compute_metrics`):** `python -m pytest tests/test_agent/test_query_tools.py -k "compute_metrics"` — storage, error handling, format hints, sample capping.

4. **After SF-3 (analysis tools):** `python -m pytest tests/test_agent/test_query_tools.py -k "rank or filter or distribution or period or contribution"` — all 5 tools.

5. **After SF-4 (system prompt):** `python -m pytest tests/test_agent/test_query_bot.py -k "system_prompt"` — catalog presence, Metric Precision Rule.

6. **After SF-5 (`QueryBot._build_agent`):** `python -m pytest tests/test_agent/test_query_bot.py -k "query_bot_has or query_bot_is or query_response"` — construction and type tests.

7. **After SF-6 (`chat` / `ask` / `_assemble_payload`):** `python -m pytest tests/test_agent/test_query_bot.py -k "assemble_payload"` — payload assembly unit tests.

8. **After SF-7 (`__init__.py`):** `python -m pytest tests/test_agent/test_primitives.py::test_public_exports_include_query_bot`.

9. **After SF-8/9 (FunctionModel integration tests):** `python -m pytest tests/test_agent/test_query_bot.py -k "chat or ask"`.

10. **After SF-10 (history tests):** `python -m pytest tests/test_agent/test_query_bot.py -k "history"`.

11. **After SF-10b (smoke test — requires `ANTHROPIC_API_KEY`):**
    ```bash
    ANTHROPIC_API_KEY=sk-... pytest tests/test_agent/test_query_bot_smoke.py -v
    ```
    Run manually or in the dedicated CI job. Without the key the test is skipped, not failed.

12. **Full Phase 2 completion:**
    ```bash
    uv pip install -e ".[agent-anthropic,dev]"
    python -m pytest tests/test_agent/ --cov=aitaem/agent --cov-report=term-missing
    python scripts/check_import_graph.py
    python -m pytest tests/ --ignore=tests/test_agent/ --cov=aitaem   # core must stay green
    ruff check aitaem/agent/
    mypy aitaem/agent/
    ```

13. **Commit** once all tests and checks pass.

---

## Open Questions Carried Forward

| ID | Question | Where Tracked |
|---|---|---|
| OQ-A1 | Context-window management via `ProcessHistory` | `08-implementation-order.md` appendix (Phase 1) |
| OQ-A2 | `compute_metrics` segment join-key override | `08-implementation-order.md` appendix (SF-11) |

---

## Success Criteria

Phase 2 is complete when:

- [ ] `from aitaem.agent import QueryBot, QueryResponse, QueryPayload` works
- [ ] `QueryBot(model=..., spec_cache=..., connection_manager=...)` constructs without errors
- [ ] `await bot.chat("...")` returns a `QueryResponse` with `status`, `narrative`, `trace`, `payload`
- [ ] `await bot.ask("...")` returns the same shape but does NOT modify `bot._message_history`
- [ ] `bot.get_result(payload.primary_result_id)` returns the Arrow table from `compute_metrics`
- [ ] All 5 analysis tools write new result store entries and return their respective result models
- [ ] `bot.dump_history()` + `QueryBot.load_history(bundle, ...)` restores result store and message history
- [ ] `_build_system_prompt()` includes every metric/slice/segment name from the SpecCache
- [ ] `python -m pytest tests/test_agent/ --cov=aitaem/agent` passes with ≥ 90% coverage
- [ ] `python -m pytest tests/ --ignore=tests/test_agent/` still passes (no core regressions)
- [ ] `python scripts/check_import_graph.py` exits 0
- [ ] `ruff check aitaem/agent/` and `mypy aitaem/agent/` pass clean
- [ ] Smoke test passes against a real LLM: `ANTHROPIC_API_KEY=... pytest tests/test_agent/test_query_bot_smoke.py -v` — `status=ok`, `primary_result_id` set, Arrow table retrievable, `format_hints` populated

Phase 3 (DefinitionBot) is unblocked once these criteria are met.
