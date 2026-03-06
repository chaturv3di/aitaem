"""
Tests for aitaem.query.builder — QueryGroup dataclass and QueryBuilder static methods.

Sub-feature coverage order (per plan):
  1. QueryGroup dataclass
  2. QueryBuildError
  3. _group_by_source
  4. _parse_table_name_from_uri
  5. _build_time_filter_sql
  6. _build_metric_value_expr
  7. _build_slice_case_when_expr
  8. _build_segment_case_when_expr
  9. _build_slice_value_concat_expr
 10. _build_metric_segment_query (all four cases + DuckDB validation)
 11. _build_queries_for_metric
 12. build_queries (integration)
"""

import ibis
import pytest

from aitaem.query.builder import QueryBuilder, QueryGroup
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.utils.exceptions import QueryBuildError

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

DUCKDB_URI = "duckdb://ad_campaigns.duckdb/ad_campaigns"
BIGQUERY_URI = "bigquery://my-project/my_dataset/my_table"


def make_metric(
    name="revenue", agg="sum", numerator="SUM(amount)", denominator=None, source=DUCKDB_URI
):
    return MetricSpec(
        name=name,
        source=source,
        aggregation=agg,
        numerator=numerator,
        denominator=denominator,
    )


def make_ratio_metric(name="ctr", source=DUCKDB_URI):
    return MetricSpec(
        name=name,
        source=source,
        aggregation="ratio",
        numerator="SUM(clicks)",
        denominator="SUM(impressions)",
    )


def make_slice(name="geo", values=None):
    if values is None:
        values = (
            SliceValue(name="USA", where="country = 'USA'"),
            SliceValue(name="EU", where="country IN ('UK', 'Germany')"),
        )
    return SliceSpec(name=name, values=tuple(values))


def make_segment(name="platform", values=None, source=DUCKDB_URI):
    if values is None:
        values = (
            SegmentValue(name="Google Ads", where="platform = 'Google Ads'"),
            SegmentValue(name="Meta Ads", where="platform = 'Meta Ads'"),
        )
    return SegmentSpec(name=name, source=source, values=tuple(values))


# ---------------------------------------------------------------------------
# 1. QueryGroup dataclass
# ---------------------------------------------------------------------------


class TestQueryGroup:
    def test_instantiate(self):
        m = make_metric()
        qg = QueryGroup(source=DUCKDB_URI, metrics=[m], sql_queries=["SELECT 1"])
        assert qg.source == DUCKDB_URI
        assert qg.metrics == [m]
        assert qg.sql_queries == ["SELECT 1"]

    def test_default_sql_queries_empty(self):
        m = make_metric()
        qg = QueryGroup(source=DUCKDB_URI, metrics=[m])
        assert qg.sql_queries == []


# ---------------------------------------------------------------------------
# 2. QueryBuildError
# ---------------------------------------------------------------------------


class TestQueryBuildError:
    def test_raise_and_catch_as_aitaem_error(self):
        from aitaem.utils.exceptions import AitaemError

        with pytest.raises(AitaemError):
            raise QueryBuildError("test error")

    def test_message(self):
        with pytest.raises(QueryBuildError, match="test error"):
            raise QueryBuildError("test error")


# ---------------------------------------------------------------------------
# 3. _group_by_source
# ---------------------------------------------------------------------------


class TestGroupBySource:
    def test_single_source(self):
        metrics = [make_metric("a"), make_metric("b"), make_metric("c")]
        result = QueryBuilder._group_by_source(metrics)
        assert list(result.keys()) == [DUCKDB_URI]
        assert len(result[DUCKDB_URI]) == 3

    def test_two_sources(self):
        source2 = "duckdb://other.db/orders"
        metrics = [
            make_metric("a", source=DUCKDB_URI),
            make_metric("b", source=source2),
            make_metric("c", source=DUCKDB_URI),
        ]
        result = QueryBuilder._group_by_source(metrics)
        assert len(result) == 2
        assert len(result[DUCKDB_URI]) == 2
        assert len(result[source2]) == 1


# ---------------------------------------------------------------------------
# 4. _parse_table_name_from_uri
# ---------------------------------------------------------------------------


class TestParseTableNameFromUri:
    def test_duckdb(self):
        assert QueryBuilder._parse_table_name_from_uri(DUCKDB_URI) == "ad_campaigns"

    def test_bigquery(self):
        assert QueryBuilder._parse_table_name_from_uri(BIGQUERY_URI) == "my_dataset.my_table"


# ---------------------------------------------------------------------------
# 5. _build_time_filter_sql
# ---------------------------------------------------------------------------


class TestBuildTimeFilterSql:
    def test_basic(self):
        result = QueryBuilder._build_time_filter_sql(("2026-01-01", "2026-02-01"), "event_ts")
        assert result == "event_ts >= '2026-01-01' AND event_ts < '2026-02-01'"


