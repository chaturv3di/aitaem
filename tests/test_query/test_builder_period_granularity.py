"""
Tests for period granularity features in QueryBuilder.

Sub-feature coverage order (per plan 08):
  1. VALID_PERIOD_TYPES constant
  2. _generate_period_boundaries()
  3. _build_periods_cte()
  4. _build_metric_segment_query() — non-all_time path (DuckDB execution)
  5. build_queries() — period_type validation + propagation
"""

import ibis
import pytest

from aitaem.query.builder import QueryBuilder, VALID_PERIOD_TYPES
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.utils.exceptions import QueryBuildError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUCKDB_URI = "duckdb://analytics.db/transactions"


def make_metric(name="revenue", timestamp_col="event_ts"):
    return MetricSpec(
        name=name,
        source=DUCKDB_URI,
        numerator="SUM(amount)",
        timestamp_col=timestamp_col,
    )


def make_metric_no_ts(name="revenue"):
    return MetricSpec(
        name=name,
        source=DUCKDB_URI,
        numerator="SUM(amount)",
        timestamp_col=None,
    )


_geo_slice = SliceSpec(
    name="geography",
    values=(
        SliceValue(name="North America", where="country_code IN ('US', 'CA')"),
        SliceValue(name="Europe", where="country_code IN ('DE', 'FR')"),
    ),
)

_user_tier_segment = SegmentSpec(
    name="user_tier",
    source="duckdb://analytics.db/dim_users",
    entity_id="user_id",
    values=(
        SegmentValue(name="premium", where="subscription_tier = 'premium'"),
        SegmentValue(name="free", where="subscription_tier = 'free'"),
    ),
)

