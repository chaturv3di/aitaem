"""
aitaem.query.executor - QueryExecutor

Executes QueryGroups using an injected ConnectionManager.
Gets one connector per source, unions ibis expressions lazily within each group,
then unions across groups (same backend) or materialises via a caller-supplied
cross-backend DuckDB connection (different backends).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

import ibis
import pandas as pd

from aitaem.connectors.base import Connector
from aitaem.connectors.connection import ConnectionManager
from aitaem.query.builder import QueryGroup
from aitaem.utils.exceptions import ConnectionNotFoundError, QueryExecutionError

logger = logging.getLogger(__name__)


class QueryExecutor:
    """Execute QueryGroups using an injected ConnectionManager."""

    def __init__(self, connection_manager: ConnectionManager) -> None:
        """
        Args:
            connection_manager: Provides backend connections for query execution.
        """
        self.connection_manager = connection_manager

    def execute(
        self,
        query_groups: list[QueryGroup],
        cross_backend_conn_factory: Callable[[], ibis.BaseBackend] | None = None,
    ) -> ibis.Table:
        """Execute all query groups and combine results as a lazy ibis.Table.

        If a connection is missing for a group, log a warning and skip that group.
        Returns partial results if any groups succeed.

        When all groups share the same backend, results are unioned lazily and
        cross_backend_conn_factory is never called. When groups span multiple
        backends, cross_backend_conn_factory() is called once to obtain a DuckDB
        connection into which the combined result is loaded.

        Raises:
            QueryExecutionError: if ALL groups fail to produce results.
            QueryExecutionError: if groups span multiple backends and
                cross_backend_conn_factory is None.
        """
        tables: list[ibis.Table] = []
        backends: list[ibis.BaseBackend] = []

        for group in query_groups:
            try:
                connector = self.connection_manager.get_connection_for_source(group.source)
            except (ConnectionNotFoundError, RuntimeError) as e:
                logger.warning("Skipping query group for source '%s': %s", group.source, e)
                continue

            table = self._union_queries(group.sql_queries, connector)
            if table is not None:
                tables.append(table)
                assert connector.connection is not None
                backends.append(connector.connection)

        if not tables:
            raise QueryExecutionError(
                "All query groups failed to produce results. "
                "Check connection configuration and query specs."
            )

        if len(tables) == 1:
            return tables[0]

        if len(set(id(b) for b in backends)) == 1:
            # All groups on the same backend — lazy union
            combined = tables[0]
            for t in tables[1:]:
                combined = combined.union(t)
            return combined

        # Cross-backend: materialise each group, concat, reload into caller-supplied DuckDB.
        # The factory is called here (lazily) so no connection is created for single-backend calls.
        if cross_backend_conn_factory is None:
            raise QueryExecutionError(
                "Cross-backend query requires a cross_backend_conn_factory argument."
            )
        dfs = [t.to_pandas() for t in tables]
        df = pd.concat(dfs, ignore_index=True)
        table_name = f"__combined_{uuid.uuid4().hex[:8]}__"
        return cross_backend_conn_factory().create_table(table_name, obj=df)

    def _union_queries(
        self,
        sql_queries: list[str],
        connector: Connector,
    ) -> ibis.Table | None:
        """Combine SQL strings into a single lazy ibis.Table via ibis union."""
        assert connector.connection is not None
        if not sql_queries:
            return None
        tables = [connector.connection.sql(q) for q in sql_queries]
        result = tables[0]
        for t in tables[1:]:
            result = result.union(t)
        return result
