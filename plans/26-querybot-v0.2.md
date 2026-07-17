# Plan 26 — QueryBot v0.2: Intent-Gated Resolution

**Prerequisite:** Plan 24 (Phase 2 QueryBot) is **fully implemented and passing**.

**Design doc:** [`plans/agent_module/09-querybot-v0.2-design.md`](agent_module/09-querybot-v0.2-design.md)

**What changes:** The resolution flow before `compute_metrics` is replaced with a two-tool gate (`record_intent` → `resolve_intent`). `compute_metrics` now accepts a single `spec_token` instead of raw parameters. Analysis tools are **unchanged**.

**What does NOT change:** `rank_by_value`, `filter_by_threshold`, `distribution_summary`, `period_over_period`, `contribution_share`, `ResultStore`, `BotResponse`, `Bot`, `RunTrace`, history I/O, `QueryOutput.result_ids` semantics, `QueryPayload`.

---

## Breaking Changes from Phase 2

| Change | Impact |
|---|---|
| `compute_metrics(ctx, spec_token)` replaces `compute_metrics(ctx, metrics, slices, ...)` | All tests that mock or call `compute_metrics` must be rewritten |
| `QueryDeps` gains two new fields (`intents`, `spec_registry`) | Every `QueryDeps(...)` instantiation in tests must add the new fields |
| System prompt completely rewritten | All `_build_system_prompt` tests must be updated |
| FunctionModel tests must use 3-step flow | `test_query_bot.py` integration tests must be rewritten |

---

## NearMiss Semantics (v0 SpecResolver)

These are the `why_not` reasons a `resolve_intent` call returns instead of an exact match:

| Reason | When it fires |
|---|---|
| `unknown_metric` | Proposed metric name is not in `spec_cache.metrics` |
| `unknown_slice` | Proposed slice name is not in `spec_cache.slices` AND not in `spec_cache.segments` |
| `unknown_segment` | Proposed segment name is not in `spec_cache.segments` AND not in `spec_cache.slices` |
| `wrong_dimension_kind` | Proposed name exists in the catalog but in the wrong category — e.g. a segment spec name was passed in `slices`, or a slice spec name was passed as `segment` |
| `unsupported_by_entity` | Proposed metric exists but `by_entity` in the intent is not in `spec.entities` (or `spec.entities` is None/empty) |
| `unsupported_period_type` | Proposed metric exists but `period_type != "all_time"` and `spec.timestamp_col` is empty (future-proofing; cannot currently fire since all specs require `timestamp_col`) |
| `scope_mismatch` | **Not emitted by v0 SpecResolver.** MetricSpec has no scope flag, so the resolver cannot tell whether a proposed metric is inherently scoped (e.g. `ctr_conversion_ads`) or a plain overall metric. Reserved for a future version when MetricSpec carries explicit scope metadata. |

---

## Sub-Feature Order

Implement and test each SF before moving to the next. SFs 3 and 4 depend on SF-2; SF-5 depends on SF-4; SF-6 depends on SFs 3–5.

---

### SF-1: New Types (`aitaem/agent/query_types.py`)

Add the following types. Do not modify or remove any existing types in this file.

#### 1a. `MetricIntent` (LLM-produced, stored in `QueryDeps.intents`)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Any

@dataclass
class MetricIntent:
    """Structured interpretation of one metric the user is asking about.

    Produced by record_intent and stored in QueryDeps.intents.
    One intent per metric; multi-metric questions produce multiple intents.
    """
    metric_concept: str                          # free-text LLM interpretation
    scope: Literal["overall", "subset"]
    subset_description: str | None = None        # prose description of the subset
    slice_type: str | None = None                # proposed slice spec name (breakdown)
    slice_value: str | None = None               # specific filter value, e.g. "US"
    segment_name: str | None = None              # proposed segment spec name
    segment_value: str | None = None             # specific segment filter value
    period_type: str = "all_time"
    time_window: tuple[str, str] | None = None   # (start_iso, end_iso)
    by_entity: str | None = None
```

#### 1b. `ResolvedSpec` (server-side; LLM never sees this)

```python
@dataclass
class ResolvedSpec:
    """Validated compute parameters keyed by spec_token in QueryDeps.spec_registry.

    Constructed by resolve_intent when SpecResolver confirms an exact match.
    Consumed by compute_metrics(spec_token) to reconstruct MetricCompute arguments.
    """
    metric_name: str
    slice_specs: list[str]          # validated slice spec names
    segment_spec: str | None        # validated segment spec name
    period_type: str
    time_window: tuple[str, str] | None
    by_entity: str | None
    intent_slice_value: str | None  # from MetricIntent; for trace only
    intent_segment_value: str | None
```

#### 1c. `ExactMatch`, `NearMiss`, `SpecMatchResult` (returned by `resolve_intent`)

```python
from pydantic import BaseModel

class ExactMatch(BaseModel):
    """Minted only when SpecResolver confirms a valid proposal."""
    spec_token: str
    metric_name: str
    slices: list[str]
    segment: str | None

class NearMiss(BaseModel):
    name: str
    why_not: Literal[
        "unknown_metric",
        "scope_mismatch", "wrong_dimension_kind",
        "unknown_slice", "unknown_segment",
        "unsupported_by_entity", "unsupported_period_type",
    ]
    suggestions: list[str] = []
    """Catalog names close to `name`. Non-empty only when why_not='unknown_metric'.
    Populated via difflib.get_close_matches (cutoff=0.75) for typo correction.
    Empty for all other why_not reasons."""

class SpecMatchResult(BaseModel):
    """Returned to the LLM by resolve_intent.

    If exact_match is not None: the LLM proceeds to compute_metrics(spec_token).
    If exact_match is None: the LLM must produce status=refused and cite near_misses.
    """
    exact_match: ExactMatch | None
    near_misses: list[NearMiss]
```

#### 1d. Tool result types for the two new tools

```python
class RecordIntentResult(BaseModel):
    """Returned by record_intent. The intent_id is used in the resolve_intent call."""
    intent_id: int

class ResolveIntentResult(BaseModel):
    """Returned by resolve_intent. Wraps SpecMatchResult for the LLM."""
    exact_match: ExactMatch | None
    near_misses: list[NearMiss]
```

**Note:** `ResolveIntentResult` mirrors `SpecMatchResult` — they have identical fields. `SpecMatchResult` is the internal resolver contract; `ResolveIntentResult` is the LLM-facing tool return type. They are the same shape in v0 but kept separate so the resolver contract and the tool contract can diverge in v1 without a forced refactor.

#### 1e. Extend `QueryDeps`

Replace the existing `QueryDeps` dataclass with:

```python
@dataclass
class QueryDeps:
    """Session-scoped resources available to every QueryBot tool."""
    spec_cache: Any
    connection_manager: Any
    store: Any
    intents: list[MetricIntent] = field(default_factory=list)     # append-only per run
    spec_registry: dict[str, ResolvedSpec] = field(default_factory=dict)  # token → params
```

**`intents` and `spec_registry` are per-run.** They are initialized fresh in each `chat()` / `ask()` call. They are not serialized in `dump_history()`. After history reload, the LLM must re-resolve if it wants to recompute.

#### 1f. Update `ComputeMetricsResult`

The existing `ComputeMetricsResult` keeps all fields but the LLM-facing inputs change. Remove the `metrics`, `slices`, `segment`, `period_type`, `time_window`, `by_entity` fields from the model (they were caller inputs; in v0.2 the inputs are encoded in the token). Keep `result_id`, `row_count`, `sample`, `columns`, `format_hints`, `error`, `payload_summary`.

Add `spec_token` as a diagnostic field (not for LLM reasoning). The `spec_token → result_id` link is already in `RunTrace` (via args + llm_summary), but including it here makes each `ComputeMetricsResult` self-contained for external logging pipelines that parse tool results without reading the full trace. On error it also avoids embedding the token inside the error string.

```python
class ComputeMetricsResult(ToolResult):
    """Summary returned by compute_metrics(spec_token). Full data is in ResultStore."""
    spec_token: str = Field(
        description=(
            "The spec_token consumed to produce this result. "
            "For diagnostics and logging only — do not reuse across turns."
        )
    )
    result_id: str
    row_count: int
    sample: list[dict[str, Any]]
    columns: list[str]
    format_hints: dict[str, str]
    # payload_summary populated from ResolvedSpec on success