# Three months of data spanning 2026-01 through 2026-03
SETUP_SQL = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    (1, 'US', 100.0, TIMESTAMP '2026-01-10 00:00:00'),
    (2, 'US',  50.0, TIMESTAMP '2026-01-20 00:00:00'),
    (3, 'DE',  80.0, TIMESTAMP '2026-02-05 00:00:00'),
    (4, 'DE',  40.0, TIMESTAMP '2026-02-15 00:00:00'),
    (1, 'US', 120.0, TIMESTAMP '2026-03-08 00:00:00'),
    (2, 'US',  60.0, TIMESTAMP '2026-03-22 00:00:00')
) AS t(user_id, country_code, amount, event_ts);
CREATE TABLE dim_users AS
SELECT * FROM (VALUES
    (1, 'premium'),
    (2, 'free'),
    (3, 'premium'),
    (4, 'free')
) AS t(user_id, subscription_tier)
"""


def _run_sql_duckdb(sql: str, setup_sql: str | None = None):
    conn = ibis.duckdb.connect(":memory:")
    if setup_sql:
        conn.raw_sql(setup_sql)
    return conn.sql(sql).to_pandas()


# ---------------------------------------------------------------------------
# 1. VALID_PERIOD_TYPES constant
# ---------------------------------------------------------------------------


class TestValidPeriodTypes:
    def test_contains_all_expected_values(self):
        assert VALID_PERIOD_TYPES == {"all_time", "daily", "weekly", "monthly", "yearly", "hourly"}

    def test_is_frozenset(self):
        assert isinstance(VALID_PERIOD_TYPES, frozenset)


# ---------------------------------------------------------------------------
# 2. _generate_period_boundaries()
# ---------------------------------------------------------------------------


class TestGeneratePeriodBoundaries:
    def test_monthly_aligned_window(self):
        result = QueryBuilder._generate_period_boundaries(("2026-01-01", "2026-04-01"), "monthly")
        assert result == [
            ("2026-01-01", "2026-02-01"),
            ("2026-02-01", "2026-03-01"),
            ("2026-03-01", "2026-04-01"),
        ]

    def test_monthly_window_starting_mid_month(self):
        # period_start rounds down to first of January
        result = QueryBuilder._generate_period_boundaries(("2026-01-15", "2026-03-01"), "monthly")
        assert result[0][0] == "2026-01-01"
        assert result[0][1] == "2026-02-01"
        assert result[-1][1] == "2026-03-01"

    def test_weekly_window_starting_mid_week(self):
        # 2026-01-07 is a Wednesday; Monday of that week is 2026-01-05
        result = QueryBuilder._generate_period_boundaries(("2026-01-07", "2026-01-22"), "weekly")
        assert result[0][0] == "2026-01-05"  # preceding Monday
        assert result[0][1] == "2026-01-12"
        assert result[1] == ("2026-01-12", "2026-01-19")
        assert result[2] == ("2026-01-19", "2026-01-26")

    def test_weekly_starts_on_monday(self):
        result = QueryBuilder._generate_period_boundaries(("2026-01-05", "2026-01-20"), "weekly")
        # 2026-01-05 is already a Monday
        assert result[0][0] == "2026-01-05"

    def test_daily_three_days(self):
        result = QueryBuilder._generate_period_boundaries(("2026-01-01", "2026-01-04"), "daily")
        assert result == [
            ("2026-01-01", "2026-01-02"),
            ("2026-01-02", "2026-01-03"),
            ("2026-01-03", "2026-01-04"),
        ]

    def test_yearly_two_years(self):
        result = QueryBuilder._generate_period_boundaries(("2026-01-01", "2028-01-01"), "yearly")
        assert result == [
            ("2026-01-01", "2027-01-01"),
            ("2027-01-01", "2028-01-01"),
        ]

    def test_yearly_mid_year_start_rounds_down(self):
        result = QueryBuilder._generate_period_boundaries(("2026-06-15", "2027-06-15"), "yearly")
        assert result[0][0] == "2026-01-01"
        assert result[0][1] == "2027-01-01"
        assert result[1][0] == "2027-01-01"

    def test_monthly_december_wraps_to_january(self):
        result = QueryBuilder._generate_period_boundaries(("2026-12-01", "2027-02-01"), "monthly")
        assert result[0] == ("2026-12-01", "2027-01-01")
        assert result[1] == ("2027-01-01", "2027-02-01")


# ---------------------------------------------------------------------------
# 3. _build_periods_cte()
# ---------------------------------------------------------------------------


class TestBuildPeriodsCte:
    def test_header_present(self):
        cte = QueryBuilder._build_periods_cte([("2026-01-01", "2026-02-01")])
        assert "_periods(period_start, period_end) AS" in cte

    def test_cast_wrapping(self):
        cte = QueryBuilder._build_periods_cte([("2026-01-01", "2026-02-01")])
        assert "CAST('2026-01-01' AS TIMESTAMP)" in cte
        assert "CAST('2026-02-01' AS TIMESTAMP)" in cte

    def test_correct_row_count(self):
        boundaries = [
            ("2026-01-01", "2026-02-01"),
            ("2026-02-01", "2026-03-01"),
            ("2026-03-01", "2026-04-01"),
        ]
        cte = QueryBuilder._build_periods_cte(boundaries)
        # Each boundary should appear as a separate row
        assert cte.count("CAST('2026") == 6  # 3 pairs × 2 casts each

    def test_values_keyword_present(self):
        cte = QueryBuilder._build_periods_cte([("2026-01-01", "2026-02-01")])
        assert "VALUES" in cte


# ---------------------------------------------------------------------------
# 4. _build_metric_segment_query — non-all_time path (DuckDB execution)
# ---------------------------------------------------------------------------


class TestBuildMetricSegmentQueryPeriodGranularity:
    def _run(self, metric, slices, segment, period_type, time_window, table="transactions"):
        sql = QueryBuilder._build_metric_segment_query(
            metric=metric,
            table_name=table,
            slice_specs=slices,
            segment_spec=segment,
            time_filter_sql=None,
            period_type=period_type,
            period_start=None,
            period_end=None,
            time_window=time_window,
        )
        return sql, _run_sql_duckdb(sql, SETUP_SQL)

    def test_monthly_no_slice_no_segment_row_count(self):
        sql, df = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        # 3 months → 3 rows
        assert len(df) == 3
        assert set(df["period_type"]) == {"monthly"}

    def test_monthly_period_start_dates(self):
        _, df = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        starts = set(df["period_start_date"].str[:10])
        assert starts == {"2026-01-01", "2026-02-01", "2026-03-01"}

    def test_monthly_period_end_dates(self):
        _, df = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        df_jan = df[df["period_start_date"].str.startswith("2026-01")]
        assert df_jan["period_end_date"].iloc[0][:10] == "2026-02-01"
        df_mar = df[df["period_start_date"].str.startswith("2026-03")]
        assert df_mar["period_end_date"].iloc[0][:10] == "2026-04-01"

    def test_monthly_metric_values_match_expected(self):
        _, df = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        by_month = {row["period_start_date"][:10]: row["metric_value"] for _, row in df.iterrows()}
        assert by_month["2026-01-01"] == pytest.approx(150.0)  # 100 + 50
        assert by_month["2026-02-01"] == pytest.approx(120.0)  # 80 + 40
        assert by_month["2026-03-01"] == pytest.approx(180.0)  # 120 + 60

    def test_monthly_with_slice_group_by_includes_period_cols(self):
        sql, df = self._run(
            make_metric(),
            [_geo_slice],
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        # GROUP BY must contain _period_start, _period_end alongside slice alias
        assert "_period_start" in sql.split("GROUP BY")[1]
        assert "_period_end" in sql.split("GROUP BY")[1]
        assert "_slice_geography" in sql.split("GROUP BY")[1]
        # Test data: US only in Jan/Mar, DE only in Feb → 3 (month, region) pairs with data
        assert len(df) == 3
        assert set(df["period_type"]) == {"monthly"}

    def test_monthly_with_segment_group_by_includes_period_cols(self):
        sql, df = self._run(
            make_metric(),
            None,
            _user_tier_segment,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        assert "_period_start" in sql.split("GROUP BY")[1]
        assert "_period_end" in sql.split("GROUP BY")[1]
        assert "_segment" in sql.split("GROUP BY")[1]
        # 3 months × 2 segment values = 6 rows (both premium and free in every month)
        assert len(df) == 6

    def test_no_slice_no_segment_group_by_has_period_cols(self):
        sql, _ = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        # Even with no slices/segments, GROUP BY must exist for period columns
        assert "GROUP BY" in sql
        assert "_period_start" in sql.split("GROUP BY")[1]

    def test_weekly_period_start_is_monday(self):
        _, df = self._run(
            make_metric(),
            None,
            None,
            "weekly",
            ("2026-01-05", "2026-01-26"),
        )
        from datetime import date

        for start_str in df["period_start_date"]:
            d = date.fromisoformat(start_str[:10])
            assert d.weekday() == 0, f"{start_str} is not a Monday"

    def test_all_time_path_unchanged(self):
        """all_time behavior is identical to before: no _periods CTE, static literals."""
        sql = QueryBuilder._build_metric_segment_query(
            metric=make_metric(),
            table_name="transactions",
            slice_specs=None,
            segment_spec=None,
            time_filter_sql=None,
            period_type="all_time",
            period_start="2026-01-01",
            period_end="2026-04-01",
            time_window=("2026-01-01", "2026-04-01"),
        )
        assert "_periods" not in sql
        assert "'2026-01-01'" in sql
        assert "'2026-04-01'" in sql
        assert "JOIN" not in sql

    def test_non_all_time_sql_uses_join(self):
        sql, _ = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        assert "_periods" in sql
        assert "JOIN _periods" in sql
        assert "CAST(t.event_ts AS TIMESTAMP)" in sql

    def test_non_all_time_select_uses_t_star(self):
        sql, _ = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        assert "SELECT\n        t.*" in sql or "SELECT t.*" in sql.replace("\n        ", " ")


# ---------------------------------------------------------------------------
# 5. build_queries() — validation + propagation
# ---------------------------------------------------------------------------


class TestBuildQueriesWithPeriodType:
    def test_unknown_period_type_raises(self):
        with pytest.raises(QueryBuildError, match="Invalid period_type"):
            QueryBuilder.build_queries(
                [make_metric()],
                slice_specs=None,
                segment_spec=None,
                time_window=("2026-01-01", "2026-04-01"),
                period_type="quarterly",
            )

    def test_non_all_time_without_time_window_raises(self):
        with pytest.raises(QueryBuildError, match="requires time_window"):
            QueryBuilder.build_queries(
                [make_metric()],
                slice_specs=None,
                segment_spec=None,
                time_window=None,
                period_type="monthly",
            )

    def test_non_all_time_missing_timestamp_col_raises(self):
        with pytest.raises(QueryBuildError, match="timestamp_col"):
            QueryBuilder.build_queries(
                [make_metric_no_ts()],
                slice_specs=None,
                segment_spec=None,
                time_window=("2026-01-01", "2026-04-01"),
                period_type="monthly",
            )

    def test_monthly_valid_inputs_produces_sql(self):
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
            time_window=("2026-01-01", "2026-04-01"),
            period_type="monthly",
        )
        assert len(groups) == 1
        sql = groups[0].sql_queries[0]
        df = _run_sql_duckdb(sql, SETUP_SQL)
        assert len(df) == 3

    def test_all_time_default_backward_compatible(self):
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
        )
        assert len(groups) == 1
        sql = groups[0].sql_queries[0]
        # old behavior: static 'all_time' literal, no _periods CTE
        assert "'all_time'" in sql
        assert "_periods" not in sql


# ---------------------------------------------------------------------------
# Hourly period type — _parse_window_endpoint_as_datetime
# ---------------------------------------------------------------------------


class TestParseWindowEndpointAsDatetime:
    def test_date_only_string_gives_midnight(self):
        from datetime import datetime

        result = QueryBuilder._parse_window_endpoint_as_datetime("2024-01-15")
        assert result == datetime(2024, 1, 15, 0, 0, 0)

    def test_datetime_string_with_T_separator(self):
        from datetime import datetime

        result = QueryBuilder._parse_window_endpoint_as_datetime("2024-01-15T14:30:00")
        assert result == datetime(2024, 1, 15, 14, 30, 0)

    def test_datetime_string_with_space_separator(self):
        from datetime import datetime

        result = QueryBuilder._parse_window_endpoint_as_datetime("2024-01-15 14:30:00")
        assert result == datetime(2024, 1, 15, 14, 30, 0)

    def test_midnight_string_gives_midnight(self):
        from datetime import datetime

        result = QueryBuilder._parse_window_endpoint_as_datetime("2024-01-15T00:00:00")
        assert result == datetime(2024, 1, 15, 0, 0, 0)


# ---------------------------------------------------------------------------
# Hourly period type — _generate_period_boundaries
# ---------------------------------------------------------------------------


class TestGeneratePeriodBoundariesHourly:
    def test_date_strings_produce_midnight_anchored_periods(self):
        result = QueryBuilder._generate_period_boundaries(
            ("2024-01-01", "2024-01-01T03:00:00"), "hourly"
        )
        assert result == [
            ("2024-01-01T00:00:00", "2024-01-01T01:00:00"),
            ("2024-01-01T01:00:00", "2024-01-01T02:00:00"),
            ("2024-01-01T02:00:00", "2024-01-01T03:00:00"),
        ]

    def test_start_sub_hour_precision_truncated(self):
        # Start 14:30 → truncated to 14:00; end 16:30 used as-is → 3 periods
        result = QueryBuilder._generate_period_boundaries(
            ("2024-01-01T14:30:00", "2024-01-01T16:30:00"), "hourly"
        )
        assert len(result) == 3
        assert result[0] == ("2024-01-01T14:00:00", "2024-01-01T15:00:00")
        assert result[1] == ("2024-01-01T15:00:00", "2024-01-01T16:00:00")
        assert result[2] == ("2024-01-01T16:00:00", "2024-01-01T17:00:00")

    def test_exact_hour_boundary_excludes_end_period(self):
        # End 16:00 means the 16:00 start period is NOT included
        result = QueryBuilder._generate_period_boundaries(
            ("2024-01-01T14:00:00", "2024-01-01T16:00:00"), "hourly"
        )
        assert len(result) == 2
        assert result[0][0] == "2024-01-01T14:00:00"
        assert result[1][0] == "2024-01-01T15:00:00"

    def test_single_hour_window(self):
        result = QueryBuilder._generate_period_boundaries(
            ("2024-01-01T10:00:00", "2024-01-01T11:00:00"), "hourly"
        )
        assert result == [("2024-01-01T10:00:00", "2024-01-01T11:00:00")]

    def test_two_date_strings_spans_midnight(self):
        # "2024-01-01" to "2024-01-02" = 24 hourly periods
        result = QueryBuilder._generate_period_boundaries(("2024-01-01", "2024-01-02"), "hourly")
        assert len(result) == 24
        assert result[0] == ("2024-01-01T00:00:00", "2024-01-01T01:00:00")
        assert result[-1] == ("2024-01-01T23:00:00", "2024-01-02T00:00:00")

    def test_empty_window_produces_no_periods(self):
        result = QueryBuilder._generate_period_boundaries(
            ("2024-01-01T10:00:00", "2024-01-01T10:00:00"), "hourly"
        )
        assert result == []

    def test_boundary_strings_use_T_separator(self):
        result = QueryBuilder._generate_period_boundaries(
            ("2024-06-15T09:00:00", "2024-06-15T11:00:00"), "hourly"
        )
        for start, end in result:
            assert "T" in start
            assert "T" in end


# ---------------------------------------------------------------------------
# Hourly period type — build_queries() validation
# ---------------------------------------------------------------------------


class TestBuildQueriesHourly:
    def test_hourly_in_valid_period_types(self):
        assert "hourly" in VALID_PERIOD_TYPES

    def test_hourly_without_time_window_raises(self):
        with pytest.raises(QueryBuildError, match="requires time_window"):
            QueryBuilder.build_queries(
                [make_metric()],
                slice_specs=None,
                segment_spec=None,
                time_window=None,
                period_type="hourly",
            )

    def test_hourly_missing_timestamp_col_raises(self):
        with pytest.raises(QueryBuildError, match="timestamp_col"):
            QueryBuilder.build_queries(
                [make_metric_no_ts()],
                slice_specs=None,
                segment_spec=None,
                time_window=("2026-01-01T00:00:00", "2026-01-01T03:00:00"),
                period_type="hourly",
            )

    def test_hourly_generates_periods_cte(self):
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
            time_window=("2026-01-01T00:00:00", "2026-01-01T03:00:00"),
            period_type="hourly",
        )
        sql = groups[0].sql_queries[0]
        assert "_periods" in sql
        assert "'hourly'" in sql
        assert "2026-01-01T00:00:00" in sql

    def test_hourly_sql_executes_and_returns_correct_periods(self):
        setup = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    (100.0, TIMESTAMP '2026-01-01 00:30:00'),
    (200.0, TIMESTAMP '2026-01-01 01:45:00'),
    (150.0, TIMESTAMP '2026-01-01 02:10:00')
) AS t(amount, event_ts)
"""
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
            time_window=("2026-01-01T00:00:00", "2026-01-01T03:00:00"),
            period_type="hourly",
        )
        sql = groups[0].sql_queries[0]
        df = _run_sql_duckdb(sql, setup)
        assert len(df) == 3
        assert set(df["period_type"]) == {"hourly"}
        # Each row carries data from exactly one hour
        assert sorted(df["metric_value"].tolist()) == [100.0, 150.0, 200.0]
