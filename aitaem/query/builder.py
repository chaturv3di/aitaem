"""
aitaem.query.builder - QueryBuilder and QueryGroup

Translates MetricSpec / SliceSpec / SegmentSpec into SQL strings grouped by source URI.
No database connection required — all work is string manipulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from aitaem.connectors.connection import ConnectionManager
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import QueryBuildError

_VALID_PERIOD_TYPES = frozenset({"all_time", "daily", "weekly", "monthly", "yearly"})


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
        spec_cache: "SpecCache | None" = None,  # type: ignore[name-defined]  # noqa: F821
        period_type: str = "all_time",
        by_entity: str | None = None,
    ) -> list[QueryGroup]:
        """Build optimized query groups (one per unique source table).

        Each QueryGroup contains all SQL queries for that source:
        one per (metric × (each segment_spec + the no-segment baseline)).

        Args:
            spec_cache: Required only when composite slices are present.
            period_type: One of 'all_time', 'daily', 'weekly', 'monthly', 'yearly'.
                         Non-'all_time' requires time_window and timestamp_col on all metrics.
            by_entity: Column name to group by for entity-level metrics. When set, every
                       metric in metric_specs must list this column in its ``entities`` field.

        Raises:
            QueryBuildError: if metric_specs is empty
            QueryBuildError: if a composite slice is used but spec_cache is None
            QueryBuildError: if period_type is not a valid value
            QueryBuildError: if period_type != 'all_time' and time_window is None
            QueryBuildError: if period_type != 'all_time' and any metric has no timestamp_col
            QueryBuildError: if by_entity is set and any metric does not list it in entities
        """
        if not metric_specs:
            raise QueryBuildError("metric_specs must not be empty")

        if period_type not in _VALID_PERIOD_TYPES:
            raise QueryBuildError(
                f"Invalid period_type '{period_type}'. Must be one of {sorted(_VALID_PERIOD_TYPES)}"
            )

        if period_type != "all_time" and time_window is None:
            raise QueryBuildError(f"period_type='{period_type}' requires time_window to be set")

        if period_type != "all_time":
            for metric in metric_specs:
                if not metric.timestamp_col:
                    raise QueryBuildError(
                        f"period_type='{period_type}' requires timestamp_col on all metrics, "
                        f"but metric '{metric.name}' has none"
                    )

        if by_entity is not None:
            for metric in metric_specs:
                if not metric.entities or by_entity not in metric.entities:
                    raise QueryBuildError(
                        f"by_entity='{by_entity}' is not supported by metric '{metric.name}'. "
                        f"Supported entities: {metric.entities or []}"
                    )

        # Period metadata (used for all_time only; non-all_time uses dynamic SQL expressions)
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
                        time_window=time_window,
                        period_type=period_type,
                        period_start=period_start,
                        period_end=period_end,
                        spec_cache=spec_cache,
                        by_entity=by_entity,
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
        time_window: tuple[str, str] | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
        spec_cache: "SpecCache | None" = None,  # type: ignore[name-defined]  # noqa: F821
        by_entity: str | None = None,
    ) -> list[str]:
        """Build all SQL queries for one metric.

        Each SliceSpec is processed independently (+ a no-slice/all baseline).
        For each (slice_spec | None) × (segment_spec | None) combination, one SQL
        query is generated. Composite SliceSpecs are resolved via spec_cache.

        len(result) == (len(slice_specs) + 1) × (len(segment_specs) + 1)
        """
        time_filter_sql: str | None = None
        if time_window is not None:
            time_filter_sql = QueryBuilder._build_time_filter_sql(time_window, metric.timestamp_col)
        table_name = QueryBuilder._parse_table_name_from_uri(metric.source)
        queries: list[str] = []

        all_slice_specs: list[SliceSpec | None] = list(slice_specs) if slice_specs else []
        all_slice_specs.append(None)  # no-slice baseline

        all_segment_specs: list[SegmentSpec | None] = list(segment_specs) if segment_specs else []
        all_segment_specs.append(None)  # no-segment baseline

        for slice_spec in all_slice_specs:
            resolved_slices = QueryBuilder._resolve_slice_components(slice_spec, spec_cache)
            for seg_spec in all_segment_specs:
                queries.append(
                    QueryBuilder._build_metric_segment_query(
                        metric=metric,
                        table_name=table_name,
                        slice_specs=resolved_slices,
                        segment_spec=seg_spec,
                        time_filter_sql=time_filter_sql,
                        period_type=period_type,
                        period_start=period_start,
                        period_end=period_end,
                        time_window=time_window,
                        by_entity=by_entity,
                    )
                )

        return queries

    @staticmethod
    def _resolve_slice_components(
        slice_spec: SliceSpec | None,
        spec_cache: "SpecCache | None",  # type: ignore[name-defined]  # noqa: F821
    ) -> list[SliceSpec] | None:
        """Return component specs for a SliceSpec.

        - None → None (no-slice baseline)
        - Leaf spec → [slice_spec]
        - Composite spec → fetch each referenced SliceSpec from spec_cache

        Raises:
            QueryBuildError: if composite spec used and spec_cache is None
            SpecNotFoundError: if a referenced name is not in the cache
        """
        if slice_spec is None:
            return None
        if not slice_spec.is_composite:
            return [slice_spec]
        if spec_cache is None:
            raise QueryBuildError(
                f"Composite slice '{slice_spec.name}' requires a SpecCache to resolve its "
                f"components {list(slice_spec.cross_product)}, but no spec_cache was provided "
                f"to build_queries()."
            )
        return [spec_cache.get_slice(name) for name in slice_spec.cross_product]

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
        time_window: tuple[str, str] | None = None,
        by_entity: str | None = None,
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

        # --- Outer SELECT scalar columns helpers ---
        def lit(v: str | None) -> str:
            return f"'{v}'" if v is not None else "NULL"

        slice_type_val = "|".join(ss.name for ss in slice_specs) if slice_specs else "none"
        slice_value_expr = (
            QueryBuilder._build_slice_value_concat_expr(slice_aliases) if slice_aliases else "'all'"
        )
        segment_name_val = segment_spec.name if segment_spec else "none"
        segment_value_expr = segment_alias if segment_alias else "'all'"
        metric_value_expr = QueryBuilder._build_metric_value_expr(metric)

        # --- WHERE (IS NOT NULL filters) ---
        null_filters: list[str] = []
        for alias in slice_aliases:
            null_filters.append(f"{alias} IS NOT NULL")
        if segment_alias:
            null_filters.append(f"{segment_alias} IS NOT NULL")
        outer_where = "WHERE " + "\n  AND ".join(null_filters) if null_filters else ""

        entity_id_expr = f"{by_entity}" if by_entity else "NULL"

        if period_type == "all_time":
            # ---- all_time path ----
            cte_select = "    SELECT\n        *"
            if cte_extra_cols:
                cte_select += ",\n        " + ",\n        ".join(cte_extra_cols)

            cte_from = f"    FROM {table_name}"
            cte_where = f"    WHERE {time_filter_sql}" if time_filter_sql else ""

            cte_lines = [cte_select, cte_from]
            if cte_where:
                cte_lines.append(cte_where)
            cte_body = "\n".join(cte_lines)

            outer_select_cols = [
                f"    {lit(period_type)}                AS period_type",
                f"    {lit(period_start)}               AS period_start_date",
                f"    {lit(period_end)}                 AS period_end_date",
                f"    {entity_id_expr}                  AS entity_id",
                f"    '{metric.name}'                   AS metric_name",
                f"    '{slice_type_val}'                AS slice_type",
                f"    {slice_value_expr}                AS slice_value",
                f"    '{segment_name_val}'              AS segment_name",
                f"    {segment_value_expr}              AS segment_value",
                f"    {metric_value_expr}               AS metric_value",
            ]
            outer_select = "SELECT\n" + ",\n".join(outer_select_cols)

            group_by_cols = []
            if by_entity:
                group_by_cols.append(by_entity)
            group_by_cols.extend(slice_aliases)
            if segment_alias:
                group_by_cols.append(segment_alias)
            outer_group_by = f"GROUP BY {', '.join(group_by_cols)}" if group_by_cols else ""

            parts = [f"WITH _labeled AS (\n{cte_body}\n)\n{outer_select}", "FROM _labeled"]
            if outer_where:
                parts.append(outer_where)
            if outer_group_by:
                parts.append(outer_group_by)

            return "\n".join(parts)

        else:
            # ---- non-all_time path (period granularity) ----
            assert time_window is not None  # validated in build_queries
            boundaries = QueryBuilder._generate_period_boundaries(time_window, period_type)
            periods_cte = QueryBuilder._build_periods_cte(boundaries)

            # _labeled: SELECT t.*, period columns, slice/segment CASE WHEN expressions
            cte_select = "    SELECT\n        t.*"
            period_cols = [
                "p.period_start AS _period_start",
                "p.period_end   AS _period_end",
            ]
            all_extra = period_cols + cte_extra_cols
            cte_select += ",\n        " + ",\n        ".join(all_extra)

            ts_col = metric.timestamp_col
            cte_from = f"    FROM {table_name} t"
            cte_join = (
                f"    JOIN _periods p\n"
                f"      ON CAST(t.{ts_col} AS TIMESTAMP) >= p.period_start\n"
                f"     AND CAST(t.{ts_col} AS TIMESTAMP) <  p.period_end"
            )

            cte_body = "\n".join([cte_select, cte_from, cte_join])

            outer_select_cols = [
                f"    '{period_type}'                        AS period_type",
                "    CAST(_period_start AS VARCHAR)         AS period_start_date",
                "    CAST(_period_end   AS VARCHAR)         AS period_end_date",
                f"    {entity_id_expr}                       AS entity_id",
                f"    '{metric.name}'                        AS metric_name",
                f"    '{slice_type_val}'                     AS slice_type",
                f"    {slice_value_expr}                     AS slice_value",
                f"    '{segment_name_val}'                   AS segment_name",
                f"    {segment_value_expr}                   AS segment_value",
                f"    {metric_value_expr}                    AS metric_value",
            ]
            outer_select = "SELECT\n" + ",\n".join(outer_select_cols)

            # GROUP BY always includes _period_start, _period_end
            group_by_cols = ["_period_start", "_period_end"]
            if by_entity:
                group_by_cols.append(by_entity)
            group_by_cols.extend(slice_aliases)
            if segment_alias:
                group_by_cols.append(segment_alias)
            outer_group_by = f"GROUP BY {', '.join(group_by_cols)}"

            with_clause = f"WITH {periods_cte},\n_labeled AS (\n{cte_body}\n)"
            parts = [f"{with_clause}\n{outer_select}", "FROM _labeled"]
            if outer_where:
                parts.append(outer_where)
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
        if metric.denominator is not None:
            return f"{metric.numerator} / NULLIF({metric.denominator}, 0)"
        return metric.numerator

    @staticmethod
    def _generate_period_boundaries(
        time_window: tuple[str, str],
        period_type: str,
    ) -> list[tuple[str, str]]:
        """Generate (period_start, period_end) ISO date string pairs covering time_window.

        Each pair is a half-open interval [period_start, period_end).
        The first period_start is rounded down to the period boundary containing
        time_window[0] (e.g., the Monday of the containing ISO week).

        Uses stdlib datetime only — no external dependencies.

        Examples:
            time_window=('2026-01-01', '2026-04-01'), period_type='monthly'
            → [('2026-01-01', '2026-02-01'),
               ('2026-02-01', '2026-03-01'),
               ('2026-03-01', '2026-04-01')]

            time_window=('2026-01-07', '2026-01-22'), period_type='weekly'
            → [('2026-01-05', '2026-01-12'),   # Monday of week containing Jan 7
               ('2026-01-12', '2026-01-19'),
               ('2026-01-19', '2026-01-26')]
        """
        start = date.fromisoformat(time_window[0])
        end = date.fromisoformat(time_window[1])

        if period_type == "daily":
            period_start = start
        elif period_type == "weekly":
            period_start = start - timedelta(days=start.weekday())  # round to Monday
        elif period_type == "monthly":
            period_start = start.replace(day=1)
        elif period_type == "yearly":
            period_start = start.replace(month=1, day=1)
        else:
            raise QueryBuildError(f"Unknown period_type '{period_type}'")

        boundaries: list[tuple[str, str]] = []
        current = period_start
        while current < end:
            if period_type == "daily":
                next_period = current + timedelta(days=1)
            elif period_type == "weekly":
                next_period = current + timedelta(weeks=1)
            elif period_type == "monthly":
                if current.month == 12:
                    next_period = current.replace(year=current.year + 1, month=1)
                else:
                    next_period = current.replace(month=current.month + 1)
            else:  # yearly
                next_period = current.replace(year=current.year + 1)

            boundaries.append((current.isoformat(), next_period.isoformat()))
            current = next_period

        return boundaries

    @staticmethod
    def _build_periods_cte(boundaries: list[tuple[str, str]]) -> str:
        """Build the _periods VALUES CTE SQL string.

        Returns:
            "_periods(period_start, period_end) AS (\\n    VALUES\\n        (...),\\n        ...\\n)"

        Each boundary value is wrapped in CAST('...' AS TIMESTAMP).
        """
        rows = [
            f"        (CAST('{s}' AS TIMESTAMP), CAST('{e}' AS TIMESTAMP))" for s, e in boundaries
        ]
        values_str = ",\n".join(rows)
        return f"_periods(period_start, period_end) AS (\n    VALUES\n{values_str}\n)"

    @staticmethod
    def _build_time_filter_sql(time_window: tuple[str, str], timestamp_col: str) -> str:
        """Build time window filter condition."""
        start, end = time_window
        return f"{timestamp_col} >= '{start}' AND {timestamp_col} < '{end}'"

    @staticmethod
    def _parse_table_name_from_uri(source_uri: str) -> str:
        """Extract table name from source URI for use in SQL FROM clause."""
        backend_type, schema, table = ConnectionManager.parse_source_uri(source_uri)
        if backend_type == "bigquery":
            return table  # already 'dataset.table'
        if backend_type == "postgres" and schema:
            return f"{schema}.{table}"
        return table