```

**Validation (SF-1 tests):**

```python
# tests/test_agent/test_query_types.py (new file; consolidates type-model tests)

def test_metric_intent_defaults():
    intent = MetricIntent(metric_concept="revenue", scope="overall")
    assert intent.period_type == "all_time"
    assert intent.time_window is None

def test_spec_match_result_exact_match_present():
    result = SpecMatchResult(
        exact_match=ExactMatch(spec_token="sm_abc", metric_name="revenue", slices=[], segment=None),
        near_misses=[],
    )
    assert result.exact_match is not None

def test_spec_match_result_no_match():
    result = SpecMatchResult(
        exact_match=None,
        near_misses=[NearMiss(name="revenue", why_not="unknown_metric")],
    )
    assert result.exact_match is None
    assert len(result.near_misses) == 1

def test_query_deps_intents_default_empty():
    from aitaem.agent.store import ResultStore
    deps = QueryDeps(spec_cache=None, connection_manager=None, store=ResultStore())
    assert deps.intents == []
    assert deps.spec_registry == {}

def test_compute_metrics_result_no_input_fields():
    # Confirm the old per-call input fields are gone
    import dataclasses
    r = ComputeMetricsResult(result_id="r1", row_count=1, sample=[], columns=[], format_hints={})
    field_names = {f.name for f in dataclasses.fields(r)} if dataclasses.is_dataclass(r) else set(r.model_fields)
    assert "metrics" not in field_names
    assert "slices" not in field_names
```

---

### SF-2: `SpecResolver` (`aitaem/agent/resolver.py`) — new file

Pure deterministic validator. No I/O, no LLM, no pydantic-ai imports. Depends only on `MetricIntent`, `SpecMatchResult`, `ExactMatch`, `NearMiss` (from query_types) and `SpecCache` (from aitaem). Zero coupling to pydantic-ai.

```python
# aitaem/agent/resolver.py
from __future__ import annotations

import difflib

from aitaem.agent.query_types import ExactMatch, MetricIntent, NearMiss, SpecMatchResult


class SpecResolver:
    """Deterministic v0 catalog validator.

    v0 → v1 swap point: the interface (resolve method signature and return type) is
    stable. Only the body changes in v1 (dict lookup → RAG retrieval + deterministic filter).
    """

    def resolve(
        self,
        intent: MetricIntent,
        proposed_metric_name: str,
        proposed_slices: list[str],
        proposed_segment: str | None,
        spec_cache: object,  # aitaem.SpecCache
    ) -> SpecMatchResult:
        """Validate the proposed names against the catalog.

        Returns SpecMatchResult with exact_match set if all validations pass.
        The spec_token inside exact_match is left empty (""); the caller (resolve_intent
        tool) mints and fills the token after this method returns.
        """
        near_misses: list[NearMiss] = []

        # scope_mismatch is deliberately NOT checked in v0. MetricSpec has no
        # "scope" flag, so the resolver cannot distinguish an inherently-scoped
        # metric (e.g. `ctr_conversion_ads`) from an overall metric proposed
        # for a subset intent. The LLM's metric selection is trusted. Revisit
        # if a future MetricSpec field marks scope explicitly.

        # ── 1. Validate metric name ──────────────────────────────────────────
        metric_spec = spec_cache.metrics.get(proposed_metric_name)
        if metric_spec is None:
            # Unknown metric — can't validate slices/segment without the spec, so return early.
            # Populate suggestions via fuzzy match to help the LLM surface typo corrections.
            suggestions = difflib.get_close_matches(
                proposed_metric_name, spec_cache.metrics.keys(), n=3, cutoff=0.75
            )
            return SpecMatchResult(
                exact_match=None,
                near_misses=near_misses + [
                    NearMiss(name=proposed_metric_name, why_not="unknown_metric", suggestions=suggestions)
                ],
            )

        # ── 2. Validate slices ───────────────────────────────────────────────
        for slice_name in proposed_slices:
            if slice_name in spec_cache.slices:
                pass  # valid
            elif slice_name in spec_cache.segments:
                near_misses.append(NearMiss(name=slice_name, why_not="wrong_dimension_kind"))
            else:
                near_misses.append(NearMiss(name=slice_name, why_not="unknown_slice"))

        # ── 3. Validate segment ──────────────────────────────────────────────
        if proposed_segment is not None:
            if proposed_segment in spec_cache.segments:
                pass  # valid
            elif proposed_segment in spec_cache.slices:
                near_misses.append(NearMiss(name=proposed_segment, why_not="wrong_dimension_kind"))
            else:
                near_misses.append(NearMiss(name=proposed_segment, why_not="unknown_segment"))

        # ── 4. Validate by_entity ────────────────────────────────────────────
        if intent.by_entity is not None:
            entities = metric_spec.entities or []
            if intent.by_entity not in entities:
                near_misses.append(NearMiss(
                    name=proposed_metric_name, why_not="unsupported_by_entity"
                ))

        # ── 5. Validate period_type ──────────────────────────────────────────
        if intent.period_type != "all_time" and not metric_spec.timestamp_col:
            near_misses.append(NearMiss(
                name=proposed_metric_name, why_not="unsupported_period_type"
            ))

        # ── Result ───────────────────────────────────────────────────────────
        if near_misses:
            return SpecMatchResult(exact_match=None, near_misses=near_misses)

        return SpecMatchResult(
            exact_match=ExactMatch(
                spec_token="",  # caller (resolve_intent tool) mints and fills this
                metric_name=proposed_metric_name,
                slices=proposed_slices,
                segment=proposed_segment,
            ),
            near_misses=[],
        )
```

**Validation (SF-2 tests):**

```python
# tests/test_agent/test_resolver.py  (new file)

import pytest
from unittest.mock import MagicMock
from aitaem.agent.resolver import SpecResolver
from aitaem.agent.query_types import MetricIntent

def _make_cache(metrics=("revenue",), slices=("by_country",), segments=("by_advertiser",)):
    """Build a minimal mock SpecCache."""
    sc = MagicMock()
    rev_spec = MagicMock()
    rev_spec.entities = ["user_id"]
    rev_spec.timestamp_col = "created_at"
    sc.metrics = {m: rev_spec for m in metrics}
    sc.slices = {s: MagicMock() for s in slices}
    sc.segments = {s: MagicMock() for s in segments}
    return sc

def _intent(scope="overall", period_type="all_time", by_entity=None):
    return MetricIntent(metric_concept="revenue", scope=scope,
                        period_type=period_type, by_entity=by_entity)

resolver = SpecResolver()

def test_exact_match_valid_metric_no_slices():
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", [], None, sc)
    assert result.exact_match is not None
    assert result.exact_match.metric_name == "revenue"
    assert result.near_misses == []

def test_exact_match_with_valid_slice():
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", ["by_country"], None, sc)
    assert result.exact_match is not None

def test_exact_match_with_valid_segment():
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", [], "by_advertiser", sc)
    assert result.exact_match is not None

def test_unknown_slice_near_miss():
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", ["by_platform"], None, sc)
    assert result.exact_match is None
    assert any(nm.why_not == "unknown_slice" for nm in result.near_misses)

def test_unknown_segment_near_miss():
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", [], "by_platform", sc)
    assert result.exact_match is None
    assert any(nm.why_not == "unknown_segment" for nm in result.near_misses)

