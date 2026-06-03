"""
aitaem.insights - Primary user interface

MetricCompute is the main entry point for computing metrics from specs.
"""

from __future__ import annotations

import pandas as pd

from aitaem.connectors.connection import ConnectionManager
from aitaem.query.builder import PeriodType, QueryBuilder
from aitaem.query.executor import QueryExecutor
from aitaem.specs.loader import SpecCache
from aitaem.utils.formatting import ensure_standard_output


class MetricCompute:
    """Compute metrics from a SpecCache and ConnectionManager.

    Primary user interface for aitaem.  Resolves specs, builds SQL queries,
    executes them, and returns a standardized pandas DataFrame.
    """

    def __init__(self, spec_cache: SpecCache, connection_manager: ConnectionManager) -> None:
        """
        Args:
            spec_cache: Loaded and validated metric, slice, and segment specs.
            connection_manager: Backend connections for query execution.
        """
        self.spec_cache = spec_cache
        self.connection_manager = connection_manager

    def compute(
        self,
        metrics: str | list[str],
        slices: str | list[str] | None = None,
        segments: dict[str, str] | str | None = None,
        time_window: tuple[str, str] | None = None,
        period_type: PeriodType = "all_time",
        by_entity: str | None = None,
        output_format: str = "pandas",
    ) -> pd.DataFrame:
        """Compute one or more metrics with optional slicing and segmentation.

        Args:
            metrics: Metric name(s) to compute.
            slices: Slice name(s). Each slice is computed independently.
            segments: Segment to apply.  Two forms are accepted:

                - ``str`` — segment name; uses the spec's ``entity_id`` as the join key.
                - ``dict[str, str]`` — ``{"segment_name": "fact_fk_col"}``; the value
                  overrides the default join key. Exactly one entry is allowed.

                Only one segment per ``compute()`` call is supported.
            time_window: (start_date, end_date) ISO strings for period filter.
                         Requires ``timestamp_col`` to be set on each metric spec.
            period_type: Granularity for time grouping. One of 'all_time', 'daily',
                         'weekly', 'monthly', 'yearly'. Non-'all_time' requires
                         time_window and timestamp_col on every metric spec.
            by_entity: Column name to group by for entity-level metrics. When set,
                       every requested metric must list this column in its ``entities``
                       field. The output includes an ``entity_id`` column with the
                       entity column value; ``None`` when ``by_entity`` is not set.
            output_format: Output format — only 'pandas' is supported in Phase 1.

        Returns:
            DataFrame with columns: period_type, period_start_date, period_end_date,
            entity_id, metric_name, slice_type, slice_value, segment_name, segment_value,
            metric_value. ``entity_id`` is ``None`` when ``by_entity`` is not set.

        Raises:
            SpecNotFoundError: if any metric/slice/segment name is not in the cache.
            QueryBuildError: if more than one segment is provided.
            QueryBuildError: if the join key in ``segments`` dict is not in the spec's
                             ``join_keys`` whitelist (when the whitelist is non-empty).
            QueryBuildError: if time_window is set but a metric has no timestamp_col.
            QueryBuildError: if period_type is invalid or missing required time_window.
            QueryBuildError: if by_entity is set but a metric does not list it in entities.
            QueryExecutionError: if all query groups fail to execute.
        """
        from aitaem.utils.exceptions import QueryBuildError

        # 1. Normalize inputs to lists
        metric_names = [metrics] if isinstance(metrics, str) else list(metrics)
        slice_names = ([slices] if isinstance(slices, str) else list(slices)) if slices else None

        # 2. Resolve segment spec and join key
        segment_spec = None
        segment_join_key: str | None = None
        if segments is not None:
            if isinstance(segments, str):
                segment_name = segments
                explicit_join_key: str | None = None
            else:
                if len(segments) != 1:
                    raise QueryBuildError(
                        f"Only one segment per compute() call is supported, "
                        f"but {len(segments)} were provided: {list(segments.keys())}"
                    )
                segment_name, explicit_join_key = next(iter(segments.items()))

            segment_spec = self.spec_cache.get_segment(segment_name)
            if explicit_join_key is not None:
                if segment_spec.join_keys and explicit_join_key not in segment_spec.join_keys:
                    raise QueryBuildError(
                        f"Join key '{explicit_join_key}' is not in the allowed join_keys for "
                        f"segment '{segment_name}': {list(segment_spec.join_keys)}"
                    )
                segment_join_key = explicit_join_key
            # When no explicit key: segment_join_key stays None, builder uses entity_id

        # 3. Resolve metric and slice specs from cache
        metric_specs = [self.spec_cache.get_metric(n) for n in metric_names]
        slice_specs = [self.spec_cache.get_slice(n) for n in slice_names] if slice_names else None

        # 4. Build SQL query groups
        query_groups = QueryBuilder.build_queries(
            metric_specs=metric_specs,
            slice_specs=slice_specs,
            segment_spec=segment_spec,
            segment_join_key=segment_join_key,
            time_window=time_window,
            spec_cache=self.spec_cache,
            period_type=period_type,
            by_entity=by_entity,
        )

        # 4. Execute and return in standard column order
        executor = QueryExecutor(self.connection_manager)
        df = executor.execute(query_groups, output_format=output_format)
        return ensure_standard_output(df)
