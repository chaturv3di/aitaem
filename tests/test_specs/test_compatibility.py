"""Tests for CompatibilityResult, ScanResult, and MetricCompute.scan() (plan 21)."""

import logging
from unittest.mock import MagicMock

import pytest

from aitaem.insights import MetricCompute, _run_scan
from aitaem.specs.compatibility import CompatibilityResult, ScanResult
from aitaem.specs.loader import SpecCache
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.slice import SliceSpec, SliceValue

SOURCE_URI = "duckdb://analytics.db/events"
DIM_URI = "duckdb://analytics.db/dim_users"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_metric(name: str = "m1", source: str = SOURCE_URI) -> MetricSpec:
    return MetricSpec(
        name=name,
        source=source,
        numerator="SUM(amount)",
        timestamp_col="created_at",
    )


def make_leaf_slice(name: str, *cols: str) -> SliceSpec:
    return SliceSpec(
        name=name,
        values=tuple(SliceValue(name=c, where=f"{c} = 'x'") for c in cols),
    )


def make_wildcard_slice(name: str, col: str) -> SliceSpec:
    return SliceSpec(name=name, column=col)


def make_segment(name: str, entity_id: str, join_keys: tuple[str, ...] = ()) -> SegmentSpec:
    return SegmentSpec(
        name=name,
        source=DIM_URI,
        entity_id=entity_id,
        join_keys=join_keys,
        values=(SegmentValue(name="tier_a", where=f"{entity_id} > 0"),),
    )


def mock_connection_manager(uri_to_columns: dict[str, list[str]]) -> MagicMock:
    """Return a mock ConnectionManager that serves fixed column lists per URI."""
    manager = MagicMock()

    def _get_connector(uri: str) -> MagicMock:
        connector = MagicMock()
        cols = uri_to_columns.get(uri, [])
        connector.get_table.return_value.schema.return_value.names = cols
        return connector

    manager.get_connection_for_source.side_effect = _get_connector
    return manager


def make_spec_cache(*specs) -> SpecCache:
    cache = SpecCache()
    for spec in specs:
        cache.add(spec)
    return cache


# ---------------------------------------------------------------------------
# Unit tests — mocked ConnectionManager
# ---------------------------------------------------------------------------


class TestLeafSliceCompatibility:
    def test_leaf_slice_compatible(self):
        metric = make_metric()
        sl = make_leaf_slice("region", "country", "city")
        cache = make_spec_cache(metric, sl)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "country", "city"]})

        result = _run_scan(cache, mgr)

        assert len(result.results) == 1
        r = result.results[0]
        assert r.compatible is True
        assert r.missing_columns == []
        assert r.reason is None

    def test_leaf_slice_incompatible(self):
        metric = make_metric()
        sl = make_leaf_slice("region", "country", "missing_col")
        cache = make_spec_cache(metric, sl)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "country"]})

        result = _run_scan(cache, mgr)

        r = result.results[0]
        assert r.compatible is False
        assert r.missing_columns == ["missing_col"]
        assert r.reason is not None and len(r.reason) > 0


class TestWildcardSliceCompatibility:
    def test_wildcard_slice_compatible(self):
        metric = make_metric()
        sl = make_wildcard_slice("platform", "platform")
        cache = make_spec_cache(metric, sl)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "platform"]})

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is True

    def test_wildcard_slice_incompatible(self):
        metric = make_metric()
        sl = make_wildcard_slice("platform", "platform")
        cache = make_spec_cache(metric, sl)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at"]})

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is False
        assert r.missing_columns == ["platform"]


class TestCompositeSliceCompatibility:
    def test_composite_all_components_compatible(self):
        metric = make_metric()
        leaf1 = make_leaf_slice("geo", "country")
        leaf2 = make_leaf_slice("device", "device_type")
        composite = SliceSpec(name="geo_x_device", cross_product=("geo", "device"))
        cache = make_spec_cache(metric, leaf1, leaf2, composite)
        mgr = mock_connection_manager(
            {SOURCE_URI: ["amount", "created_at", "country", "device_type"]}
        )

        rows = {r.spec_name: r for r in _run_scan(cache, mgr).results}
        assert rows["geo_x_device"].compatible is True

    def test_composite_one_component_incompatible(self):
        metric = make_metric()
        leaf1 = make_leaf_slice("geo", "country")
        leaf2 = make_leaf_slice("device", "device_type")
        composite = SliceSpec(name="geo_x_device", cross_product=("geo", "device"))
        cache = make_spec_cache(metric, leaf1, leaf2, composite)
        # device_type is missing from source
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "country"]})

        rows = {r.spec_name: r for r in _run_scan(cache, mgr).results}
        assert rows["geo_x_device"].compatible is False
        assert "device_type" in rows["geo_x_device"].missing_columns