def test_wrong_dimension_kind_segment_as_slice():
    """A segment spec name passed in the slices list."""
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", ["by_advertiser"], None, sc)
    assert result.exact_match is None
    nms = {nm.name: nm.why_not for nm in result.near_misses}
    assert nms["by_advertiser"] == "wrong_dimension_kind"

def test_wrong_dimension_kind_slice_as_segment():
    """A slice spec name passed as segment."""
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", [], "by_country", sc)
    assert result.exact_match is None
    assert any(nm.why_not == "wrong_dimension_kind" for nm in result.near_misses)

def test_unsupported_by_entity():
    sc = _make_cache()
    result = resolver.resolve(_intent(by_entity="unknown_col"), "revenue", [], None, sc)
    assert result.exact_match is None
    assert any(nm.why_not == "unsupported_by_entity" for nm in result.near_misses)

def test_by_entity_supported():
    sc = _make_cache()
    result = resolver.resolve(_intent(by_entity="user_id"), "revenue", [], None, sc)
    assert result.exact_match is not None

def test_scope_subset_no_slices_still_resolves():
    """scope=subset with no slices/segment is valid if the metric itself is scoped.

    e.g. `ctr_conversion_ads` is inherently a subset metric — no slice needed.
    SpecResolver does not emit scope_mismatch because it has no way to distinguish
    a scoped metric from an overall one (MetricSpec has no scope field).
    """
    sc = _make_cache(metrics=("ctr_conversion_ads",))
    result = resolver.resolve(_intent(scope="subset"), "ctr_conversion_ads", [], None, sc)
    assert result.exact_match is not None
    assert result.near_misses == []

def test_scope_subset_with_slice_also_resolves():
    """scope=subset with a valid slice is also an exact match."""
    sc = _make_cache()
    result = resolver.resolve(_intent(scope="subset"), "revenue", ["by_country"], None, sc)
    assert result.exact_match is not None

def test_unsupported_period_type_no_timestamp():
    """Metric with no timestamp_col cannot support non-all_time period_type."""
    sc = _make_cache()
    sc.metrics["revenue"].timestamp_col = ""   # simulate empty
    result = resolver.resolve(_intent(period_type="monthly"), "revenue", [], None, sc)
    assert result.exact_match is None
    assert any(nm.why_not == "unsupported_period_type" for nm in result.near_misses)

def test_multiple_near_misses_accumulated():
    """Resolver accumulates all reasons rather than stopping at first failure."""
    sc = _make_cache()
    result = resolver.resolve(
        _intent(),
        "revenue",
        ["bad_slice", "by_advertiser"],  # unknown_slice + wrong_dimension_kind
        None,
        sc,
    )
    assert result.exact_match is None
    why_nots = {nm.why_not for nm in result.near_misses}
    assert "unknown_slice" in why_nots
    assert "wrong_dimension_kind" in why_nots

def test_exact_match_token_is_empty_string():
    """SpecResolver never mints a token; the tool layer does."""
    sc = _make_cache()
    result = resolver.resolve(_intent(), "revenue", [], None, sc)
    assert result.exact_match.spec_token == ""

def test_unknown_metric_name():
    sc = _make_cache()
    result = resolver.resolve(_intent(), "nonexistent_metric", [], None, sc)
    assert result.exact_match is None

def test_unknown_metric_suggestions_populated_on_typo():
    """Typo in metric name → suggestions contains the correct catalog entry."""
    sc = _make_cache(metrics=("revenue_gross",))
    result = resolver.resolve(_intent(), "revenue_gros", [], None, sc)
    assert result.exact_match is None
    nm = result.near_misses[0]
    assert nm.why_not == "unknown_metric"
    assert "revenue_gross" in nm.suggestions

def test_unknown_metric_suggestions_empty_when_no_close_match():
    """Completely unrelated name → suggestions is empty, not an error."""
    sc = _make_cache()
    result = resolver.resolve(_intent(), "xyz_completely_different", [], None, sc)
    assert result.exact_match is None
    assert result.near_misses[0].suggestions == []
```

---

### SF-3: `record_intent` tool (`aitaem/agent/query_tools.py`)

Add this function to `query_tools.py`. The existing tools are untouched.

```python
from aitaem.agent.query_types import MetricIntent, RecordIntentResult

def record_intent(
    ctx: RunContext[QueryDeps],
    metric_concept: str,
    scope: str,
    subset_description: str | None = None,
    slice_type: str | None = None,
    slice_value: str | None = None,
    segment_name: str | None = None,
    segment_value: str | None = None,
    period_type: str = "all_time",
    time_window: tuple[str, str] | None = None,
    by_entity: str | None = None,
) -> RecordIntentResult:
    """Record the user's metric intent. Call once per metric in the question.

    Args:
        metric_concept: Free-text name as interpreted from the user's question.
            (e.g. "click-through rate", "monthly revenue"). Not a canonical catalog name.
        scope: "overall" for unfiltered aggregate; "subset" if the user wants a
            filtered or broken-down view (requires slice_type or segment_name).
        subset_description: Optional prose description of the filter (e.g.
            "only US users who clicked in January").
        slice_type: Proposed slice spec name for a breakdown (e.g. "by_country").
        slice_value: Specific filter value within the slice (e.g. "US").
        segment_name: Proposed segment spec name for entity-level segmentation.
        segment_value: Specific segment filter value.
        period_type: "all_time" | "hourly" | "daily" | "weekly" | "monthly" | "yearly".
            Non-"all_time" requires time_window.
        time_window: [start_iso, end_iso]. For hourly, use YYYY-MM-DDTHH:MM:SS,
            floored to the hour.
        by_entity: Entity column for entity-level questions ("which user", "top 10 advertisers").

    Returns:
        RecordIntentResult with intent_id (integer index into the intents list).
        Pass this intent_id to resolve_intent.
    """
    intent = MetricIntent(
        metric_concept=metric_concept,
        scope=scope,
        subset_description=subset_description,
        slice_type=slice_type,
        slice_value=slice_value,
        segment_name=segment_name,
        segment_value=segment_value,
        period_type=period_type,
        time_window=tuple(time_window) if time_window else None,
        by_entity=by_entity,
    )
    ctx.deps.intents.append(intent)
    return RecordIntentResult(intent_id=len(ctx.deps.intents) - 1)
```

**Validation (SF-3 tests):**

```python
# tests/test_agent/test_query_tools.py (add to existing file)

from aitaem.agent.query_tools import record_intent
from aitaem.agent.query_types import QueryDeps, MetricIntent
from aitaem.agent.store import ResultStore

def _make_deps():
    return QueryDeps(spec_cache=MagicMock(), connection_manager=MagicMock(), store=ResultStore())

def _make_ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx

def test_record_intent_appends_to_deps():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    result = record_intent(ctx, metric_concept="revenue", scope="overall")
    assert len(deps.intents) == 1
    assert deps.intents[0].metric_concept == "revenue"

def test_record_intent_returns_index():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    r0 = record_intent(ctx, metric_concept="revenue", scope="overall")
    r1 = record_intent(ctx, metric_concept="ctr", scope="overall")
    assert r0.intent_id == 0
    assert r1.intent_id == 1
    assert len(deps.intents) == 2

def test_record_intent_stores_time_window():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall",
                  period_type="monthly", time_window=("2024-01-01", "2024-03-31"))
    assert deps.intents[0].time_window == ("2024-01-01", "2024-03-31")
    assert deps.intents[0].period_type == "monthly"

def test_record_intent_scope_subset():
    deps = _make_deps()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="subset", slice_type="by_country")
    assert deps.intents[0].scope == "subset"
    assert deps.intents[0].slice_type == "by_country"
```

---

### SF-4: `resolve_intent` tool (`aitaem/agent/query_tools.py`)

Add this function after `record_intent`. Imports `SpecResolver` from `aitaem.agent.resolver`.

```python
import uuid
from aitaem.agent.resolver import SpecResolver
from aitaem.agent.query_types import (
    ExactMatch, NearMiss, ResolvedSpec, ResolveIntentResult,
)

