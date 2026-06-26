"""
aitaem.utils.formatting - Standard output formatting utilities
"""

from __future__ import annotations

import ibis

STANDARD_COLUMNS: list[str] = [
    "period_type",
    "period_start_date",
    "period_end_date",
    "entity_id",
    "metric_name",
    "metric_format",
    "slice_type",
    "slice_value",
    "segment_name",
    "segment_value",
    "metric_value",
]


def ensure_standard_output(table: ibis.Table) -> ibis.Table:
    """Select and reorder columns to match the standard output schema.

    Raises:
        ValueError: if any required column is missing from the Table.
    """
    missing = set(STANDARD_COLUMNS) - set(table.columns)
    if missing:
        raise ValueError(f"Table missing expected columns: {missing}")
    return table.select(STANDARD_COLUMNS)
