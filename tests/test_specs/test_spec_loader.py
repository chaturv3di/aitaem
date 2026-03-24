"""Tests for spec loader functions and SpecCache."""

import pytest

from aitaem.specs.loader import (
    SpecCache,
    load_spec_from_file,
    load_spec_from_string,
    load_specs_from_directory,
)
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import SpecNotFoundError
from tests.test_specs.conftest import (
    VALID_METRIC_RATIO_YAML,
    VALID_METRIC_SUM_YAML,
    VALID_SEGMENT_YAML,
    VALID_SLICE_YAML,
)


class TestLoadSpecFromFile:
    def test_load_metric_from_file(self, fixtures_dir):
        spec = load_spec_from_file(fixtures_dir / "valid_metric_ratio.yaml", MetricSpec)
        assert isinstance(spec, MetricSpec)
        assert spec.name == "homepage_ctr"

    def test_load_slice_from_file(self, fixtures_dir):
        spec = load_spec_from_file(fixtures_dir / "valid_slice.yaml", SliceSpec)
        assert isinstance(spec, SliceSpec)
        assert spec.name == "geography"

    def test_load_segment_from_file(self, fixtures_dir):
        spec = load_spec_from_file(fixtures_dir / "valid_segment.yaml", SegmentSpec)
        assert isinstance(spec, SegmentSpec)
        assert spec.name == "customer_value_tier"

    def test_nonexistent_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_spec_from_file(tmp_path / "missing.yaml", MetricSpec)


class TestLoadSpecFromString:
    def test_load_metric_from_string(self):
        spec = load_spec_from_string(VALID_METRIC_RATIO_YAML, MetricSpec)
        assert isinstance(spec, MetricSpec)
        assert spec.name == "homepage_ctr"

    def test_load_slice_from_string(self):
        spec = load_spec_from_string(VALID_SLICE_YAML, SliceSpec)
        assert isinstance(spec, SliceSpec)
        assert spec.name == "geography"

    def test_load_segment_from_string(self):
        spec = load_spec_from_string(VALID_SEGMENT_YAML, SegmentSpec)
        assert isinstance(spec, SegmentSpec)
        assert spec.name == "customer_value_tier"


