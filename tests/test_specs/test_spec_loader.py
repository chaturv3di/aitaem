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
    FIXTURES_DIR,
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
        cache = SpecCache(metric_paths=[tmp_path])
        spec = cache.get_metric("homepage_ctr")
        assert isinstance(spec, MetricSpec)
        assert spec.name == "homepage_ctr"

    def test_get_metric_returns_same_object_on_second_call(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache(metric_paths=[tmp_path])
        spec1 = cache.get_metric("homepage_ctr")
        spec2 = cache.get_metric("homepage_ctr")
        assert spec1 is spec2

    def test_get_slice_returns_correct_spec(self, tmp_path):
        (tmp_path / "slice.yaml").write_text(VALID_SLICE_YAML)
        cache = SpecCache(slice_paths=[tmp_path])
        spec = cache.get_slice("geography")
        assert isinstance(spec, SliceSpec)

    def test_get_segment_returns_correct_spec(self, tmp_path):
        (tmp_path / "segment.yaml").write_text(VALID_SEGMENT_YAML)
        cache = SpecCache(segment_paths=[tmp_path])
        spec = cache.get_segment("customer_value_tier")
        assert isinstance(spec, SegmentSpec)

    def test_unknown_metric_raises_spec_not_found(self, tmp_path):
        cache = SpecCache(metric_paths=[tmp_path])
        with pytest.raises(SpecNotFoundError) as exc_info:
            cache.get_metric("nonexistent")
        assert exc_info.value.spec_type == "metric"
        assert exc_info.value.name == "nonexistent"
        assert str(tmp_path) in exc_info.value.searched_paths

    def test_no_paths_configured_raises(self):
        cache = SpecCache()
        with pytest.raises(SpecNotFoundError) as exc_info:
            cache.get_metric("foo")
        assert exc_info.value.searched_paths == []

    def test_accepts_single_file_path(self, tmp_path):
        f = tmp_path / "metric.yaml"
        f.write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache(metric_paths=[f])
        spec = cache.get_metric("homepage_ctr")
        assert spec.name == "homepage_ctr"

    def test_clear_forces_reload(self, tmp_path):
        f = tmp_path / "metric.yaml"
        f.write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache(metric_paths=[tmp_path])
        spec1 = cache.get_metric("homepage_ctr")
        cache.clear()
        # Overwrite with a new spec
        f2 = tmp_path / "metric2.yaml"
        f2.write_text(VALID_METRIC_SUM_YAML)
        # After clear, cache should re-scan and pick up new file
        spec2 = cache.get_metric("total_revenue")
        assert spec2.name == "total_revenue"

    def test_lazy_load_no_io_at_init(self, tmp_path):
        # Ensure __init__ does no file I/O (accessing private state)
        cache = SpecCache(metric_paths=[tmp_path])
        assert cache._metrics is None

    def test_cache_populated_after_first_get(self, tmp_path):
        (tmp_path / "metric.yaml").write_text(VALID_METRIC_RATIO_YAML)
        cache = SpecCache(metric_paths=[tmp_path])
        cache.get_metric("homepage_ctr")
        assert cache._metrics is not None
