"""Unit tests for _extract_columns_from_sql and referenced_columns in ValidationResult."""

from aitaem.utils.validation import (
    _extract_columns_from_sql,
    validate_metric_spec,
    validate_slice_spec,
)


# ---------------------------------------------------------------------------
# _extract_columns_from_sql
# ---------------------------------------------------------------------------


class TestExtractColumnsFromSql:
    def test_simple_aggregate(self):
        assert _extract_columns_from_sql("SUM(revenue)") == ["revenue"]

    def test_two_aggregates(self):
        cols = _extract_columns_from_sql("SUM(amount) / NULLIF(SUM(impressions), 0)")
        assert cols == ["amount", "impressions"]

    def test_count_star_no_columns(self):
        assert _extract_columns_from_sql("COUNT(*)") == []

    def test_deduplicated(self):
        assert _extract_columns_from_sql("SUM(a) + SUM(a)") == ["a"]

    def test_case_when_extracts_all_columns(self):
        expr = "SUM(CASE WHEN status = 'active' THEN revenue ELSE 0 END)"
        cols = _extract_columns_from_sql(expr)
        assert "status" in cols
        assert "revenue" in cols

    def test_table_qualifier_stripped(self):
        assert _extract_columns_from_sql("SUM(t.revenue)") == ["revenue"]

    def test_where_context_simple_condition(self):
        cols = _extract_columns_from_sql("industry = 'tech'", context="where")
        assert cols == ["industry"]

    def test_where_context_compound_condition(self):
        cols = _extract_columns_from_sql("amt > 0 AND channel = 'email'", context="where")
        assert "amt" in cols
        assert "channel" in cols

    def test_where_context_in_list(self):
        cols = _extract_columns_from_sql("region IN ('US', 'EU')", context="where")
        assert cols == ["region"]

    def test_unparseable_returns_empty(self):
        assert _extract_columns_from_sql("SELECT SELECT SELECT") == []


# ---------------------------------------------------------------------------
# validate_metric_spec — referenced_columns
# ---------------------------------------------------------------------------

VALID_METRIC = {
    "name": "my_metric",
    "source": "duckdb://db/t",
    "numerator": "SUM(revenue)",
    "timestamp_col": "created_at",
}


class TestMetricReferencedColumns:
    def test_minimal_valid_spec_has_expected_keys(self):
        result = validate_metric_spec(VALID_METRIC)
        assert result.valid
        assert result.referenced_columns is not None
        assert set(result.referenced_columns.keys()) == {"numerator", "timestamp_col"}

    def test_numerator_columns_extracted(self):
        result = validate_metric_spec(VALID_METRIC)
        assert result.referenced_columns["numerator"] == ["revenue"]

    def test_timestamp_col_included(self):
        result = validate_metric_spec(VALID_METRIC)
        assert result.referenced_columns["timestamp_col"] == ["created_at"]

    def test_denominator_key_present_when_set(self):
        spec = {**VALID_METRIC, "denominator": "SUM(impressions)"}
        result = validate_metric_spec(spec)
        assert result.valid
        assert "denominator" in result.referenced_columns
        assert result.referenced_columns["denominator"] == ["impressions"]

    def test_denominator_key_absent_when_not_set(self):
        result = validate_metric_spec(VALID_METRIC)
        assert "denominator" not in result.referenced_columns

    def test_entities_key_present_when_set(self):
        spec = {**VALID_METRIC, "entities": ["user_id", "org_id"]}
        result = validate_metric_spec(spec)
        assert result.valid
        assert result.referenced_columns["entities"] == ["user_id", "org_id"]

    def test_entities_key_absent_when_not_set(self):
        result = validate_metric_spec(VALID_METRIC)
        assert "entities" not in result.referenced_columns

    def test_count_star_numerator_gives_empty_list(self):
        spec = {**VALID_METRIC, "numerator": "COUNT(*)"}
        result = validate_metric_spec(spec)
        assert result.valid
        assert result.referenced_columns["numerator"] == []

    def test_complex_numerator_extracts_multiple_columns(self):
        spec = {
            **VALID_METRIC,
            "numerator": "SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END)",
        }
        result = validate_metric_spec(spec)
        assert result.valid
        cols = result.referenced_columns["numerator"]
        assert "status" in cols
        assert "amount" in cols

    def test_invalid_spec_referenced_columns_is_none(self):
        spec = {**VALID_METRIC, "numerator": ""}
        result = validate_metric_spec(spec)
        assert not result.valid
        assert result.referenced_columns is None

    def test_missing_timestamp_col_referenced_columns_is_none(self):
        spec = {k: v for k, v in VALID_METRIC.items() if k != "timestamp_col"}
        result = validate_metric_spec(spec)
        assert not result.valid
        assert result.referenced_columns is None


# ---------------------------------------------------------------------------
# validate_slice_spec — referenced_columns
# ---------------------------------------------------------------------------


class TestSliceReferencedColumns:
    def test_leaf_single_value(self):
        spec = {
            "name": "geo",
            "values": [{"name": "us", "where": "region = 'US'"}],
        }
        result = validate_slice_spec(spec)
        assert result.valid
        assert result.referenced_columns == {"values[0].where": ["region"]}

    def test_leaf_multiple_values(self):
        spec = {
            "name": "geo",
            "values": [
                {"name": "us", "where": "region = 'US'"},
                {"name": "eu", "where": "region = 'EU' AND country IS NOT NULL"},
            ],
        }
        result = validate_slice_spec(spec)
        assert result.valid
        rc = result.referenced_columns
        assert "values[0].where" in rc
        assert "values[1].where" in rc
        assert rc["values[0].where"] == ["region"]
        assert "region" in rc["values[1].where"]
        assert "country" in rc["values[1].where"]

    def test_wildcard_spec(self):
        spec = {"name": "industry", "where": "industry"}
        result = validate_slice_spec(spec)
        assert result.valid
        assert result.referenced_columns == {"where": ["industry"]}

    def test_composite_spec_empty_dict(self):
        spec = {"name": "combo", "cross_product": ["geo", "industry"]}
        result = validate_slice_spec(spec)
        assert result.valid
        assert result.referenced_columns == {}

    def test_invalid_slice_referenced_columns_is_none(self):
        spec = {"name": "geo"}  # missing values/where/cross_product
        result = validate_slice_spec(spec)
        assert not result.valid
        assert result.referenced_columns is None
