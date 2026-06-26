"""
aitaem.connectors - Backend connection management

This module provides connectors for various OLAP databases and data sources.
"""

from aitaem.connectors.connection import ConnectionManager
from aitaem.connectors.ibis_connector import IbisConnector

__all__ = ["IbisConnector", "ConnectionManager"]
