"""
Tests for period granularity features in QueryBuilder.

Post Plan 34: the VALUES-with-column-list periods CTE is replaced by a
server-side ibis.range()+interval spine (_build_periods_spine). Assertions on
_build_metric_segment_query are expression/schema/executed-data based.

Sub-feature coverage order (per plan 08 / plan 34):
  1. VALID_PERIOD_TYPES constant
  2. _generate_period_boundaries()
  3. _build_periods_spine()
  4. _build_metric_segment_query() — non-all_time path (DuckDB execution)
  5. build_queries() — period_type validation + propagation
"""

import ibis
import pytest

from aitaem.connectors.connection import ConnectionManager
from aitaem.query.builder import VALID_PERIOD_TYPES, QueryBuilder
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


def _make_manager(setup_sql: str | None = None) -> ConnectionManager:
    manager = ConnectionManager()
    manager.add_connection("duckdb", path=":memory:")
    if setup_sql:
        manager.get_connection("duckdb").connection.raw_sql(setup_sql)
    return manager


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
# 3. _build_periods_spine()
# ---------------------------------------------------------------------------


class TestBuildPeriodsSpine:
    def test_returns_period_start_and_period_end_columns(self):
        spine = QueryBuilder._build_periods_spine([("2026-01-01", "2026-02-01")], "monthly")
        assert set(spine.columns) == {"period_start", "period_end"}

    def test_correct_row_count(self):
        boundaries = [
            ("2026-01-01", "2026-02-01"),
            ("2026-02-01", "2026-03-01"),
            ("2026-03-01", "2026-04-01"),
        ]
        spine = QueryBuilder._build_periods_spine(boundaries, "monthly")
        df = spine.execute()
        assert len(df) == 3

    def test_values_match_generated_boundaries(self):
        boundaries = [
            ("2026-01-01", "2026-02-01"),
            ("2026-02-01", "2026-03-01"),
        ]
        spine = QueryBuilder._build_periods_spine(boundaries, "monthly")
        df = spine.execute().sort_values("period_start").reset_index(drop=True)
        assert str(df["period_start"].iloc[0])[:10] == "2026-01-01"
        assert str(df["period_end"].iloc[0])[:10] == "2026-02-01"
        assert str(df["period_start"].iloc[1])[:10] == "2026-02-01"
        assert str(df["period_end"].iloc[1])[:10] == "2026-03-01"

    def test_no_values_with_column_list_in_bigquery_sql(self):
        """Direct regression test for Gap A: the old VALUES-with-column-list
        CTE syntax that BigQuery's grammar rejects is gone."""
        boundaries = [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01")]
        spine = QueryBuilder._build_periods_spine(boundaries, "monthly")
        sql = ibis.to_sql(spine, dialect="bigquery")
        assert "period_start, period_end) AS" not in sql

    def test_hourly_unit(self):
        boundaries = [("2026-01-01T00:00:00", "2026-01-01T01:00:00")]
        spine = QueryBuilder._build_periods_spine(boundaries, "hourly")
        df = spine.execute()
        assert len(df) == 1

    def test_weekly_unit_steps_seven_days(self):
        boundaries = [("2026-01-05", "2026-01-12"), ("2026-01-12", "2026-01-19")]
        spine = QueryBuilder._build_periods_spine(boundaries, "weekly")
        df = spine.execute().sort_values("period_start").reset_index(drop=True)
        delta = df["period_end"].iloc[0] - df["period_start"].iloc[0]
        assert delta.days == 7


# ---------------------------------------------------------------------------
# 4. _build_metric_segment_query — non-all_time path (DuckDB execution)
# ---------------------------------------------------------------------------


class TestBuildMetricSegmentQueryPeriodGranularity:
    def _run(self, metric, slices, segment, period_type, time_window):
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=metric,
            connector=connector,
            slice_specs=slices,
            segment_spec=segment,
            period_type=period_type,
            period_start=None,
            period_end=None,
            time_window=time_window,
        )
        return expr, expr.execute()

    def test_monthly_no_slice_no_segment_row_count(self):
        expr, df = self._run(
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

    def test_monthly_with_slice_row_count(self):
        expr, df = self._run(
            make_metric(),
            [_geo_slice],
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        # Test data: US only in Jan/Mar, DE only in Feb → 3 (month, region) pairs with data
        assert len(df) == 3
        assert set(df["period_type"]) == {"monthly"}

    def test_monthly_with_segment_row_count(self):
        expr, df = self._run(
            make_metric(),
            None,
            _user_tier_segment,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        # 3 months × 2 segment values = 6 rows (both premium and free in every month)
        assert len(df) == 6

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
        """all_time behavior is identical to before: no periods spine, static literals."""
        manager = _make_manager(SETUP_SQL)
        connector = manager.get_connection("duckdb")
        expr = QueryBuilder._build_metric_segment_query(
            metric=make_metric(),
            connector=connector,
            slice_specs=None,
            segment_spec=None,
            period_type="all_time",
            period_start="2026-01-01",
            period_end="2026-04-01",
            time_window=("2026-01-01", "2026-04-01"),
        )
        df = expr.execute()
        assert df["period_start_date"].iloc[0] == "2026-01-01"
        assert df["period_end_date"].iloc[0] == "2026-04-01"
        assert len(df) == 1

    def test_non_all_time_joins_against_spine(self):
        """The non-all_time path produces period boundaries dynamically via
        the periods spine, not via static literals passed through."""
        expr, df = self._run(
            make_metric(),
            None,
            None,
            "monthly",
            ("2026-01-01", "2026-04-01"),
        )
        assert set(df["period_start_date"].str[:10]) == {"2026-01-01", "2026-02-01", "2026-03-01"}


# ---------------------------------------------------------------------------
# 5. build_queries() — validation + propagation
# ---------------------------------------------------------------------------


class TestBuildQueriesWithPeriodType:
    def test_unknown_period_type_raises(self):
        manager = _make_manager()
        with pytest.raises(QueryBuildError, match="Invalid period_type"):
            QueryBuilder.build_queries(
                [make_metric()],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                time_window=("2026-01-01", "2026-04-01"),
                period_type="quarterly",
            )

    def test_non_all_time_without_time_window_raises(self):
        manager = _make_manager()
        with pytest.raises(QueryBuildError, match="requires time_window"):
            QueryBuilder.build_queries(
                [make_metric()],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                time_window=None,
                period_type="monthly",
            )

    def test_non_all_time_missing_timestamp_col_raises(self):
        manager = _make_manager()
        with pytest.raises(QueryBuildError, match="timestamp_col"):
            QueryBuilder.build_queries(
                [make_metric_no_ts()],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                time_window=("2026-01-01", "2026-04-01"),
                period_type="monthly",
            )

    def test_monthly_valid_inputs_produces_correct_rows(self):
        manager = _make_manager(SETUP_SQL)
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
            connection_manager=manager,
            time_window=("2026-01-01", "2026-04-01"),
            period_type="monthly",
        )
        assert len(groups) == 1
        df = groups[0].expressions[0].execute()
        assert len(df) == 3

    def test_all_time_default_backward_compatible(self):
        manager = _make_manager(SETUP_SQL)
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
            connection_manager=manager,
        )
        assert len(groups) == 1
        df = groups[0].expressions[0].execute()
        assert df["period_type"].iloc[0] == "all_time"
        assert len(df) == 1


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
        manager = _make_manager()
        with pytest.raises(QueryBuildError, match="requires time_window"):
            QueryBuilder.build_queries(
                [make_metric()],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                time_window=None,
                period_type="hourly",
            )

    def test_hourly_missing_timestamp_col_raises(self):
        manager = _make_manager()
        with pytest.raises(QueryBuildError, match="timestamp_col"):
            QueryBuilder.build_queries(
                [make_metric_no_ts()],
                slice_specs=None,
                segment_spec=None,
                connection_manager=manager,
                time_window=("2026-01-01T00:00:00", "2026-01-01T03:00:00"),
                period_type="hourly",
            )

    def test_hourly_sql_executes_and_returns_correct_periods(self):
        setup = """
CREATE TABLE transactions AS
SELECT * FROM (VALUES
    (100.0, TIMESTAMP '2026-01-01 00:30:00'),
    (200.0, TIMESTAMP '2026-01-01 01:45:00'),
    (150.0, TIMESTAMP '2026-01-01 02:10:00')
) AS t(amount, event_ts)
"""
        manager = _make_manager(setup)
        groups = QueryBuilder.build_queries(
            [make_metric()],
            slice_specs=None,
            segment_spec=None,
            connection_manager=manager,
            time_window=("2026-01-01T00:00:00", "2026-01-01T03:00:00"),
            period_type="hourly",
        )
        df = groups[0].expressions[0].execute()
        assert len(df) == 3
        assert set(df["period_type"]) == {"hourly"}
        # Each row carries data from exactly one hour
        assert sorted(df["metric_value"].tolist()) == [100.0, 150.0, 200.0]
