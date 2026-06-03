# Plan: Spec Compatibility Scan (plan 21)

## Context

`MetricCompute.compute()` fails at query-execution time if a slice references a column that
doesn't exist in the metric's source table, or a segment's join key doesn't exist in the fact
table. There is currently no way to detect these mismatches before calling `compute()` — you
can introspect *which* columns each spec references (via `ValidationResult.referenced_columns`)
but not *whether those columns exist* in the source table.

This feature adds `MetricCompute.scan()` — a pre-flight companion to `compute()` that
introspects source table schemas and returns a full compatibility matrix for all loaded
metrics × slices and metrics × segments. Primary use cases: pre-flight validation, LLM agent
tooling ("which slices are valid for metric X?"), and developer debugging.

---

## Compatibility Semantics

### Metric ↔ Slice (leaf / wildcard)
All columns referenced in `values[i].where` (or the bare `column` field for wildcard slices)
must exist in the metric's source table. A single missing column makes the pair incompatible.

### Metric ↔ Slice (composite)
Resolved transitively: each component leaf/wildcard slice must independently be compatible with
the metric. Composite slices carry no SQL expressions of their own.

### Metric ↔ Segment
The **fact-table side only** is checked: effective join-key candidates —
`join_keys` if non-empty, otherwise `{entity_id}` — are intersected with the metric's source
table columns. Compatible if at least one candidate key exists. The result records *which* keys
are valid so the caller knows what to pass to `compute(segments={...})`.
DIM-table columns (`values[i].where`) are on the segment's own source and are **not** checked.

---

## Design

### Schema introspection
`IbisConnector.get_table(table_name).schema().names` returns column names without executing a
query. `ConnectionManager.get_connection_for_source(uri)` resolves the right connector.
`QueryBuilder._parse_table_name_from_uri(uri)` (already exists in `query/builder.py`) extracts
the table name. Introspections are **batched by unique source URI** — O(U) roundtrips where
U = unique source URIs across all metrics.

### New file: `aitaem/specs/compatibility.py`

Pure data types only — no imports from `connectors/` or `query/`:

```python
@dataclass(frozen=True)
class CompatibilityResult:
    metric_name: str
    spec_name: str
    spec_type: Literal["slice", "segment"]
    compatible: bool
    valid_join_keys: list[str]    # segment only: candidate keys present in fact table
    missing_columns: list[str]    # columns/keys checked but absent from source table
    reason: str | None            # None when compatible; human-readable when not

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

`MetricCompute` already holds `SpecCache` and `ConnectionManager`, making it the natural home.
`SpecCache` stays connection-free. The logic lives in a private `_run_scan(spec_cache,
connection_manager)` free function in `insights.py`, keeping it independently testable.

```python
def scan(self) -> ScanResult:
    return _run_scan(self.spec_cache, self.connection_manager)
