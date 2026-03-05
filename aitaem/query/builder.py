"""
aitaem.query.builder - QueryBuilder and QueryGroup

Translates MetricSpec / SliceSpec / SegmentSpec into SQL strings grouped by source URI.
No database connection required — all work is string manipulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aitaem.connectors.connection import ConnectionManager
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import QueryBuildError


@dataclass
class QueryGroup:
    """All SQL queries for a single source URI."""

    source: str
    metrics: list[MetricSpec]
    sql_queries: list[str] = field(default_factory=list)


class QueryBuilder:
    """Builds SQL query strings from specs.  All methods are static."""

    @staticmethod
    def build_queries(
        metric_specs: list[MetricSpec],
        slice_specs: list[SliceSpec] | None,
        segment_specs: list[SegmentSpec] | None,
        time_window: tuple[str, str] | None = None,
        timestamp_col: str | None = None,
    ) -> list[QueryGroup]:
        """Build optimized query groups (one per unique source table).

        Each QueryGroup contains all SQL queries for that source:
        one per (metric × (each segment_spec + the no-segment baseline)).

        Raises:
            QueryBuildError: if metric_specs is empty
            QueryBuildError: if time_window provided but timestamp_col is None
        """
        if not metric_specs:
            raise QueryBuildError("metric_specs must not be empty")

        if time_window is not None and timestamp_col is None:
            raise QueryBuildError("timestamp_col is required when time_window is provided")

        # Build time filter SQL once
        time_filter_sql: str | None = None
        if time_window is not None:
            time_filter_sql = QueryBuilder._build_time_filter_sql(time_window, timestamp_col)

        # Period metadata
        period_type = "all_time"
        period_start: str | None = time_window[0] if time_window else None
        period_end: str | None = time_window[1] if time_window else None

        groups_by_source = QueryBuilder._group_by_source(metric_specs)

        query_groups: list[QueryGroup] = []
        for source, metrics in groups_by_source.items():
            sql_queries: list[str] = []
            for metric in metrics:
                sql_queries.extend(
                    QueryBuilder._build_queries_for_metric(
                        metric=metric,
                        slice_specs=slice_specs,
                        segment_specs=segment_specs,
                        time_filter_sql=time_filter_sql,
                        period_type=period_type,
                        period_start=period_start,
                        period_end=period_end,
                    )
                )
            query_groups.append(QueryGroup(source=source, metrics=metrics, sql_queries=sql_queries))

        return query_groups

    @staticmethod
    def _group_by_source(metric_specs: list[MetricSpec]) -> dict[str, list[MetricSpec]]:
        """Group metric specs by source URI."""
        groups: dict[str, list[MetricSpec]] = {}
        for metric in metric_specs:
            groups.setdefault(metric.source, []).append(metric)
        return groups

    @staticmethod
    def _build_queries_for_metric(
        metric: MetricSpec,
        slice_specs: list[SliceSpec] | None,
        segment_specs: list[SegmentSpec] | None,
        time_filter_sql: str | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
    ) -> list[str]:
        """Build all SQL queries for one metric.

        Returns one query per segment spec, plus one for the no-segment baseline.
        len(result) == len(segment_specs) + 1  (or 1 if no segment_specs)
        """
        table_name = QueryBuilder._parse_table_name_from_uri(metric.source)
        queries: list[str] = []

        # One query per segment spec
        if segment_specs:
            for seg_spec in segment_specs:
                queries.append(
                    QueryBuilder._build_metric_segment_query(
                        metric=metric,
                        table_name=table_name,
                        slice_specs=slice_specs,
                        segment_spec=seg_spec,
                        time_filter_sql=time_filter_sql,
                        period_type=period_type,
                        period_start=period_start,
                        period_end=period_end,
                    )
                )

        # No-segment baseline query
        queries.append(
            QueryBuilder._build_metric_segment_query(
                metric=metric,
                table_name=table_name,
                slice_specs=slice_specs,
                segment_spec=None,
                time_filter_sql=time_filter_sql,
                period_type=period_type,
                period_start=period_start,
                period_end=period_end,
            )
        )

        return queries

    @staticmethod
    def _build_metric_segment_query(
        metric: MetricSpec,
        table_name: str,
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        time_filter_sql: str | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
    ) -> str:
        """Build a single SQL query for one (metric, segment_spec | None) combination."""
        # --- CTE SELECT columns ---
        cte_extra_cols: list[str] = []
        slice_aliases: list[str] = []

        if slice_specs:
            for ss in slice_specs:
                alias = f"_slice_{ss.name}"
                slice_aliases.append(alias)
                cte_extra_cols.append(QueryBuilder._build_slice_case_when_expr(ss, alias))

        segment_alias: str | None = None
        if segment_spec is not None:
            segment_alias = "_segment"
            cte_extra_cols.append(
                QueryBuilder._build_segment_case_when_expr(segment_spec, segment_alias)
            )

        # --- CTE body ---
        cte_select = "    SELECT\n        *"
        if cte_extra_cols:
            cte_select += ",\n        " + ",\n        ".join(cte_extra_cols)

        cte_from = f"    FROM {table_name}"
        cte_where = f"    WHERE {time_filter_sql}" if time_filter_sql else ""

        cte_lines = [cte_select, cte_from]
        if cte_where:
            cte_lines.append(cte_where)
        cte_body = "\n".join(cte_lines)

        # --- Outer SELECT scalar columns ---
        def lit(v: str | None) -> str:
            return f"'{v}'" if v is not None else "NULL"

        slice_type_val = "|".join(ss.name for ss in slice_specs) if slice_specs else "none"
        slice_value_expr = (
            QueryBuilder._build_slice_value_concat_expr(slice_aliases) if slice_aliases else "'all'"
        )

        segment_name_val = segment_spec.name if segment_spec else "none"
        segment_value_expr = segment_alias if segment_alias else "'all'"

        metric_value_expr = QueryBuilder._build_metric_value_expr(metric)

        outer_select_cols = [
            f"    {lit(period_type)}                AS period_type",
            f"    {lit(period_start)}               AS period_start_date",
            f"    {lit(period_end)}                 AS period_end_date",
            f"    '{metric.name}'                   AS metric_name",
            f"    '{slice_type_val}'                AS slice_type",
            f"    {slice_value_expr}                AS slice_value",
            f"    '{segment_name_val}'              AS segment_name",
            f"    {segment_value_expr}              AS segment_value",
            f"    {metric_value_expr}               AS metric_value",
        ]
        outer_select = "SELECT\n" + ",\n".join(outer_select_cols)

        # --- WHERE (IS NOT NULL filters) ---
        null_filters: list[str] = []
        for alias in slice_aliases:
            null_filters.append(f"{alias} IS NOT NULL")
        if segment_alias:
            null_filters.append(f"{segment_alias} IS NOT NULL")

        outer_where = "WHERE " + "\n  AND ".join(null_filters) if null_filters else ""

        # --- GROUP BY ---
        group_by_cols = list(slice_aliases)
        if segment_alias:
            group_by_cols.append(segment_alias)
        outer_group_by = f"GROUP BY {', '.join(group_by_cols)}" if group_by_cols else ""

        # --- Assemble ---
        parts = [f"WITH _labeled AS (\n{cte_body}\n)\n{outer_select}", "FROM _labeled"]
        if outer_where:
            parts.append(outer_where)
        if outer_group_by:
            parts.append(outer_group_by)

        return "\n".join(parts)

    @staticmethod
    def _build_slice_case_when_expr(slice_spec: SliceSpec, alias: str) -> str:
        """Build CASE WHEN expression for a SliceSpec."""
        when_clauses = "\n        ".join(
            f"WHEN {sv.where} THEN '{sv.name}'" for sv in slice_spec.values
        )
        return f"CASE\n        {when_clauses}\n        ELSE NULL\n    END AS {alias}"

    @staticmethod
    def _build_segment_case_when_expr(segment_spec: SegmentSpec, alias: str) -> str:
        """Build CASE WHEN expression for a SegmentSpec."""
        when_clauses = "\n        ".join(
            f"WHEN {sv.where} THEN '{sv.name}'" for sv in segment_spec.values
        )
        return f"CASE\n        {when_clauses}\n        ELSE NULL\n    END AS {alias}"

    @staticmethod
    def _build_slice_value_concat_expr(slice_aliases: list[str]) -> str:
        """Build slice_value column expression by concatenating slice aliases with '|'."""
        if len(slice_aliases) == 1:
            return slice_aliases[0]
        return " || '|' || ".join(slice_aliases)

    @staticmethod
    def _build_metric_value_expr(metric: MetricSpec) -> str:
        """Build the metric value SQL expression."""
        if metric.aggregation == "ratio":
            return f"{metric.numerator} / NULLIF({metric.denominator}, 0)"
        return metric.numerator

    @staticmethod
    def _build_time_filter_sql(time_window: tuple[str, str], timestamp_col: str) -> str:
        """Build time window filter condition."""
        start, end = time_window
        return f"{timestamp_col} >= '{start}' AND {timestamp_col} < '{end}'"

    @staticmethod
    def _parse_table_name_from_uri(source_uri: str) -> str:
        """Extract table name from source URI for use in SQL FROM clause."""
        backend_type, _, table = ConnectionManager.parse_source_uri(source_uri)
        if backend_type == "bigquery":
            return table  # already 'dataset.table'
        return table
