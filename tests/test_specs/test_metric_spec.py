"""Tests for MetricSpec."""

import pytest

from aitaem.specs.metric import MetricSpec
from aitaem.utils.exceptions import SpecValidationError


class TestMetricSpecFromYamlString:
    def test_valid_ratio_metric(self, valid_metric_ratio_yaml):
        spec = MetricSpec.from_yaml(valid_metric_ratio_yaml)
        assert spec.name == "homepage_ctr"
        assert spec.source == "duckdb://analytics.db/events"
        assert "SUM" in spec.numerator
        assert spec.denominator is not None
        assert "SUM" in spec.denominator
        assert spec.description == "Click-through rate"

    def test_valid_sum_metric(self, valid_metric_sum_yaml):
        spec = MetricSpec.from_yaml(valid_metric_sum_yaml)
        assert spec.name == "total_revenue"
        assert spec.denominator is None
        assert spec.description == ""

    def test_missing_name_raises(self):
        yaml_str = """
metric:
  source: duckdb://db/tbl
  numerator: "SUM(a)"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert exc_info.value.spec_type == "metric"
        assert any(e.field == "name" for e in exc_info.value.errors)

    def test_missing_source_raises(self):
        yaml_str = """
metric:
  name: foo
  numerator: "SUM(a)"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "source" for e in exc_info.value.errors)

    def test_invalid_source_uri_raises(self):
        yaml_str = """
metric:
  name: foo
  source: events
  numerator: "SUM(a)"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "source" for e in exc_info.value.errors)

    def test_malformed_numerator_sql_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  numerator: "SUM(amount"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "numerator" for e in exc_info.value.errors)

    def test_malformed_denominator_sql_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  numerator: "SUM(a)"
  denominator: "SUM(b"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "denominator" for e in exc_info.value.errors)

    def test_empty_numerator_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  numerator: ""
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "numerator" for e in exc_info.value.errors)

    def test_numerator_without_aggregate_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  numerator: "amount"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "numerator" for e in exc_info.value.errors)

    def test_denominator_without_aggregate_raises(self):
        yaml_str = """
metric:
  name: foo
  source: duckdb://db/tbl
  numerator: "SUM(clicks)"
  denominator: "impressions"
  timestamp_col: created_at
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "denominator" for e in exc_info.value.errors)

    def test_denominator_with_aggregate_is_valid(self):
        yaml_str = """
metric:
  name: ctr
  source: duckdb://db/tbl
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: created_at
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert spec.denominator == "SUM(impressions)"

    def test_count_star_is_valid(self):
        yaml_str = """
metric:
  name: event_count
  source: duckdb://db/tbl
  numerator: "COUNT(*)"
  timestamp_col: created_at
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert "COUNT" in spec.numerator

    def test_count_distinct_is_valid(self):
        yaml_str = """
metric:
  name: unique_users
  source: duckdb://db/tbl
  numerator: "COUNT(DISTINCT user_id)"
  timestamp_col: created_at
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert "COUNT" in spec.numerator

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

    def test_denominator_present_is_stored(self):
        yaml_str = """
metric:
  name: ctr
  source: duckdb://db/tbl
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: created_at
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert spec.denominator == "SUM(impressions)"


class TestMetricSpecFromFile:
    def test_load_ratio_from_file(self, fixtures_dir):
        spec = MetricSpec.from_yaml(fixtures_dir / "valid_metric_ratio.yaml")
        assert spec.name == "homepage_ctr"
        assert spec.denominator is not None

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

    def test_invalid_no_aggregate_in_numerator_file(self, fixtures_dir):
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(fixtures_dir / "invalid_metric_no_aggregate_in_numerator.yaml")
        assert any(e.field == "numerator" for e in exc_info.value.errors)


class TestMetricSpecEntities:
    def test_entities_absent_defaults_to_none(self, valid_metric_sum_yaml):
        spec = MetricSpec.from_yaml(valid_metric_sum_yaml)
        assert spec.entities is None

    def test_entities_single_column(self):
        yaml_str = """
metric:
  name: revenue
  source: duckdb://db/tbl
  numerator: "SUM(amount)"
  timestamp_col: event_ts
  entities: [user_id]
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert spec.entities == ["user_id"]

    def test_entities_multiple_columns(self):
        yaml_str = """
metric:
  name: revenue
  source: duckdb://db/tbl
  numerator: "SUM(amount)"
  timestamp_col: event_ts
  entities: [user_id, device_id]
"""
        spec = MetricSpec.from_yaml(yaml_str)
        assert spec.entities == ["user_id", "device_id"]

    def test_entities_empty_list_raises(self):
        yaml_str = """
metric:
  name: revenue
  source: duckdb://db/tbl
  numerator: "SUM(amount)"
  timestamp_col: event_ts
  entities: []
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any(e.field == "entities" for e in exc_info.value.errors)

    def test_entities_blank_entry_raises(self):
        yaml_str = """
metric:
  name: revenue
  source: duckdb://db/tbl
  numerator: "SUM(amount)"
  timestamp_col: event_ts
  entities: [""]
"""
        with pytest.raises(SpecValidationError) as exc_info:
            MetricSpec.from_yaml(yaml_str)
        assert any("entities" in e.field for e in exc_info.value.errors)

    def test_validate_with_entities(self):
        spec = MetricSpec(
            name="revenue",
            source="duckdb://db/tbl",
            numerator="SUM(amount)",
            timestamp_col="event_ts",
            entities=["user_id", "device_id"],
        )
        result = spec.validate()
        assert result.valid is True

    def test_validate_without_entities(self):
        spec = MetricSpec(
            name="revenue",
            source="duckdb://db/tbl",
            numerator="SUM(amount)",
            timestamp_col="event_ts",
        )
        result = spec.validate()
        assert result.valid is True


class TestMetricSpecValidate:
    def test_validate_returns_result_on_valid(self, valid_metric_ratio_yaml):
        spec = MetricSpec.from_yaml(valid_metric_ratio_yaml)
        result = spec.validate()
        assert result.valid is True
        assert result.errors == []

    def test_validate_does_not_raise(self):
        spec = MetricSpec(
            name="foo",
            source="duckdb://db/tbl",
            numerator="SUM(a)",
            timestamp_col="created_at",
        )
        result = spec.validate()
        assert result.valid is True

    def test_spec_is_frozen(self, valid_metric_ratio_yaml):
        spec = MetricSpec.from_yaml(valid_metric_ratio_yaml)
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "changed"  # type: ignore