def resolve_intent(
    ctx: RunContext[QueryDeps],
    intent_id: int,
    metric_name: str,
    slices: list[str] | None = None,
    segment: str | None = None,
) -> ResolveIntentResult:
    """Validate proposed canonical names against the catalog and mint a spec_token.

    Must be called after record_intent. Pass the intent_id from record_intent.

    Args:
        intent_id: Integer returned by record_intent for this metric.
        metric_name: Proposed canonical metric name (must exactly match catalog).
        slices: Proposed slice spec names (for breakdowns). Defaults to no slices.
        segment: Proposed segment spec name. Defaults to no segment.

    Returns:
        ResolveIntentResult:
          - exact_match: set if the proposal is valid. spec_token is the handle
            for compute_metrics. Proceed to compute_metrics(spec_token=...).
          - near_misses: set when exact_match is None. Each entry explains why a
            proposed name did not match. Set status=refused and cite these.
    """
    # Guard: intent_id out of range
    if intent_id < 0 or intent_id >= len(ctx.deps.intents):
        return ResolveIntentResult(
            exact_match=None,
            near_misses=[NearMiss(name=metric_name, why_not="unknown_metric")],
        )

    intent = ctx.deps.intents[intent_id]
    resolver = SpecResolver()
    match_result = resolver.resolve(
        intent=intent,
        proposed_metric_name=metric_name,
        proposed_slices=slices or [],
        proposed_segment=segment,
        spec_cache=ctx.deps.spec_cache,
    )

    if match_result.exact_match is None:
        return ResolveIntentResult(exact_match=None, near_misses=match_result.near_misses)

    # Mint token and register the resolved spec
    spec_token = f"sm_{uuid.uuid4().hex}"
    resolved = ResolvedSpec(
        metric_name=metric_name,
        slice_specs=slices or [],
        segment_spec=segment,
        period_type=intent.period_type,
        time_window=intent.time_window,
        by_entity=intent.by_entity,
        intent_slice_value=intent.slice_value,
        intent_segment_value=intent.segment_value,
    )
    ctx.deps.spec_registry[spec_token] = resolved

    exact = ExactMatch(
        spec_token=spec_token,
        metric_name=metric_name,
        slices=slices or [],
        segment=segment,
    )
    return ResolveIntentResult(exact_match=exact, near_misses=[])
```

**Note on token format:** The design doc specifies `sm_` + ULID. We use `sm_` + UUID4 hex (32 chars) which is equally non-guessable and avoids adding a new dependency. The token is still opaque and per-run. If ULID is preferred for time-ordering, add `python-ulid` to `[agent-anthropic]` optional-dependencies and use `from ulid import ULID; token = f"sm_{ULID()}"`.

**Validation (SF-4 tests):**

```python
# tests/test_agent/test_query_tools.py (continued)

from aitaem.agent.query_tools import record_intent, resolve_intent
from aitaem.agent.query_types import QueryDeps

def _make_spec_cache():
    sc = MagicMock()
    rev = MagicMock(); rev.entities = ["user_id"]; rev.timestamp_col = "ts"
    sc.metrics = {"revenue": rev}
    sc.slices = {"by_country": MagicMock()}
    sc.segments = {"by_advertiser": MagicMock()}
    return sc

def _make_deps_with_cache():
    return QueryDeps(
        spec_cache=_make_spec_cache(),
        connection_manager=MagicMock(),
        store=ResultStore(),
    )

def _record_and_ctx(deps, concept="revenue", scope="overall"):
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept=concept, scope=scope)
    return ctx

def test_resolve_intent_exact_match_mints_token():
    deps = _make_deps_with_cache()
    ctx = _record_and_ctx(deps)
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    assert result.exact_match is not None
    assert result.exact_match.spec_token.startswith("sm_")
    assert len(result.near_misses) == 0

def test_resolve_intent_token_stored_in_registry():
    deps = _make_deps_with_cache()
    ctx = _record_and_ctx(deps)
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    token = result.exact_match.spec_token
    assert token in ctx.deps.spec_registry
    resolved = ctx.deps.spec_registry[token]
    assert resolved.metric_name == "revenue"

def test_resolve_intent_near_miss_unknown_slice():
    deps = _make_deps_with_cache()
    ctx = _record_and_ctx(deps)
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue", slices=["by_platform"])
    assert result.exact_match is None
    assert any(nm.why_not == "unknown_slice" for nm in result.near_misses)

def test_resolve_intent_with_valid_slice():
    deps = _make_deps_with_cache()
    ctx = _record_and_ctx(deps)
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue", slices=["by_country"])
    assert result.exact_match is not None
    assert result.exact_match.slices == ["by_country"]

def test_resolve_intent_invalid_intent_id():
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    # No record_intent called — intent_id=0 is out of range
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    assert result.exact_match is None

def test_resolve_intent_multiple_intents_correct_index():
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="ctr", scope="overall")
    record_intent(ctx, metric_concept="revenue", scope="overall")
    # intent_id=1 → revenue
    result = resolve_intent(ctx, intent_id=1, metric_name="revenue")
    assert result.exact_match is not None

def test_resolve_intent_each_token_unique():
    """Two valid resolve calls must produce different tokens."""
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    record_intent(ctx, metric_concept="revenue", scope="overall")
    record_intent(ctx, metric_concept="revenue", scope="overall")
    r1 = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    r2 = resolve_intent(ctx, intent_id=1, metric_name="revenue")
    assert r1.exact_match.spec_token != r2.exact_match.spec_token
```

---

### SF-5: `compute_metrics` migration (`aitaem/agent/query_tools.py`)

**Replace** the existing `compute_metrics` function entirely. The new signature accepts only `spec_token`.

```python
def compute_metrics(
    ctx: RunContext[QueryDeps],
    spec_token: str,
) -> ComputeMetricsResult:
    """Execute a resolved metric spec and store the result.

    Call this only after resolve_intent returns an exact_match. Pass
    exact_match.spec_token directly — do not construct or modify the token.

    Args:
        spec_token: Opaque handle returned by resolve_intent.exact_match.spec_token.

    Returns:
        ComputeMetricsResult with result_id pointing to the stored artifact.
        On failure, result_id is "" and error contains the exception message.
    """
    # Pop on consume: single-use by design. With Anthropic parallel tool calls the LLM
    # can emit two compute_metrics(spec_token=X) in the same message; popping here
    # prevents double warehouse execution and duplicate result_ids from one query.
    resolved = ctx.deps.spec_registry.pop(spec_token, None)
    if resolved is None:
        return ComputeMetricsResult(
            spec_token=spec_token,
            result_id="", row_count=0, sample=[], columns=[],
            format_hints={},
            error="spec_token already consumed. A parallel compute_metrics call with this token may have succeeded — use that result_id. Do not call resolve_intent again.",
        )

    try:
        mc = MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)
        ibis_table = mc.compute(
            metrics=[resolved.metric_name],
            slices=resolved.slice_specs or None,
            segments=resolved.segment_spec,
            time_window=resolved.time_window,
            period_type=resolved.period_type,
            by_entity=resolved.by_entity,
        )
        arrow_table = ibis_table.to_pyarrow()
        result_id = ctx.deps.store.store(arrow_table, ibis_table)

        # Format hints from spec cache
        format_hints: dict[str, str] = {}
        spec = ctx.deps.spec_cache.metrics.get(resolved.metric_name)
        if spec and spec.format:
            format_hints[resolved.metric_name] = spec.format

        return ComputeMetricsResult(
            spec_token=spec_token,
            result_id=result_id,
            row_count=len(arrow_table),
            sample=_sample_arrow(arrow_table),
            columns=arrow_table.schema.names,
            format_hints=format_hints,
            payload_summary={
                "metrics_used": [resolved.metric_name],
                "slices_used": resolved.slice_specs or [],
                "segment_used": resolved.segment_spec,
                "period_type": resolved.period_type,
                "time_window": list(resolved.time_window) if resolved.time_window else None,
                "by_entity": resolved.by_entity,
                "format_hints": format_hints,
            },
        )
    except (SpecNotFoundError, QueryBuildError, QueryExecutionError, AitaemConnectionError) as exc:
        return ComputeMetricsResult(
            spec_token=spec_token,
            result_id="", row_count=0, sample=[], columns=[],
            format_hints={},
            error=f"{type(exc).__name__}: {exc}",
        )
