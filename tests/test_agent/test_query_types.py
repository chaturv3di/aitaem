"""SF-1: Type model tests for the v0.2 resolution types."""
from __future__ import annotations

from aitaem.agent.query_types import (
    MetricIntent,
    ExactMatch,
    NearMiss,
    SpecMatchResult,
    RecordIntentResult,
    ResolveIntentResult,
    QueryDeps,
    ComputeMetricsResult,
)
from aitaem.agent.store import ResultStore


def test_metric_intent_defaults():
    intent = MetricIntent(metric_concept="revenue", scope="overall")
    assert intent.period_type == "all_time"
    assert intent.time_window is None
    assert intent.by_entity is None
    assert intent.slice_type is None


def test_metric_intent_subset_scope():
    intent = MetricIntent(
        metric_concept="ctr", scope="subset",
        slice_type="by_country", slice_value="US",
    )
    assert intent.scope == "subset"
    assert intent.slice_value == "US"


def test_spec_match_result_exact_match_present():
    result = SpecMatchResult(
        exact_match=ExactMatch(spec_token="sm_abc", metric_name="revenue", slices=[], segment=None),
        near_misses=[],
    )
    assert result.exact_match is not None
    assert result.exact_match.spec_token == "sm_abc"


def test_spec_match_result_no_match():
    result = SpecMatchResult(
        exact_match=None,
        near_misses=[NearMiss(name="revenue", why_not="unknown_metric")],
    )
    assert result.exact_match is None
    assert len(result.near_misses) == 1


def test_near_miss_suggestions_default_empty():
    nm = NearMiss(name="revenus", why_not="unknown_metric")
    assert nm.suggestions == []


def test_near_miss_suggestions_populated():
    nm = NearMiss(name="revenus", why_not="unknown_metric", suggestions=["revenue"])
    assert "revenue" in nm.suggestions


def test_near_miss_non_unknown_metric_suggestions_empty():
    nm = NearMiss(name="by_country", why_not="wrong_dimension_kind")
    assert nm.suggestions == []


def test_query_deps_intents_default_empty():
    deps = QueryDeps(spec_cache=None, connection_manager=None, store=ResultStore())
    assert deps.intents == []
    assert deps.spec_registry == {}


def test_query_deps_fresh_per_instance():
    """Two QueryDeps instances must not share the same list/dict objects."""
    d1 = QueryDeps(spec_cache=None, connection_manager=None, store=ResultStore())
    d2 = QueryDeps(spec_cache=None, connection_manager=None, store=ResultStore())
    d1.intents.append(MetricIntent(metric_concept="x", scope="overall"))
    assert len(d2.intents) == 0


def test_compute_metrics_result_no_old_input_fields():
    """The old per-call input fields (metrics, slices, segment, period_type, etc.) are gone."""
    r = ComputeMetricsResult(
        spec_token="sm_tok",
        result_id="r1", row_count=1, sample=[], columns=[], format_hints={},
    )
    assert not hasattr(r, "metrics")
    assert not hasattr(r, "slices")
    assert not hasattr(r, "segment")
    assert not hasattr(r, "period_type")
    assert not hasattr(r, "time_window")
    assert not hasattr(r, "by_entity")


def test_compute_metrics_result_has_spec_token():
    r = ComputeMetricsResult(
        spec_token="sm_tok",
        result_id="r1", row_count=0, sample=[], columns=[], format_hints={},
    )
    assert r.spec_token == "sm_tok"


def test_record_intent_result():
    r = RecordIntentResult(intent_id=3)
    assert r.intent_id == 3


def test_resolve_intent_result_exact_match():
    r = ResolveIntentResult(
        exact_match=ExactMatch(spec_token="sm_x", metric_name="ctr", slices=[], segment=None),
        near_misses=[],
    )
    assert r.exact_match.spec_token == "sm_x"


def test_resolve_intent_result_no_match():
    r = ResolveIntentResult(
        exact_match=None,
        near_misses=[NearMiss(name="ctr", why_not="unknown_metric")],
    )
    assert r.exact_match is None
    assert len(r.near_misses) == 1
