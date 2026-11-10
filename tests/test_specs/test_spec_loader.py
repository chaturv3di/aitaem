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
from aitaem.utils.exceptions import SpecNotFoundError, SpecValidationError
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

        # load_specs_from_directory (standalone) retains lenient warn-and-overwrite semantics
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

    def test_from_yaml_raises_on_duplicate_metric_name(self, tmp_path):
        from aitaem.utils.exceptions import SpecValidationError

        (tmp_path / "a.yaml").write_text(VALID_METRIC_RATIO_YAML)
        (tmp_path / "b.yaml").write_text(VALID_METRIC_RATIO_YAML)
        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_yaml(metric_paths=[tmp_path])
        assert exc_info.value.name == "homepage_ctr"
        assert any("Duplicate" in e.message for e in exc_info.value.errors)

    def test_from_string_raises_on_duplicate_metric_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(metric_yaml=[VALID_METRIC_RATIO_YAML, VALID_METRIC_RATIO_YAML])
        assert exc_info.value.name == "homepage_ctr"
        assert any("Duplicate" in e.message for e in exc_info.value.errors)

    def test_from_string_raises_on_duplicate_slice_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(slice_yaml=[VALID_SLICE_YAML, VALID_SLICE_YAML])
        assert exc_info.value.name == "geography"

    def test_from_string_raises_on_duplicate_segment_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(segment_yaml=[VALID_SEGMENT_YAML, VALID_SEGMENT_YAML])
        assert exc_info.value.name == "customer_value_tier"

    def test_duplicate_across_types_is_allowed(self):
        # A metric and a slice may share a name — they are independent namespaces
        same_name_metric = VALID_METRIC_RATIO_YAML.replace("homepage_ctr", "geography")
        cache = SpecCache.from_string(
            metric_yaml=same_name_metric,
            slice_yaml=VALID_SLICE_YAML,
        )
        assert cache.get_metric("geography").name == "geography"
        assert cache.get_slice("geography").name == "geography"