```

**Validation (SF-5 tests):**

```python
# tests/test_agent/test_query_tools.py (update existing compute_metrics tests)

from aitaem.agent.query_tools import compute_metrics, record_intent, resolve_intent
from unittest.mock import patch, MagicMock

def _mock_mc_returning(arrow_table):
    mc = MagicMock()
    mock_ibis = MagicMock()
    mock_ibis.to_pyarrow.return_value = arrow_table
    mc.compute.return_value = mock_ibis
    return mc

def _sample_table():
    return pa.table({
        "metric_name": ["revenue"], "metric_value": [1000.0],
        "period_type": ["all_time"], "period_start_date": [None],
        "period_end_date": [None], "entity_id": [None],
        "metric_format": [None], "slice_type": [None],
        "slice_value": [None], "segment_name": [None], "segment_value": [None],
    })

def _setup_resolved_token(deps, ctx):
    """Helper: record + resolve 'revenue' and return the spec_token."""
    record_intent(ctx, metric_concept="revenue", scope="overall")
    result = resolve_intent(ctx, intent_id=0, metric_name="revenue")
    return result.exact_match.spec_token

def test_compute_metrics_success_via_token():
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    mc = _mock_mc_returning(_sample_table())
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mc):
        result = compute_metrics(ctx, spec_token=token)
    assert result.error is None
    assert result.result_id != ""
    assert result.row_count == 1
    assert result.result_id in deps.store.ids()

def test_compute_metrics_unknown_token():
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    result = compute_metrics(ctx, spec_token="sm_bogus")
    assert result.error is not None
    assert "already consumed" in result.error
    assert result.result_id == ""

def test_compute_metrics_token_consumed_on_use():
    """Second call with the same spec_token must return an error (pop-on-consume)."""
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_mock_mc_returning(_sample_table())):
        r1 = compute_metrics(ctx, spec_token=token)
        r2 = compute_metrics(ctx, spec_token=token)
    assert r1.error is None
    assert r2.error is not None
    assert "already-consumed" in r2.error

def test_compute_metrics_spec_not_found():
    from aitaem.utils.exceptions import SpecNotFoundError
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    mc = MagicMock()
    mc.compute.side_effect = SpecNotFoundError("revenue")
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=mc):
        result = compute_metrics(ctx, spec_token=token)
    assert result.error is not None
    assert "SpecNotFoundError" in result.error

def test_compute_metrics_payload_summary_from_resolved_spec():
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_mock_mc_returning(_sample_table())):
        result = compute_metrics(ctx, spec_token=token)
    assert result.payload_summary["metrics_used"] == ["revenue"]

def test_compute_metrics_ibis_ref_stored():
    deps = _make_deps_with_cache()
    ctx = _make_ctx(deps)
    token = _setup_resolved_token(deps, ctx)
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_mock_mc_returning(_sample_table())):
        result = compute_metrics(ctx, spec_token=token)
    entry = deps.store.get(result.result_id)
    assert entry.ibis_ref is not None
```

---

### SF-6: System Prompt Redesign + `_build_agent()` Update (`aitaem/agent/query_bot.py`)

#### 6a. Rewrite `_build_system_prompt`

Replace the existing `_build_system_prompt` with two functions — one per static layer:

```python
def _build_layer_a() -> str:
    """Layer A: stable workflow and rules (identical for all tenants)."""
    return """\
# ─── Layer A: workflow & rules ───────────────────────────────────────────────

You are a data analysis assistant for an AITAEM metrics platform. You answer
questions by resolving them against a defined catalog and calling tools. Never
invent values; every number must come from a tool call.

## Workflow — three required steps, in order

### Step 1 — record_intent
Call `record_intent` once per metric the user is asking about. Fields:
- metric_concept: free-text name (e.g. "click-through rate")
- scope: "overall" (unfiltered) or "subset" (filter specified)
- slice_type / slice_value: for breakdowns or a specific slice member
- segment_name / segment_value: for entity-level filters
- period_type: "all_time" | "hourly" | "daily" | "weekly" | "monthly" | "yearly"
  (default: all_time). Non-"all_time" requires time_window.
- time_window: [start, end] ISO dates. Hourly uses YYYY-MM-DDTHH:MM:SS,
  floored to the hour.
- by_entity: only for entity-level questions ("which user", "top 10 advertisers").

Returns: intent_id (integer).

### Step 2 — resolve_intent
Call `resolve_intent` with the intent_id and your proposed canonical names
(metric_name, slices, segment) drawn from the catalog.

Returns:
- exact_match: {spec_token, metric_name, slices, segment} if the proposal is
  valid — proceed to Step 3.
- near_misses: [{name, why_not}] — specs that came close but did not match.

If exact_match is null: STOP. Do not call compute_metrics. Set status="refused"
and cite near_misses in the reason. See Metric Precision Rule below.

### Step 3 — compute_metrics
Call `compute_metrics(spec_token=...)`. All compute parameters are encoded in
the token; pass nothing else. Returns result_id and optional warnings.

Each spec_token is single-use. If you receive an "already-consumed" error,
check for a concurrent compute_metrics result carrying the same token and use
its result_id — do not call resolve_intent again.

## Analysis tools

Use these on a result_id from compute_metrics (or from a prior analysis call).
Each produces a new result_id.

| If the question asks for…                                  | Tool                        |
|------------------------------------------------------------|-----------------------------|
| Top / bottom N entities, slice members, or periods         | rank_by_value               |
| Rows above / below a value; who exceeds a target           | filter_by_threshold         |
| Distribution, percentile rank, count above/below median    | distribution_summary        |
| Ranking all members of one dimension                       | rank_by_value (n=None)      |
| Growth or decline across periods; period deltas            | period_over_period          |
| Share of total; concentration in top X% or top N           | contribution_share          |

Rules:
- Complete Steps 1–3 first. Pass the result_id to analysis tools.
- With ≤ 20 rows and no analytical intent, skip analysis tools; narrate directly.
- Time-series questions (period_type ≠ all_time) always narrate from compute_metrics.
- For "which entities are above/below the median": call distribution_summary
  to get p50, then call filter_by_threshold with that value.

## Metric Precision Rule (CRITICAL)

- Never substitute an approximate metric. CTR ≠ conversion rate.
  Revenue ≠ profit. Sessions ≠ unique users.
- If resolve_intent returns exact_match=null: set status="refused",
  cite near_misses[].name + why_not in the reason, and prompt the user to
  refine or define a new spec.

## Final Response

After tool calls, produce a QueryOutput:
- status: "ok" if data was returned; "empty" if zero rows; "refused" if
  resolution failed or out of scope; "error" if a tool returned an error.
- narrative: plain-language explanation referencing tool-returned values.
- result_ids: list of result_ids, primary/most relevant first. Empty unless
  status="ok".
- reason: brief note when status is "refused" or "error". Null otherwise.

## Value Formatting

Read format_hints from the compute_metrics result and apply them yourself
when writing the narrative. Common values: "percent" → format as a percentage
(e.g. 4.2%); "currency" → add currency symbol (e.g. $1,234); "integer" →
round to whole number. If format_hints is empty, use plain numeric values.

## Multi-Metric Questions

If the user asks about multiple metrics, run Steps 1–3 independently for each."""


_LARGE_CATALOG_THRESHOLD = 32


