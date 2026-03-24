"""
Tests for aitaem.insights.MetricCompute — primary user interface.

Uses the session-scoped ad_campaigns_connection_manager fixture and
loads specs from examples/ directories end-to-end.
"""

from pathlib import Path

import pandas as pd
import pytest

from aitaem import MetricCompute
from aitaem.specs.loader import SpecCache
from aitaem.utils.exceptions import SpecNotFoundError
from aitaem.utils.formatting import STANDARD_COLUMNS

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
METRICS_DIR = EXAMPLES_DIR / "metrics"
SLICES_DIR = EXAMPLES_DIR / "slices"
SEGMENTS_DIR = EXAMPLES_DIR / "segments"


@pytest.fixture(scope="module")
def spec_cache():
    """SpecCache loaded from the examples/ directories."""
    return SpecCache.from_yaml(
        metric_paths=str(METRICS_DIR),
        slice_paths=str(SLICES_DIR),
        segment_paths=str(SEGMENTS_DIR),
    )


@pytest.fixture(scope="module")
def mc(spec_cache, ad_campaigns_connection_manager):
    """MetricCompute wired to the in-memory ad_campaigns DuckDB."""
    return MetricCompute(spec_cache, ad_campaigns_connection_manager)


# ---------------------------------------------------------------------------
# 1. Single metric, no slices or segments
# ---------------------------------------------------------------------------


def test_compute_single_metric_no_slices(mc):
    df = mc.compute("ctr")
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == STANDARD_COLUMNS
    assert len(df) == 1
    assert df["metric_name"].iloc[0] == "ctr"
    assert df["slice_type"].iloc[0] == "none"
    assert df["segment_name"].iloc[0] == "none"
    assert df["metric_value"].notna().all()


# ---------------------------------------------------------------------------
# 2. Single metric with a slice
# ---------------------------------------------------------------------------


def test_compute_single_metric_with_slice(mc):
    df = mc.compute("ctr", slices="campaign_type")
    # 4 campaign_type values + 1 all-slice baseline = 5 rows
    # (no segment baseline × all)
    assert len(df) == 5
    sliced = df[df["slice_type"] == "campaign_type"]
    assert len(sliced) == 4
    assert set(sliced["slice_value"]) == {"Search", "Display", "Video", "Shopping"}
    baseline = df[df["slice_type"] == "none"]
    assert len(baseline) == 1
    assert baseline["slice_value"].iloc[0] == "all"


# ---------------------------------------------------------------------------
# 3. Single metric with a segment
# ---------------------------------------------------------------------------


def test_compute_single_metric_with_segment(mc):
    df = mc.compute("ctr", segments="platform")
    # 3 platform values + 1 all-segment baseline = 4 rows
    assert len(df) == 4
    segmented = df[df["segment_name"] == "platform"]
    assert len(segmented) == 3
    assert set(segmented["segment_value"]) == {"Google Ads", "Meta Ads", "TikTok Ads"}
    baseline = df[df["segment_name"] == "none"]
    assert len(baseline) == 1
    assert baseline["segment_value"].iloc[0] == "all"


# ---------------------------------------------------------------------------
# 4. Time window filtering
# ---------------------------------------------------------------------------


def test_compute_with_time_window(mc):
    time_window = ("2024-01-01", "2024-04-01")
    df = mc.compute("ctr", time_window=time_window)
    assert df["period_start_date"].iloc[0] == "2024-01-01"
    assert df["period_end_date"].iloc[0] == "2024-04-01"
    assert df["metric_value"].notna().all()

    # Windowed result should differ from all-time (fewer data points)
    df_all = mc.compute("total_revenue")  # no time_window, uses all rows
    df_windowed = mc.compute("total_revenue", time_window=time_window)
    assert float(df_windowed["metric_value"].iloc[0]) != float(df_all["metric_value"].iloc[0])


# ---------------------------------------------------------------------------
# 5. Multiple metrics
# ---------------------------------------------------------------------------


def test_compute_multiple_metrics(mc):
    df = mc.compute(["ctr", "roas", "total_revenue"])
    assert set(df["metric_name"]) == {"ctr", "roas", "total_revenue"}
    # 3 metrics × 1 row each (no slices/segments)
    assert len(df) == 3


# ---------------------------------------------------------------------------
# 6. Multiple slices
# ---------------------------------------------------------------------------


def test_compute_multiple_slices(mc):
    df = mc.compute("ctr", slices=["campaign_type", "geo"])
    # Two slices computed independently, each produces their own rows + baselines
    campaign_rows = df[df["slice_type"] == "campaign_type"]
    geo_rows = df[df["slice_type"] == "geo"]
    assert len(campaign_rows) > 0
    assert len(geo_rows) > 0
    # Verify both slices are present as independent rows (not cross-product)
    assert set(df["slice_type"]).issuperset({"campaign_type", "geo", "none"})


# ---------------------------------------------------------------------------
# 7. Error: metric not found
# ---------------------------------------------------------------------------


def test_compute_metric_not_found(mc):
    with pytest.raises(SpecNotFoundError):
        mc.compute("nonexistent_metric")


# ---------------------------------------------------------------------------
# 8. Error: slice not found
# ---------------------------------------------------------------------------


def test_compute_slice_not_found(mc):
    with pytest.raises(SpecNotFoundError):
        mc.compute("ctr", slices="nonexistent_slice")


# ---------------------------------------------------------------------------
# 9. Output column order matches STANDARD_COLUMNS exactly
# ---------------------------------------------------------------------------


def test_output_column_order(mc):
    df = mc.compute("ctr")
    assert list(df.columns) == STANDARD_COLUMNS


# ---------------------------------------------------------------------------
# 10. Default output is a pandas DataFrame
# ---------------------------------------------------------------------------


def test_compute_returns_pandas_by_default(mc):
    df = mc.compute("ctr")
    assert isinstance(df, pd.DataFrame)