# ---------------------------------------------------------------------------
# 6. _build_metric_value_expr
# ---------------------------------------------------------------------------


class TestBuildMetricValueExpr:
    def test_sum(self):
        m = make_metric(agg="sum", numerator="SUM(amount)")
        assert QueryBuilder._build_metric_value_expr(m) == "SUM(amount)"

    def test_ratio(self):
        m = make_ratio_metric()
        expr = QueryBuilder._build_metric_value_expr(m)
        assert expr == "SUM(clicks) / NULLIF(SUM(impressions), 0)"

    def test_count(self):
        m = make_metric(agg="count", numerator="COUNT(*)")
        assert QueryBuilder._build_metric_value_expr(m) == "COUNT(*)"


# ---------------------------------------------------------------------------
# 7. _build_slice_case_when_expr
# ---------------------------------------------------------------------------


class TestBuildSliceCaseWhenExpr:
    def test_contains_all_values(self):
        ss = make_slice()
        result = QueryBuilder._build_slice_case_when_expr(ss, "_slice_geo")
        assert "country = 'USA'" in result
        assert "'USA'" in result
        assert "country IN ('UK', 'Germany')" in result
        assert "'EU'" in result

    def test_ends_with_alias(self):
        ss = make_slice()
        result = QueryBuilder._build_slice_case_when_expr(ss, "_slice_geo")
        assert result.strip().endswith("AS _slice_geo")

    def test_else_null(self):
        ss = make_slice()
        result = QueryBuilder._build_slice_case_when_expr(ss, "_slice_geo")
        assert "ELSE NULL" in result


# ---------------------------------------------------------------------------
# 8. _build_segment_case_when_expr
# ---------------------------------------------------------------------------


class TestBuildSegmentCaseWhenExpr:
    def test_contains_all_values(self):
        seg = make_segment()
        result = QueryBuilder._build_segment_case_when_expr(seg, "_segment")
        assert "platform = 'Google Ads'" in result
        assert "'Google Ads'" in result
        assert "platform = 'Meta Ads'" in result

    def test_ends_with_alias(self):
        seg = make_segment()
        result = QueryBuilder._build_segment_case_when_expr(seg, "_segment")
        assert result.strip().endswith("AS _segment")


# ---------------------------------------------------------------------------
# 9. _build_slice_value_concat_expr
# ---------------------------------------------------------------------------


class TestBuildSliceValueConcatExpr:
    def test_single_alias(self):
        result = QueryBuilder._build_slice_value_concat_expr(["_slice_geo"])
        assert result == "_slice_geo"
        assert "||" not in result

    def test_two_aliases(self):
        result = QueryBuilder._build_slice_value_concat_expr(["_slice_geo", "_slice_device"])
        assert result == "_slice_geo || '|' || _slice_device"

    def test_three_aliases(self):
        result = QueryBuilder._build_slice_value_concat_expr(["_a", "_b", "_c"])
        assert result == "_a || '|' || _b || '|' || _c"
        assert result.count("||") == 4  # two '|' separators × 2 each


# ---------------------------------------------------------------------------
# 10. _build_metric_segment_query — all four cases
# ---------------------------------------------------------------------------


def _run_sql_duckdb(sql: str, setup_sql: str | None = None):
    """Execute SQL against an in-memory DuckDB connection (via Ibis)."""
    conn = ibis.duckdb.connect(":memory:")
    if setup_sql:
        conn.raw_sql(setup_sql)
    expr = conn.sql(sql)
    return expr.to_pandas()


