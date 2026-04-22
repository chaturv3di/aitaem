"""Tests for aitaem.utils.yaml_validation.load_yaml_spec_dict."""

import sys

import pytest

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.yaml_validation import load_yaml_spec_dict

# Minimal valid YAML for each spec type used across tests
VALID_METRIC_YAML = """
metric:
  name: test_metric
  source: duckdb://db/tbl
  numerator: "SUM(amount)"
  timestamp_col: created_at
"""

VALID_SLICE_YAML = """
slice:
  name: test_slice
  values:
    - name: A
      where: "col = 'a'"
"""

VALID_SEGMENT_YAML = """
segment:
  name: test_segment
  source: duckdb://db/tbl
  values:
    - name: high
      where: "val > 100"
"""


class TestLoadYamlSpecDictFromPath:
    def test_valid_path_object_returns_dict(self, tmp_path):
        f = tmp_path / "metric.yaml"
        f.write_text(VALID_METRIC_YAML, encoding="utf-8")
        result = load_yaml_spec_dict(f, "metric")
        assert result["name"] == "test_metric"

    def test_valid_file_string_returns_dict(self, tmp_path):
        f = tmp_path / "metric.yaml"
        f.write_text(VALID_METRIC_YAML, encoding="utf-8")
        result = load_yaml_spec_dict(str(f), "metric")
        assert result["name"] == "test_metric"

    def test_nonexistent_path_object_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_yaml_spec_dict(tmp_path / "missing.yaml", "metric")

    def test_nonexistent_file_string_treated_as_yaml_content(self, tmp_path):
        # A string path to a non-existent file cannot be distinguished from
        # arbitrary YAML content — it is treated as YAML (and fails to parse).
        # Only an explicit Path object forces file-path semantics.
        missing = str(tmp_path / "missing.yaml")
        with pytest.raises(SpecValidationError):
            load_yaml_spec_dict(missing, "metric")


class TestLoadYamlSpecDictFromString:
    def test_valid_yaml_string_returns_dict(self):
        result = load_yaml_spec_dict(VALID_METRIC_YAML, "metric")
        assert result["name"] == "test_metric"

    def test_yaml_string_exceeding_path_max_returns_dict(self):
        # Pad description to push the full YAML string well beyond PATH_MAX.
        # PATH_MAX is typically 4096 on Linux and 1024 on some systems.
        padding = "x" * (sys.maxsize if sys.maxsize < 10_000 else 5_000)
        yaml_str = f"""
metric:
  name: test_metric
  source: duckdb://db/tbl
  numerator: "SUM(amount)"
  timestamp_col: created_at
  description: "{padding}"
"""
        result = load_yaml_spec_dict(yaml_str, "metric")
        assert result["name"] == "test_metric"

    def test_empty_string_raises_spec_validation_error(self):
        with pytest.raises(SpecValidationError):
            load_yaml_spec_dict("", "metric")

    def test_whitespace_only_raises_spec_validation_error(self):
        with pytest.raises(SpecValidationError):
            load_yaml_spec_dict("   \n  ", "metric")

    def test_malformed_yaml_raises_spec_validation_error(self):
        with pytest.raises(SpecValidationError) as exc_info:
            load_yaml_spec_dict("metric:\n  name: [unclosed", "metric")
        assert any(e.field == "yaml" for e in exc_info.value.errors)

    def test_missing_top_level_key_raises_spec_validation_error(self):
        yaml_str = "name: foo\nsource: duckdb://db/tbl\n"
        with pytest.raises(SpecValidationError) as exc_info:
            load_yaml_spec_dict(yaml_str, "metric")
        assert exc_info.value.spec_type == "metric"
        assert any(e.field == "yaml" for e in exc_info.value.errors)

    def test_wrong_top_level_key_raises_spec_validation_error(self):
        with pytest.raises(SpecValidationError) as exc_info:
            load_yaml_spec_dict(VALID_SLICE_YAML, "metric")
        assert "metric" in str(exc_info.value)

    def test_top_level_value_not_dict_raises_spec_validation_error(self):
        yaml_str = "metric: just_a_string\n"
        with pytest.raises(SpecValidationError) as exc_info:
            load_yaml_spec_dict(yaml_str, "metric")
        assert any(e.field == "metric" for e in exc_info.value.errors)

    def test_spec_type_name_propagated_in_error(self):
        with pytest.raises(SpecValidationError) as exc_info:
            load_yaml_spec_dict("", "slice")
        assert exc_info.value.spec_type == "slice"

    @pytest.mark.parametrize("spec_type,yaml_str", [
        ("metric", VALID_METRIC_YAML),
        ("slice", VALID_SLICE_YAML),
        ("segment", VALID_SEGMENT_YAML),
    ])
    def test_correct_dict_returned_for_each_spec_type(self, spec_type, yaml_str):
        result = load_yaml_spec_dict(yaml_str, spec_type)
        assert result["name"] == f"test_{spec_type}"
