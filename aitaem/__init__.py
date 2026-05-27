"""
aitaem - All Interesting Things Are Essentially Metrics

A Python library for generating data insights from OLAP databases or local CSV files.
"""

from aitaem.connectors.connection import ConnectionManager
from aitaem.connectors.ibis_connector import IbisConnector
from aitaem.insights import MetricCompute
from aitaem.query.builder import PeriodType, VALID_PERIOD_TYPES
from aitaem.utils.formatting import STANDARD_COLUMNS
from aitaem.specs.loader import SpecCache
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.slice import SliceSpec, SliceValue
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

__version__ = "0.2.0"

__all__ = [
    "SpecCache",
    "ConnectionManager",
    "MetricCompute",
    "IbisConnector",
    # spec types
    "MetricSpec",
    "SliceSpec",
    "SliceValue",
    "SegmentSpec",
    "SegmentValue",
    # constants and types
    "PeriodType",
    "VALID_PERIOD_TYPES",
    "STANDARD_COLUMNS",
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
