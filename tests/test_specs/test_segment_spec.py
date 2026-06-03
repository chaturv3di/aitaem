"""Tests for SegmentSpec."""

import pytest

from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.utils.exceptions import SpecValidationError


class TestSegmentSpecFromYamlString:
    def test_valid_segment_all_fields(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert spec.name == "customer_value_tier"
        assert spec.source == "duckdb://analytics.db/dim_customers"
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
        assert spec.source == "duckdb://analytics.db/dim_customers"

    def test_description_optional_defaults_empty(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  entity_id: user_id
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
  entity_id: user_id
  extra_field: ignored
  values:
    - name: tier_a
      where: "x > 100"
"""
        spec = SegmentSpec.from_yaml(yaml_str)
        assert spec.name == "foo"


class TestSegmentSpecPathMax:
    def test_yaml_string_exceeding_path_max_loads_correctly(self):
        padding = "x" * 5_000
        yaml_str = f"""
segment:
  name: padded_segment
  source: duckdb://db/tbl
  entity_id: user_id
  description: "{padding}"
  values:
    - name: high
      where: "val > 100"
"""
        spec = SegmentSpec.from_yaml(yaml_str)
        assert spec.name == "padded_segment"
        assert len(spec.values) == 1


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

    def test_validate_referenced_columns_includes_entity_id_and_join_keys(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        result = spec.validate()
        assert result.valid is True
        assert result.referenced_columns is not None
        assert result.referenced_columns["entity_id"] == ["customer_id"]
        assert result.referenced_columns["join_keys"] == ["buyer_id", "seller_id"]

    def test_validate_referenced_columns_includes_where_columns(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        result = spec.validate()
        assert result.valid is True
        assert result.referenced_columns is not None
        assert "lifetime_value" in result.referenced_columns["values[0].where"]
        assert "customer_status" in result.referenced_columns["values[0].where"]

    def test_validate_referenced_columns_none_when_invalid(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values: []
"""
        from aitaem.utils.validation import validate_segment_spec
        result = validate_segment_spec({"name": "foo", "source": "duckdb://db/tbl", "values": []})
        assert result.valid is False
        assert result.referenced_columns is None


class TestSegmentSpecEntityId:
    def test_entity_id_stored_correctly(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert spec.entity_id == "customer_id"

    def test_join_keys_stored_as_tuple(self, valid_segment_yaml):
        spec = SegmentSpec.from_yaml(valid_segment_yaml)
        assert isinstance(spec.join_keys, tuple)
        assert spec.join_keys == ("buyer_id", "seller_id")

    def test_join_keys_optional_defaults_empty(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  entity_id: user_id
  values:
    - name: tier_a
      where: "x > 100"
"""
        spec = SegmentSpec.from_yaml(yaml_str)
        assert spec.join_keys == ()

    def test_missing_entity_id_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  values:
    - name: tier_a
      where: "x > 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any(e.field == "entity_id" for e in exc_info.value.errors)

    def test_entity_id_invalid_identifier_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  entity_id: "my id"
  values:
    - name: tier_a
      where: "x > 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any(e.field == "entity_id" for e in exc_info.value.errors)

    def test_join_keys_empty_list_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  entity_id: user_id
  join_keys: []
  values:
    - name: tier_a
      where: "x > 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any(e.field == "join_keys" for e in exc_info.value.errors)

    def test_join_keys_invalid_identifier_raises(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  entity_id: user_id
  join_keys: ["buyer id"]
  values:
    - name: tier_a
      where: "x > 100"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SegmentSpec.from_yaml(yaml_str)
        assert any("join_keys" in e.field for e in exc_info.value.errors)

    def test_referenced_columns_no_join_keys_key_when_absent(self):
        yaml_str = """
segment:
  name: foo
  source: duckdb://db/tbl
  entity_id: user_id
  values:
    - name: tier_a
      where: "x > 100"
"""
        spec = SegmentSpec.from_yaml(yaml_str)
        result = spec.validate()
        assert result.valid is True
        assert "join_keys" not in (result.referenced_columns or {})
