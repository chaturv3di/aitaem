"""Tests for P3.0a IbisConnector.list_tables() and P3.0c ConnectionManager.backend_types."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aitaem.connectors.ibis_connector import IbisConnector
from aitaem.connectors.connection import ConnectionManager
from aitaem.utils.exceptions import AitaemConnectionError


# ---------------------------------------------------------------------------
# P3.0a: IbisConnector.list_tables()
# ---------------------------------------------------------------------------


def test_list_tables_delegates_to_connection():
    connector = IbisConnector("duckdb")
    mock_conn = MagicMock()
    mock_conn.list_tables.return_value = ["events", "users", "orders"]
    connector.connection = mock_conn

    result = connector.list_tables()

    mock_conn.list_tables.assert_called_once()
    assert result == ["events", "users", "orders"]


def test_list_tables_raises_when_not_connected():
    connector = IbisConnector("duckdb")
    assert not connector.is_connected

    with pytest.raises(AitaemConnectionError, match="Not connected"):
        connector.list_tables()


def test_list_tables_returns_empty_list_when_no_tables():
    connector = IbisConnector("duckdb")
    mock_conn = MagicMock()
    mock_conn.list_tables.return_value = []
    connector.connection = mock_conn

    result = connector.list_tables()
    assert result == []


def test_list_tables_with_duckdb_in_memory():
    """Integration test: actual DuckDB in-memory backend."""
    import ibis

    connector = IbisConnector("duckdb")
    connector.connection = ibis.duckdb.connect(":memory:")

    # No tables yet
    tables = connector.list_tables()
    assert isinstance(tables, list)
    assert len(tables) == 0


# ---------------------------------------------------------------------------
# P3.0c: ConnectionManager.backend_types
# ---------------------------------------------------------------------------


def test_backend_types_returns_empty_when_no_connections():
    cm = ConnectionManager()
    assert cm.backend_types == []


def test_backend_types_returns_registered_keys():
    cm = ConnectionManager()
    mock_connector = MagicMock(spec=IbisConnector)
    mock_connector.backend_type = "duckdb"
    cm._connections["duckdb"] = mock_connector

    assert cm.backend_types == ["duckdb"]


def test_backend_types_returns_all_registered_keys():
    cm = ConnectionManager()
    for bt in ("duckdb", "bigquery", "postgres"):
        mock = MagicMock(spec=IbisConnector)
        mock.backend_type = bt
        cm._connections[bt] = mock

    result = cm.backend_types
    assert set(result) == {"duckdb", "bigquery", "postgres"}
    assert len(result) == 3


def test_backend_types_is_a_list():
    cm = ConnectionManager()
    assert isinstance(cm.backend_types, list)
