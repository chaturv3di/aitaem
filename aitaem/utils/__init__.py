"""
aitaem.utils - Utility functions and classes

This module provides common utilities used across the aitaem library.
"""

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
    "AitaemError",
    "ConnectionError",
    "ConnectionNotFoundError",
    "TableNotFoundError",
    "ConfigurationError",
    "InvalidURIError",
    "UnsupportedBackendError",
    "QueryExecutionError",
]
