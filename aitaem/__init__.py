"""
aitaem - All Interesting Things Are Essentially Metrics

A Python library for generating data insights from OLAP databases or local CSV files.
"""

from aitaem.connectors.connection import ConnectionManager
from aitaem.insights import MetricCompute
from aitaem.query.builder import PeriodType, VALID_PERIOD_TYPES
from aitaem.specs.loader import SpecCache
from aitaem.utils.exceptions import (
    AitaemConnectionError,
    AitaemError,
    ConfigurationError,
    ConnectionNotFoundError,
    InvalidURIError,
    QueryBuildError,
    QueryExecutionError,
    SpecNotFoundError,
    SpecValidationError,
    TableNotFoundError,
    UnsupportedBackendError,
)

__version__ = "0.1.0"

__all__ = [
    "SpecCache",
    "ConnectionManager",
    "MetricCompute",
    # constants and types
    "PeriodType",
    "VALID_PERIOD_TYPES",
    # exceptions
    "AitaemError",
    "AitaemConnectionError",
    "ConnectionNotFoundError",
    "TableNotFoundError",
    "ConfigurationError",
    "InvalidURIError",
    "UnsupportedBackendError",
    "QueryBuildError",
    "QueryExecutionError",
    "SpecValidationError",
    "SpecNotFoundError",
]
