"""data group: read-only access to source backends through the ConnectionManager."""

from __future__ import annotations

from typing import Any

import ibis
from pydantic_ai import FunctionToolset, RunContext

from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import (
    ToolOutput,
    apply_filter,
    artifact_tool,
    distribution_agg,
    rows,
    stringify_bounds,
)

SAMPLE_ROWS_MAX = 20


def _source_table(ctx: RunContext[SessionDeps], source: str) -> tuple[Any, ibis.Table]:
    cm = ctx.deps.connection_manager
    connector = cm.get_connection_for_source(source)
    table_name, database = cm.resolve_table_reference(source)
    return connector, connector.get_table(table_name, database=database)


def _require_column(table: ibis.Table, column: str) -> None:
    if column not in table.columns:
        raise ValueError(f"Column {column!r} not found. Available columns: {list(table.columns)}")


@artifact_tool
def list_tables(ctx: RunContext[SessionDeps], backend_type: str | None = None) -> ToolOutput:
    """List the tables of the connected backends as source URIs.

    Args:
        backend_type: The backend to list; every connected backend when omitted.
    """
    cm = ctx.deps.connection_manager
    tables: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    with ctx.deps.compute_guard():
        for bt in [backend_type] if backend_type else cm.backend_types:
            try:
                connector = cm.get_connection(bt)
                tables[bt] = [
                    uri
                    for name in connector.list_tables()
                    if (uri := connector.build_source_uri(name)) is not None
                ]
            except Exception as exc:  # noqa: BLE001
                errors[bt] = f"{type(exc).__name__}: {exc}"
    count = sum(len(v) for v in tables.values())
    payload = {"tables": tables, "errors": errors}
    summary = f"{count} tables on {', '.join(tables) or 'no backend'}"
    if errors:
        summary += f"; errors on {', '.join(errors)}"
    return ToolOutput(payload, "object", "table_list", summary, facts=payload)


@artifact_tool
def describe_table(ctx: RunContext[SessionDeps], source: str) -> ToolOutput:
    """Return the column names and types of a table.

    Args:
        source: The table's source URI, as `list_tables` returns it.
    """
    with ctx.deps.compute_guard():
        _, table = _source_table(ctx, source)
        columns = {name: str(dtype) for name, dtype in table.schema().items()}
    payload = {"source": source, "columns": columns}
    return ToolOutput(
        payload, "object", "table_schema", f"{source}: {len(columns)} columns", facts=payload
    )


@artifact_tool
def sample_rows(ctx: RunContext[SessionDeps], source: str, n: int = 10) -> ToolOutput:
    """Return the first rows of a table.

    Args:
        source: The table's source URI, as `list_tables` returns it.
        n: The number of rows, at most 20.
    """
    n = max(0, min(n, SAMPLE_ROWS_MAX))
    with ctx.deps.compute_guard():
        _, table = _source_table(ctx, source)
        arrow = table.limit(n).to_pyarrow()
    return ToolOutput(
        arrow,
        "table",
        "sample",
        f"{arrow.num_rows} sample rows of {source}",
        facts={"columns": arrow.column_names, "rows": rows(arrow)},
    )


@artifact_tool
def column_distribution(
    ctx: RunContext[SessionDeps],
    metric_name: str,
    column: str | None = None,
    filter: str | None = None,
) -> ToolOutput:
    """Summarize a column of a catalog metric's source table: count, nulls, bounds, and percentiles.

    Args:
        metric_name: A metric in the spec catalog; its source table is summarized.
        column: The column to summarize; the metric's `timestamp_col` when omitted.
        filter: A SQL predicate on the source table's columns, applied as a WHERE clause.
    """
    spec = ctx.deps.spec_cache.metrics.get(metric_name)
    if spec is None:
        raise ValueError(
            f"Unknown metric {metric_name!r}; column_distribution takes catalog metrics."
        )
    resolved = column or spec.timestamp_col
    if not resolved:
        raise ValueError("The metric has no timestamp_col; pass a column.")
    with ctx.deps.compute_guard():
        connector, table = _source_table(ctx, spec.source)
        _require_column(table, resolved)
        table = apply_filter(table, filter, connector.backend_type)
        arrow = stringify_bounds(distribution_agg(table, resolved, None).to_pyarrow())
    stats = rows(arrow)[0]
    return ToolOutput(
        arrow,
        "table",
        "distribution",
        f"{metric_name}.{resolved}: {stats['count']} values, {stats['min_val']} to {stats['max_val']}",
        facts={"metric_name": metric_name, "column": resolved, **stats},
    )


@artifact_tool
def date_range(
    ctx: RunContext[SessionDeps], source: str, date_column: str, filter: str | None = None
) -> ToolOutput:
    """Return the bounds of a date or timestamp column: count, nulls, min, max, distinct values.

    Args:
        source: The table's source URI, as `list_tables` returns it.
        date_column: A date or timestamp column of the table.
        filter: A SQL predicate on the table's columns, applied as a WHERE clause.
    """
    with ctx.deps.compute_guard():
        connector, table = _source_table(ctx, source)
        _require_column(table, date_column)
        dtype = table[date_column].type()
        if not dtype.is_temporal():
            raise ValueError(
                f"Column {date_column!r} has dtype {dtype}, which is not temporal; "
                "date_range summarizes date and timestamp columns."
            )
        table = apply_filter(table, filter, connector.backend_type)
        col = table[date_column]
        # Bounds only: no code path here computes a percentile, mean, or std.
        agg = table.aggregate(
            count=col.count(),
            null_count=col.isnull().sum(),
            min_val=col.min(),
            max_val=col.max(),
            distinct_count=col.nunique(),
        )
        bounds = rows(stringify_bounds(agg.to_pyarrow()))[0]
    payload = {"source": source, "column": date_column, **bounds}
    return ToolOutput(
        payload,
        "object",
        "date_range",
        f"{source}.{date_column}: {bounds['min_val']} to {bounds['max_val']}",
        facts=payload,
    )


data_toolset: FunctionToolset[SessionDeps] = FunctionToolset(
    [list_tables, describe_table, sample_rows, column_distribution, date_range]
)
