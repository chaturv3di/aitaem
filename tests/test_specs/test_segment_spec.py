"""Tests for SegmentSpec."""

import pytest

from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.utils.exceptions import SpecValidationError


class TestSegmentSpecFromYamlString:
    def test_valid_segment_all_fields(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert spec.name == "customer_value_tier"
        assert spec.source == "duckdb://analytics.db/customers"
        assert spec.description == "Customer segmentation by value"
        assert isinstance(spec.values, tuple)
        assert len(spec.values) == 2

    def test_values_are_segment_value_instances(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        for v in spec.values:
            assert isinstance(v, SegmentValue)

    def test_segment_values_content(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert spec.values[0].name == "high_value"
        assert "lifetime_value" in spec.values[0].where
        assert spec.values[1].name == "low_value"

    def test_source_uri_stored_correctly(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert spec.source == "duckdb://analytics.db/customers"

    def test_description_optional_defaults_empty(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values:
    - name: tier_a
      where: "x > 100"
"""
        spec = SegmentSpec.from_yaml(yaml_str)
        assert spec.description == ""

    def test_missing_source_raises(self):
        yaml_str = """
segment:
  name: foo
  values:
    - name: tier_a
      where: "x > 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any(e.field == "source" for e in exc_info.value.errors)

    def test_invalid_source_uri_raises(self):
        yaml_str = """
segment:
  name: foo
  source: customers
  values:
    - name: tier_a
      where: "x > 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any(e.field == "source" for e in exc_info.value.errors)

    def test_empty_values_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values: []
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any(e.field == "values" for e in exc_info.value.errors)

    def test_value_missing_name_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values:
    - where: "x = 1"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any("name" in e.field for e in exc_info.value.errors)

    def test_value_missing_where_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values:
    - name: bar
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any("where" in e.field for e in exc_info.value.errors)

    def test_malformed_where_sql_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values:
    - name: tier_a
      where: "lifetime_value IN ('US'"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any("where" in e.field for e in exc_info.value.errors)

    def test_duplicate_value_names_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values:
    - name: tier_a
      where: "x > 100"
    - name: tier_a
      where: "x <= 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any("duplicate" in e.message.lower() for e in exc_info.value.errors)

    def test_missing_top_level_segment_key_raises(self):
        yaml_str = """
name: foo
source: duckdb://db/tbl
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert "segment" in str(exc_info.value).lower()

    def test_invalid_yaml_syntax_raises(self):
        with pytest.raises(SpecValidationError):
            SegmentSpec.from_yaml("segment:\n  name: [unclosed")

    def test_nonexistent_path_object_raises_file_not_found(self, tmp_path):

        with pytest.raises(FileNotFoundError):
            SegmentSpec.from_yaml(tmp_path / "nonexistent.yaml")

    def test_segment_value_not_a_mapping_raises(self):
        yaml_str = "segment: not_a_dict"
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert "mapping" in str(exc_info.value).lower() or any(
            "mapping" in e.message.lower() for e in exc_info.value.errors
        )

    def test_unknown_fields_ignored(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  extra_field: ignored
  values:
    - name: tier_a
      where: "x > 100"
"""
        spec = SegmentSpec.from_yaml(yaml_str)
        assert spec.name == "foo"


class TestSegmentSpecFromFile:
    def test_load_from_file(self, fixtures_dir):
        spec = SegmentSpec.from_yaml(fixtures_dir / "valid_segment.yaml")
        assert spec.name == "customer_value_tier"
        assert len(spec.values) == 2

    def test_invalid_segment_no_source_file(self, fixtures_dir):
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(fixtures_dir / "invalid_segment_no_source.yaml")
        assert any(e.field == "source" for e in exc_info.value.errors)


class TestSegmentSpecValidate:
    def test_validate_returns_valid_result(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        result = spec.validate()
        assert result.valid is True
        assert result.errors == []

    def test_spec_is_frozen(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "changed"  # type: ignore

    def test_values_tuple_is_immutable(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert isinstance(spec.values, tuple)
