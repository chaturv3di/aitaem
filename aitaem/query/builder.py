"""
aitaem.query.builder - QueryBuilder and QueryGroup

Translates MetricSpec / SliceSpec / SegmentSpec into ibis.Table expressions
grouped by source URI. Building a ConnectionManager (or per-source connector)
to obtain bound table handles, since resolving schema for the DIM join and
splicing user-authored SQL fragments (via Table.sql()) both require a live
backend — see plans/34-ibis-native-query-building.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal, get_args

import ibis

from aitaem.connectors.connection import ConnectionManager
from aitaem.connectors.ibis_connector import IbisConnector
from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import QueryBuildError

PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly", "hourly"]
VALID_PERIOD_TYPES: frozenset[str] = frozenset(get_args(PeriodType))

# period_type -> ibis.interval() kwarg for one period's width
_INTERVAL_UNIT_KWARGS: dict[str, dict[str, int]] = {
    "hourly": {"hours": 1},
    "daily": {"days": 1},
    "weekly": {"days": 7},
    "monthly": {"months": 1},
    "yearly": {"years": 1},
}


@dataclass
class QueryGroup:
    """All Ibis table expressions for a single source URI."""

    source: str
    metrics: list[MetricSpec]
    expressions: list[ibis.Table] = field(default_factory=list)


class QueryBuilder:
    """Builds Ibis table expressions from specs. All methods are static."""

    @staticmethod
    def build_queries(
        metric_specs: list[MetricSpec],
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        connection_manager: ConnectionManager,
        segment_join_key: str | None = None,
        time_window: tuple[str, str] | None = None,
        spec_cache: "SpecCache | None" = None,  # type: ignore[name-defined]  # noqa: F821
        period_type: str = "all_time",
        by_entity: str | None = None,
    ) -> list[QueryGroup]:
        """Build optimized query groups (one per unique source table).

        Each QueryGroup contains all Ibis expressions for that source:
        one per (metric × slice × (segment | no-segment baseline)).

        Args:
            connection_manager: Provides bound ibis.Table handles per source —
                required for the DIM join and for splicing user-authored SQL
                fragments via Table.sql(), both of which need real schema.
            segment_spec: A single SegmentSpec to apply, or None for no segmentation.
            segment_join_key: Fact-table FK column to join to the segment's DIM table.
                              Defaults to ``segment_spec.entity_id`` when omitted.
            spec_cache: Required only when composite slices are present.
            period_type: One of 'all_time', 'daily', 'weekly', 'monthly', 'yearly', 'hourly'.
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

        if period_type not in VALID_PERIOD_TYPES:
            raise QueryBuildError(
                f"Invalid period_type '{period_type}'. Must be one of {sorted(VALID_PERIOD_TYPES)}"
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

        # Period metadata (used for all_time only; non-all_time uses the periods spine)
        period_start: str | None = time_window[0] if time_window else None
        period_end: str | None = time_window[1] if time_window else None

        groups_by_source = QueryBuilder._group_by_source(metric_specs)

        query_groups: list[QueryGroup] = []
        for source, metrics in groups_by_source.items():
            connector = connection_manager.get_connection_for_source(source)
            expressions: list[ibis.Table] = []
            for metric in metrics:
                expressions.extend(
                    QueryBuilder._build_queries_for_metric(
                        metric=metric,
                        connector=connector,
                        slice_specs=slice_specs,
                        segment_spec=segment_spec,
                        segment_join_key=segment_join_key,
                        time_window=time_window,
                        period_type=period_type,
                        period_start=period_start,
                        period_end=period_end,
                        spec_cache=spec_cache,
                        by_entity=by_entity,
                    )
                )
            query_groups.append(
                QueryGroup(source=source, metrics=metrics, expressions=expressions)
            )

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
        connector: IbisConnector,
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        segment_join_key: str | None,
        time_window: tuple[str, str] | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
        spec_cache: "SpecCache | None" = None,  # type: ignore[name-defined]  # noqa: F821
        by_entity: str | None = None,
    ) -> list[ibis.Table]:
        """Build all Ibis expressions for one metric.

        Each SliceSpec is processed independently (+ a no-slice/all baseline).
        For each slice × (segment | no-segment baseline) combination, one
        expression is generated. Composite SliceSpecs are resolved via spec_cache.

        len(result) == (len(slice_specs) + 1) × (2 if segment_spec else 1)
        """
        expressions: list[ibis.Table] = []

        all_slice_specs: list[SliceSpec | None] = list(slice_specs) if slice_specs else []
        all_slice_specs.append(None)  # no-slice baseline

        segment_states: list[SegmentSpec | None] = []
        if segment_spec is not None:
            segment_states.append(segment_spec)
        segment_states.append(None)  # no-segment baseline

        for slice_spec in all_slice_specs:
            resolved_slices = QueryBuilder._resolve_slice_components(slice_spec, spec_cache)
            for seg in segment_states:
                expressions.append(
                    QueryBuilder._build_metric_segment_query(
                        metric=metric,
                        connector=connector,
                        slice_specs=resolved_slices,
                        segment_spec=seg,
                        segment_join_key=segment_join_key if seg is not None else None,
                        time_window=time_window,
                        period_type=period_type,
                        period_start=period_start,
                        period_end=period_end,
                        by_entity=by_entity,
                    )
                )

        return expressions

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
        connector: IbisConnector,
        slice_specs: list[SliceSpec] | None,
        segment_spec: SegmentSpec | None,
        period_type: str,
        period_start: str | None,
        period_end: str | None,
        segment_join_key: str | None = None,
        time_window: tuple[str, str] | None = None,
        by_entity: str | None = None,
    ) -> ibis.Table:
        """Build a single Ibis expression for one (metric, segment_spec | None) combination."""
        table_name = QueryBuilder._parse_table_name_from_uri(metric.source)
        t = connector.get_table(table_name)

        # --- Segment: splice classification onto the DIM table's own schema, ---
        # --- then join — unqualified column refs in segment `where` resolve  ---
        # --- naturally against the DIM table alone, no qualification needed. ---
        dim_labeled: ibis.Table | None = None
        effective_join_key: str | None = None
        segment_alias: str | None = None
        if segment_spec is not None:
            dim_table_name = QueryBuilder._parse_table_name_from_uri(segment_spec.source)
            dim = connector.get_table(dim_table_name)
            effective_join_key = segment_join_key or segment_spec.entity_id
            segment_alias = "_segment"
            segment_col_sql = QueryBuilder._build_segment_case_when_expr(
                segment_spec, segment_alias
            )
            dim_src_name = QueryBuilder._unique_alias("_dim_src")
            dim_src = dim.alias(dim_src_name)
            dim_labeled = dim_src.sql(f"SELECT *, {segment_col_sql} FROM {dim_src_name}")

        # --- Slices: splice classification(s) onto the fact table's own schema. ---
        slice_aliases: list[str] = []
        slice_col_sqls: list[str] = []
        if slice_specs:
            for ss in slice_specs:
                alias = f"_slice_{ss.name}"
                slice_aliases.append(alias)
                if ss.is_wildcard:
                    slice_col_sqls.append(QueryBuilder._build_wildcard_slice_expr(ss, alias))
                else:
                    slice_col_sqls.append(QueryBuilder._build_slice_case_when_expr(ss, alias))

        if slice_col_sqls:
            t_src_name = QueryBuilder._unique_alias("_fact_src")
            t_src = t.alias(t_src_name)
            t_labeled = t_src.sql(f"SELECT *, {', '.join(slice_col_sqls)} FROM {t_src_name}")
        else:
            t_labeled = t

        # --- Join fact + dim, keeping fact columns plus only the derived _segment label. ---
        fact_cols = list(t_labeled.columns)
        if dim_labeled is not None:
            joined = t_labeled.join(
                dim_labeled,
                t_labeled[effective_join_key] == dim_labeled[segment_spec.entity_id],  # type: ignore[union-attr]
            )
            labeled = joined.select(*fact_cols, joined[segment_alias])  # type: ignore[arg-type]
        else:
            labeled = t_labeled

        # --- Time / periods filter. ---
        ts_col = metric.timestamp_col
        if period_type == "all_time":
            if time_window is not None:
                start, end = time_window
                labeled = labeled.filter(
                    (labeled[ts_col] >= ibis.literal(start))
                    & (labeled[ts_col] < ibis.literal(end))
                )
            period_start_expr = QueryBuilder._lit_or_null(period_start)
            period_end_expr = QueryBuilder._lit_or_null(period_end)
        else:
            assert time_window is not None  # validated in build_queries
            pre_period_cols = list(labeled.columns)
            boundaries = QueryBuilder._generate_period_boundaries(time_window, period_type)
            spine = QueryBuilder._build_periods_spine(boundaries, period_type)
            joined = labeled.join(
                spine,
                (labeled[ts_col].cast("timestamp") >= spine["period_start"])
                & (labeled[ts_col].cast("timestamp") < spine["period_end"]),
            )
            labeled = joined.select(
                *pre_period_cols, joined["period_start"], joined["period_end"]
            )

        # --- Null-filtering: drop rows unclassified by any slice/segment. ---
        null_filters = [labeled[alias].notnull() for alias in slice_aliases]
        if segment_alias:
            null_filters.append(labeled[segment_alias].notnull())
        if null_filters:
            condition = null_filters[0]
            for c in null_filters[1:]:
                condition = condition & c
            labeled = labeled.filter(condition)

        # --- Group by + aggregate: splice numerator/denominator (already an ---
        # --- aggregate expression) against the filtered/joined table.       ---
        group_by_cols: list[str] = []
        if period_type != "all_time":
            group_by_cols.extend(["period_start", "period_end"])
        if by_entity:
            group_by_cols.append(by_entity)
        group_by_cols.extend(slice_aliases)
        if segment_alias:
            group_by_cols.append(segment_alias)

        metric_value_fragment = QueryBuilder._build_metric_value_expr(metric)
        agg_src_name = QueryBuilder._unique_alias("_agg_src")
        labeled_src = labeled.alias(agg_src_name)
        if group_by_cols:
            group_sql = ", ".join(group_by_cols)
            agg_query = (
                f"SELECT {group_sql}, {metric_value_fragment} AS metric_value "
                f"FROM {agg_src_name} GROUP BY {group_sql}"
            )
        else:
            agg_query = f"SELECT {metric_value_fragment} AS metric_value FROM {agg_src_name}"
        aggregated = labeled_src.sql(agg_query)

        if period_type != "all_time":
            period_start_expr = aggregated["period_start"].cast("string")
            period_end_expr = aggregated["period_end"].cast("string")

        # --- Outer projection: literals, derived slice/segment values, metric_value. ---
        entity_id_expr = aggregated[by_entity] if by_entity else ibis.null()
        slice_type_val = "|".join(ss.name for ss in slice_specs) if slice_specs else "none"
        slice_value_expr = QueryBuilder._build_slice_value_expr(aggregated, slice_aliases)
        segment_name_val = segment_spec.name if segment_spec else "none"
        segment_value_expr = (
            aggregated[segment_alias] if segment_alias else ibis.literal("all")
        )

        final = aggregated.mutate(
            period_type=ibis.literal(period_type),
            period_start_date=period_start_expr,
            period_end_date=period_end_expr,
            entity_id=entity_id_expr,
            metric_name=ibis.literal(metric.name),
            metric_format=QueryBuilder._lit_or_null(metric.format),
            slice_type=ibis.literal(slice_type_val),
            slice_value=slice_value_expr,
            segment_name=ibis.literal(segment_name_val),
            segment_value=segment_value_expr,
        ).select(
            "period_type",
            "period_start_date",
            "period_end_date",
            "entity_id",
            "metric_name",
            "metric_format",
            "slice_type",
            "slice_value",
            "segment_name",
            "segment_value",
            "metric_value",
        )

        return final

    @staticmethod
    def _unique_alias(prefix: str) -> str:
        """Generate a unique Table.alias() name.

        Every query in a QueryGroup is built independently but later unioned
        into one combined statement (QueryExecutor). A fixed alias string
        (e.g. "_agg_src") would compile to a duplicate CTE name once more than
        one query using it is combined, so each call gets its own suffix.
        """
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _lit_or_null(value: str | None) -> ibis.Value:
        """Build a string literal, or a string-typed NULL when value is None.

        Explicitly typed (not bare ibis.null()) so this column's dtype is
        consistent across queries whose value differs between calls — e.g.
        one metric with metric.format set, another with it None — which
        QueryExecutor later unions into one combined statement.
        """
        return ibis.literal(value, type="string") if value is not None else ibis.null("string")

    @staticmethod
    def _build_slice_value_expr(table: ibis.Table, slice_aliases: list[str]) -> ibis.Value:
        """Build the slice_value column: '|'-joined slice alias columns, or literal 'all'."""
        if not slice_aliases:
            return ibis.literal("all")
        if len(slice_aliases) == 1:
            return table[slice_aliases[0]]
        first, *rest = slice_aliases
        parts: list[ibis.Value] = []
        for alias in rest:
            parts.extend([ibis.literal("|"), table[alias]])
        return table[first].concat(*parts)

    @staticmethod
    def _build_wildcard_slice_expr(slice_spec: SliceSpec, alias: str) -> str:
        """Build direct column cast expression for a wildcard SliceSpec."""
        return f"CAST({slice_spec.column} AS VARCHAR) AS {alias}"

    @staticmethod
    def _build_slice_case_when_expr(slice_spec: SliceSpec, alias: str) -> str:
        """Build CASE WHEN expression for a SliceSpec."""
        when_clauses = "\n        ".join(
            f"WHEN {sv.where} THEN '{sv.name}'" for sv in slice_spec.values
        )
        return f"CASE\n        {when_clauses}\n        ELSE NULL\n    END AS {alias}"

    @staticmethod
    def _build_segment_case_when_expr(segment_spec: SegmentSpec, alias: str) -> str:
        """Build CASE WHEN expression for a SegmentSpec.

        Spliced directly onto the DIM table's own schema (see
        _build_metric_segment_query), so unqualified column references in
        each value's `where` resolve against the DIM table alone — no
        qualification/rewrite step needed.
        """
        when_clauses = "\n        ".join(
            f"WHEN {sv.where} THEN '{sv.name}'" for sv in segment_spec.values
        )
        return f"CASE\n        {when_clauses}\n        ELSE NULL\n    END AS {alias}"

    @staticmethod
    def _build_metric_value_expr(metric: MetricSpec) -> str:
        """Build the metric value SQL fragment (numerator/denominator, raw text)."""
        if metric.denominator is not None:
            return f"CAST({metric.numerator} / NULLIF({metric.denominator}, 0) AS DOUBLE)"
        return f"CAST({metric.numerator} AS DOUBLE)"

    @staticmethod
    def _parse_window_endpoint_as_datetime(s: str) -> datetime:
        """Parse a date-only or datetime string to a datetime object.

        "YYYY-MM-DD" → midnight (T00:00:00).
        Accepts T or space as date/time separator (standard fromisoformat behaviour).
        """
        if len(s) == 10:  # date-only: "YYYY-MM-DD"
            return datetime.combine(date.fromisoformat(s), time(0, 0, 0))
        return datetime.fromisoformat(s)

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
        if period_type == "hourly":
            start_dt = QueryBuilder._parse_window_endpoint_as_datetime(time_window[0])
            end_dt = QueryBuilder._parse_window_endpoint_as_datetime(time_window[1])
            # Truncate start to the nearest full hour; end is used as-is
            start_dt = start_dt.replace(minute=0, second=0, microsecond=0)
            boundaries: list[tuple[str, str]] = []
            current_dt = start_dt
            while current_dt < end_dt:
                next_dt = current_dt + timedelta(hours=1)
                boundaries.append(
                    (
                        current_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                        next_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    )
                )
                current_dt = next_dt
            return boundaries

        start = date.fromisoformat(time_window[0])
        end = date.fromisoformat(time_window[1])

        if period_type == "daily":
            period_start = start
        elif period_type == "weekly":
            period_start = start - timedelta(days=start.weekday())  # round to Monday
        elif period_type == "monthly":
            period_start = start.replace(day=1)
        else:  # yearly
            period_start = start.replace(month=1, day=1)

        boundaries = []
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
    def _build_periods_spine(boundaries: list[tuple[str, str]], period_type: str) -> ibis.Table:
        """Build the periods spine table server-side via ibis.range()+interval arithmetic.

        anchor/count are derived from boundaries (the unchanged
        _generate_period_boundaries() output, which steps by exactly one fixed
        calendar unit per iteration) — the per-row values themselves are
        computed by the backend (GENERATE_ARRAY/RANGE-equivalent per backend),
        not embedded as literals. See "Periods CTE replacement" in the plan.
        """
        anchor_str = boundaries[0][0]
        count = len(boundaries)
        unit_kwargs = _INTERVAL_UNIT_KWARGS[period_type]

        anchor = ibis.literal(anchor_str).cast("timestamp")
        spine = ibis.range(0, count).unnest().name("_offset").as_table()
        interval = ibis.interval(**unit_kwargs)
        period_start = anchor + spine["_offset"] * interval
        period_end = period_start + interval
        return spine.mutate(period_start=period_start, period_end=period_end).select(
            "period_start", "period_end"
        )

    @staticmethod
    def _parse_table_name_from_uri(source_uri: str) -> str:
        """Extract table name from source URI for use looking up a bound table."""
        backend_type, schema, table = ConnectionManager.parse_source_uri(source_uri)
        if backend_type == "bigquery":
            return table  # already 'dataset.table'
        if backend_type == "postgres" and schema:
            return f"{schema}.{table}"
        return table