class TestLoadSpecsFromDirectory:
    def test_load_directory_with_valid_files(self, tmp_path):
        (tmp_path / "metric1.yaml").write_text(VALID_METRIC_RATIO_YAML)
        (tmp_path / "metric2.yaml").write_text(VALID_METRIC_SUM_YAML)
        result = load_specs_from_directory(tmp_path, MetricSpec)
        assert len(result) == 2
        assert "homepage_ctr" in result
        assert "total_revenue" in result

    def test_returns_dict_keyed_by_name(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        result = load_specs_from_directory(tmp_path, MetricSpec)
        assert isinstance(result, dict)
        assert isinstance(result["homepage_ctr"], MetricSpec)

    def test_empty_directory_returns_empty_dict(self, tmp_path):
        result = load_specs_from_directory(tmp_path, MetricSpec)
        assert result == {}

    def test_invalid_files_skipped_with_warning(self, tmp_path, caplog):
        import logging

        (tmp_path / "valid.yaml").write_text(VALID_METRIC_RATIO_YAML)
        (tmp_path / "invalid.yaml").write_text("metric:\n  name: [unclosed")
        with caplog.at_level(logging.WARNING):
            result = load_specs_from_directory(tmp_path, MetricSpec)
        assert len(result) == 1
        assert "homepage_ctr" in result

    def test_nonexistent_directory_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Directory does not exist"):
            load_specs_from_directory(tmp_path / "nonexistent", MetricSpec)

    def test_file_path_instead_of_dir_raises(self, tmp_path):
        f = tmp_path / "metric.yaml"
        f.write_text(VALID_METRIC_RATIO_YAML)
        with pytest.raises(ValueError, match="Expected a directory"):
            load_specs_from_directory(f, MetricSpec)

    def test_loads_yml_extension(self, tmp_path):
        (tmp_path / "metric.yml").write_text(VALID_METRIC_SUM_YAML)
        result = load_specs_from_directory(tmp_path, MetricSpec)
        assert "total_revenue" in result

    def test_loads_slice_specs_from_directory(self, tmp_path):
        (tmp_path / "slice.yaml").write_text(VALID_SLICE_YAML)
        result = load_specs_from_directory(tmp_path, SliceSpec)
        assert "geography" in result

    def test_loads_segment_specs_from_directory(self, tmp_path):
        (tmp_path / "segment.yaml").write_text(VALID_SEGMENT_YAML)
        result = load_specs_from_directory(tmp_path, SegmentSpec)
        assert "customer_value_tier" in result

    def test_duplicate_name_in_directory_last_wins(self, tmp_path, caplog):
        import logging

        # Two files defining the same metric name
        (tmp_path / "a.yaml").write_text(VALID_METRIC_RATIO_YAML)
        (tmp_path / "b.yaml").write_text(VALID_METRIC_RATIO_YAML)
        with caplog.at_level(logging.WARNING):
            result = load_specs_from_directory(tmp_path, MetricSpec)
        assert "homepage_ctr" in result


class TestSpecCache:
    def test_get_metric_returns_correct_spec(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        spec = cache.get_metric("homepage_ctr")
        assert isinstance(spec, MetricSpec)
        assert spec.name == "homepage_ctr"

    def test_get_metric_returns_same_object_on_second_call(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        spec1 = cache.get_metric("homepage_ctr")
        spec2 = cache.get_metric("homepage_ctr")
        assert spec1 is spec2

    def test_get_slice_returns_correct_spec(self, tmp_path):
        (tmp_path / "slice.yaml").write_text(VALID_SLICE_YAML)
        cache = SpecCache.from_yaml(slice_paths=[tmp_path])
        spec = cache.get_slice("geography")
        assert isinstance(spec, SliceSpec)

    def test_get_segment_returns_correct_spec(self, tmp_path):
        (tmp_path / "segment.yaml").write_text(VALID_SEGMENT_YAML)
        cache = SpecCache.from_yaml(segment_paths=[tmp_path])
        spec = cache.get_segment("customer_value_tier")
        assert isinstance(spec, SegmentSpec)

    def test_unknown_metric_raises_spec_not_found(self, tmp_path):
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        with pytest.raises(SpecNotFoundError) as exc_info:
            cache.get_metric("nonexistent")
        assert exc_info.value.spec_type == "metric"
        assert exc_info.value.name == "nonexistent"

    def test_no_paths_configured_raises(self):
        cache = SpecCache()
        with pytest.raises(SpecNotFoundError) as exc_info:
            cache.get_metric("foo")
        assert exc_info.value.searched_paths == []

    def test_accepts_single_file_path(self, tmp_path):
        f = tmp_path / "metric.yaml"
        f.write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=str(f))
        spec = cache.get_metric("homepage_ctr")
        assert spec.name == "homepage_ctr"

    def test_clear_empties_cache(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        cache.get_metric("homepage_ctr")
        cache.clear()
        with pytest.raises(SpecNotFoundError):
            cache.get_metric("homepage_ctr")

    def test_empty_cache_has_empty_dicts(self):
        cache = SpecCache()
        assert cache._metrics == {}
        assert cache._slices == {}
        assert cache._segments == {}

    def test_from_yaml_loads_all_specs_eagerly(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        # Specs are loaded before any get_* call
        assert "homepage_ctr" in cache._metrics

    def test_from_yaml_raises_on_invalid_spec(self, tmp_path):

        (tmp_path / "invalid.yaml").write_text("metric:\n  name: [unclosed")
        with pytest.raises(Exception):
            SpecCache.from_yaml(metric_paths=[tmp_path])

    def test_from_string_single_metric(self):
        cache = SpecCache.from_string(metric_yaml=VALID_METRIC_RATIO_YAML)
        spec = cache.get_metric("homepage_ctr")
        assert spec.name == "homepage_ctr"

    def test_from_string_multiple_metrics(self):
        cache = SpecCache.from_string(metric_yaml=[VALID_METRIC_RATIO_YAML, VALID_METRIC_SUM_YAML])
        assert cache.get_metric("homepage_ctr").name == "homepage_ctr"
        assert cache.get_metric("total_revenue").name == "total_revenue"

    def test_from_string_mixed_types(self):
        cache = SpecCache.from_string(
            metric_yaml=VALID_METRIC_RATIO_YAML,
            slice_yaml=VALID_SLICE_YAML,
            segment_yaml=VALID_SEGMENT_YAML,
        )
        assert cache.get_metric("homepage_ctr").name == "homepage_ctr"
        assert cache.get_slice("geography").name == "geography"
        assert cache.get_segment("customer_value_tier").name == "customer_value_tier"

    def test_from_yaml_raises_on_missing_path(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SpecCache.from_yaml(metric_paths=[tmp_path / "nonexistent_dir"])


class TestSpecCacheAdd:
    """Tests for SpecCache.add()."""

    def test_add_metric_spec(self):
        cache = SpecCache()
        metric = MetricSpec(
            name="my_metric",
            source="duckdb://db/table",
            numerator="SUM(x)",
            timestamp_col="created_at",
        )
        cache.add(metric)
        result = cache.get_metric("my_metric")
        assert result is metric

    def test_add_slice_spec(self):
        from aitaem.specs.slice import SliceValue

        cache = SpecCache()
        spec = SliceSpec(name="my_slice", values=(SliceValue(name="a", where="x = 1"),))
        cache.add(spec)
        result = cache.get_slice("my_slice")
        assert result is spec

    def test_add_segment_spec(self):
        from aitaem.specs.segment import SegmentSpec as SS, SegmentValue as SV

        cache = SpecCache()
        seg = SS(name="my_seg", source="duckdb://db/table", values=(SV(name="a", where="x=1"),))
        cache.add(seg)
        result = cache.get_segment("my_seg")
        assert result is seg

    def test_add_first_write_wins(self):
        from aitaem.specs.slice import SliceValue

        cache = SpecCache()
        spec1 = SliceSpec(name="geo", values=(SliceValue(name="USA", where="country='USA'"),))
        spec2 = SliceSpec(name="geo", values=(SliceValue(name="EU", where="country='EU'"),))
        cache.add(spec1)
        cache.add(spec2)  # silently ignored
        result = cache.get_slice("geo")
        assert result is spec1

    def test_add_coexists_with_from_yaml_specs(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        manual = MetricSpec(
            name="manual_metric",
            source="duckdb://db/table",
            numerator="SUM(x)",
            timestamp_col="created_at",
        )
        cache.add(manual)
        assert cache.get_metric("homepage_ctr").name == "homepage_ctr"
        assert cache.get_metric("manual_metric") is manual


class TestSpecCacheCompositeSliceValidation:
    """Tests for cross-reference validation in composite SliceSpecs."""

    LEAF_GEO_YAML = """
slice:
  name: geo
  values:
    - name: USA
      where: "country = 'USA'"
"""
    LEAF_DEVICE_YAML = """
slice:
  name: device
  values:
    - name: mobile
      where: "device_type = 'mobile'"
"""
    COMPOSITE_YAML = """
slice:
  name: geo_x_device
  cross_product:
    - geo
    - device
"""

    def test_get_slice_validates_composite_cross_references(self, tmp_path):
        (tmp_path / "geo.yaml").write_text(self.LEAF_GEO_YAML)
        (tmp_path / "device.yaml").write_text(self.LEAF_DEVICE_YAML)
        (tmp_path / "composite.yaml").write_text(self.COMPOSITE_YAML)
        cache = SpecCache.from_yaml(slice_paths=[tmp_path])
        composite = cache.get_slice("geo_x_device")
        assert composite.is_composite
        assert composite.cross_product == ("geo", "device")

    def test_missing_referenced_slice_raises(self, tmp_path):
        from aitaem.utils.exceptions import SpecValidationError

        # Composite references 'device' which doesn't exist
        (tmp_path / "geo.yaml").write_text(self.LEAF_GEO_YAML)
        (tmp_path / "composite.yaml").write_text(self.COMPOSITE_YAML)
        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_yaml(slice_paths=[tmp_path])
        assert any("device" in e.message for e in exc_info.value.errors)

    def test_nested_composite_raises(self, tmp_path):
        from aitaem.utils.exceptions import SpecValidationError

        nested_yaml = """
slice:
  name: nested
  cross_product:
    - geo
    - geo_x_device
"""
        (tmp_path / "geo.yaml").write_text(self.LEAF_GEO_YAML)
        (tmp_path / "device.yaml").write_text(self.LEAF_DEVICE_YAML)
        (tmp_path / "composite.yaml").write_text(self.COMPOSITE_YAML)
        (tmp_path / "nested.yaml").write_text(nested_yaml)
        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_yaml(slice_paths=[tmp_path])
        assert any(
            "composite" in e.message.lower() or "nested" in e.message.lower()
            for e in exc_info.value.errors
        )
