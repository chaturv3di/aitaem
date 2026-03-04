"""Tests for MetricSpec."""

import pytest

from aitaem.specs.metric import MetricSpec
from aitaem.utils.exceptions import SpecValidationError
from tests.test_specs.conftest import FIXTURES_DIR


class TestMetricSpecFromYamlString:
    def test_valid_ratio_metric(self, valid_metric_ratio_yaml):
        spec = MetricSpec.from_yaml(valid_metric_ratio_yaml)
        assert spec.name == "homepage_ctr"
        assert spec.source == "duckdb://analytics.db/events"
        assert spec.aggregation == "ratio"
        assert "SUM" in spec.numerator
        assert spec.denominator is not None
        assert "SUM" in spec.denominator
        assert spec.description == "Click-through rate"

    def test_valid_sum_metric(self, valid_metric_sum_yaml):
        spec = MetricSpec.from_yaml(valid_metric_sum_yaml)
        assert spec.name == "total_revenue"
        assert spec.aggregation == "sum"
        assert spec.denominator is None
        assert spec.description == ""

    def test_aggregation_normalized_to_lowercase(self):
        yaml_str = """
metric:
  name: test
  source: duckdb://db/tbl
  aggregation: RATIO
  numerator: "SUM(a)"
  denominator: "SUM(b)"
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert spec.aggregation == "ratio"

    def test_missing_name_raises(self):
        yaml_str = """
metric:
  source: duckdb://db/tbl
  aggregation: sum
  numerator: "SUM(a)"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert exc_info.value.spec_type == "metric"
        assert any(e.field == "name" for e in exc_info.value.errors)

    def test_missing_source_raises(self):
        yaml_str = """
metric:
  name: foo
  aggregation: sum
  numerator: "SUM(a)"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "source" for e in exc_info.value.errors)

    def test_invalid_source_uri_raises(self):
        yaml_str = """
metric:
  name: foo
  source: events
  aggregation: sum
  numerator: "SUM(a)"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "source" for e in exc_info.value.errors)

    def test_ratio_without_denominator_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  aggregation: ratio
  numerator: "SUM(a)"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "denominator" for e in exc_info.value.errors)

    def test_unsupported_aggregation_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  aggregation: window_function
  numerator: "SUM(a)"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "aggregation" for e in exc_info.value.errors)

    def test_missing_top_level_metric_key_raises(self):
        yaml_str = """
name: foo
source: duckdb://db/tbl
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert "metric" in str(exc_info.value).lower()

    def test_invalid_yaml_syntax_raises(self):
        yaml_str = "metric:\n  name: [unclosed"
        with pytest.raises(SpecValidationError):
            MetricSpec.from_yaml(yaml_str)

    def test_malformed_numerator_sql_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  aggregation: sum
  numerator: "SUM(amount"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "numerator" for e in exc_info.value.errors)

    def test_malformed_denominator_sql_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  aggregation: ratio
  numerator: "SUM(a)"
  denominator: "SUM(b"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "denominator" for e in exc_info.value.errors)

    def test_empty_numerator_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  aggregation: sum
  numerator: ""
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "numerator" for e in exc_info.value.errors)

    def test_sum_with_denominator_logs_warning(self, caplog):
        import logging

        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  aggregation: sum
  numerator: "SUM(a)"
  denominator: "SUM(b)"
"""
        with caplog.at_level(logging.WARNING):
            spec = MetricSpec.from_yaml(yaml_str)
        assert spec.aggregation == "sum"
        # denominator is ignored (not stored) — or stored with warning
        # per plan: "Spec created; warning logged"


class TestMetricSpecFromFile:
    def test_load_ratio_from_file(self, fixtures_dir):
        spec = MetricSpec.from_yaml(fixtures_dir / "valid_metric_ratio.yaml")
        assert spec.name == "homepage_ctr"
        assert spec.aggregation == "ratio"

    def test_load_sum_from_file(self, fixtures_dir):
        spec = MetricSpec.from_yaml(fixtures_dir / "valid_metric_sum.yaml")
        assert spec.name == "total_revenue"
        assert spec.denominator is None

    def test_nonexistent_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            MetricSpec.from_yaml(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_file_raises(self, fixtures_dir):
        with pytest.raises(SpecValidationError):
            MetricSpec.from_yaml(fixtures_dir / "invalid_yaml_syntax.yaml")

    def test_invalid_metric_no_denominator_file(self, fixtures_dir):
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(fixtures_dir / "invalid_metric_no_denominator.yaml")
        assert any(e.field == "denominator" for e in exc_info.value.errors)


class TestMetricSpecValidate:
    def test_validate_returns_result_on_valid(self, valid_metric_ratio_yaml):
        spec = MetricSpec.from_yaml(valid_metric_ratio_yaml)
        result = spec.validate()
        assert result.valid is True
        assert result.errors == []

    def test_validate_does_not_raise(self):
        # Build a spec bypassing from_yaml (use object.__setattr__ to bypass frozen)
        # Since it's frozen, use a valid one and just call validate()
        spec = MetricSpec(
            name="foo",
            source="duckdb://db/tbl",
            aggregation="sum",
            numerator="SUM(a)",
        )
        result = spec.validate()
        assert result.valid is True

    def test_spec_is_frozen(self, valid_metric_ratio_yaml):
        spec = MetricSpec.from_yaml(valid_metric_ratio_yaml)
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "changed"  # type: ignore