def _build_layer_b(spec_cache: Any) -> str:
    """Layer B: per-tenant spec catalog (session-stable).

    Above 32 metrics, the catalog is replaced with a one-liner directing the
    LLM to rely on resolve_intent for catalog search (v1 RAG path).
    """
    n_metrics = len(spec_cache.metrics)
    if n_metrics > _LARGE_CATALOG_THRESHOLD:
        return (
            "# ─── Layer B: catalog ──────────────────────────────────────────────────────\n\n"
            "## SPEC CATALOG\n"
            "Call resolve_intent to search the catalog. Do not enumerate metrics from memory."
        )

    metric_lines = []
    for name, spec in spec_cache.metrics.items():
        parts = [f"- **{name}**: {spec.description or '(no description)'}"]
        if spec.entities:
            parts.append(f"  Entities: {', '.join(spec.entities)}")
        metric_lines.append("\n".join(parts))

    slice_lines = [
        f"- **{name}**: {spec.description or '(no description)'}"
        for name, spec in spec_cache.slices.items()
    ]
    segment_lines = [
        f"- **{name}**: {spec.description or '(no description)'}"
        for name, spec in spec_cache.segments.items()
    ]

    catalog = "\n".join([
        "## Metrics",
        "\n".join(metric_lines) or "(none)",
        "",
        "## Slices",
        "\n".join(slice_lines) or "(none)",
        "",
        "## Segments",
        "\n".join(segment_lines) or "(none)",
    ])

    return (
        "# ─── Layer B: catalog (per-tenant, session-stable) ─────────────────────────\n\n"
        "## SPEC CATALOG\n"
        + catalog
    )
```

#### 6b. Update `QueryBot.__init__()` and `_build_agent()`

**`__init__` — add `tenant_id` parameter:**

```python
def __init__(
    self,
    *,
    model: Any,
    spec_cache: Any,
    connection_manager: Any,
    tenant_id: str | None = None,
    tools: list[Any] | None = None,
) -> None:
    self._spec_cache = spec_cache
    self._connection_manager = connection_manager
    self._tenant_id = tenant_id          # <── new
    super().__init__(model=model, tools=tools)
    self._conversation_id: str | None = None
```

`tenant_id` scopes OpenAI routing and cache entries per tenant. When `None`, `_build_agent()` derives a stable key from a hash of the metric names (see below).

**Add a module-level helper above `_build_agent()` to keep cache config provider-aware:**

```python
import hashlib

def _permission_fingerprint(spec_cache: Any) -> str:
    """8-char hex fingerprint of the visible catalog (metrics + slices + segments).

    Two spec_caches with the same visible keys produce the same fingerprint and
    share an OpenAI routing lane. Different keys (e.g. post-RBAC) produce
    different fingerprints and get separate lanes with no cross-eviction.

    "|" separates categories; "," separates names within a category.
    Both characters are illegal in YAML keys, so no collision is possible.
    """
    parts = (
        sorted(spec_cache.metrics.keys()),
        sorted(spec_cache.slices.keys()),
        sorted(spec_cache.segments.keys()),
    )
    payload = "|".join(",".join(p) for p in parts)
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def _provider_cache_config(model_str: str, tenant_id: str | None) -> dict:
    """Return model_settings for prompt caching, keyed by provider prefix.

    Anthropic: cache_control breakpoints are explicit — requires
    anthropic_cache_instructions to mark where the static prefix ends.
    Verified against pydantic-ai 2.2.0 (_agent_graph.py:908 + anthropic adapter).

    OpenAI: prompt_cache_key is a routing hint — the server routes requests
    sharing the same key to the same backend pool for prefix-cache hits. A
    shared key across tenants concentrates routing to a smaller pool and causes
    cross-tenant eviction contention. Key on tenant instead.

    tenant_id is used directly when provided. When None (single-tenant /
    self-hosted), a short hash of the sorted metric names serves as a stable
    per-installation key. Two installs with the same spec_cache will share a
    pool, which is harmless — their Layer B content is identical anyway.

    prompt_cache_retention="24h" keeps the cached prefix warm across sessions.
    Verified against pydantic-ai 2.2.0 (models/openai.py:558-568).

    Other providers: return {} (no-op; stable-content-first ordering still
    helps server-side caching where it exists).
    """
    provider = model_str.split(":")[0] if ":" in model_str else ""
    if provider == "anthropic":
        return {"anthropic_cache_instructions": "5m"}
    if provider == "openai":
        return {
            "openai_prompt_cache_key": f"aitaem-{tenant_id}",
            "openai_prompt_cache_retention": "24h",
        }
    return {}
```

> **`cache_read_tokens` normalization note:** No change needed in `aitaem/agent/trace.py`.
> pydantic-ai 2.2.0 already populates `UsageBase.cache_read_tokens` for both providers:
> Anthropic extracts `cache_read_input_tokens` explicitly (models/anthropic.py:2202–2215);
> OpenAI delegates `prompt_tokens_details.cached_tokens` to genai-prices (models/openai.py:4161–4199).
> Both paths land in the same `cache_read_tokens` field that `from_run_usage()` reads.

```python
def _build_agent(self) -> Agent:
    from pydantic_ai import Agent
    from pydantic_ai.toolsets import FunctionToolset

    toolset = FunctionToolset()
    toolset.add_function(record_intent)        # Step 1
    toolset.add_function(resolve_intent)       # Step 2
    toolset.add_function(compute_metrics)      # Step 3
    toolset.add_function(rank_by_value)
    toolset.add_function(filter_by_threshold)
    toolset.add_function(distribution_summary)
    toolset.add_function(period_over_period)
    toolset.add_function(contribution_share)

    # Static instructions: Layers A + B combined.
    # These become InstructionPart(dynamic=False) and are cached at the
    # provider-appropriate breakpoint (see _provider_cache_config above).
    static_instructions = _build_layer_a() + "\n\n" + _build_layer_b(self._spec_cache)

    # Derive a stable routing key for OpenAI. Explicit tenant_id wins; fall back
    # to _permission_fingerprint so single-tenant installs require zero config and
    # RBAC-differentiated users naturally land in separate routing lanes.
    tenant_id = self._tenant_id or _permission_fingerprint(self._spec_cache)

    agent = Agent(
        model=self._model,
        deps_type=QueryDeps,
        output_type=QueryOutput,
        toolsets=[toolset],
        instructions=static_instructions,
        # anthropic_cache_instructions: verified against pydantic-ai 2.2.0 source.
        # _agent_graph.py:908 calls InstructionPart.sorted(), which sorts static
        # (dynamic=False) before dynamic parts. The Anthropic adapter then sets
        # cache_block_idx = num_prefix_blocks + num_static - 1, placing the
        # cache_control breakpoint after the last static block (Layer B). Layer C
        # follows as a dynamic block and is NOT cached. Other providers ignore this.
        model_settings=_provider_cache_config(self._model, tenant_id),
        capabilities=[ReinjectSystemPrompt(replace_existing=True)],
    )

    # Layer C: per-turn date context (dynamic=True → NOT cached).
    # Registered here, after the agent is built, so it captures today's date on each run.
    @agent.instructions
    def _layer_c() -> str:
        from datetime import date
        return (
            f"# ─── Layer C: per-turn context ─────────────────────────────────────────────\n\n"
            f"Today is {date.today().isoformat()}. Use it to resolve relative time references "
            f'("last month", "recently", "May") into concrete time_window values before '
            f"calling record_intent."
        )

    return agent
```

#### 6c. `chat()` and `ask()` — no changes required

`QueryDeps.intents` and `QueryDeps.spec_registry` have `field(default_factory=...)` defaults, so existing `QueryDeps(spec_cache=..., connection_manager=..., store=...)` instantiations in `chat()` and `ask()` already produce fresh empty collections on every call.

**Validation (SF-6 tests — update `test_query_bot.py`):**

```python
# tests/test_agent/test_query_bot.py (update these tests)

