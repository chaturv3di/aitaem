"""
aitaem.utils - Utility functions and classes

This module provides common utilities used across the aitaem library.
"""

from aitaem.utils.csv_to_duckdb import load_csvs_to_duckdb
from aitaem.utils.exceptions import (
    AitaemError,
    ConnectionError,
    ConnectionNotFoundError,
    TableNotFoundError,
    ConfigurationError,
    InvalidURIError,
    UnsupportedBackendError,
    QueryExecutionError,
)

__all__ = [
    "load_csvs_to_duckdb",
    "AitaemError",
    "ConnectionError",
    "ConnectionNotFoundError",
    "TableNotFoundError",
    "ConfigurationError",
    "InvalidURIError",
    "UnsupportedBackendError",
    "QueryExecutionError",
]
