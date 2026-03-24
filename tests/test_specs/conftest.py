"""Shared fixtures for specs module tests."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

VALID_METRIC_RATIO_YAML = """
metric:
  name: homepage_ctr
  description: Click-through rate
  source: duckdb://analytics.db/events
  numerator: "SUM(CASE WHEN event_type = 'click' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' THEN 1 ELSE 0 END)"
  timestamp_col: event_ts
"""

VALID_METRIC_SUM_YAML = """
metric:
  name: total_revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: transaction_date
"""

VALID_SLICE_YAML = """
slice:
  name: geography
  description: Regional breakdown
  values:
    - name: North America
      where: "country_code IN ('US', 'CA')"
    - name: Europe
      where: "country_code IN ('DE', 'FR')"
"""

VALID_SEGMENT_YAML = """
segment:
  name: customer_value_tier
  description: Customer segmentation by value
  source: duckdb://analytics.db/customers
  values:
    - name: high_value
      where: "lifetime_value > 1000 AND customer_status = 'active'"
    - name: low_value
      where: "lifetime_value <= 1000 OR customer_status != 'active'"
"""


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def valid_metric_ratio_yaml():
    return VALID_METRIC_RATIO_YAML


@pytest.fixture
def valid_metric_sum_yaml():
    return VALID_METRIC_SUM_YAML


@pytest.fixture
def valid_slice_yaml():
    return VALID_SLICE_YAML


@pytest.fixture
def valid_segment_yaml():
    return VALID_SEGMENT_YAML