def test_system_prompt_layer_a_contains_workflow():
    from aitaem.agent.query_bot import _build_layer_a
    layer_a = _build_layer_a()
    assert "record_intent" in layer_a
    assert "resolve_intent" in layer_a
    assert "compute_metrics" in layer_a
    assert "Metric Precision Rule" in layer_a

def test_system_prompt_layer_b_contains_catalog():
    from aitaem.agent.query_bot import _build_layer_b
    layer_b = _build_layer_b(_make_spec_cache())
    assert "revenue" in layer_b
    assert "ctr" in layer_b
    assert "by_country" in layer_b

def test_system_prompt_layer_b_large_catalog_placeholder():
    from aitaem.agent.query_bot import _build_layer_b, _LARGE_CATALOG_THRESHOLD
    sc = MagicMock()
    # Create a catalog that exceeds the threshold
    sc.metrics = {f"metric_{i}": MagicMock(description="", entities=None, format=None)
                  for i in range(_LARGE_CATALOG_THRESHOLD + 1)}
    sc.slices = {}
    sc.segments = {}
    layer_b = _build_layer_b(sc)
    assert "resolve_intent" in layer_b
    # Should NOT enumerate metrics
    assert "metric_0" not in layer_b

def test_build_agent_has_record_resolve_compute():
    bot = _make_bot()
    # Verify all three resolution tools are in the agent
    tool_names = {t.name for t in bot._agent._function_tools.values()}
    assert "record_intent" in tool_names
    assert "resolve_intent" in tool_names
    assert "compute_metrics" in tool_names