class TestSegmentCompatibility:
    def test_segment_all_join_keys_valid(self):
        metric = make_metric()
        seg = make_segment("user_tier", entity_id="user_id", join_keys=("buyer_id", "seller_id"))
        cache = make_spec_cache(metric, seg)
        mgr = mock_connection_manager(
            {SOURCE_URI: ["amount", "created_at", "buyer_id", "seller_id"]}
        )

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is True
        assert set(r.valid_join_keys) == {"buyer_id", "seller_id"}
        assert r.missing_columns == []

    def test_segment_partial_join_keys_valid(self):
        metric = make_metric()
        seg = make_segment("user_tier", entity_id="user_id", join_keys=("buyer_id", "seller_id"))
        cache = make_spec_cache(metric, seg)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "buyer_id"]})

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is True
        assert r.valid_join_keys == ["buyer_id"]
        assert r.missing_columns == ["seller_id"]

    def test_segment_no_join_keys_valid(self):
        metric = make_metric()
        seg = make_segment("user_tier", entity_id="user_id", join_keys=("buyer_id", "seller_id"))
        cache = make_spec_cache(metric, seg)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at"]})

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is False
        assert r.valid_join_keys == []
        assert set(r.missing_columns) == {"buyer_id", "seller_id"}
        assert r.reason is not None

    def test_segment_entity_id_fallback_compatible(self):
        metric = make_metric()
        # No join_keys declared — entity_id is the default candidate
        seg = make_segment("platform_seg", entity_id="platform")
        cache = make_spec_cache(metric, seg)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "platform"]})

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is True
        assert r.valid_join_keys == ["platform"]

    def test_segment_entity_id_fallback_incompatible(self):
        metric = make_metric()
        seg = make_segment("platform_seg", entity_id="platform")
        cache = make_spec_cache(metric, seg)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at"]})

        r = _run_scan(cache, mgr).results[0]
        assert r.compatible is False
        assert r.valid_join_keys == []


class TestSchemaIntrospectionBatching:
    def test_schema_introspection_batched_by_uri(self):
        m1 = make_metric("m1", SOURCE_URI)
        m2 = make_metric("m2", SOURCE_URI)
        sl = make_leaf_slice("sl", "country")
        cache = make_spec_cache(m1, m2, sl)
        mgr = mock_connection_manager({SOURCE_URI: ["amount", "created_at", "country"]})

        _run_scan(cache, mgr)

        # get_connection_for_source must be called exactly once despite two metrics
        assert mgr.get_connection_for_source.call_count == 1

    def test_unavailable_connection_skips_metric(self, caplog):
        good_uri = "duckdb://analytics.db/orders"
        bad_uri = SOURCE_URI
        m_good = make_metric("good", good_uri)
        m_bad = make_metric("bad", bad_uri)
        sl = make_leaf_slice("sl", "country")
        cache = make_spec_cache(m_good, m_bad, sl)

        mgr = MagicMock()

        def side_effect(uri):
            if uri == bad_uri:
                raise ConnectionError("no connection")
            connector = MagicMock()
            connector.get_table.return_value.schema.return_value.names = [
                "amount",
                "created_at",
                "country",
            ]
            return connector

        mgr.get_connection_for_source.side_effect = side_effect

        with caplog.at_level(logging.WARNING):
            result = _run_scan(cache, mgr)

        # bad metric is excluded; good metric still produces a result
        metric_names = {r.metric_name for r in result.results}
        assert "good" in metric_names
        assert "bad" not in metric_names
        assert any("bad_uri" in msg or bad_uri in msg for msg in caplog.messages)


