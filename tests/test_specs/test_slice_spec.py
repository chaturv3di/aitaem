"""Tests for SliceSpec."""

import pytest

from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.utils.exceptions import SpecValidationError
from tests.test_specs.conftest import FIXTURES_DIR


class TestSliceSpecFromYamlString:
    def test_valid_slice_all_fields(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        assert spec.name == "geography"
        assert spec.description == "Regional breakdown"
        assert isinstance(spec.values, tuple)
        assert len(spec.values) == 2

    def test_values_are_slice_value_instances(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        for v in spec.values:
            assert isinstance(v, SliceValue)

    def test_slice_values_content(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        assert spec.values[0].name == "North America"
        assert "US" in spec.values[0].where
        assert spec.values[1].name == "Europe"

    def test_empty_values_raises(self):
        yaml_str = """
slice:
  name: foo
  values: []
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(e.field == "values" for e in exc_info.value.errors)

    def test_value_missing_name_raises(self):
        yaml_str = """
slice:
  name: foo
  values:
    - where: "x = 1"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("name" in e.field for e in exc_info.value.errors)

    def test_value_missing_where_raises(self):
        yaml_str = """
slice:
  name: foo
  values:
    - name: bar
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("where" in e.field for e in exc_info.value.errors)

    def test_duplicate_value_names_raises(self):
        yaml_str = """
slice:
  name: foo
  values:
    - name: North America
      where: "x = 1"
    - name: North America
      where: "x = 2"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("duplicate" in e.message.lower() for e in exc_info.value.errors)

    def test_malformed_where_sql_raises(self):
        yaml_str = """
slice:
  name: foo
  values:
    - name: bar
      where: "country_code IN ('US'"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("where" in e.field for e in exc_info.value.errors)

    def test_missing_top_level_slice_key_raises(self):
        yaml_str = """
name: foo
values: []
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert "slice" in str(exc_info.value).lower()

    def test_missing_name_raises(self):
        yaml_str = """
slice:
  values:
    - name: bar
      where: "x = 1"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(e.field == "name" for e in exc_info.value.errors)

    def test_description_defaults_to_empty(self):
        yaml_str = """
slice:
  name: foo
  values:
    - name: bar
      where: "x = 1"
"""
        spec = SliceSpec.from_yaml(yaml_str)
        assert spec.description == ""

    def test_invalid_yaml_syntax_raises(self):
        with pytest.raises(SpecValidationError):
            SliceSpec.from_yaml("slice:\n  name: [unclosed")

    def test_nonexistent_path_object_raises_file_not_found(self, tmp_path):
        from pathlib import Path

        with pytest.raises(FileNotFoundError):
            SliceSpec.from_yaml(tmp_path / "nonexistent.yaml")

    def test_slice_value_not_a_mapping_raises(self):
        yaml_str = "slice: not_a_dict"
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert "mapping" in str(exc_info.value).lower() or any(
            "mapping" in e.message.lower() for e in exc_info.value.errors
        )

    def test_unknown_fields_ignored(self):
        yaml_str = """
slice:
  name: foo
  extra_field: ignored
  values:
    - name: bar
      where: "x = 1"
"""
        spec = SliceSpec.from_yaml(yaml_str)
        assert spec.name == "foo"


class TestSliceSpecFromFile:
    def test_load_from_file(self, fixtures_dir):
        spec = SliceSpec.from_yaml(fixtures_dir / "valid_slice.yaml")
        assert spec.name == "geography"
        assert len(spec.values) == 2


class TestSliceSpecValidate:
    def test_validate_returns_valid_result(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        result = spec.validate()
        assert result.valid is True
        assert result.errors == []

    def test_spec_is_frozen(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "changed"  # type: ignore

    def test_values_tuple_is_immutable(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        assert isinstance(spec.values, tuple)
