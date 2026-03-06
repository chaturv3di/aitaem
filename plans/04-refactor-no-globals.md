# Refactor Plan: Remove Global Singletons and Promote SpecCache

## Overview

This plan refactors the existing connector, specs, and query modules to align with two architectural decisions made after their initial implementation:

1. **`SpecCache` becomes a first-class public type** — it owns YAML loading via `from_yaml()` / `from_string()` classmethods, validates eagerly at load time, and is passed explicitly to consumers.
2. **No global singletons** — `ConnectionManager.get_global()` / `set_global()` and `SpecCache.get_global()` / `set_global()` are removed. Both objects are injected explicitly wherever needed.

The primary cascading effect: `QueryExecutor` (which pulled `ConnectionManager` from global state) and `QueryBuilder` (which pulled `SpecCache` from global state for composite slice resolution) must be updated to receive these objects via constructor / parameter injection.

**Scope**: Refactor only — no new functionality. All existing behaviour is preserved; only the wiring changes.

---

## Impact Analysis

| File | Change Type | Summary |
|------|-------------|---------|
| `aitaem/connectors/connection.py` | Modify | Remove `set_global()`, `get_global()`, `_global_instance` |
| `aitaem/specs/loader.py` | Modify | Add `from_yaml()` / `from_string()` classmethods; rename `add_spec()` → `add()`; switch to eager loading; remove `set_global()` / `get_global()` |
| `aitaem/query/builder.py` | Modify | `build_queries()` and `_resolve_slice_components()` receive `spec_cache` parameter instead of calling `SpecCache.get_global()` |
| `aitaem/query/executor.py` | Modify | `QueryExecutor.__init__` takes `connection_manager`; remove `ConnectionManager.get_global()` call |
| `aitaem/__init__.py` | Modify | Export `MetricCompute`, `SpecCache`, `ConnectionManager` at depth-1 |
| `tests/test_connectors/test_connection_manager.py` | Modify | Remove global singleton test cases |
| `tests/test_specs/` | Modify | Update `SpecCache` construction and usage |
| `tests/test_query/` | Modify | Update `QueryBuilder` and `QueryExecutor` call sites |
| `examples/connections.yaml` | Modify | Add BigQuery backend with env var substitution |
| `examples/README.md` | Modify | Update usage example to the new API |

---

## 1. `connectors/connection.py` — Remove Global Singleton

### What changes

Remove the three global singleton members from `ConnectionManager`:

```python
# REMOVE these three:
_global_instance: ClassVar['ConnectionManager | None'] = None

@classmethod
def set_global(cls, manager: 'ConnectionManager') -> None: ...

@classmethod
def get_global(cls) -> 'ConnectionManager': ...
```

No other changes to `ConnectionManager`. All other methods (`from_yaml`, `add_connection`, `get_connection`, `get_connection_for_source`, `parse_source_uri`, `close_all`) remain unchanged.

### connectors/__init__.py

No change needed — `ConnectionManager` is already exported.

### Test changes (`test_connection_manager.py`)

Remove test cases that cover the global singleton:
- `test_set_global_and_get_global`
- `test_get_global_before_set_raises_error`
- Any test that calls `ConnectionManager.set_global()` or `ConnectionManager.get_global()`

No new tests needed — the remaining method coverage is unchanged.

---

## 2. `specs/loader.py` — Promote SpecCache to First-Class Type

### What changes

This is the most substantial change. The current `SpecCache` constructor accepts path lists and loads lazily on first `get_*()` call. The new design:

- Constructor becomes private / empty (no paths)
- `from_yaml()` and `from_string()` classmethods replace the constructor as the public entry points
- Loading is **eager** — all specs are loaded and validated when `from_yaml()` / `from_string()` returns
- `add_spec()` is renamed to `add()`
- `set_global()` and `get_global()` are removed

#### New public API

