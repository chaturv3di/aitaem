# Plan 05: Insights Module, Tests, Examples, and README

## Context

All underlying modules (specs, query, connectors, utils) are fully implemented and tested. This plan completes Phase 1 of the aitaem library by implementing:
1. The primary user interface (`insights.py` with `MetricCompute`)
2. A thin formatting utility (`utils/formatting.py`)
3. Tests for the insights module
4. An updated README reflecting the actual API

One prerequisite change is required: `timestamp_col` must move from a `QueryBuilder.build_queries()` parameter into `MetricSpec` (so each metric declares its own timestamp column for time-window filtering, consistent with the architecture's single-source-of-truth principle).

---

## Gap Analysis

| Component | Status | Action |
|-----------|--------|--------|
| `aitaem/specs/` | ✅ Done | Add `timestamp_col` field to MetricSpec |
| `aitaem/query/builder.py` | ✅ Done | Remove `timestamp_col` param; use `metric.timestamp_col` per-metric |
| `aitaem/query/executor.py` | ✅ Done | No changes |
| `aitaem/connectors/` | ✅ Done | No changes |
| `aitaem/utils/exceptions.py` | ✅ Done | No changes |
| `aitaem/utils/validation.py` | ✅ Done | Accept `timestamp_col` as known optional field |
| `aitaem/insights.py` | ❌ Missing | Implement MetricCompute |
| `aitaem/utils/formatting.py` | ❌ Missing | Implement standard output formatting utility |
| `aitaem/__init__.py` | ⚠️ Partial | Add MetricCompute export |
| `tests/test_insights.py` | ❌ Missing | Implement insight tests |
| `README.md` | ⚠️ Outdated | Add API quickstart section |
| `examples/metrics/*.yaml` | ⚠️ Missing field | Add `timestamp_col: date` to metric YAMLs |

---

## Sub-Features (Ordered by Dependency)

### Sub-Feature 1: Add `timestamp_col` to MetricSpec

**Files to modify**:
- `aitaem/specs/metric.py` — add `timestamp_col: str | None = None` field to the frozen dataclass; YAML key: optional `timestamp_col` under `metric:`
- `aitaem/utils/validation.py` — add `"timestamp_col"` to the set of known optional fields in `validate_metric_spec()` so it does not trigger an "unknown field" warning

**Tests to update** (existing call sites that pass `timestamp_col` to `build_queries()`):
- `tests/test_query/test_builder.py` lines 552–570: set `timestamp_col` on MetricSpec instead
- `tests/test_query/test_executor.py` line 238: same change

**Verification**: `python -m pytest tests/test_specs/ tests/test_query/` — all existing tests must still pass

---

### Sub-Feature 2: Refactor QueryBuilder to use MetricSpec.timestamp_col

**File to modify**: `aitaem/query/builder.py`

Changes:
1. Remove `timestamp_col: str | None = None` from `build_queries()` signature
2. Remove the global `time_filter_sql` pre-build at the top of `build_queries()` (currently lines 56–63)
3. Pass raw `time_window` tuple down to `_build_queries_for_metric()` instead of pre-built `time_filter_sql`
4. In `_build_queries_for_metric()`:
   - Change parameter from `time_filter_sql: str | None` to `time_window: tuple[str, str] | None`
   - Build `time_filter_sql` per metric:
     ```python
     time_filter_sql = None
     if time_window is not None:
         if metric.timestamp_col is None:
             raise QueryBuildError(
                 f"Metric '{metric.name}' has no timestamp_col but time_window was provided. "
                 "Add `timestamp_col: <col_name>` to the metric YAML."
             )
         time_filter_sql = QueryBuilder._build_time_filter_sql(time_window, metric.timestamp_col)
     ```
5. `_build_time_filter_sql()` is unchanged

**Verification**: `python -m pytest tests/test_query/` — all existing tests pass

---

### Sub-Feature 3: Implement `aitaem/utils/formatting.py`

**Purpose**: Ensure the final DataFrame has the standardized column order defined in the architecture.

```python
STANDARD_COLUMNS = [
    "period_type",
    "period_start_date",
    "period_end_date",
    "metric_name",
    "slice_type",
    "slice_value",
    "segment_name",
    "segment_value",
    "metric_value",
]

def ensure_standard_output(df: pd.DataFrame) -> pd.DataFrame:
    """Reorder columns to match the standard output schema.

    Raises:
        ValueError: if any required column is missing from the DataFrame.
    """
    missing = set(STANDARD_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing expected columns: {missing}")
    return df[STANDARD_COLUMNS]
```

**Note**: No new test file needed for this — it is tested implicitly through the insights tests.

---

### Sub-Feature 4: Implement `aitaem/insights.py`

```python
class MetricCompute:
    def __init__(self, spec_cache: SpecCache, connection_manager: ConnectionManager):
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
        segments: str | list[str] | None = None,
        time_window: tuple[str, str] | None = None,
        output_format: str = "pandas",
    ) -> pd.DataFrame:
        """Compute one or more metrics with optional slicing and segmentation.

        Args:
            metrics: Metric name(s) to compute.
            slices: Slice name(s). Each slice computed independently.
            segments: Segment name(s). Each segment computed independently.
            time_window: (start_date, end_date) ISO strings for period filter.
                         Requires `timestamp_col` to be set on each metric spec.
            output_format: 'pandas' (default).

        Returns:
            DataFrame in standard format (9 columns, see ARCHITECTURE.md).
        """
        # 1. Normalize inputs to lists
        metric_names = [metrics] if isinstance(metrics, str) else list(metrics)
        slice_names = ([slices] if isinstance(slices, str) else list(slices)) if slices else None
        segment_names = (
            ([segments] if isinstance(segments, str) else list(segments)) if segments else None
        )

        # 2. Resolve specs from cache (raises SpecNotFoundError if not found)
        metric_specs = [self.spec_cache.get_metric(n) for n in metric_names]
        slice_specs = [self.spec_cache.get_slice(n) for n in slice_names] if slice_names else None
        segment_specs = (
            [self.spec_cache.get_segment(n) for n in segment_names] if segment_names else None
        )

        # 3. Build SQL query groups
        query_groups = QueryBuilder.build_queries(
            metric_specs=metric_specs,
            slice_specs=slice_specs,
            segment_specs=segment_specs,
            time_window=time_window,
            spec_cache=self.spec_cache,
        )

        # 4. Execute and return
        executor = QueryExecutor(self.connection_manager)
        df = executor.execute(query_groups, output_format=output_format)
        return ensure_standard_output(df)
```

**Verification**: Run Sub-Feature 5 tests.

---

### Sub-Feature 5: Update `aitaem/__init__.py`

Add `MetricCompute` to the top-level exports so users can do `from aitaem import MetricCompute`.

```python
from aitaem.insights import MetricCompute
```

---

### Sub-Feature 6: Update Example YAML files

**Files to modify**: `examples/metrics/ctr.yaml`, `cpc.yaml`, `roas.yaml`, `cpa.yaml`

Add `timestamp_col: date` to each (the `ad_campaigns` table has a `date` column).

Example diff for `ctr.yaml`:
```yaml
metric:
  name: ctr
  description: Click-through rate — ratio of clicks to impressions
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  aggregation: ratio
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date   # ← add this
```

---

### Sub-Feature 7: Implement `tests/test_insights.py`

Uses existing `ad_campaigns_connection_manager` fixture and `examples/` YAML files.

**Setup**: Load SpecCache once via `SpecCache.from_yaml()` pointing at `examples/` directories.

**Test cases**:
1. `test_compute_single_metric_no_slices` — output has 9 columns, metric_value is non-null, slice_type == 'none'
2. `test_compute_single_metric_with_slice` — slice_type/slice_value populated correctly
3. `test_compute_single_metric_with_segment` — segment_name/segment_value populated
4. `test_compute_with_time_window` — period_start_date/period_end_date match time_window
5. `test_compute_multiple_metrics` — all metric names appear in output
6. `test_compute_multiple_slices` — independent slice rows (each slice has its own rows)
7. `test_compute_metric_not_found` — SpecNotFoundError raised with clear message
8. `test_compute_slice_not_found` — SpecNotFoundError raised
9. `test_output_column_order` — columns match STANDARD_COLUMNS exactly in order
10. `test_compute_returns_pandas_by_default` — isinstance(df, pd.DataFrame)

**Verification**: `python -m pytest tests/test_insights.py -v --cov=aitaem/insights`

---

### Sub-Feature 8: Update `README.md`

Add a **Quick Start** section after the existing intro with:
1. Installation: `pip install aitaem`
2. Three-step usage: load specs → set up connection → compute
3. Code example using the `ad_campaigns` dataset (matching `examples/` files)
4. Standard output format table (matching `ARCHITECTURE.md`)

---

## Critical Files Summary

| File | Change Type |
|------|-------------|
| `aitaem/specs/metric.py` | Modify — add `timestamp_col` field |
| `aitaem/utils/validation.py` | Modify — accept `timestamp_col` as known optional field |
| `aitaem/query/builder.py` | Modify — remove `timestamp_col` param; use per-metric col |
| `aitaem/insights.py` | Create — MetricCompute class |
| `aitaem/utils/formatting.py` | Create — `ensure_standard_output()` |
| `aitaem/__init__.py` | Modify — export MetricCompute |
| `tests/test_insights.py` | Create — 10 test cases |
| `tests/test_query/test_builder.py` | Modify — update 2 timestamp_col call sites |
| `tests/test_query/test_executor.py` | Modify — update 1 timestamp_col call site |
| `examples/metrics/ctr.yaml` | Modify — add `timestamp_col: date` |
| `examples/metrics/cpc.yaml` | Modify — add `timestamp_col: date` |
| `examples/metrics/roas.yaml` | Modify — add `timestamp_col: date` |
| `examples/metrics/cpa.yaml` | Modify — add `timestamp_col: date` |
| `README.md` | Modify — add Quick Start section |

---

## Verification Plan

1. **Unit tests**: `python -m pytest tests/ -v` — all existing + new tests pass
2. **Coverage**: `python -m pytest --cov=aitaem tests/test_insights.py` — insights.py at >90%
3. **Lint**: `ruff check aitaem/ tests/`
4. **End-to-end check**: Manually run a `MetricCompute.compute()` call using the examples directory
