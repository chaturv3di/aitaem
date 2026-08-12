from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from aitaem.specs.metric import MetricSpec


# ── Intent types (LLM-produced) ─────────────────────────────────────────────

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
    slice_value: str | None = None                # specific filter value, e.g. "US"
    segment_name: str | None = None               # proposed segment spec name
    segment_value: str | None = None              # specific segment filter value
    period_type: str = "all_time"
    time_window: tuple[str, str] | None = None    # (start_iso, end_iso)
    by_entity: str | None = None
    column_distribution_result_id: str | None = None  # source of a derived time_window


# ── Resolution result types (LLM-facing tool returns) ───────────────────────

class ExactMatch(BaseModel):
    """Minted only when SpecResolver confirms a valid proposal."""
    spec_token: str
    metric_name: str
    slices: list[str]
    segment: str | None


class NearMiss(BaseModel):
    """A catalog entry that resolve_intent considered but rejected, with the reason why."""

    name: str
    why_not: Literal[
        "unknown_metric",
        "scope_mismatch", "wrong_dimension_kind",
        "unknown_slice", "unknown_segment",
        "unsupported_by_entity", "unsupported_period_type",
        "column_distribution_metric_mismatch",
    ]
    suggestions: list[str] = []
    """Populated for two why_not reasons, each for a different purpose:
    - 'unknown_metric': catalog names close to `name`, via difflib.get_close_matches
      (cutoff=0.75), for typo correction.
    - 'column_distribution_metric_mismatch': the single metric name the referenced
      column_distribution result was actually computed against.
    Empty for all other why_not reasons."""


class SpecMatchResult(BaseModel):
    """Returned to the LLM by resolve_intent.

    If exact_match is not None: the LLM proceeds to compute_metrics(spec_token).
    If exact_match is None: the LLM must produce status=refused and cite near_misses.
    """
    exact_match: ExactMatch | None
    near_misses: list[NearMiss]


class SpecResolver:
    """Deterministic v0 catalog validator.

    v0 → v1 swap point: the interface (resolve method signature and return type) is
    stable. Only the body changes in v1 (dict lookup → RAG retrieval + deterministic filter).
    """

    @staticmethod
    def resolve_metric_name(
        name: str, spec_cache: Any
    ) -> tuple[MetricSpec | None, list[str]]:
        """Look up a canonical metric name in the catalog.

        Returns (spec, []) on success, (None, suggestions) on failure — suggestions
        are fuzzy-matched catalog names (difflib.get_close_matches, cutoff=0.75).
        Callable directly, without a MetricIntent.
        """
        spec = spec_cache.metrics.get(name)
        if spec is not None:
            return spec, []
        suggestions = difflib.get_close_matches(
            name, spec_cache.metrics.keys(), n=3, cutoff=0.75
        )
        return None, suggestions

    def resolve(
        self,
        intent: MetricIntent,
        proposed_metric_name: str,
        proposed_slices: list[str],
        proposed_segment: str | None,
        spec_cache: Any,  # aitaem.SpecCache
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
        metric_spec, suggestions = self.resolve_metric_name(proposed_metric_name, spec_cache)
        if metric_spec is None:
            # Unknown metric — can't validate slices/segment without the spec, so return early.
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
