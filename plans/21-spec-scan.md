# Plan: Spec Compatibility Scan (plan 21)

## Context

`MetricCompute.compute()` fails at query-execution time if a slice references a column that
doesn't exist in the metric's source table, or a segment's join key doesn't exist in the fact
table. There is currently no way to detect these mismatches before calling `compute()` — you
can introspect *which* columns each spec references (via `ValidationResult.referenced_columns`)
but not *whether those columns exist* in the source table.

This feature adds a `SpecCache.scan(connection_manager)` method that introspects source table
schemas and returns a compatibility matrix for all loaded metrics × slices and metrics × segments.
The primary use cases are: pre-flight validation before a `compute()` call, LLM agent tooling
("which slices are valid for metric X?"), and developer debugging.

---

## Compatibility Semantics

### Metric ↔ Slice (leaf / wildcard)
All columns referenced in `values[i].where` (or the bare `column` field for wildcard slices)
must exist in the metric's source table. A single missing column makes the pair incompatible.

### Metric ↔ Slice (composite)
Resolved transitively: each component leaf/wildcard slice must independently be compatible with
the metric. Composite slices carry no columns of their own.

### Metric ↔ Segment
The **fact-table side only** is checked: the effective join-key candidates —
`join_keys` if non-empty, otherwise `{entity_id}` — are intersected with the metric's source
table columns. The pair is **compatible** if at least one candidate key exists in the fact table.
The result records *which* keys are valid so the caller knows what to pass to `compute()`.
DIM-table columns (`values[i].where`) are on the segment's own source, not the metric's, and
are **not** checked here.

---

## Design

### Schema introspection
`IbisConnector.get_table(table_name).schema().names` returns column names without executing a
query. `ConnectionManager.get_connection_for_source(uri)` returns the right connector.
`QueryBuilder._parse_table_name_from_uri(uri)` (already exists — reuse it) gives the table name
for `get_table()`.

Introspections are **batched by unique source URI** — each distinct URI is introspected once,
regardless of how many metrics share it. Cost = O(U) roundtrips where U = unique source URIs.

### New file: `aitaem/specs/compatibility.py`

```python
@dataclass(frozen=True)
class CompatibilityResult:
    metric_name: str
    spec_name: str
    spec_type: Literal["slice", "segment"]
    compatible: bool
    valid_join_keys: list[str]    # segment only: candidate keys that exist in fact table
    missing_columns: list[str]    # columns/keys checked but absent from source table
    reason: str | None            # None when compatible; human-readable when not
```

```python
@dataclass(frozen=True)
class ScanResult:
    results: tuple[CompatibilityResult, ...]

    def compatible_slices(self, metric_name: str) -> list[str]: ...
    def compatible_segments(self, metric_name: str) -> list[str]: ...
    def compatible_metrics(self, spec_name: str) -> list[str]: ...
    def for_metric(self, metric_name: str) -> list[CompatibilityResult]: ...
    def for_spec(self, spec_name: str) -> list[CompatibilityResult]: ...
```

### `MetricCompute.scan()` — `aitaem/insights.py`

`MetricCompute` already holds both `SpecCache` and `ConnectionManager` as instance attributes,
making it the natural home. No new dependencies are introduced anywhere; `SpecCache` stays a
pure spec-loading/retrieval class with no knowledge of connections.

```python
def scan(self) -> ScanResult:
```

The scanning logic itself lives in a standalone `_run_scan(spec_cache, connection_manager)`
free function in `aitaem/specs/compatibility.py` so it remains independently testable.

Algorithm:
1. Collect unique source URIs from all loaded metrics.
2. For each URI: resolve connector → `get_table(table_name).schema().names` → `frozenset[str]`.
   Skip (warn + exclude metric) if connection unavailable or introspection fails.
3. For each metric × each slice:
   - Resolve component leaf specs (composite → `[cache.get_slice(n) for n in cross_product]`).
   - Collect required columns from each component's `validate().referenced_columns`.
   - `missing = required − source_columns`; compatible = `missing == ∅`.
4. For each metric × each segment:
   - `candidates = set(segment.join_keys) or {segment.entity_id}`.
   - `valid_keys = candidates ∩ source_columns`.
   - compatible = `len(valid_keys) > 0`.
5. Return `ScanResult(tuple(all_results))`.

`_parse_table_name_from_uri` is a static method on `QueryBuilder` — import it at call time
inside `_run_scan` to avoid a circular import.

### Tech Debt — Scaling

**TD-1 (scale):** The current algorithm is O(M × C + M × T) comparisons where M = metrics,
C = slices, T = segments. This is acceptable for catalogs in the low hundreds but degrades
for large spec registries. A future optimisation would invert the index: group slices and
segments by the set of columns they require, then for each unique source schema perform a
single set-intersection pass to classify all specs at once, reducing to O(U × (C + T)) where
U = unique source URIs. Deferred until real-world scale pressures arise.

---

## Files Modified

| File | Change |
|------|--------|
| `aitaem/specs/compatibility.py` | **New** — `CompatibilityResult`, `ScanResult` |
| `aitaem/insights.py` | Add `scan()` method to `MetricCompute`; delegate to `_run_scan` |
| `aitaem/specs/__init__.py` | Re-export `CompatibilityResult`, `ScanResult` |
| `aitaem/__init__.py` | Add `CompatibilityResult`, `ScanResult` to top-level exports and `__all__` |
| `docs/api/specs.md` | Add `CompatibilityResult` and `ScanResult` API entries |
| `docs/user-guide/specs.md` | Add "Compatibility scanning" section with example |
| `docs/changelog.md` | Add Unreleased entry |
| `tests/test_specs/test_compatibility.py` | **New** — unit + integration tests (see Verification) |

---

## Verification

1. **Unit — pure logic (no DB)**: mock `ConnectionManager` to return fixed column sets; assert
   `ScanResult.compatible_slices()` / `compatible_segments()` / `compatible_metrics()` are
   correct for leaf, wildcard, composite, and segment cases.

2. **Unit — incompatibility reasons**: assert `CompatibilityResult.missing_columns` is populated
   and `reason` is a non-empty string when incompatible.

3. **Integration — real DuckDB**: use the `ad_campaigns_connection_manager` fixture with example
   specs from `examples/`; assert all expected metric×slice pairs are compatible and a
   deliberately wrong slice (referencing a non-existent column) returns `compatible=False`.

4. **Full suite**: `pytest --cov=aitaem --cov-report=term-missing` — no regressions.
