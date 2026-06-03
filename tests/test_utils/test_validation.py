"""Unit tests for aitaem.utils.validation — focusing on spec name identifier validation."""

import pytest

from aitaem.utils.validation import (
    _is_valid_spec_name,
    validate_metric_spec,
    validate_segment_spec,
    validate_slice_spec,
)

# ---------------------------------------------------------------------------
# Minimal valid spec dicts (only fields needed to avoid unrelated errors)
# ---------------------------------------------------------------------------

VALID_METRIC = {
    "name": "my_metric",
    "source": "duckdb://db/t",
    "numerator": "SUM(x)",
    "timestamp_col": "ts",
}

VALID_SLICE = {
    "name": "my_slice",
    "values": [{"name": "a", "where": "x = 1"}],
}

VALID_SEGMENT = {
    "name": "my_segment",
    "source": "duckdb://db/t",
    "values": [{"name": "a", "where": "x = 1"}],
}


# ---------------------------------------------------------------------------
# _is_valid_spec_name unit tests
# ---------------------------------------------------------------------------


class TestIsValidSpecName:
    @pytest.mark.parametrize(
        "name",
        [
            "my_metric",
            "MyMetric",
            "revenue",
            "_private",
            "_",
            "a",
            "A1",
            "snake_case_name",
            "ALLCAPS",
            "mixed123",
            "x1y2z3",
        ],
    )
    def test_valid_names(self, name):
        assert _is_valid_spec_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "english speaking countries",  # space
            "revenue-2024",  # hyphen
            "2024_signups",  # starts with digit
            "schema.metric",  # dot
            "my!metric",  # exclamation
            "my metric",  # space
            "a b",  # space
            "café",  # non-ASCII
            "my/metric",  # slash
            "",  # empty (edge)
        ],
    )
    def test_invalid_names(self, name):
        assert _is_valid_spec_name(name) is False


# ---------------------------------------------------------------------------
# validate_metric_spec — name field
# ---------------------------------------------------------------------------


class TestMetricSpecNameValidation:
    def test_valid_name_passes(self):
        result = validate_metric_spec(VALID_METRIC)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert name_errors == []

    @pytest.mark.parametrize(
        "bad_name",
        [
            "english speaking countries",
            "revenue-2024",
            "2024_signups",
            "Net Revenue",
            "my.metric",
        ],
    )
    def test_invalid_name_produces_error(self, bad_name):
        spec = {**VALID_METRIC, "name": bad_name}
        result = validate_metric_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert len(name_errors) == 1
        assert "not a valid SQL identifier" in name_errors[0].message
        assert bad_name in name_errors[0].message

    def test_invalid_name_includes_suggestion(self):
        spec = {**VALID_METRIC, "name": "my metric"}
        result = validate_metric_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert name_errors[0].suggestion is not None
        assert "my_metric" in name_errors[0].suggestion

    def test_missing_name_not_double_reported(self):
        spec = {**VALID_METRIC, "name": ""}
        result = validate_metric_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert len(name_errors) == 1
        assert "not a valid SQL identifier" not in name_errors[0].message


# ---------------------------------------------------------------------------
# validate_slice_spec — name field
# ---------------------------------------------------------------------------


class TestSliceSpecNameValidation:
    def test_valid_name_passes(self):
        result = validate_slice_spec(VALID_SLICE)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert name_errors == []

    @pytest.mark.parametrize(
        "bad_name",
        [
            "English speaking countries",
            "geo-region",
            "1st_slice",
            "slice type",
        ],
    )
    def test_invalid_name_produces_error(self, bad_name):
        spec = {**VALID_SLICE, "name": bad_name}
        result = validate_slice_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert len(name_errors) == 1
        assert "not a valid SQL identifier" in name_errors[0].message

    def test_invalid_name_includes_suggestion(self):
        spec = {**VALID_SLICE, "name": "geo region"}
        result = validate_slice_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert name_errors[0].suggestion is not None
        assert "geo_region" in name_errors[0].suggestion

    def test_missing_name_not_double_reported(self):
        spec = {**VALID_SLICE, "name": "   "}
        result = validate_slice_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert len(name_errors) == 1
        assert "not a valid SQL identifier" not in name_errors[0].message


# ---------------------------------------------------------------------------
# validate_segment_spec — name field
# ---------------------------------------------------------------------------


class TestSegmentSpecNameValidation:
    def test_valid_name_passes(self):
        result = validate_segment_spec(VALID_SEGMENT)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert name_errors == []

    @pytest.mark.parametrize(
        "bad_name",
        [
            "customer value tier",
            "high-value",
            "3rd_segment",
            "segment.name",
        ],
    )
    def test_invalid_name_produces_error(self, bad_name):
        spec = {**VALID_SEGMENT, "name": bad_name}
        result = validate_segment_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert len(name_errors) == 1
        assert "not a valid SQL identifier" in name_errors[0].message

    def test_invalid_name_includes_suggestion(self):
        spec = {**VALID_SEGMENT, "name": "customer value tier"}
        result = validate_segment_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert name_errors[0].suggestion is not None
        assert "customer_value_tier" in name_errors[0].suggestion

    def test_missing_name_not_double_reported(self):
        spec = {**VALID_SEGMENT, "name": None}
        result = validate_segment_spec(spec)
        name_errors = [e for e in result.errors if e.field == "name"]
        assert len(name_errors) == 1
        assert "not a valid SQL identifier" not in name_errors[0].message
