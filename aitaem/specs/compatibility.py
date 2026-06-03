"""
aitaem.specs.compatibility - Spec compatibility result types

CompatibilityResult and ScanResult are returned by MetricCompute.scan().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CompatibilityResult:
    """Compatibility verdict for a single metric × slice or metric × segment pair.

    Returned as part of a ScanResult from MetricCompute.scan().
    """

    metric_name: str
    spec_name: str
    spec_type: Literal["slice", "segment"]
    compatible: bool
    valid_join_keys: list[str]
    """Segment only: join-key candidates that exist in the metric's source table."""
    missing_columns: list[str]
    """Columns (slices) or join-key candidates (segments) absent from the source table."""
    reason: str | None
    """Human-readable explanation when compatible is False; None when compatible."""


@dataclass(frozen=True)
class ScanResult:
    """Full compatibility matrix returned by MetricCompute.scan().

    Contains one CompatibilityResult per metric × slice and metric × segment pair.
    """

    results: tuple[CompatibilityResult, ...]

    def compatible_slices(self, metric_name: str) -> list[str]:
        """Slice names compatible with the given metric."""
        return [
            r.spec_name
            for r in self.results
            if r.metric_name == metric_name and r.spec_type == "slice" and r.compatible
        ]

    def compatible_segments(self, metric_name: str) -> list[str]:
        """Segment names compatible with the given metric."""
        return [
            r.spec_name
            for r in self.results
            if r.metric_name == metric_name and r.spec_type == "segment" and r.compatible
        ]

    def compatible_metrics(self, spec_name: str) -> list[str]:
        """Metric names compatible with the given slice or segment name."""
        return [r.metric_name for r in self.results if r.spec_name == spec_name and r.compatible]

    def for_metric(self, metric_name: str) -> list[CompatibilityResult]:
        """All CompatibilityResult rows for the given metric."""
        return [r for r in self.results if r.metric_name == metric_name]

    def for_spec(self, spec_name: str) -> list[CompatibilityResult]:
        """All CompatibilityResult rows for the given slice or segment name."""
        return [r for r in self.results if r.spec_name == spec_name]