```python
class SpecCache:
    def __init__(self):
        """Empty cache. Use from_yaml() or from_string() to load specs."""
        self._metrics: dict[str, MetricSpec] = {}
        self._slices: dict[str, SliceSpec] = {}
        self._segments: dict[str, SegmentSpec] = {}

    @classmethod
    def from_yaml(
        cls,
        metric_paths: str | list[str] | None = None,
        slice_paths: str | list[str] | None = None,
        segment_paths: str | list[str] | None = None,
    ) -> 'SpecCache':
        """
        Load and validate all specs from YAML files or directories.
        Validates eagerly — raises SpecValidationError on the first invalid spec.
        Raises SpecNotFoundError if a path does not exist.
        """

    @classmethod
    def from_string(
        cls,
        metric_yaml: str | list[str] | None = None,
        slice_yaml: str | list[str] | None = None,
        segment_yaml: str | list[str] | None = None,
    ) -> 'SpecCache':
        """Load specs from YAML strings. Validates eagerly."""

    def add(self, spec: MetricSpec | SliceSpec | SegmentSpec) -> None:
        """Add a spec programmatically. Validates immediately."""

    def get_metric(self, name: str) -> MetricSpec: ...
    def get_slice(self, name: str) -> SliceSpec: ...
    def get_segment(self, name: str) -> SegmentSpec: ...
    def clear(self) -> None: ...
```

#### Remove from SpecCache

```python
# REMOVE these three:
_global_instance: ClassVar['SpecCache | None'] = None

@classmethod
def set_global(cls, cache: 'SpecCache') -> None: ...

@classmethod
def get_global(cls) -> 'SpecCache': ...
```

#### Eager vs. lazy loading

The existing lazy load logic (loading on first `get_metric()` call) moves into `from_yaml()` itself. The `_load_*` private helpers can be reused — they just get called immediately inside `from_yaml()` rather than deferred.

Cross-reference validation (`_validate_slice_cross_references`) is called at the end of `from_yaml()` after all slices are loaded.

#### specs/__init__.py

Add `SpecCache` to the exports (it may already be there; confirm and keep it).

### Test changes (`test_specs/`)

Update all test cases that construct `SpecCache` with path arguments in the constructor:

```python
# BEFORE
cache = SpecCache(metric_paths=['metrics/'], slice_paths=['slices/'])

# AFTER
cache = SpecCache.from_yaml(metric_paths=['metrics/'], slice_paths=['slices/'])
```

Update test cases that call `add_spec()`:

```python
# BEFORE
cache.add_spec(metric_spec)

# AFTER
cache.add(metric_spec)
```

Remove test cases that cover `set_global` / `get_global`:
- `test_set_global_and_get_global`
- `test_get_global_before_set_raises_runtime_error`

Add test cases for the new classmethods:
- `test_from_yaml_loads_all_specs_eagerly` — verify all specs loaded before any `get_*()` call
- `test_from_yaml_raises_on_invalid_spec` — verify `SpecValidationError` raised immediately
- `test_from_string_single_metric`
- `test_from_string_multiple_metrics` (list of YAML strings)
- `test_add_validates_immediately` — `add()` on an invalid spec raises immediately

---

## 3. `query/builder.py` — Inject SpecCache Instead of Global

### What changes

`QueryBuilder._resolve_slice_components()` currently calls `SpecCache.get_global()` to resolve composite slices. Since there is no longer a global, the cache must be passed in.

The change propagates upward through the call chain:

```
build_queries()
  └── _build_queries_for_metric()
        └── _resolve_slice_components()   ← calls SpecCache.get_global() TODAY
```

#### Updated signatures

```python
@staticmethod
def build_queries(
    metric_specs: list[MetricSpec],
    slice_specs: list[SliceSpec] | None,
    segment_specs: list[SegmentSpec] | None,
    time_window: tuple[str, str] | None,
    timestamp_col: str | None = None,
    spec_cache: SpecCache | None = None,   # NEW — required only when composite slices used
) -> list[QueryGroup]: ...

@staticmethod
def _build_queries_for_metric(
    metric: MetricSpec,
    slice_specs: list[SliceSpec] | None,
    segment_specs: list[SegmentSpec] | None,
    time_filter_sql: str | None,
    period_type: str,
    period_start: str | None,
    period_end: str | None,
    spec_cache: SpecCache | None,           # NEW — threaded through
) -> list[str]: ...

@staticmethod
def _resolve_slice_components(
    slice_spec: SliceSpec,
    spec_cache: SpecCache | None,           # NEW — replaces SpecCache.get_global()
) -> list[SliceSpec]: ...
```

