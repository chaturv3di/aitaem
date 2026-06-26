"""
Integration tests for MetricSpec.format and the metric_format output column.

Verifies that format metadata flows from spec → SQL → ibis.Table correctly
for both the all_time and non-all_time query paths.
"""

from pathlib import Path

import pytest

from aitaem import MetricCompute
from aitaem.specs.loader import SpecCache
from aitaem.utils.formatting import STANDARD_COLUMNS

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
AD_CAMPAIGNS_SOURCE = "duckdb://ad_campaigns.duckdb/ad_campaigns"

_METRIC_PERCENTAGE = f"""
metric:
  name: ctr_pct
  source: {AD_CAMPAIGNS_SOURCE}
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
  format: percentage
"""

_METRIC_CURRENCY = f"""
metric:
  name: total_revenue
  source: {AD_CAMPAIGNS_SOURCE}
  numerator: "SUM(revenue)"
  timestamp_col: date
  format: "currency:USD"
"""

_METRIC_CURRENCY_NO_CODE = f"""
metric:
  name: ad_spend
  source: {AD_CAMPAIGNS_SOURCE}
  numerator: "SUM(ad_spend)"
  timestamp_col: date
  format: currency
"""

_METRIC_NO_FORMAT = f"""
metric:
  name: impressions
  source: {AD_CAMPAIGNS_SOURCE}
  numerator: "SUM(impressions)"
  timestamp_col: date
"""


@pytest.fixture(scope="module")
def mc_format(ad_campaigns_connection_manager):
    cache = SpecCache.from_string(
        metric_yaml=[
            _METRIC_PERCENTAGE,
            _METRIC_CURRENCY,
            _METRIC_CURRENCY_NO_CODE,
            _METRIC_NO_FORMAT,
        ]
    )
    return MetricCompute(cache, ad_campaigns_connection_manager)


# ---------------------------------------------------------------------------
# Column presence and ordering
# ---------------------------------------------------------------------------


def test_metric_format_column_present(mc_format):
    result = mc_format.compute("ctr_pct")
    assert "metric_format" in result.columns


def test_output_columns_match_standard(mc_format):
    result = mc_format.compute("ctr_pct")
    assert list(result.columns) == STANDARD_COLUMNS


def test_metric_format_column_position(mc_format):
    result = mc_format.compute("ctr_pct")
    cols = result.columns
    assert cols.index("metric_format") == cols.index("metric_name") + 1


# ---------------------------------------------------------------------------
# Correct format values per metric
# ---------------------------------------------------------------------------


def test_format_percentage_propagated(mc_format):
    df = mc_format.compute("ctr_pct").to_pandas()
    assert df["metric_format"].iloc[0] == "percentage"


def test_format_currency_with_code_propagated(mc_format):
    df = mc_format.compute("total_revenue").to_pandas()
    assert df["metric_format"].iloc[0] == "currency:USD"


def test_format_currency_no_code_propagated(mc_format):
    df = mc_format.compute("ad_spend").to_pandas()
    assert df["metric_format"].iloc[0] == "currency"


def test_format_absent_is_null(mc_format):
    df = mc_format.compute("impressions").to_pandas()
    assert df["metric_format"].isna().all()


# ---------------------------------------------------------------------------
# Mixed metrics in one compute() call
# ---------------------------------------------------------------------------


def test_mixed_metrics_correct_per_row_format(mc_format):
    df = mc_format.compute(["ctr_pct", "impressions"]).to_pandas()
    pct_rows = df[df["metric_name"] == "ctr_pct"]
    no_fmt_rows = df[df["metric_name"] == "impressions"]
    assert (pct_rows["metric_format"] == "percentage").all()
    assert no_fmt_rows["metric_format"].isna().all()


# ---------------------------------------------------------------------------
# format flows through non-all_time (period granularity) path
# ---------------------------------------------------------------------------


def test_format_propagated_in_monthly_period(mc_format):
    df = mc_format.compute(
        "total_revenue",
        time_window=("2024-01-01", "2024-04-01"),
        period_type="monthly",
    ).to_pandas()
    assert (df["metric_format"] == "currency:USD").all()


def test_no_format_is_null_in_monthly_period(mc_format):
    df = mc_format.compute(
        "impressions",
        time_window=("2024-01-01", "2024-04-01"),
        period_type="monthly",
    ).to_pandas()
    assert df["metric_format"].isna().all()
