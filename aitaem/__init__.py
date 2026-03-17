"""
aitaem - All Interesting Things Are Essentially Metrics

A Python library for generating data insights from OLAP databases or local CSV files.
"""

from aitaem.connectors.connection import ConnectionManager
from aitaem.insights import MetricCompute
from aitaem.specs.loader import SpecCache
from aitaem.utils.csv_to_duckdb import load_csvs_to_duckdb

__version__ = "0.1.0"

__all__ = ["SpecCache", "ConnectionManager", "MetricCompute", "load_csvs_to_duckdb"]
