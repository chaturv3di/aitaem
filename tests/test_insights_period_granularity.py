"""
Integration tests for MetricCompute.compute() with period_type parameter.

Uses the session-scoped ad_campaigns fixture (in-memory DuckDB) and the
existing example specs. Validates the full pipeline from compute() down to SQL.
"""

from pathlib import Path

import pytest

from aitaem import MetricCompute
from aitaem.specs.loader import SpecCache
from aitaem.utils.exceptions import QueryBuildError
from aitaem.utils.formatting import STANDARD_COLUMNS

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.fixture(scope="module")
def spec_cache():
    return SpecCache.from_yaml(
        metric_paths=str(EXAMPLES_DIR / "metrics"),
        slice_paths=str(EXAMPLES_DIR / "slices"),
        segment_paths=str(EXAMPLES_DIR / "segments"),
    )


@pytest.fixture(scope="module")
def mc(spec_cache, ad_campaigns_connection_manager):
    return MetricCompute(spec_cache, ad_campaigns_connection_manager)


# ---------------------------------------------------------------------------
# Backward compatibility — period_type='all_time' (default)
# ---------------------------------------------------------------------------


def test_all_time_default_no_regression(mc):
    """Omitting period_type produces the same result as period_type='all_time'."""
    df_default = mc.compute("ctr")
    df_explicit = mc.compute("ctr", period_type="all_time")
    assert df_default.equals(df_explicit)
    assert df_default["period_type"].iloc[0] == "all_time"


def test_all_time_with_time_window_no_regression(mc):
    """all_time + time_window still uses static period literals."""
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-03-01"))
    assert df["period_type"].iloc[0] == "all_time"
    assert df["period_start_date"].iloc[0] == "2024-01-01"
    assert df["period_end_date"].iloc[0] == "2024-03-01"
    assert len(df) == 1


# ---------------------------------------------------------------------------
# period_type='monthly' — basic correctness
# ---------------------------------------------------------------------------


def test_monthly_period_type_column(mc):
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
    assert set(df["period_type"]) == {"monthly"}


def test_monthly_produces_one_row_per_month(mc):
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
    # 3 months → 3 rows (no slice, no segment)
    assert len(df) == 3


def test_monthly_period_start_dates(mc):
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
    starts = set(df["period_start_date"].str[:10])
    assert starts == {"2024-01-01", "2024-02-01", "2024-03-01"}


def test_monthly_period_end_dates(mc):
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
    df_jan = df[df["period_start_date"].str.startswith("2024-01")]
    assert df_jan["period_end_date"].iloc[0][:10] == "2024-02-01"
    df_mar = df[df["period_start_date"].str.startswith("2024-03")]
    assert df_mar["period_end_date"].iloc[0][:10] == "2024-04-01"


def test_monthly_output_has_standard_columns(mc):
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
    assert list(df.columns) == STANDARD_COLUMNS


def test_monthly_metric_values_non_null(mc):
    df = mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="monthly")
    assert df["metric_value"].notna().all()


def test_monthly_metric_value_matches_independent_calculation(mc, ad_campaigns_connection_manager):
    """metric_value for each month matches a direct sum computed independently."""

    df = mc.compute("ctr", time_window=("2024-01-01", "2024-02-01"), period_type="monthly")
    assert len(df) == 1
    computed_value = df["metric_value"].iloc[0]

    # Compute expected value directly via DuckDB
    conn = ad_campaigns_connection_manager.get_connection("duckdb").connection
    result = conn.sql(
        """
        SELECT SUM(clicks) / NULLIF(SUM(impressions), 0) AS val
        FROM ad_campaigns
        WHERE date >= '2024-01-01' AND date < '2024-02-01'
        """
    ).to_pandas()
    expected = result["val"].iloc[0]
    assert computed_value == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# period_type='monthly' with slices and segments
# ---------------------------------------------------------------------------


def test_monthly_with_slice_produces_multiple_rows(mc):
    df = mc.compute(
        "ctr",
        slices="geo",
        time_window=("2024-01-01", "2024-04-01"),
        period_type="monthly",
    )
    # Multiple months × multiple slice values (at least as many as months alone)
    assert len(df) > 3
    assert set(df["period_type"]) == {"monthly"}


# ---------------------------------------------------------------------------
# Validation errors propagated through compute()
# ---------------------------------------------------------------------------


def test_invalid_period_type_raises_query_build_error(mc):
    with pytest.raises(QueryBuildError, match="Invalid period_type"):
        mc.compute("ctr", time_window=("2024-01-01", "2024-04-01"), period_type="quarterly")


def test_monthly_without_time_window_raises_query_build_error(mc):
    with pytest.raises(QueryBuildError, match="requires time_window"):
        mc.compute("ctr", period_type="monthly")
