"""Tests for SliceSpec."""

import pytest

from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.utils.exceptions import SpecValidationError


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


class TestCompositeSliceSpec:
    """Tests for composite SliceSpec (cross_product field)."""

    COMPOSITE_YAML = """
slice:
  name: geo_x_device
  description: Cross of geo and device
  cross_product:
    - geography
    - device
"""

    def test_composite_from_yaml(self):
        spec = SliceSpec.from_yaml(self.COMPOSITE_YAML)
        assert spec.name == "geo_x_device"
        assert spec.is_composite is True
        assert spec.cross_product == ("geography", "device")
        assert spec.values == ()

    def test_leaf_spec_is_not_composite(self, valid_slice_yaml):
        spec = SliceSpec.from_yaml(valid_slice_yaml)
        assert spec.is_composite is False
        assert spec.cross_product == ()

    def test_composite_description_stored(self):
        spec = SliceSpec.from_yaml(self.COMPOSITE_YAML)
        assert spec.description == "Cross of geo and device"

    def test_composite_validate_returns_valid(self):
        spec = SliceSpec.from_yaml(self.COMPOSITE_YAML)
        result = spec.validate()
        assert result.valid is True
        assert result.errors == []

    def test_both_values_and_cross_product_raises(self):
        yaml_str = """
slice:
  name: conflict
  values:
    - name: USA
      where: "country = 'USA'"
  cross_product:
    - geo
    - device
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(
            "cross_product" in e.message.lower() or "values" in e.message.lower()
            for e in exc_info.value.errors
        )

    def test_cross_product_with_one_item_raises(self):
        yaml_str = """
slice:
  name: bad_composite
  cross_product:
    - geo
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("cross_product" in e.field for e in exc_info.value.errors)

    def test_cross_product_with_empty_list_raises(self):
        yaml_str = """
slice:
  name: bad_composite
  cross_product: []
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("cross_product" in e.field for e in exc_info.value.errors)

    def test_cross_product_duplicate_entry_raises(self):
        yaml_str = """
slice:
  name: dup_composite
  cross_product:
    - geo
    - geo
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("cross_product" in e.field for e in exc_info.value.errors)

    def test_neither_values_nor_cross_product_raises(self):
        yaml_str = """
slice:
  name: no_content
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any("values" in e.field for e in exc_info.value.errors)


class TestWildcardSliceSpec:
    """Tests for wildcard SliceSpec (top-level where: column_name)."""

    WILDCARD_YAML = """
slice:
  name: industry
  where: industry
"""

    WILDCARD_YAML_WITH_DESC = """
slice:
  name: industry
  where: industry
  description: "Breakdown by industry (auto-populated)"
"""

    WILDCARD_YAML_DOT_QUALIFIED = """
slice:
  name: country
  where: public.campaigns.country
"""

    def test_wildcard_parses_column(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML)
        assert spec.name == "industry"
        assert spec.column == "industry"
        assert spec.is_wildcard is True

    def test_wildcard_values_and_cross_product_are_empty(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML)
        assert spec.values == ()
        assert spec.cross_product == ()

    def test_wildcard_is_not_composite(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML)
        assert spec.is_composite is False

    def test_wildcard_description_stored(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML_WITH_DESC)
        assert spec.description == "Breakdown by industry (auto-populated)"

    def test_wildcard_description_defaults_to_empty(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML)
        assert spec.description == ""

    def test_wildcard_dot_qualified_column(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML_DOT_QUALIFIED)
        assert spec.column == "public.campaigns.country"
        assert spec.is_wildcard is True

    def test_wildcard_validate_returns_valid(self):
        spec = SliceSpec.from_yaml(self.WILDCARD_YAML)
        result = spec.validate()
        assert result.valid is True
        assert result.errors == []

    def test_wildcard_sql_expression_raises(self):
        yaml_str = """
slice:
  name: industry
  where: "industry = 'SaaS'"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(e.field == "where" for e in exc_info.value.errors)

    def test_wildcard_where_with_spaces_raises(self):
        yaml_str = """
slice:
  name: industry
  where: "col name"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(e.field == "where" for e in exc_info.value.errors)

    def test_wildcard_and_values_conflict_raises(self):
        yaml_str = """
slice:
  name: industry
  where: industry
  values:
    - name: SaaS
      where: "industry = 'SaaS'"
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(e.field in ("where", "values") for e in exc_info.value.errors)

    def test_wildcard_and_cross_product_conflict_raises(self):
        yaml_str = """
slice:
  name: industry
  where: industry
  cross_product:
    - geo
    - device
"""
        with pytest.raises(SpecValidationError) as exc_info:
            SliceSpec.from_yaml(yaml_str)
        assert any(e.field in ("where", "cross_product") for e in exc_info.value.errors)

    def test_leaf_spec_is_not_wildcard(self):
        yaml_str = """
slice:
  name: geo
  values:
    - name: USA
      where: "country = 'USA'"
"""
        spec = SliceSpec.from_yaml(yaml_str)
        assert spec.is_wildcard is False
        assert spec.column == ""

    def test_composite_spec_is_not_wildcard(self):
        yaml_str = """
slice:
  name: geo_x_device
  cross_product:
    - geo
    - device
"""
        spec = SliceSpec.from_yaml(yaml_str)
        assert spec.is_wildcard is False
        assert spec.column == ""
