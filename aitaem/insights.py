"""
aitaem.insights - Primary user interface

MetricCompute is the main entry point for computing metrics from specs.
"""

from __future__ import annotations

import logging
import os
import tempfile

import ibis

from aitaem.connectors.connection import ConnectionManager
from aitaem.query.builder import PeriodType, QueryBuilder
from aitaem.query.executor import QueryExecutor
from aitaem.specs.compatibility import CompatibilityResult, ScanResult
from aitaem.specs.loader import SpecCache
from aitaem.utils.formatting import ensure_standard_output

logger = logging.getLogger(__name__)


def _run_scan(spec_cache: SpecCache, connection_manager: ConnectionManager) -> ScanResult:
    """Core logic for MetricCompute.scan() — separated for independent testability."""
    from aitaem.query.builder import QueryBuilder

    results: list[CompatibilityResult] = []

    # Batch schema introspections by unique source URI
    unique_uris: set[str] = {m.source for m in spec_cache.metrics.values()}
    source_columns: dict[str, frozenset[str]] = {}
    for uri in unique_uris:
        try:
            connector = connection_manager.get_connection_for_source(uri)
            table_name = QueryBuilder._parse_table_name_from_uri(uri)
            source_columns[uri] = frozenset(connector.get_table(table_name).schema().names)
        except Exception as e:
            logger.warning(
                "scan: could not introspect schema for '%s' (%s) — metrics with this source will be skipped",
                uri,
                type(e).__name__,
            )

    slices = list(spec_cache.slices.values())
    segments = list(spec_cache.segments.values())

    for metric in spec_cache.metrics.values():
        cols = source_columns.get(metric.source)
        if cols is None:
            continue

        for slice_spec in slices:
            components = (
                [spec_cache.get_slice(n) for n in slice_spec.cross_product]
                if slice_spec.is_composite
                else [slice_spec]
            )
            required: set[str] = set()
            for comp in components:
                for col_list in (comp.validate().referenced_columns or {}).values():
                    required.update(col_list)

            missing = sorted(required - cols)
            compatible = len(missing) == 0
            results.append(
                CompatibilityResult(
                    metric_name=metric.name,
                    spec_name=slice_spec.name,
                    spec_type="slice",
                    compatible=compatible,
                    valid_join_keys=[],
                    missing_columns=missing,
                    reason=None if compatible else f"columns not found in source table: {missing}",
                )
            )

        for seg in segments:
            candidates: set[str] = set(seg.join_keys) if seg.join_keys else {seg.entity_id}
            valid_keys = sorted(candidates & cols)
            missing_keys = sorted(candidates - cols)
            compatible = len(valid_keys) > 0
            results.append(
                CompatibilityResult(
                    metric_name=metric.name,
                    spec_name=seg.name,
                    spec_type="segment",
                    compatible=compatible,
                    valid_join_keys=valid_keys,
                    missing_columns=missing_keys,
                    reason=None
                    if compatible
                    else f"no valid join keys found in source table: {sorted(candidates)}",
                )
            )

    return ScanResult(results=tuple(results))


class MetricCompute:
    """Compute metrics from a SpecCache and ConnectionManager.

    Primary user interface for aitaem.  Resolves specs, builds SQL queries,
    executes them, and returns a lazy ibis.Table.
    """

    def __init__(
        self,
        spec_cache: SpecCache,
        connection_manager: ConnectionManager,
        tmp_dir: str | None = "/tmp",
    ) -> None:
        """
        Args:
            spec_cache: Loaded and validated metric, slice, and segment specs.
            connection_manager: Backend connections for query execution.
            tmp_dir: Directory for the temporary DuckDB file used when a
                compute() call spans multiple source backends. Defaults to
                '/tmp', which prevents large cross-backend result sets from
                bloating process memory. Set to None to force an in-memory
                DuckDB instead (safe when result sets are known to be small).
                The file is deleted automatically when this MetricCompute
                instance is garbage collected; the OS reclaims it on reboot
                as a final backstop.
        """
        self.spec_cache = spec_cache
        self.connection_manager = connection_manager
        self._tmp_dir = tmp_dir
        self._cross_backend_conn: ibis.BaseBackend | None = None
        self._cross_backend_db_path: str | None = None

    def __del__(self) -> None:
        self._cross_backend_conn = None
        if self._cross_backend_db_path is not None:
            try:
                os.unlink(self._cross_backend_db_path)
            except OSError:
                pass

    def _get_cross_backend_conn(self) -> ibis.BaseBackend:
        """Return the persistent cross-backend DuckDB connection, creating it on first call."""
        if self._cross_backend_conn is None:
            if self._tmp_dir is not None:
                fd, path = tempfile.mkstemp(suffix=".duckdb", dir=self._tmp_dir)
                os.close(fd)
                os.unlink(path)  # DuckDB must create the file itself; mkstemp leaves it empty
                self._cross_backend_db_path = path
                self._cross_backend_conn = ibis.duckdb.connect(path)
            else:
                self._cross_backend_conn = ibis.duckdb.connect(":memory:")
        return self._cross_backend_conn

    def compute(
        self,
        metrics: str | list[str],
        slices: str | list[str] | None = None,
        segments: dict[str, str] | str | None = None,
        time_window: tuple[str, str] | None = None,
        period_type: PeriodType = "all_time",
        by_entity: str | None = None,
    ) -> ibis.Table:
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

        Returns:
            Lazy ibis.Table with columns: period_type, period_start_date,
            period_end_date, entity_id, metric_name, metric_format, slice_type,
            slice_value, segment_name, segment_value, metric_value.
            Call .to_pandas() to materialise.

            When all metrics share the same source backend the returned Table is a
            deferred expression on that backend and no data is transferred until
            .to_pandas() (or any other materialising call) is invoked.

            When metrics span multiple source backends the results are materialised
            internally and re-exposed as a Table backed by a temporary DuckDB
            database (file in tmp_dir, or in-memory when tmp_dir=None). This
            database is not accessible via ConnectionManager and is cleaned up
            when this MetricCompute instance is garbage collected.

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

        # 5. Execute and return in standard column order
        executor = QueryExecutor(self.connection_manager)
        table = executor.execute(
            query_groups,
            cross_backend_conn_factory=self._get_cross_backend_conn,
        )
        return ensure_standard_output(table)

    def scan(self) -> ScanResult:
        """Introspect source schemas and return a compatibility matrix for all loaded specs.

        For each loaded metric, checks every loaded slice and segment:

        - **Slice**: compatible when all referenced columns exist in the metric's source table.
        - **Segment**: compatible when at least one join key (from ``join_keys``, or
          ``entity_id`` when ``join_keys`` is empty) exists in the metric's source table.

        Schema introspection is batched by unique source URI — each table is queried once
        regardless of how many metrics share it. Metrics whose source connection is unavailable
        are skipped with a warning.

        Returns:
            ScanResult with one CompatibilityResult per metric × slice and per metric × segment.
        """
        return _run_scan(self.spec_cache, self.connection_manager)