SETUP_SQL = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    ('US', 'mobile', 'premium', TRUE, 100.0, '2026-01-10'),
    ('US', 'desktop', 'free', FALSE, 50.0, '2026-01-15'),
    ('DE', 'mobile', 'premium', TRUE, 80.0, '2026-01-20'),
    ('DE', 'desktop', 'free', FALSE, 40.0, '2026-01-25')
) AS t(country_code, device_type, subscription_tier, is_logged_in, amount, event_ts)
"""

_geo_slice = SliceSpec(
    name="geography",
    values=(
        SliceValue(name="North America", where="country_code IN ('US', 'CA', 'MX')"),
        SliceValue(name="Europe", where="country_code IN ('DE', 'FR', 'UK')"),
    ),
)
_device_slice = SliceSpec(
    name="device",
    values=(
        SliceValue(name="mobile", where="device_type = 'mobile'"),
        SliceValue(name="desktop", where="device_type = 'desktop'"),
    ),
)
_user_tier_segment = SegmentSpec(
    name="user_tier",
    source=DUCKDB_URI,
    values=(
        SegmentValue(name="premium", where="subscription_tier = 'premium'"),
        SegmentValue(name="free", where="subscription_tier = 'free'"),
    ),
)
_txn_metric = MetricSpec(
    name="revenue",
    source="duckdb://analytics.db/transactions",
    aggregation="sum",
    numerator="SUM(amount)",
)


class TestBuildMetricSegmentQuery:
    def _run(
        self,
        metric,
        slices,
        segment,
        time_filter=None,
        period_type="all_time",
        period_start=None,
        period_end=None,
        table="transactions",
    ):
        sql = QueryBuilder._build_metric_segment_query(
            metric=metric,
            table_name=table,
            slice_specs=slices,
            segment_spec=segment,
            time_filter_sql=time_filter,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
        )
        return sql, _run_sql_duckdb(sql, SETUP_SQL)

    def test_slices_and_segment(self):
        sql, df = self._run(
            _txn_metric,
            [_geo_slice, _device_slice],
            _user_tier_segment,
        )
        # CTE should have CASE WHEN for each slice and segment
        assert "_slice_geography" in sql
        assert "_slice_device" in sql
        assert "_segment" in sql
        # GROUP BY all labeled columns
        assert "GROUP BY" in sql
        assert "_slice_geography" in sql.split("GROUP BY")[1]
        assert "_segment" in sql.split("GROUP BY")[1]
        # All IS NOT NULL checks
        assert "_slice_geography IS NOT NULL" in sql
        assert "_slice_device IS NOT NULL" in sql
        assert "_segment IS NOT NULL" in sql
        # Result has expected columns
        expected_cols = {
            "period_type",
            "period_start_date",
            "period_end_date",
            "metric_name",
            "slice_type",
            "slice_value",
            "segment_name",
            "segment_value",
            "metric_value",
        }
        assert expected_cols.issubset(set(df.columns))
        # slice_type uses pipe separator
        assert all(df["slice_type"] == "geography|device")
        # segment_name is correct
        assert all(df["segment_name"] == "user_tier")
        # non-null metric values
        assert df["metric_value"].notna().all()

    def test_slices_only_no_segment(self):
        sql, df = self._run(_txn_metric, [_geo_slice], None)
        assert "_slice_geography" in sql
        assert "_segment" not in sql
        assert "GROUP BY" in sql
        assert all(df["segment_name"] == "none")
        assert all(df["segment_value"] == "all")
        assert all(df["slice_type"] == "geography")

    def test_segment_only_no_slices(self):
        sql, df = self._run(_txn_metric, None, _user_tier_segment)
        assert "_segment" in sql
        assert "_slice_" not in sql
        assert all(df["slice_type"] == "none")
        assert all(df["slice_value"] == "all")
        assert all(df["segment_name"] == "user_tier")

    def test_no_slices_no_segment(self):
        sql, df = self._run(_txn_metric, None, None)
        assert "_slice_" not in sql
        assert "_segment" not in sql
        assert "GROUP BY" not in sql
        assert "WHERE" not in sql  # no null filters, no time filter
        assert len(df) == 1
        assert df["slice_type"].iloc[0] == "none"
        assert df["slice_value"].iloc[0] == "all"
        assert df["segment_name"].iloc[0] == "none"
        assert df["segment_value"].iloc[0] == "all"

    def test_time_filter_applied(self):
        sql, df = self._run(
            _txn_metric,
            None,
            None,
            time_filter="event_ts >= '2026-01-12' AND event_ts < '2026-01-20'",
            period_start="2026-01-12",
            period_end="2026-01-20",
        )
        # Only US/desktop/free row (2026-01-15) matches
        assert df["metric_value"].iloc[0] == pytest.approx(50.0)

    def test_period_metadata(self):
        sql, df = self._run(
            _txn_metric,
            None,
            None,
            period_type="all_time",
            period_start="2026-01-01",
            period_end="2026-02-01",
        )
        assert df["period_type"].iloc[0] == "all_time"
        assert df["period_start_date"].iloc[0] == "2026-01-01"
        assert df["period_end_date"].iloc[0] == "2026-02-01"

    def test_period_none_values(self):
        sql, df = self._run(_txn_metric, None, None)
        assert df["period_start_date"].iloc[0] is None or str(df["period_start_date"].iloc[0]) in (
            "None",
            "nan",
            "",
        )


# ---------------------------------------------------------------------------
# 10b. _resolve_slice_components
# ---------------------------------------------------------------------------


class TestResolveSliceComponents:
    """Tests for QueryBuilder._resolve_slice_components()."""

    def test_none_returns_none(self):
        result = QueryBuilder._resolve_slice_components(None)
        assert result is None

    def test_leaf_spec_returns_list_of_one(self):
        ss = make_slice("geo")
        result = QueryBuilder._resolve_slice_components(ss)
        assert result == [ss]

    def test_composite_spec_resolves_from_cache(self):
        from aitaem.specs.loader import SpecCache
        from aitaem.specs.slice import SliceValue

        geo = SliceSpec(name="geo", values=(SliceValue(name="USA", where="country='USA'"),))
        device = SliceSpec(name="device", values=(SliceValue(name="mobile", where="device='mobile'"),))
        composite = SliceSpec(name="geo_x_device", cross_product=("geo", "device"))

        cache = SpecCache()
        cache.add_spec(geo)
        cache.add_spec(device)
        cache.add_spec(composite)
        SpecCache.set_global(cache)

        try:
            result = QueryBuilder._resolve_slice_components(composite)
            assert result == [geo, device]
        finally:
            SpecCache._global_instance = None

    def test_composite_spec_missing_cache_raises(self):
        from aitaem.specs.loader import SpecCache

        SpecCache._global_instance = None
        composite = SliceSpec(name="geo_x_device", cross_product=("geo", "device"))
        with pytest.raises(RuntimeError, match="No global SpecCache"):
            QueryBuilder._resolve_slice_components(composite)


# ---------------------------------------------------------------------------
# 11. _build_queries_for_metric
# ---------------------------------------------------------------------------


class TestBuildQueriesForMetric:
    def test_two_segments_returns_three_queries(self):
        seg1 = make_segment("platform")
        seg2 = SegmentSpec(
            name="login_status",
            source=DUCKDB_URI,
            values=(
                SegmentValue(name="logged_in", where="is_logged_in = TRUE"),
                SegmentValue(name="visitor", where="is_logged_in = FALSE"),
            ),
        )
        metric = make_metric()
        queries = QueryBuilder._build_queries_for_metric(
            metric=metric,
            slice_specs=None,
            segment_specs=[seg1, seg2],
            time_filter_sql=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        assert len(queries) == 3

    def test_no_segments_returns_two_queries(self):
        # 1 slice spec + no-slice baseline = (1+1) × (0+1) = 2
        metric = make_metric()
        queries = QueryBuilder._build_queries_for_metric(
            metric=metric,
            slice_specs=[make_slice()],
            segment_specs=None,
            time_filter_sql=None,
            period_type="all_time",
            period_start=None,
            period_end=None,
        )
        assert len(queries) == 2


# ---------------------------------------------------------------------------
# 12. build_queries — integration
# ---------------------------------------------------------------------------


class TestBuildQueriesIntegration:
    def test_one_source_one_metric_no_segments(self):
        metric = make_metric()
        groups = QueryBuilder.build_queries([metric], slice_specs=None, segment_specs=None)
        assert len(groups) == 1
        assert len(groups[0].sql_queries) == 1

    def test_one_source_two_metrics_two_segments(self):
        m1 = make_metric("revenue")
        m2 = make_ratio_metric("ctr")
        seg1 = make_segment("platform")
        seg2 = SegmentSpec(
            name="login_status",
            source=DUCKDB_URI,
            values=(SegmentValue(name="logged_in", where="is_logged_in = TRUE"),),
        )
        groups = QueryBuilder.build_queries([m1, m2], slice_specs=None, segment_specs=[seg1, seg2])
        assert len(groups) == 1
        # 2 metrics × (2 segment specs + 1 no-segment) = 6 queries
        assert len(groups[0].sql_queries) == 6

    def test_two_sources_correct_grouping(self):
        source2 = "duckdb://other.db/orders"
        m1 = make_metric("revenue", source=DUCKDB_URI)
        m2 = make_metric("orders", source=source2)
        m3 = make_metric("clicks", source=DUCKDB_URI)
        groups = QueryBuilder.build_queries([m1, m2, m3], slice_specs=None, segment_specs=None)
        assert len(groups) == 2
        sources = {g.source for g in groups}
        assert sources == {DUCKDB_URI, source2}
        counts = {g.source: len(g.sql_queries) for g in groups}
        assert counts[DUCKDB_URI] == 2  # 2 metrics × 1 query each
        assert counts[source2] == 1

    def test_raises_on_empty_metrics(self):
        with pytest.raises(QueryBuildError, match="metric_specs must not be empty"):
            QueryBuilder.build_queries([], slice_specs=None, segment_specs=None)

    def test_raises_on_time_window_without_timestamp_col(self):
        metric = make_metric()
        with pytest.raises(QueryBuildError, match="timestamp_col is required"):
            QueryBuilder.build_queries(
                [metric],
                slice_specs=None,
                segment_specs=None,
                time_window=("2026-01-01", "2026-02-01"),
                timestamp_col=None,
            )

    def test_time_window_with_timestamp_col_ok(self):
        metric = make_metric()
        groups = QueryBuilder.build_queries(
            [metric],
            slice_specs=None,
            segment_specs=None,
            time_window=("2026-01-01", "2026-02-01"),
            timestamp_col="event_ts",
        )
        assert len(groups) == 1
        assert "event_ts >= '2026-01-01'" in groups[0].sql_queries[0]