class TestSpecCacheIntrospection:
    """Tests for SpecCache.metrics / .slices / .segments read-only properties."""

    def test_metrics_returns_mapping_with_expected_keys(self, tmp_path):
        (tmp_path / "m1.yaml").write_text(VALID_METRIC_RATIO_YAML)
        (tmp_path / "m2.yaml").write_text(VALID_METRIC_SUM_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        assert set(cache.metrics.keys()) == {"homepage_ctr", "total_revenue"}

    def test_slices_returns_mapping_with_expected_keys(self, tmp_path):
        (tmp_path / "s.yaml").write_text(VALID_SLICE_YAML)
        cache = SpecCache.from_yaml(slice_paths=[tmp_path])
        assert "geography" in cache.slices

    def test_segments_returns_mapping_with_expected_keys(self, tmp_path):
        (tmp_path / "seg.yaml").write_text(VALID_SEGMENT_YAML)
        cache = SpecCache.from_yaml(segment_paths=[tmp_path])
        assert "customer_value_tier" in cache.segments

    def test_metrics_values_are_metric_spec_instances(self, tmp_path):
        from aitaem.specs.metric import MetricSpec

        (tmp_path / "m.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        assert all(isinstance(v, MetricSpec) for v in cache.metrics.values())

    def test_empty_cache_returns_empty_mappings(self):
        cache = SpecCache()
        assert len(cache.metrics) == 0
        assert len(cache.slices) == 0
        assert len(cache.segments) == 0

    def test_metrics_is_read_only(self, tmp_path):
        (tmp_path / "m.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        with pytest.raises(TypeError):
            cache.metrics["new_key"] = None  # type: ignore[index]

    def test_clear_reflected_in_properties(self, tmp_path):
        (tmp_path / "m.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        assert len(cache.metrics) == 1
        cache.clear()
        assert len(cache.metrics) == 0


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
        seg = SS(
            name="my_seg",
            source="duckdb://db/table",
            entity_id="id",
            values=(SV(name="a", where="x=1"),),
        )
        cache.add(seg)
        result = cache.get_segment("my_seg")
        assert result is seg

    def test_add_duplicate_name_raises(self):
        from aitaem.specs.slice import SliceValue
        from aitaem.utils.exceptions import SpecValidationError

        cache = SpecCache()
        spec1 = SliceSpec(name="geo", values=(SliceValue(name="USA", where="country='USA'"),))
        spec2 = SliceSpec(name="geo", values=(SliceValue(name="EU", where="country='EU'"),))
        cache.add(spec1)
        with pytest.raises(SpecValidationError) as exc_info:
            cache.add(spec2)
        assert exc_info.value.name == "geo"

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


class TestSpecCacheUpdate:
    """Tests for SpecCache.update()."""

    def test_update_overwrites_existing_metric(self):
        cache = SpecCache()
        original = MetricSpec(
            name="m", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts"
        )
        cache.add(original)
        replacement = MetricSpec(
            name="m", source="duckdb://db/t2", numerator="SUM(y)", timestamp_col="ts2"
        )
        cache.update(replacement)
        assert cache.get_metric("m") is replacement

    def test_update_missing_name_raises_spec_not_found(self):
        cache = SpecCache()
        spec = MetricSpec(
            name="ghost", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts"
        )
        with pytest.raises(SpecNotFoundError) as exc_info:
            cache.update(spec)
        assert exc_info.value.spec_type == "metric"
        assert exc_info.value.name == "ghost"

    def test_update_composite_slice_rejects_missing_ref(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="geo", values=(SV(name="USA", where="country='USA'"),)))
        cache.add(SS(name="device", values=(SV(name="mobile", where="d='mobile'"),)))
        composite = SS(name="composite", cross_product=("geo", "device"))
        cache.add(composite)
        pre_state = dict(cache._slices)
        replacement = SS(name="composite", cross_product=("geo", "missing"))
        with pytest.raises(SpecValidationError) as exc_info:
            cache.update(replacement)
        assert "missing" in str(exc_info.value)
        assert cache._slices == pre_state
        assert cache._slices["composite"] is composite

    def test_update_composite_slice_rejects_already_composite_ref(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="leaf1", values=(SV(name="a", where="x=1"),)))
        cache.add(SS(name="leaf2", values=(SV(name="b", where="x=2"),)))
        cache.add(SS(name="nested_candidate", cross_product=("leaf1", "leaf2")))
        cache.add(SS(name="target", cross_product=("leaf1", "leaf2")))
        pre_state = dict(cache._slices)
        replacement = SS(name="target", cross_product=("leaf1", "nested_candidate"))
        with pytest.raises(SpecValidationError) as exc_info:
            cache.update(replacement)
        assert "nested_candidate" in str(exc_info.value)
        assert cache._slices == pre_state

    def test_update_rejects_self_reference(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="leaf", values=(SV(name="a", where="x=1"),)))
        cache.add(SS(name="other", values=(SV(name="b", where="x=2"),)))
        cache.add(SS(name="self_ref", cross_product=("leaf", "other")))
        pre_state = dict(cache._slices)
        # Update self_ref to reference itself — self_ref is composite, so this
        # is rejected as "references an already-composite slice".
        replacement = SS(name="self_ref", cross_product=("leaf", "self_ref"))
        with pytest.raises(SpecValidationError) as exc_info:
            cache.update(replacement)
        assert "self_ref" in str(exc_info.value)
        assert cache._slices == pre_state

    def test_update_leaf_into_composite_rejected_when_referenced_by_existing_composite(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="leaf", values=(SV(name="a", where="x=1"),)))
        cache.add(SS(name="other_leaf", values=(SV(name="b", where="x=2"),)))
        cache.add(SS(name="referrer", cross_product=("leaf", "other_leaf")))
        pre_state = dict(cache._slices)
        # Turn "leaf" into a composite itself — "referrer" now nests a composite.
        promoted = SS(name="leaf", cross_product=("other_leaf",))
        with pytest.raises(SpecValidationError) as exc_info:
            cache.update(promoted)
        assert "referrer" in str(exc_info.value)
        assert cache._slices == pre_state


