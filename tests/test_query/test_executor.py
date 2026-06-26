"""
Tests for aitaem.query.executor — QueryExecutor

Sub-feature coverage:
 _union_queries: lazy ibis.Table union per group
 execute: returns ibis.Table; single/multi-backend; all fail → exception; partial failure → warning
"""

import logging

import ibis
import pytest

from aitaem.connectors.connection import ConnectionManager
from aitaem.query.builder import QueryBuilder, QueryGroup
from aitaem.query.executor import QueryExecutor
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.utils.exceptions import QueryExecutionError

AD_CAMPAIGNS_SOURCE_URI = "duckdb://ad_campaigns.duckdb/ad_campaigns"
DIM_PLATFORMS_SOURCE_URI = "duckdb://ad_campaigns.duckdb/dim_platforms"

EXPECTED_OUTPUT_COLUMNS = {
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


# ---------------------------------------------------------------------------
# _union_queries
# ---------------------------------------------------------------------------


class TestUnionQueries:
    def test_union_queries_single_sql_returns_ibis_table(self, ad_campaigns_connection_manager):
        connector = ad_campaigns_connection_manager.get_connection("duckdb")
        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        sql = "SELECT 1 AS metric_value"
        result = executor._union_queries([sql], connector)
        assert isinstance(result, ibis.Table)

    def test_union_queries_multiple_sqls_returns_ibis_table(self, ad_campaigns_connection_manager):
        connector = ad_campaigns_connection_manager.get_connection("duckdb")
        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        sqls = ["SELECT 1 AS metric_value", "SELECT 2 AS metric_value"]
        result = executor._union_queries(sqls, connector)
        assert isinstance(result, ibis.Table)

    def test_union_queries_result_materialises_correctly(self, ad_campaigns_connection_manager):
        connector = ad_campaigns_connection_manager.get_connection("duckdb")
        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        sqls = ["SELECT 1 AS metric_value", "SELECT 2 AS metric_value"]
        result = executor._union_queries(sqls, connector)
        df = result.to_pandas()
        assert list(df.columns) == ["metric_value"]
        assert set(df["metric_value"].tolist()) == {1, 2}

    def test_union_queries_empty_list_returns_none(self, ad_campaigns_connection_manager):
        connector = ad_campaigns_connection_manager.get_connection("duckdb")
        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        result = executor._union_queries([], connector)
        assert result is None


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


class TestExecute:
    def test_execute_returns_ibis_table(self, ad_campaigns_connection_manager):
        """execute() always returns an ibis.Table."""
        metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        groups = QueryBuilder.build_queries([metric], slice_specs=None, segment_spec=None)
        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        result = executor.execute(groups)
        assert isinstance(result, ibis.Table)

    def test_full_integration_no_slice_no_segment(self, ad_campaigns_connection_manager):
        """End-to-end: single metric, no slices, no segments → 1 row with correct schema."""
        metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        groups = QueryBuilder.build_queries([metric], slice_specs=None, segment_spec=None)

        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        result = executor.execute(groups)
        df = result.to_pandas()

        assert EXPECTED_OUTPUT_COLUMNS.issubset(set(df.columns))
        assert len(df) == 1
        assert df["slice_type"].iloc[0] == "none"
        assert df["slice_value"].iloc[0] == "all"
        assert df["segment_name"].iloc[0] == "none"
        assert df["segment_value"].iloc[0] == "all"
        assert df["metric_value"].notna().all()

    def test_full_integration_with_slices_and_segment(self, ad_campaigns_connection_manager):
        """Integration: 1 metric × 1 slice (+ no-slice) × 1 segment (+ no-segment) = 4 queries."""
        metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        campaign_type_slice = SliceSpec(
            name="campaign_type",
            values=(
                SliceValue(name="Search", where="campaign_type = 'Search'"),
                SliceValue(name="Display", where="campaign_type = 'Display'"),
                SliceValue(name="Video", where="campaign_type = 'Video'"),
                SliceValue(name="Shopping", where="campaign_type = 'Shopping'"),
            ),
        )
        platform_segment = SegmentSpec(
            name="platform",
            source=DIM_PLATFORMS_SOURCE_URI,
            entity_id="platform",
            values=(
                SegmentValue(name="Google Ads", where="platform = 'Google Ads'"),
                SegmentValue(name="Meta Ads", where="platform = 'Meta Ads'"),
                SegmentValue(name="TikTok Ads", where="platform = 'TikTok Ads'"),
            ),
        )

        groups = QueryBuilder.build_queries(
            [metric],
            slice_specs=[campaign_type_slice],
            segment_spec=platform_segment,
        )
        # (1 slice + 1 no-slice) × (1 segment + 1 no-segment) = 4 queries
        assert len(groups[0].sql_queries) == 4

        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        result = executor.execute(groups)
        df = result.to_pandas()

        assert EXPECTED_OUTPUT_COLUMNS.issubset(set(df.columns))
        assert df["metric_value"].notna().all()
        slice_types = set(df["slice_type"].unique())
        assert "campaign_type" in slice_types
        assert "none" in slice_types
        segment_names = set(df["segment_name"].unique())
        assert "platform" in segment_names
        assert "none" in segment_names

    def test_raises_when_all_groups_fail(self):
        """QueryExecutionError raised when all groups fail (no connection)."""
        isolated_manager = ConnectionManager()

        groups = [
            QueryGroup(
                source="duckdb://nonexistent.db/table",
                metrics=[],
                sql_queries=["SELECT 1"],
            )
        ]
        executor = QueryExecutor(connection_manager=isolated_manager)
        with pytest.raises(QueryExecutionError):
            executor.execute(groups)

    def test_partial_failure_returns_partial_result(self, ad_campaigns_connection_manager, caplog):
        """One failing group + one succeeding group → partial result + warning."""
        good_metric = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        good_groups = QueryBuilder.build_queries([good_metric], slice_specs=None, segment_spec=None)

        bad_group = QueryGroup(
            source="bigquery://my-project/my_dataset/my_table",
            metrics=[],
            sql_queries=["SELECT 1"],
        )

        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        with caplog.at_level(logging.WARNING):
            result = executor.execute(good_groups + [bad_group])

        assert isinstance(result, ibis.Table)
        df = result.to_pandas()
        assert len(df) > 0
        assert any("Skipping" in r.message for r in caplog.records)

    def test_multiple_metrics_combined(self, ad_campaigns_connection_manager):
        """Multiple metrics from same source are combined into single ibis.Table."""
        ctr = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        roas = MetricSpec(
            name="roas",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(revenue)",
            denominator="SUM(ad_spend)",
            timestamp_col="date",
        )

        groups = QueryBuilder.build_queries([ctr, roas], slice_specs=None, segment_spec=None)
        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        result = executor.execute(groups)
        df = result.to_pandas()

        assert set(df["metric_name"].unique()) == {"ctr", "roas"}
        assert len(df) == 2

    def test_time_window_filters_data(self, ad_campaigns_connection_manager):
        """Results differ with time_window vs without (data is filtered)."""
        metric = MetricSpec(
            name="impressions_sum",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(impressions)",
            timestamp_col="date",
        )

        groups_all = QueryBuilder.build_queries([metric], slice_specs=None, segment_spec=None)
        groups_windowed = QueryBuilder.build_queries(
            [metric],
            slice_specs=None,
            segment_spec=None,
            time_window=("2024-01-01", "2024-04-01"),
        )

        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        total = float(executor.execute(groups_all).to_pandas()["metric_value"].iloc[0])
        windowed = float(executor.execute(groups_windowed).to_pandas()["metric_value"].iloc[0])
        assert windowed < total


# ---------------------------------------------------------------------------
# End-to-end integration scenario (from plan)
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """Full integration test using example specs and ad_campaigns dataset."""

    def test_ctr_by_geo_and_campaign_type_with_platform_segment(
        self, ad_campaigns_connection_manager
    ):
        """
        1 metric (ctr) × 2 independent slices (geo, campaign_type) × 1 segment (platform).

        New independent slicing: (2 slices + 1 no-slice) × (1 segment + 1 no-segment) = 6 queries.
        """
        ctr = MetricSpec(
            name="ctr",
            source=AD_CAMPAIGNS_SOURCE_URI,
            numerator="SUM(clicks)",
            denominator="SUM(impressions)",
            timestamp_col="date",
        )
        geo_slice = SliceSpec(
            name="geo",
            values=(
                SliceValue(name="USA", where="country = 'USA'"),
                SliceValue(name="EU", where="country IN ('UK', 'Germany')"),
                SliceValue(name="APAC", where="country IN ('India', 'Australia')"),
                SliceValue(
                    name="ROW",
                    where="country NOT IN ('USA', 'UK', 'Germany', 'India', 'Australia')",
                ),
            ),
        )
        campaign_type_slice = SliceSpec(
            name="campaign_type",
            values=(
                SliceValue(name="Search", where="campaign_type = 'Search'"),
                SliceValue(name="Display", where="campaign_type = 'Display'"),
                SliceValue(name="Video", where="campaign_type = 'Video'"),
                SliceValue(name="Shopping", where="campaign_type = 'Shopping'"),
            ),
        )
        platform_segment = SegmentSpec(
            name="platform",
            source=DIM_PLATFORMS_SOURCE_URI,
            entity_id="platform",
            values=(
                SegmentValue(name="Google Ads", where="platform = 'Google Ads'"),
                SegmentValue(name="Meta Ads", where="platform = 'Meta Ads'"),
                SegmentValue(name="TikTok Ads", where="platform = 'TikTok Ads'"),
            ),
        )

        groups = QueryBuilder.build_queries(
            [ctr],
            slice_specs=[geo_slice, campaign_type_slice],
            segment_spec=platform_segment,
        )
        # 1 metric × (2 slices + 1 no-slice) × (1 segment + 1 no-segment) = 6 queries in 1 group
        assert len(groups) == 1
        assert len(groups[0].sql_queries) == 6

        executor = QueryExecutor(connection_manager=ad_campaigns_connection_manager)
        result = executor.execute(groups)
        assert isinstance(result, ibis.Table)
        df = result.to_pandas()

        assert EXPECTED_OUTPUT_COLUMNS.issubset(set(df.columns))
        assert df["metric_value"].notna().all()

        slice_types = set(df["slice_type"].unique())
        assert "geo" in slice_types
        assert "campaign_type" in slice_types
        assert "none" in slice_types

        segment_names = set(df["segment_name"].unique())
        assert "platform" in segment_names
        assert "none" in segment_names

        platform_rows = df[df["segment_name"] == "platform"]
        assert set(platform_rows["segment_value"].unique()).issubset(
            {"Google Ads", "Meta Ads", "TikTok Ads"}
        )

        baseline_rows = df[df["segment_name"] == "none"]
        assert (baseline_rows["segment_value"] == "all").all()