```

---

### SF-7: Integration Tests — FunctionModel 3-Step Flow (`tests/test_agent/test_query_bot.py`)

Replace the existing FunctionModel `_make_compute_then_answer_model` with a 3-step model that exercises the full intent-gated flow.

```python
def _make_three_step_model(metric: str = "revenue"):
    """FunctionModel: record_intent → resolve_intent → compute_metrics → QueryOutput."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ModelRequest, ToolReturnPart

        # Collect all tool return parts seen so far
        tool_returns = {
            p.tool_name: json.loads(p.content)
            for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)
            # Note: older pydantic-ai ToolReturnPart may not have tool_name;
            # adapt as needed based on actual pydantic-ai 2.2.0 API.
        }

        if "record_intent" not in tool_returns:
            # Step 1: record intent
            return ModelResponse(parts=[ToolCallPart(
                tool_name="record_intent",
                args=json.dumps({"metric_concept": metric, "scope": "overall"}),
                tool_call_id="tc-1",
            )])
        elif "resolve_intent" not in tool_returns:
            # Step 2: resolve intent (use the intent_id from step 1)
            intent_id = tool_returns["record_intent"].get("intent_id", 0)
            return ModelResponse(parts=[ToolCallPart(
                tool_name="resolve_intent",
                args=json.dumps({"intent_id": intent_id, "metric_name": metric}),
                tool_call_id="tc-2",
            )])
        elif "compute_metrics" not in tool_returns:
            # Step 3: compute with the spec_token from step 2
            exact = tool_returns["resolve_intent"].get("exact_match")
            if exact is None:
                # Resolution failed → refuse
                output = QueryOutput(
                    status=Status.refused,
                    narrative="Could not resolve the requested metric.",
                    result_ids=[],
                    reason="No exact match found.",
                )
                return ModelResponse(parts=[TextPart(content=output.model_dump_json())])
            token = exact["spec_token"]
            return ModelResponse(parts=[ToolCallPart(
                tool_name="compute_metrics",
                args=json.dumps({"spec_token": token}),
                tool_call_id="tc-3",
            )])
        else:
            # All tool calls done → produce final output
            compute_data = tool_returns["compute_metrics"]
            result_id = compute_data.get("result_id", "")
            output = QueryOutput(
                status=Status.ok if result_id else Status.error,
                narrative=f"{metric.capitalize()} computed: {compute_data.get('row_count', 0)} rows.",
                result_ids=[result_id] if result_id else [],
            )
            return ModelResponse(parts=[TextPart(content=output.model_dump_json())])

    return FunctionModel(fn)


def _make_refused_at_resolve_model():
    """FunctionModel: resolves with no exact_match → refuses."""

    def fn(messages, info):
        # ... (similar to three_step but resolve returns no exact_match) ...
        pass

    return FunctionModel(fn)
```

**Note on FunctionModel tool tracking:** pydantic-ai 2.2.0's `ToolReturnPart` may not have a `tool_name` attribute directly. The actual attribute name must be verified against the pydantic-ai 2.2.0 source before implementing these test helpers. The Phase 2 approach using `messages` inspection should be adapted to track which tool was called by matching `ToolCallPart.tool_name` with the subsequent `ToolReturnPart` by `tool_call_id`.

**Integration test cases to add/replace:**

```python
def test_three_step_flow_status_ok():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    assert response.status == Status.ok
    assert len(response.payload.result_ids) == 1

def test_three_step_flow_result_retrievable():
    bot = _make_bot_with_model(_make_three_step_model())
    response = asyncio.run(bot.chat("What was revenue?"))
    rid = response.payload.primary_result_id
    entry = bot.get_result(rid)
    assert entry.arrow is not None

def test_three_step_flow_refused_on_near_miss():
    # Use a model variant where resolve_intent returns no exact_match
    # ... test that status=refused and reason is populated
    pass

def test_per_run_intents_cleared_between_turns():
    """Intents and spec_registry are fresh per turn (not cross-turn)."""
    # After turn 1, deps.intents was populated. In turn 2, it should be empty again.
    # Test by checking that turn 2 can produce its own token independently.
    bot = _make_bot_with_model(_make_three_step_model())
    asyncio.run(bot.chat("What was revenue?"))
    # Second turn — intents should be empty at the START of the run
    # (they're populated fresh each time chat() is called)
    asyncio.run(bot.chat("Same question again?"))
    # Both turns should succeed
    pass
```

---

### SF-8: Update `aitaem/agent/__init__.py`

Add exports for the new resolution types:

```python
from aitaem.agent.query_types import (
    QueryPayload,
    MetricIntent,          # NEW
    ResolvedSpec,          # NEW
    ExactMatch,            # NEW
    NearMiss,              # NEW
    SpecMatchResult,       # NEW
    RecordIntentResult,    # NEW
    ResolveIntentResult,   # NEW
)
from aitaem.agent.resolver import SpecResolver  # NEW
```

Add to `__all__`:
```python
"MetricIntent", "ResolvedSpec", "ExactMatch", "NearMiss",
"SpecMatchResult", "RecordIntentResult", "ResolveIntentResult", "SpecResolver",
```

**Validation:**
```python
def test_v02_exports():
    from aitaem.agent import (
        MetricIntent, ResolvedSpec, ExactMatch, NearMiss,
        SpecMatchResult, RecordIntentResult, ResolveIntentResult, SpecResolver,
    )
    assert all(x is not None for x in [
        MetricIntent, ResolvedSpec, ExactMatch, NearMiss,
        SpecMatchResult, RecordIntentResult, ResolveIntentResult, SpecResolver,
    ])
```

---

### SF-9: Smoke Test Update (`tests/test_agent/test_query_bot_smoke.py`)

Update the existing smoke test to:
1. Use the 3-step tool flow with a real LLM
2. Verify `cache_read_input_tokens > 0` on turn 2 (Anthropic caching working)

```python
def test_query_bot_smoke_three_step_flow():
    """One real-LLM chat() turn exercising record_intent → resolve_intent → compute_metrics."""
    bot = QueryBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=_smoke_spec_cache(),
        connection_manager=MagicMock(),
    )
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_smoke_mc()):
        response = asyncio.run(bot.chat("What was total revenue?"))

    assert response.status == Status.ok
    rid = response.payload.primary_result_id
    assert rid is not None
    entry = bot.get_result(rid)
    assert entry.arrow is not None

    # Verify the 3-step flow was used
    tool_names = [tc.name for tc in response.trace.tool_calls]
    assert "record_intent" in tool_names
    assert "resolve_intent" in tool_names
    assert "compute_metrics" in tool_names


def test_query_bot_smoke_prompt_cache_hit_on_turn_2():
    """Turn 2 in the same session should show cache_read_input_tokens > 0."""
    bot = QueryBot(
        model="anthropic:claude-haiku-4-5-20251001",
        spec_cache=_smoke_spec_cache(),
        connection_manager=MagicMock(),
    )
    with patch("aitaem.agent.query_tools.MetricCompute", return_value=_smoke_mc()):
        asyncio.run(bot.chat("What was total revenue?"))
        response2 = asyncio.run(bot.chat("What about last month?"))

    # On turn 2, Layers A+B must be served from cache (Anthropic only).
    # RunTrace.Usage.cache_read_tokens mirrors pydantic-ai's Usage.cache_read_tokens.
    # Anthropic: Verified that the adapter populates cache_read_tokens from the
    # API response's cache_read_input_tokens (pydantic-ai 2.2.0 models/anthropic.py:2202-2215).
    # OpenAI: caching is server-side and not guaranteed in test environments
    # (depends on whether the runtime has seen this prompt_cache_key before),
    # so we skip the assertion for non-Anthropic providers.
    if "anthropic:" in bot._model:
        assert response2.trace.usage.cache_read_tokens > 0, (
            "cache_read_tokens is 0 — Layers A+B were not served from cache on turn 2. "
            "Check that anthropic_cache_instructions='5m' is set and that the static "
            "instructions are not being regenerated between turns."
        )
```

---

### SF-10: Architecture Doc Note

Add a brief note to `plans/agent_module/ARCHITECTURE.md` in the "Phase 2 — QueryBot" section (Section 8, Implementation Order):

```markdown
> **v0.2 update (Plan 26):** QueryBot ships a two-tool resolution gate
> (`record_intent` → `resolve_intent`) before `compute_metrics`. This
> code-enforces the Metric Precision Rule. See
> [`09-querybot-v0.2-design.md`](09-querybot-v0.2-design.md) for the full design
> and [`plans/26-querybot-v0.2.md`](../26-querybot-v0.2.md) for the implementation
> plan. Analysis tools are unchanged.
```

---

## Files Changed Summary

| File | Change | Notes |
|---|---|---|
| `aitaem/agent/query_types.py` | Modified | Add MetricIntent, ResolvedSpec, ExactMatch, NearMiss, SpecMatchResult, RecordIntentResult, ResolveIntentResult; extend QueryDeps; update ComputeMetricsResult |
| `aitaem/agent/resolver.py` | **New** | SpecResolver class — pure deterministic validator |
| `aitaem/agent/query_tools.py` | Modified | Add record_intent, resolve_intent; replace compute_metrics |
| `aitaem/agent/query_bot.py` | Modified | Add tenant_id to __init__; replace _build_system_prompt with _build_layer_a/_build_layer_b/_permission_fingerprint/_provider_cache_config; rewrite _build_agent() |
| `aitaem/agent/__init__.py` | Modified | Add 8 new exports |
| `tests/test_agent/test_query_types.py` | **New** | SF-1 type model tests (consolidates from test_query_tools.py) |
| `tests/test_agent/test_resolver.py` | **New** | SF-2: SpecResolver unit tests — all NearMiss reasons |
| `tests/test_agent/test_query_tools.py` | Modified | Replace compute_metrics tests; add record_intent/resolve_intent tests; update QueryDeps instantiations throughout |
| `tests/test_agent/test_query_bot.py` | Modified | Replace FunctionModel helpers; update system prompt tests; replace integration tests with 3-step flow |
| `tests/test_agent/test_query_bot_smoke.py` | Modified | Update to 3-step flow; add cache verification |
| `plans/agent_module/ARCHITECTURE.md` | Modified | Brief note about v0.2, link to design doc and this plan |

**Unchanged files:** `aitaem/agent/base.py`, `aitaem/agent/store.py`, `aitaem/agent/trace.py`, `aitaem/agent/response.py`, `aitaem/agent/history.py`, `tests/test_agent/test_primitives.py`, `tests/test_agent/test_trace.py`, `tests/test_agent/test_history.py`.

---

## Known Deviations from Design Doc

| Design doc says | Implementation does | Rationale |
|---|---|---|
| Token = "sm_" + ULID | Token = "sm_" + UUID4 hex | Avoids adding `python-ulid` dependency; same non-guessable property |
| SystemPromptPart with cache_control | `instructions=` (static) + `@agent.instructions` (dynamic) + `model_settings={"anthropic_cache_instructions": "5m"}` | pydantic-ai 2.2.0 does not expose cache_control on SystemPromptPart. Verified: `InstructionPart.sorted()` (_agent_graph.py:908) puts static parts first; Anthropic adapter sets `cache_block_idx = num_prefix_blocks + num_static - 1`, placing the breakpoint after Layer B. Layer C is dynamic and is not cached. |

---

## Testing Strategy

Run after each SF:

1. **After SF-1:** `python -m pytest tests/test_agent/test_query_types.py -v`
2. **After SF-2:** `python -m pytest tests/test_agent/test_resolver.py -v`
3. **After SF-3:** `python -m pytest tests/test_agent/test_query_tools.py -k "record_intent" -v`
4. **After SF-4:** `python -m pytest tests/test_agent/test_query_tools.py -k "resolve_intent" -v`
5. **After SF-5:** `python -m pytest tests/test_agent/test_query_tools.py -k "compute_metrics" -v`
6. **After SF-6:** `python -m pytest tests/test_agent/test_query_bot.py -k "system_prompt or layer or build_agent" -v`
7. **After SF-7:** `python -m pytest tests/test_agent/test_query_bot.py -k "chat or ask or flow" -v`
8. **After SF-8:** `python -m pytest tests/test_agent/ -k "exports" -v`
9. **Full suite before commit:**
   ```bash
   python -m pytest tests/test_agent/ --cov=aitaem/agent --cov-report=term-missing
   python -m pytest tests/ --ignore=tests/test_agent/   # core must stay green
   python scripts/check_import_graph.py
   ruff check aitaem/agent/
   ```
10. **Smoke test (requires ANTHROPIC_API_KEY):**
    ```bash
    ANTHROPIC_API_KEY=sk-... pytest tests/test_agent/test_query_bot_smoke.py -v
    ```

---

## Success Criteria

- [ ] `from aitaem.agent import MetricIntent, ResolvedSpec, ExactMatch, NearMiss, SpecMatchResult, SpecResolver` works
- [ ] `record_intent` → `resolve_intent` → `compute_metrics(spec_token)` three-step flow works end-to-end in a FunctionModel test
- [ ] `resolve_intent` with an invalid proposal returns `exact_match=None` and non-empty `near_misses`
- [ ] `compute_metrics` with an unknown `spec_token` returns `error` (not raises)
- [ ] A second `compute_metrics` call with the same `spec_token` returns `error` containing "already-consumed" (pop-on-consume, prevents parallel double-execution)
- [ ] `SpecResolver` unit tests cover all 7 `NearMiss.why_not` reasons
- [ ] Calling `compute_metrics(metrics=..., slices=...)` (old Phase 2 API) is no longer possible — the tool only accepts `spec_token`
- [ ] All Phase 1 primitive tests still pass
- [ ] All analysis tool tests still pass (unchanged)
- [ ] System prompt contains Layer A + B content; Layer C (today's date) is injected dynamically
- [ ] `python -m pytest tests/test_agent/ --cov=aitaem/agent` passes with ≥ 90% coverage
- [ ] `python scripts/check_import_graph.py` exits 0
- [ ] Smoke test: `status=ok`, all three tool names in trace, primary_result_id set, cache read tokens > 0 on turn 2