class TestSpecCacheRemove:
    """Tests for SpecCache.remove()."""

    def test_remove_metric_success(self):
        cache = SpecCache()
        spec = MetricSpec(name="m", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts")
        cache.add(spec)
        cache.remove("metric", "m")
        with pytest.raises(SpecNotFoundError):
            cache.get_metric("m")

    def test_remove_missing_name_raises_spec_not_found(self):
        cache = SpecCache()
        with pytest.raises(SpecNotFoundError) as exc_info:
            cache.remove("metric", "ghost")
        assert exc_info.value.spec_type == "metric"
        assert exc_info.value.name == "ghost"

    def test_remove_blocked_by_dependent_composite(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="geo", values=(SV(name="USA", where="country='USA'"),)))
        cache.add(SS(name="device", values=(SV(name="mobile", where="d='mobile'"),)))
        cache.add(SS(name="geo_x_device", cross_product=("geo", "device")))
        pre_state = dict(cache._slices)
        with pytest.raises(SpecValidationError) as exc_info:
            cache.remove("slice", "geo")
        assert "geo_x_device" in str(exc_info.value)
        assert cache._slices == pre_state
        assert "geo" in cache._slices


class TestSpecCacheAddCompositeValidation:
    """Tests for add()'s cross-reference validation on composite slices."""

    def test_add_composite_slice_rejects_missing_ref(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="geo", values=(SV(name="USA", where="country='USA'"),)))
        pre_state = dict(cache._slices)
        with pytest.raises(SpecValidationError) as exc_info:
            cache.add(SS(name="composite", cross_product=("geo", "missing")))
        assert "missing" in str(exc_info.value)
        assert cache._slices == pre_state
        assert "composite" not in cache._slices

    def test_add_composite_slice_rejects_already_composite_ref(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="leaf1", values=(SV(name="a", where="x=1"),)))
        cache.add(SS(name="leaf2", values=(SV(name="b", where="x=2"),)))
        cache.add(SS(name="nested", cross_product=("leaf1", "leaf2")))
        pre_state = dict(cache._slices)
        with pytest.raises(SpecValidationError) as exc_info:
            cache.add(SS(name="outer", cross_product=("leaf1", "nested")))
        assert "nested" in str(exc_info.value)
        assert cache._slices == pre_state
        assert "outer" not in cache._slices


