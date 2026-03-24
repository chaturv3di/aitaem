"""
aitaem.utils.formatting - Standard output formatting utilities
"""

from __future__ import annotations

import pandas as pd

STANDARD_COLUMNS = [
    "period_type",
    "period_start_date",
    "period_end_date",
    "entity_id",
    "metric_name",
    "slice_type",
    "slice_value",
    "segment_name",
    "segment_value",
    "metric_value",
]


def ensure_standard_output(df: pd.DataFrame) -> pd.DataFrame:
    """Reorder columns to match the standard output schema.

    Raises:
        ValueError: if any required column is missing from the DataFrame.
    """
    missing = set(STANDARD_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing expected columns: {missing}")
    return df[STANDARD_COLUMNS]