#### Behaviour of `_resolve_slice_components` with `spec_cache=None`

If `slice_spec.is_composite` and `spec_cache is None`, raise a clear `QueryBuildError`:

```
QueryBuildError: Composite slice 'geo_device' requires a SpecCache to resolve its
components ['geo', 'device'], but no spec_cache was provided to build_queries().
```

If `slice_spec` is a leaf, `spec_cache=None` is fine — composite resolution is not needed.

### Test changes (`test_query/test_builder.py`)

Update all `QueryBuilder.build_queries()` call sites to pass `spec_cache` when composite slices are involved. For tests using only leaf slices, `spec_cache=None` (the default) is sufficient and requires no change.

Add test case:
- `test_build_queries_composite_slice_without_cache_raises` — verify `QueryBuildError` raised with a clear message when a composite slice is passed without a cache.

---

## 4. `query/executor.py` — Inject ConnectionManager

### What changes

`QueryExecutor` currently calls `ConnectionManager.get_global()` inside `_execute_query_group()`. The manager is now injected via the constructor.

#### Updated class

```python
class QueryExecutor:
    def __init__(self, connection_manager: ConnectionManager):
        """
        Args:
            connection_manager: Provides backend connections for query execution.
        """
        self.connection_manager = connection_manager

    def execute(
        self,
        query_groups: list[QueryGroup],
        output_format: str = 'pandas',
    ) -> DataFrame: ...

    def _execute_query_group(
        self,
        query_group: QueryGroup,
        output_format: str,
    ) -> DataFrame | None:
        # BEFORE: conn_mgr = ConnectionManager.get_global()
        # AFTER:  conn_mgr = self.connection_manager
        connector = self.connection_manager.get_connection_for_source(query_group.source)
        ...
```

No other changes to execution logic, partial-result handling, or warning behaviour.

### Test changes (`test_query/test_executor.py`)

Update all `QueryExecutor()` instantiations:

```python
# BEFORE
executor = QueryExecutor()

# AFTER
executor = QueryExecutor(connection_manager=mock_conn_mgr)
```

Remove test cases that rely on `ConnectionManager.set_global()` / `get_global()` as setup:
- Replace `ConnectionManager.set_global(mock_mgr)` setup with direct constructor injection.

---

## 5. `aitaem/__init__.py` — Update Depth-1 Exports

### What changes

Update top-level exports to reflect the new primary interface:

```python
# aitaem/__init__.py

from aitaem.insights import MetricCompute          # future — when insights.py is implemented
from aitaem.specs.loader import SpecCache
from aitaem.connectors.connection import ConnectionManager

__all__ = ["MetricCompute", "SpecCache", "ConnectionManager"]
```

Since `insights.py` is not yet implemented, export `SpecCache` and `ConnectionManager` now. Add `MetricCompute` when that module is implemented.

---

## 6. Examples

### `examples/connections.yaml` — Add BigQuery Backend

The current file only configures DuckDB. Update it to include a BigQuery backend with environment variable substitution for the project ID, demonstrating the multi-backend pattern:

```yaml
# Example connections configuration for the ad campaigns dataset.
# Run `python examples/data/setup_db.py` first to create the DuckDB file.
# Paths are relative to the project root.

# DuckDB - local ad campaigns database
duckdb:
  path: examples/data/ad_campaigns.duckdb

# BigQuery - cloud data warehouse
# Requires Application Default Credentials: gcloud auth application-default login
bigquery:
  project_id: ${GCP_PROJECT_ID}
```

The `${GCP_PROJECT_ID}` value is read from the environment at load time. If the variable is not set, `ConnectionManager.from_yaml()` raises a `ConfigurationError` immediately.

### `examples/README.md` — Update Usage Section

The current usage example uses the removed API (`set_connections`, `MetricCompute.from_yaml`). Replace it with the new explicit pattern:

