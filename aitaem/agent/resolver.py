from __future__ import annotations

import difflib
from typing import Any

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