```

`_run_scan` algorithm:
1. Collect unique source URIs from all loaded metrics.
2. For each URI: `connector.get_table(table_name).schema().names` → `frozenset[str]`.
   Skip (warn + mark metric as unavailable) if connection fails.
3. For each metric × each loaded slice:
   - Resolve component leaf specs if composite (`cache.get_slice(n) for n in cross_product`).
   - Union required columns from each component's `validate().referenced_columns`.
   - `missing = required − source_columns`; `compatible = (missing == ∅)`.
4. For each metric × each loaded segment:
   - `candidates = set(segment.join_keys) or {segment.entity_id}`.
   - `valid_keys = candidates ∩ source_columns`; `compatible = len(valid_keys) > 0`.
5. Return `ScanResult(tuple(all_results))`.

Import `QueryBuilder._parse_table_name_from_uri` at call time inside `_run_scan` to avoid
any circular import risk.

### Tech Debt — Scaling
**TD-1:** Current algorithm is O(M × C + M × T). Acceptable for low hundreds. A future
inverted-index approach would group specs by required columns and do one set-intersection pass
per unique source URI — O(U × (C + T)). Deferred until real-world scale pressure.

---

## Files Modified

| File | Change |
|------|--------|
| `aitaem/specs/compatibility.py` | **New** — `CompatibilityResult`, `ScanResult` (pure data types) |
| `aitaem/specs/__init__.py` | Re-export `CompatibilityResult`, `ScanResult` |
| `aitaem/insights.py` | Add `MetricCompute.scan()` + private `_run_scan()` |
| `aitaem/__init__.py` | Add `CompatibilityResult`, `ScanResult` to top-level exports and `__all__` |
| `docs/api/specs.md` | Add `CompatibilityResult` and `ScanResult` under new "## Compatibility" heading |
| `docs/api/insights.md` | Document `MetricCompute.scan()` method |
| `docs/user-guide/specs.md` | Add "Compatibility scanning" section (end-to-end example) |
| `docs/user-guide/computing-metrics.md` | Add "Pre-flight check" section before Error Handling table |
| `docs/changelog.md` | Add Unreleased entry |
| `tests/test_specs/test_compatibility.py` | **New** — see test cases below |

### Documentation details

**`docs/user-guide/specs.md` — new "Compatibility scanning" section:**
End-to-end example: create `MetricCompute`, call `mc.scan()`, use
`result.compatible_slices("ctr")` and `result.compatible_segments("ctr")`, pass to
`mc.compute()`. Explain that only the fact-table side is checked for segments.

**`docs/user-guide/computing-metrics.md` — new "Pre-flight check" section:**
One paragraph + code snippet showing `mc.scan()` → `result.compatible_slices(metric)` →
`mc.compute(metrics=metric, slices=compatible)`. Placed just before the Error Handling table.

**`docs/api/specs.md`:** Add `CompatibilityResult` field table and `ScanResult` method
descriptions under a new "## Compatibility" heading.

**`docs/api/insights.md`:** Add `scan()` docstring reference under `MetricCompute`.

**`docs/changelog.md`:** Unreleased entry — Added `MetricCompute.scan()`, `CompatibilityResult`,
`ScanResult`.

---

## Test Cases — `tests/test_specs/test_compatibility.py`

### Unit tests (mocked ConnectionManager, no DB)

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_leaf_slice_compatible` | All referenced columns present → `compatible=True`, `missing_columns=[]` |
| 2 | `test_leaf_slice_incompatible` | One column missing → `compatible=False`, `missing_columns=[col]`, `reason` non-empty |
| 3 | `test_wildcard_slice_compatible` | Bare column present → `compatible=True` |
| 4 | `test_wildcard_slice_incompatible` | Bare column absent → `compatible=False` |
| 5 | `test_composite_slice_all_components_compatible` | Both component leaf slices compatible → `compatible=True` |
| 6 | `test_composite_slice_one_component_incompatible` | One component missing column → `compatible=False` |
| 7 | `test_segment_all_join_keys_valid` | All `join_keys` in source → `compatible=True`, `valid_join_keys` = all keys |
| 8 | `test_segment_partial_join_keys_valid` | Some `join_keys` in source → `compatible=True`, `valid_join_keys` = present subset |
| 9 | `test_segment_no_join_keys_valid` | No `join_keys` in source → `compatible=False`, `valid_join_keys=[]` |
| 10 | `test_segment_entity_id_fallback_compatible` | No `join_keys` declared, `entity_id` present → `compatible=True` |
| 11 | `test_segment_entity_id_fallback_incompatible` | No `join_keys` declared, `entity_id` absent → `compatible=False` |
| 12 | `test_schema_introspection_batched_by_uri` | Two metrics sharing a source URI → `get_table` called exactly once |
| 13 | `test_unavailable_connection_skips_metric` | `get_connection_for_source` raises → that metric skipped, warning logged, remaining metrics still scanned |
| 14 | `test_scan_result_compatible_slices` | `ScanResult.compatible_slices("m")` returns names of compatible slices only |
| 15 | `test_scan_result_compatible_segments` | `ScanResult.compatible_segments("m")` returns names of compatible segments only |
| 16 | `test_scan_result_compatible_metrics_for_spec` | `ScanResult.compatible_metrics("s")` returns metric names compatible with spec "s" |
| 17 | `test_scan_result_for_metric` | `ScanResult.for_metric("m")` returns all `CompatibilityResult` rows for that metric |
| 18 | `test_scan_result_for_spec` | `ScanResult.for_spec("s")` returns all rows for that spec across all metrics |
| 19 | `test_empty_cache_returns_empty_scan_result` | No slices/segments loaded → `ScanResult.results == ()` |

### Integration tests (real DuckDB, `ad_campaigns_connection_manager` fixture)

| # | Test | Asserts |
|---|------|---------|
| 20 | `test_ad_campaigns_slices_all_compatible` | `ctr` metric + slices referencing real `ad_campaigns` columns → all `compatible=True` |
| 21 | `test_ad_campaigns_segment_compatible` | `ctr` metric + `platform` segment → `compatible=True`, `valid_join_keys=['platform']` |
| 22 | `test_incompatible_slice_detected` | Slice referencing `nonexistent_column` → `compatible=False`, `missing_columns=['nonexistent_column']` |

---

## Verification

```
pytest tests/test_specs/test_compatibility.py -v
pytest --cov=aitaem --cov-report=term-missing
```