class TestScanResultQueryMethods:
    @pytest.fixture
    def scan_result(self) -> ScanResult:
        return ScanResult(
            results=(
                CompatibilityResult("m1", "geo", "slice", True, [], [], None),
                CompatibilityResult("m1", "device", "slice", False, [], ["device_type"], "..."),
                CompatibilityResult("m1", "seg_a", "segment", True, ["user_id"], [], None),
                CompatibilityResult("m1", "seg_b", "segment", False, [], ["org_id"], "..."),
                CompatibilityResult("m2", "geo", "slice", True, [], [], None),
                CompatibilityResult("m2", "seg_a", "segment", False, [], ["user_id"], "..."),
            )
        )

    def test_compatible_slices(self, scan_result):
        assert scan_result.compatible_slices("m1") == ["geo"]
        assert scan_result.compatible_slices("m2") == ["geo"]

    def test_compatible_segments(self, scan_result):
        assert scan_result.compatible_segments("m1") == ["seg_a"]
        assert scan_result.compatible_segments("m2") == []

    def test_compatible_metrics_for_spec(self, scan_result):
        assert set(scan_result.compatible_metrics("geo")) == {"m1", "m2"}
        assert scan_result.compatible_metrics("seg_a") == ["m1"]
        assert scan_result.compatible_metrics("device") == []

    def test_for_metric(self, scan_result):
        rows = scan_result.for_metric("m1")
        assert len(rows) == 4
        assert all(r.metric_name == "m1" for r in rows)

    def test_for_spec(self, scan_result):
        rows = scan_result.for_spec("geo")
        assert len(rows) == 2
        assert all(r.spec_name == "geo" for r in rows)

    def test_empty_cache_returns_empty_scan_result(self):
        cache = SpecCache()
        mgr = mock_connection_manager({})
        result = _run_scan(cache, mgr)
        assert result.results == ()


# ---------------------------------------------------------------------------
# Integration tests — real DuckDB (ad_campaigns_connection_manager fixture)
# ---------------------------------------------------------------------------

AD_CAMPAIGNS_SOURCE_URI = "duckdb://ad_campaigns.duckdb/ad_campaigns"
DIM_PLATFORMS_SOURCE_URI = "duckdb://ad_campaigns.duckdb/dim_platforms"


class TestScanIntegration:
    def _make_mc(self, connection_manager, *specs):
        cache = make_spec_cache(*specs)
        return MetricCompute(cache, connection_manager)

    def test_ad_campaigns_slices_all_compatible(self, ad_campaigns_connection_manager):
        metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        campaign_type_slice = SliceSpec(
            name="campaign_type",
            values=(
                SliceValue(name="Search", where="campaign_type = 'Search'"),
                SliceValue(name="Display", where="campaign_type = 'Display'"),
            ),
        )
        country_slice = make_wildcard_slice("country", "country")

        mc = self._make_mc(ad_campaigns_connection_manager, metric, campaign_type_slice, country_slice)
        result = mc.scan()

        assert result.compatible_slices("ctr") == ["campaign_type", "country"] or set(
            result.compatible_slices("ctr")
        ) == {"campaign_type", "country"}
        for r in result.for_metric("ctr"):
            assert r.spec_type == "slice"
            assert r.compatible is True

    def test_ad_campaigns_segment_compatible(self, ad_campaigns_connection_manager):
        metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        platform_segment = SegmentSpec(
            name="platform",
            source=DIM_PLATFORMS_SOURCE_URI,
            entity_id="platform",
            values=(SegmentValue(name="Google", where="platform = 'Google Ads'"),),
        )

        mc = self._make_mc(ad_campaigns_connection_manager, metric, platform_segment)
        result = mc.scan()

        segs = result.compatible_segments("ctr")
        assert "platform" in segs
        r = result.for_spec("platform")[0]
        assert r.compatible is True
        assert r.valid_join_keys == ["platform"]

    def test_incompatible_slice_detected(self, ad_campaigns_connection_manager):
        metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        bad_slice = SliceSpec(
            name="bad_slice",
            values=(SliceValue(name="x", where="nonexistent_column = 'foo'"),),
        )

        mc = self._make_mc(ad_campaigns_connection_manager, metric, bad_slice)
        result = mc.scan()

        r = result.for_spec("bad_slice")[0]
        assert r.compatible is False
        assert "nonexistent_column" in r.missing_columns