```python
from aitaem.specs import SpecCache
from aitaem.connectors import ConnectionManager
from aitaem.insights import MetricCompute

# Step 1: Load and validate specs (run from project root)
cache = SpecCache.from_yaml(
    metric_paths='examples/metrics/',
    slice_paths='examples/slices/',
    segment_paths='examples/segments/',
)

# Step 2: Set up backend connections
conn_mgr = ConnectionManager.from_yaml('examples/connections.yaml')

# Step 3: Compute CTR sliced by campaign type, segmented by platform
mc = MetricCompute(cache, conn_mgr)
df = mc.compute(
    metrics='ctr',
    slices='campaign_type',
    segments='platform',
    time_window=('2024-01-01', '2024-07-01'),
    timestamp_col='date',
)
print(df)
```

No other sections of `examples/README.md` require changes.

---

## 8. Implementation Order

These changes are mostly independent, but the following order avoids broken intermediate states:

| Step | Target | Why this order |
|------|--------|----------------|
| 1 | `specs/loader.py` (SpecCache) | `query/builder.py` depends on the new SpecCache API |
| 2 | `connectors/connection.py` | `query/executor.py` depends on ConnectionManager (no global) |
| 3 | `query/builder.py` | Depends on SpecCache changes (step 1) |
| 4 | `query/executor.py` | Depends on ConnectionManager changes (step 2) |
| 5 | `aitaem/__init__.py` | Depends on all modules being stable |
| 6 | `examples/connections.yaml` | Independent; can be updated at any point |
| 7 | `examples/README.md` | Update after source changes are stable |
| 8 | Tests | Update after all source changes |

---

## 9. Verification Checklist

### Functionality
- [ ] `SpecCache.from_yaml(metric_paths=..., slice_paths=..., segment_paths=...)` loads all specs eagerly
- [ ] `SpecCache.from_string(metric_yaml=...)` works for single and list-of-strings inputs
- [ ] `SpecCache.add(spec)` validates immediately, raises on invalid spec
- [ ] `SpecCache` has no `set_global`, `get_global`, or `_global_instance`
- [ ] `ConnectionManager` has no `set_global`, `get_global`, or `_global_instance`
- [ ] `QueryBuilder.build_queries(..., spec_cache=cache)` resolves composite slices correctly
- [ ] `QueryBuilder.build_queries(..., spec_cache=None)` works for leaf-only slices
- [ ] `QueryBuilder.build_queries(composite_slice, spec_cache=None)` raises `QueryBuildError` with clear message
- [ ] `QueryExecutor(connection_manager=conn_mgr).execute(...)` works end-to-end
- [ ] `from aitaem import SpecCache, ConnectionManager` works (depth-1 import)

### Tests
- [ ] All existing tests pass after updates (no test regressions)
- [ ] Global singleton tests removed from `test_connection_manager.py`
- [ ] Global singleton tests removed from `test_specs/` (SpecCache)
- [ ] Global singleton setup removed from `test_query/` tests
- [ ] New `SpecCache` classmethod tests added and passing
- [ ] New `QueryBuilder` composite-without-cache error test added and passing
- [ ] `pytest --cov` shows no drop in coverage

### Examples
- [ ] `examples/connections.yaml` includes both `duckdb` and `bigquery` backends
- [ ] `examples/connections.yaml` uses `${GCP_PROJECT_ID}` env var substitution for BigQuery project ID
- [ ] `examples/README.md` usage example uses `SpecCache.from_yaml()`, `ConnectionManager.from_yaml()`, and `MetricCompute(cache, conn_mgr)`
- [ ] No reference to `set_connections` or `MetricCompute.from_yaml` remains in examples

### Code Quality
- [ ] No remaining calls to `ConnectionManager.get_global()` in source files
- [ ] No remaining calls to `SpecCache.get_global()` in source files
- [ ] `ruff check` passes
- [ ] `ruff format` applied

---

## Notes

- **`insights.py` is not yet implemented.** The refactors here establish the contracts that `MetricCompute` will depend on. When `insights.py` is implemented, it will take `SpecCache` and `ConnectionManager` as constructor arguments — consistent with this plan.
- **No behaviour changes.** All query generation, SQL output, and result formatting logic is untouched. Only the wiring (how objects are obtained) changes.
- **Existing plan documents (01, 02, 03) are historical.** They describe what was built; this plan describes what is being updated. Do not modify the earlier plan files.