class TestSpecCacheVersion:
    """Tests for SpecCache.version — the monotonic mutation-signal counter."""

    def test_version_starts_at_zero(self):
        assert SpecCache().version == 0

    def test_version_increments_on_add(self):
        cache = SpecCache()
        spec = MetricSpec(name="m", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts")
        cache.add(spec)
        assert cache.version == 1

    def test_version_increments_on_update(self):
        cache = SpecCache()
        spec = MetricSpec(name="m", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts")
        cache.add(spec)
        cache.update(spec)
        assert cache.version == 2

    def test_version_increments_on_remove(self):
        cache = SpecCache()
        spec = MetricSpec(name="m", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts")
        cache.add(spec)
        cache.remove("metric", "m")
        assert cache.version == 2

    def test_version_increments_on_clear(self):
        cache = SpecCache()
        cache.clear()
        assert cache.version == 1

    def test_version_does_not_increment_on_add_duplicate_rejection(self):
        cache = SpecCache()
        spec = MetricSpec(name="m", source="duckdb://db/t", numerator="SUM(x)", timestamp_col="ts")
        cache.add(spec)
        version_after_add = cache.version
        with pytest.raises(SpecValidationError):
            cache.add(spec)
        assert cache.version == version_after_add

    def test_version_does_not_increment_on_update_rollback(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="leaf", values=(SV(name="a", where="x=1"),)))
        cache.add(SS(name="composite", cross_product=("leaf", "leaf")))
        version_before = cache.version
        with pytest.raises(SpecValidationError):
            cache.update(SS(name="composite", cross_product=("leaf", "missing")))
        assert cache.version == version_before

    def test_version_does_not_increment_on_remove_rollback(self):
        from aitaem.specs.slice import SliceSpec as SS, SliceValue as SV

        cache = SpecCache()
        cache.add(SS(name="geo", values=(SV(name="USA", where="country='USA'"),)))
        cache.add(SS(name="device", values=(SV(name="mobile", where="d='mobile'"),)))
        cache.add(SS(name="geo_x_device", cross_product=("geo", "device")))
        version_before = cache.version
        with pytest.raises(SpecValidationError):
            cache.remove("slice", "geo")
        assert cache.version == version_before


class TestSpecNameIdentifierValidation:
    """Integration tests: invalid spec names are rejected at SpecCache load time."""

    INVALID_METRIC_YAML = """
metric:
  name: "my invalid metric"
  source: duckdb://db/t
  numerator: "SUM(x)"
  timestamp_col: ts
"""

    INVALID_SLICE_YAML = """
slice:
  name: "geo region"
  values:
    - name: US
      where: "country = 'US'"
"""

    INVALID_SEGMENT_YAML = """
segment:
  name: "customer value tier"
  source: duckdb://db/t
  values:
    - name: high
      where: "ltv > 100"
"""

    def test_from_string_rejects_metric_with_invalid_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(metric_yaml=self.INVALID_METRIC_YAML)
        assert any("not a valid SQL identifier" in e.message for e in exc_info.value.errors)

    def test_from_string_rejects_slice_with_invalid_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(slice_yaml=self.INVALID_SLICE_YAML)
        assert any("not a valid SQL identifier" in e.message for e in exc_info.value.errors)

    def test_from_string_rejects_segment_with_invalid_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(segment_yaml=self.INVALID_SEGMENT_YAML)
        assert any("not a valid SQL identifier" in e.message for e in exc_info.value.errors)

    def test_from_yaml_rejects_metric_with_invalid_name(self, tmp_path):
        from aitaem.utils.exceptions import SpecValidationError

        (tmp_path / "m.yaml").write_text(self.INVALID_METRIC_YAML)
        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_yaml(metric_paths=[tmp_path])
        assert any("not a valid SQL identifier" in e.message for e in exc_info.value.errors)

    def test_from_yaml_rejects_slice_with_invalid_name(self, tmp_path):
        from aitaem.utils.exceptions import SpecValidationError

        (tmp_path / "s.yaml").write_text(self.INVALID_SLICE_YAML)
        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_yaml(slice_paths=[tmp_path])
        assert any("not a valid SQL identifier" in e.message for e in exc_info.value.errors)

    def test_error_message_includes_bad_name(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(slice_yaml=self.INVALID_SLICE_YAML)
        messages = [e.message for e in exc_info.value.errors]
        assert any("geo region" in m for m in messages)

    def test_error_includes_suggestion(self):
        from aitaem.utils.exceptions import SpecValidationError

        with pytest.raises(SpecValidationError) as exc_info:
            SpecCache.from_string(slice_yaml=self.INVALID_SLICE_YAML)
        suggestions = [e.suggestion for e in exc_info.value.errors if e.suggestion]
        assert any("geo_region" in s for s in suggestions)

    def test_hyphen_in_name_is_rejected(self):
        from aitaem.utils.exceptions import SpecValidationError

        yaml = VALID_METRIC_RATIO_YAML.replace("homepage_ctr", "homepage-ctr")
        with pytest.raises(SpecValidationError):
            SpecCache.from_string(metric_yaml=yaml)

    def test_name_starting_with_digit_is_rejected(self):
        from aitaem.utils.exceptions import SpecValidationError

        yaml = VALID_METRIC_RATIO_YAML.replace("homepage_ctr", "2024_signups")
        with pytest.raises(SpecValidationError):
            SpecCache.from_string(metric_yaml=yaml)

    def test_valid_names_still_load_successfully(self):
        cache = SpecCache.from_string(
            metric_yaml=VALID_METRIC_RATIO_YAML,
            slice_yaml=VALID_SLICE_YAML,
            segment_yaml=VALID_SEGMENT_YAML,
        )
        assert "homepage_ctr" in cache.metrics
        assert "geography" in cache.slices
        assert "customer_value_tier" in cache.segments


def _metric(name: str, numerator: str = "SUM(x)") -> MetricSpec:
    return MetricSpec(name=name, source="duckdb://db/t", numerator=numerator, timestamp_col="ts")


class TestSpecCacheFromYamlFsspec:
    """SpecCache.from_yaml on fsspec URLs (memory://)."""

    def test_loads_file_url(self, memfs):
        memfs.pipe("/specs/ctr.yaml", VALID_METRIC_RATIO_YAML.encode())
        cache = SpecCache.from_yaml(metric_paths="memory://specs/ctr.yaml")
        assert list(cache.metrics) == ["homepage_ctr"]

    def test_loads_directory_url_yaml_and_yml(self, memfs):
        memfs.pipe("/specs/a.yaml", VALID_METRIC_RATIO_YAML.encode())
        memfs.pipe("/specs/b.yml", VALID_METRIC_SUM_YAML.encode())
        memfs.pipe("/specs/notes.txt", b"not a spec")
        cache = SpecCache.from_yaml(metric_paths="memory://specs/")
        assert set(cache.metrics) == {"homepage_ctr", "total_revenue"}

    def test_accepts_path_objects(self, tmp_path):
        (tmp_path / "a.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache.from_yaml(metric_paths=[tmp_path])
        assert list(cache.metrics) == ["homepage_ctr"]

    def test_missing_url_raises_file_not_found(self, memfs):
        with pytest.raises(FileNotFoundError, match="memory://missing"):
            SpecCache.from_yaml(metric_paths="memory://missing")

    def test_duplicate_name_across_url_files_raises(self, memfs):
        memfs.pipe("/one/a.yaml", VALID_METRIC_RATIO_YAML.encode())
        memfs.pipe("/two/b.yaml", VALID_METRIC_RATIO_YAML.encode())
        with pytest.raises(SpecValidationError, match="Duplicate spec name"):
            SpecCache.from_yaml(metric_paths=["memory://one/a.yaml", "memory://two"])


class TestSpecCacheSave:
    """SpecCache.save(): each directory mirrors the cache."""

    @staticmethod
    def _full_cache() -> SpecCache:
        return SpecCache.from_string(
            metric_yaml=[VALID_METRIC_RATIO_YAML, VALID_METRIC_SUM_YAML],
            slice_yaml=VALID_SLICE_YAML,
            segment_yaml=VALID_SEGMENT_YAML,
        )

    def _assert_round_trip(self, cache: SpecCache, m: str, s: str, g: str) -> None:
        cache.save(metric_dir=m, slice_dir=s, segment_dir=g)
        reloaded = SpecCache.from_yaml(metric_paths=m, slice_paths=s, segment_paths=g)
        assert dict(reloaded.metrics) == dict(cache.metrics)
        assert dict(reloaded.slices) == dict(cache.slices)
        assert dict(reloaded.segments) == dict(cache.segments)

    def test_round_trip_local_creates_directories(self, tmp_path):
        dirs = [tmp_path / "new" / t for t in ("metrics", "slices", "segments")]
        self._assert_round_trip(self._full_cache(), *[str(d) for d in dirs])
        metric_files = sorted(p.name for p in dirs[0].iterdir())
        assert metric_files == ["homepage_ctr.yaml", "total_revenue.yaml"]

    def test_round_trip_memory(self, memfs):
        self._assert_round_trip(
            self._full_cache(), "memory://s/metrics", "memory://s/slices", "memory://s/segments"
        )

    def test_none_skips_type(self, tmp_path):
        slices = tmp_path / "slices"
        slices.mkdir()
        (slices / "old.yaml").write_text(VALID_SLICE_YAML)
        self._full_cache().save(metric_dir=tmp_path / "metrics")
        assert (tmp_path / "metrics").is_dir()
        assert [p.name for p in slices.iterdir()] == ["old.yaml"]
        assert not (tmp_path / "segments").exists()

    def test_save_after_update_overwrites_file(self, tmp_path):
        cache = SpecCache()
        cache.add(_metric("m"))
        cache.save(metric_dir=tmp_path)
        cache.update(_metric("m", numerator="SUM(y)"))
        cache.save(metric_dir=tmp_path)
        assert SpecCache.from_yaml(metric_paths=tmp_path).get_metric("m").numerator == "SUM(y)"

    def test_save_after_remove_deletes_file(self, tmp_path):
        cache = SpecCache()
        cache.add(_metric("a"))
        cache.add(_metric("b"))
        cache.save(metric_dir=tmp_path)
        cache.remove("metric", "b")
        cache.save(metric_dir=tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["a.yaml"]
        assert list(SpecCache.from_yaml(metric_paths=tmp_path).metrics) == ["a"]

    def test_other_yaml_files_deleted_non_yaml_kept(self, tmp_path):
        (tmp_path / "legacy.yml").write_text(VALID_METRIC_SUM_YAML)
        (tmp_path / "other.yaml").write_text(VALID_METRIC_RATIO_YAML)
        (tmp_path / "notes.txt").write_text("keep me")
        cache = SpecCache()
        cache.add(_metric("m"))
        cache.save(metric_dir=tmp_path)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["m.yaml", "notes.txt"]

    def test_other_yaml_files_deleted_memory(self, memfs):
        memfs.pipe("/d/legacy.yml", b"x")
        memfs.pipe("/d/notes.txt", b"x")
        cache = SpecCache()
        cache.add(_metric("m"))
        cache.save(metric_dir="memory://d")
        assert sorted(memfs.ls("/d", detail=False)) == ["/d/m.yaml", "/d/notes.txt"]

    def test_case_variant_file_raises_file_exists(self, memfs):
        # memory:// is case-sensitive, so the original file is observable on any OS.
        memfs.pipe("/d/CTR.yaml", b"original")
        memfs.pipe("/d/other.yaml", b"other")
        cache = SpecCache()
        cache.add(_metric("ctr"))
        with pytest.raises(FileExistsError, match="CTR.yaml"):
            cache.save(metric_dir="memory://d")
        assert sorted(memfs.ls("/d", detail=False)) == ["/d/CTR.yaml", "/d/other.yaml"]
        assert memfs.cat("/d/CTR.yaml") == b"original"
        assert "ctr" in cache.metrics

    def test_invalid_spec_raises_before_any_write(self, tmp_path):
        cache = SpecCache()
        cache.add(_metric("good"))
        cache.add(SliceSpec(name="1 bad", column="c"))
        (tmp_path / "metrics").mkdir()
        (tmp_path / "metrics" / "old.yaml").write_text(VALID_METRIC_SUM_YAML)
        with pytest.raises(SpecValidationError) as exc:
            cache.save(metric_dir=tmp_path / "metrics", slice_dir=tmp_path / "slices")
        assert exc.value.spec_type == "slice"
        assert [p.name for p in (tmp_path / "metrics").iterdir()] == ["old.yaml"]
        assert not (tmp_path / "slices").exists()

    def test_path_traversal_name_rejected(self, tmp_path):
        target = tmp_path / "metrics"
        cache = SpecCache()
        cache.add(_metric("../x"))
        with pytest.raises(SpecValidationError):
            cache.save(metric_dir=target)
        assert list(tmp_path.iterdir()) == []

    def test_case_duplicate_names_rejected(self, tmp_path):
        (tmp_path / "old.yaml").write_text(VALID_METRIC_SUM_YAML)
        cache = SpecCache()
        cache.add(_metric("Ctr"))
        cache.add(_metric("ctr"))
        with pytest.raises(SpecValidationError, match="differ only in case"):
            cache.save(metric_dir=tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["old.yaml"]
