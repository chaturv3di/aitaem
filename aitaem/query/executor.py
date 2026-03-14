"""
aitaem.query.executor - QueryExecutor

Executes QueryGroups using an injected ConnectionManager.
Gets one connector per source, executes each SQL string, pd.concat results.
"""

from __future__ import annotations

import logging

import pandas as pd

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
        output_format: str = "pandas",
    ) -> pd.DataFrame:
        """Execute all query groups sequentially and combine results.

        If a connection is missing for a group, log a warning and skip that group.
        Returns partial results if any groups succeed.

        Raises:
            QueryExecutionError: if ALL groups fail to produce results
        """
        all_dfs: list[pd.DataFrame] = []

        for group in query_groups:
            result = self._execute_query_group(group, output_format)
            if result is not None:
                all_dfs.append(result)

        if not all_dfs:
            raise QueryExecutionError(
                "All query groups failed to produce results. "
                "Check connection configuration and query specs."
            )

        return pd.concat(all_dfs, ignore_index=True)

    def _execute_query_group(
        self,
        query_group: QueryGroup,
        output_format: str,
    ) -> pd.DataFrame | None:
        """Execute all SQL queries in a single QueryGroup.

        Returns None (with warning) if connection is unavailable.
        """
        try:
            connector = self.connection_manager.get_connection_for_source(query_group.source)
        except (ConnectionNotFoundError, RuntimeError) as e:
            logger.warning("Skipping query group for source '%s': %s", query_group.source, e)
            return None

        dfs: list[pd.DataFrame] = []
        assert connector.connection is not None
        for sql in query_group.sql_queries:
            ibis_expr = connector.connection.sql(sql)
            df = connector.execute(ibis_expr, output_format)
            dfs.append(df)

        if not dfs:
            return None

        return pd.concat(dfs, ignore_index=True)
