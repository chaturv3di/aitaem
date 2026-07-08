"""SF-2: SpecResolver unit tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from aitaem.agent.resolver import SpecResolver
from aitaem.agent.query_types import MetricIntent


def _make_cache(
    metrics=("revenue",),
    slices=("by_country",),
    segments=("by_advertiser",),
):
    sc = MagicMock()
    rev_spec = MagicMock()
    rev_spec.entities = ["user_id"]
    rev_spec.timestamp_col = "created_at"
    sc.metrics = {m: rev_spec for m in metrics}
    sc.slices = {s: MagicMock() for s in slices}
    sc.segments = {s: MagicMock() for s in segments}
    return sc


def _intent(scope="overall", period_type="all_time", by_entity=None):
    return MetricIntent(
        metric_concept="revenue", scope=scope,
        period_type=period_type, by_entity=by_entity,
    )


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
    """scope=subset with no slices/segment is valid — SpecResolver does not check scope_mismatch."""
    sc = _make_cache(metrics=("ctr_conversion_ads",))
    result = resolver.resolve(_intent(scope="subset"), "ctr_conversion_ads", [], None, sc)
    assert result.exact_match is not None
    assert result.near_misses == []


def test_scope_subset_with_slice_also_resolves():
    sc = _make_cache()
    result = resolver.resolve(_intent(scope="subset"), "revenue", ["by_country"], None, sc)
    assert result.exact_match is not None


def test_unsupported_period_type_no_timestamp():
    sc = _make_cache()
    sc.metrics["revenue"].timestamp_col = ""
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
    assert any(nm.why_not == "unknown_metric" for nm in result.near_misses)


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


def test_unknown_metric_early_return_no_slice_validation():
    """When metric is unknown, slices are not validated — only one near_miss returned."""
    sc = _make_cache()
    result = resolver.resolve(_intent(), "nonexistent", ["bad_slice"], None, sc)
    assert result.exact_match is None
    assert len(result.near_misses) == 1
    assert result.near_misses[0].why_not == "unknown_metric"
