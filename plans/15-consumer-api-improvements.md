# Plan 15 — Consumer API Improvements (R-1, R-2, R-4 + SpecCache Uniqueness)

Addresses four items from `REQ01-api-improvement-requests.md` plus an additional
uniqueness invariant for `SpecCache`. Also covers R-6 (typed `period_type` on
`compute()`) because it is a natural consequence of R-2.

---

## Scope

| Item | Description |
|------|-------------|
| R-1 | Expose all exception classes at the top-level `aitaem` package |
| R-2 | Public `VALID_PERIOD_TYPES` frozenset and `PeriodType` Literal |
| R-4 | `SpecCache.metrics` / `.slices` / `.segments` read-only introspection |
| R-6 | Type-annotate `MetricCompute.compute(period_type)` with `PeriodType` |
| +U  | `SpecCache` raises on duplicate spec names (all loading paths) |

Out of scope: R-3, R-5, R-7, R-8, R-9.

---

## Background & Critical Observations

### `ConnectionError` name collision
`aitaem.utils.exceptions.ConnectionError` shadows Python's built-in `ConnectionError`
(a subclass of `OSError`). Any consumer who writes `from aitaem import ConnectionError`
would silently shadow the built-in in their scope. **Resolution**: rename the class to
`AitaemConnectionError` throughout the codebase before exporting it.

Current usages of the old name:
- `aitaem/utils/exceptions.py` — class definition
- `aitaem/utils/__init__.py` — re-export
- `aitaem/connectors/ibis_connector.py` — imports with alias `ConnectionError as AitaemConnectionError`
- `aitaem/connectors/base.py` — docstring only
- `aitaem/connectors/README.md` — docstring only
- `tests/test_connectors/test_ibis_connector.py` — imports with alias

### `PeriodType` single source of truth
`PeriodType = Literal[...]` and `VALID_PERIOD_TYPES: frozenset` must stay in sync.
The cleanest approach is to **derive the frozenset from the Literal** using
`typing.get_args()`, so there is only one place to edit when a period type is added:

```python
from typing import Literal, get_args
PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly"]
VALID_PERIOD_TYPES: frozenset[str] = frozenset(get_args(PeriodType))
```

Both live in `aitaem/query/builder.py` (where period-type logic already lives).
`insights.py` already imports `QueryBuilder` from `builder.py`, so it can also
import `PeriodType` from there without introducing a new dependency.

### `SpecCache` duplicate detection — inconsistent current behavior
| Loading path | Current behavior |
|---|---|
| `from_yaml()` → `_load_paths_strict()` | Logs warning, **overwrites** (last-write-wins) |
| `from_string()` | Silent **first-write-wins** via `setdefault` |
| `add()` | Silent first-write-wins; documented as intended |

All three should raise `SpecValidationError` on duplicate. This is a **breaking
change** for `add()`, which currently documents first-write-wins semantics —
one existing test (`test_add_first_write_wins`) must be updated.

### `SpecCache` introspection — return type
Properties should return `Mapping[str, XxxSpec]` (from `collections.abc`), backed
by `types.MappingProxyType`. This gives callers read-only access to the underlying
dict without copying it. A caller who tries to mutate the mapping will get a
`TypeError` at runtime rather than silently corrupting internal state.

---

## Implementation Sub-Features

Implement in this order — each sub-feature is independently testable.

---

### SF-1: Rename `ConnectionError` → `AitaemConnectionError`

**Files changed:**
- `aitaem/utils/exceptions.py` — rename class definition
- `aitaem/utils/__init__.py` — update import and `__all__` entry
- `aitaem/connectors/ibis_connector.py` — drop alias (`ConnectionError as AitaemConnectionError`
  → `AitaemConnectionError` directly)
- `aitaem/connectors/base.py` — update docstring references
- `aitaem/connectors/README.md` — update prose references
- `tests/test_connectors/test_ibis_connector.py` — drop alias

**Edge cases:**
- The rename must touch every `isinstance(e, AitaemConnectionError)` check in
  `ibis_connector.py` — there are two (lines 81 and 99 area). The alias already
  makes them consistent; we're just making the import direct.

**Validation:**
- Existing `test_connectors/test_ibis_connector.py` tests already cover
  `AitaemConnectionError` semantics and will continue passing after the alias is removed.

---

### SF-2: R-1 — Export all exceptions from `aitaem/__init__.py`

**Files changed:**
- `aitaem/__init__.py`

Add imports and extend `__all__`:
```python
from aitaem.utils.exceptions import (
    AitaemError,
    AitaemConnectionError,
    ConnectionNotFoundError,
    TableNotFoundError,
    ConfigurationError,
    InvalidURIError,
    UnsupportedBackendError,
    QueryBuildError,
    QueryExecutionError,
    SpecValidationError,
    SpecNotFoundError,
)

__all__ = [
    "SpecCache", "ConnectionManager", "MetricCompute",
    # exceptions
    "AitaemError",
    "AitaemConnectionError",
    "ConnectionNotFoundError",
    "TableNotFoundError",
    "ConfigurationError",
    "InvalidURIError",
    "UnsupportedBackendError",
    "QueryBuildError",
    "QueryExecutionError",
    "SpecValidationError",
    "SpecNotFoundError",
]
```

**Validation:**
- New test: `tests/test_public_api.py` — import each exception from `aitaem` and
  verify it is the same object as the one from `aitaem.utils.exceptions`.
- Verify `from aitaem import AitaemConnectionError` does **not** shadow the built-in
  `ConnectionError` (they are distinct names now).

---

### SF-3: R-2 — `PeriodType` and `VALID_PERIOD_TYPES`

**Files changed:**
- `aitaem/query/builder.py` — add `PeriodType`, derive `VALID_PERIOD_TYPES`, rename private `_VALID_PERIOD_TYPES`
- `aitaem/__init__.py` — re-export both

In `builder.py`:
```python
from typing import Literal, get_args

PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly"]
VALID_PERIOD_TYPES: frozenset[str] = frozenset(get_args(PeriodType))
```

Replace the two internal uses of `_VALID_PERIOD_TYPES` with `VALID_PERIOD_TYPES`.

In `aitaem/__init__.py`:
```python
from aitaem.query.builder import PeriodType, VALID_PERIOD_TYPES
__all__ = [..., "PeriodType", "VALID_PERIOD_TYPES"]
```

**Edge cases:**
- `get_args(PeriodType)` returns a `tuple[str, ...]`. `frozenset(...)` on a tuple works
  correctly.
- `_generate_period_boundaries()` in `builder.py` has an `else` branch that raises
  `QueryBuildError("Unknown period_type '...'")` after checking for daily/weekly/monthly/yearly.
  This is separate from the top-level `VALID_PERIOD_TYPES` check and is a defensive
  guard — leave it unchanged.

**Validation:**
- `VALID_PERIOD_TYPES == frozenset(get_args(PeriodType))` — assertable in a unit test
- Import `PeriodType, VALID_PERIOD_TYPES` from `aitaem` in the new `test_public_api.py`

---

### SF-4: R-6 — Type-annotate `MetricCompute.compute(period_type)`

**Files changed:**
- `aitaem/insights.py`

```python
from aitaem.query.builder import PeriodType, QueryBuilder

def compute(
    self,
    ...
    period_type: PeriodType = "all_time",
    ...
) -> pd.DataFrame:
```

**Edge cases:**
- Runtime behavior is unchanged — `QueryBuilder.build_queries()` still validates
  against `VALID_PERIOD_TYPES`. The annotation is purely informational for IDEs
  and static checkers.
- The default value `"all_time"` is a string literal compatible with `PeriodType`;
  no change needed.

**Validation:**
- Existing `test_insights_period_granularity.py` tests cover period_type paths.
  Run them after the annotation change to confirm no regression.

---

### SF-5: R-4 — `SpecCache` introspection properties

**Files changed:**
- `aitaem/specs/loader.py`

Add three properties:
```python
from collections.abc import Mapping
from types import MappingProxyType

@property
def metrics(self) -> Mapping[str, MetricSpec]:
    return MappingProxyType(self._metrics)

@property
def slices(self) -> Mapping[str, SliceSpec]:
    return MappingProxyType(self._slices)

@property
def segments(self) -> Mapping[str, SegmentSpec]:
    return MappingProxyType(self._segments)
```

**Edge cases:**
- `MappingProxyType` is read-only: `cache.metrics["foo"] = x` raises `TypeError`.
  This is the intended behaviour and should be tested.
- `MappingProxyType` wraps the live dict, so mutations via internal `_metrics`
  (e.g., `add()`, `clear()`) are immediately reflected in a held proxy reference.
  This is correct — no stale snapshot problem.
- Do **not** export `MetricSpec`, `SliceSpec`, `SegmentSpec` from `aitaem/__init__.py`
  in this plan (that is R-7). The properties are still useful for consumers who
  import the spec types from their existing internal paths.

**Validation:**
- New tests in `tests/test_specs/test_spec_loader.py`:
  - `.metrics` returns a mapping with expected keys
  - `.slices` and `.segments` similarly
  - Attempting to mutate raises `TypeError`
  - Properties return an empty mapping for an empty cache
  - `clear()` followed by property access returns an empty mapping

---

### SF-6: SpecCache duplicate name enforcement

**Files changed:**
- `aitaem/specs/loader.py`

**`_load_paths_strict()`** — replace warning+overwrite with a raise:
```python
if spec.name in result:
    from aitaem.utils.validation import ValidationError
    raise SpecValidationError(
        spec_type.__name__.replace("Spec", "").lower(),
        spec.name,
        [ValidationError(
            field="name",
            message=f"Duplicate spec name '{spec.name}': already loaded from a previous file",
        )],
    )
result[spec.name] = spec
```

**`from_string()`** — replace `setdefault` with explicit check:
```python
for yaml_str in cls._normalize_strings(metric_yaml):
    spec = load_spec_from_string(yaml_str, MetricSpec)
    if spec.name in cache._metrics:
        raise SpecValidationError(
            "metric", spec.name,
            [ValidationError(field="name", message=f"Duplicate spec name '{spec.name}'")],
        )
    cache._metrics[spec.name] = spec
```
(Same pattern for slice and segment loops.)

**`add()`** — raise instead of first-write-wins, update docstring:
```python
def add(self, spec: MetricSpec | SliceSpec | SegmentSpec) -> None:
    """Add a spec programmatically.

    Raises:
        SpecValidationError: if a spec with the same name is already present.
    """
    if isinstance(spec, MetricSpec):
        if spec.name in self._metrics:
            raise SpecValidationError(...)
        self._metrics[spec.name] = spec
    ...
```

**Tests to update:**
- `test_duplicate_name_in_directory_last_wins` (in `TestLoadSpecsFromDirectory`):
  rename to `test_duplicate_name_in_directory_raises` and assert `SpecValidationError`
- `test_add_first_write_wins` (in `TestSpecCacheAdd`):
  rename to `test_add_duplicate_name_raises` and assert `SpecValidationError`

**New tests to add:**
- `from_string()` with duplicate metric name raises `SpecValidationError`
- `from_string()` with duplicate slice name raises `SpecValidationError`
- `from_yaml()` with two files sharing a metric name raises `SpecValidationError`
- Error message includes the duplicate spec name

**Edge cases:**
- Two specs of **different types** (e.g., a metric and a slice both named `"revenue"`)
  are stored in separate dicts and do **not** conflict. The uniqueness constraint is
  per-type, not global.
- `load_specs_from_directory()` (standalone function, not part of `SpecCache`) currently
  also logs a warning on duplicate names. Leave this function unchanged — it is a
  lower-level utility with lenient semantics documented separately, and changing it
  is outside scope.

---

### SF-7: Documentation updates

**Files changed:**
- `docs/api/index.md` — add rows for `PeriodType`, `VALID_PERIOD_TYPES`, and all
  newly exported exception classes; add `metrics`/`slices`/`segments` properties
  to the `SpecCache` section
- `docs/api/specs.md` — document new `SpecCache` properties; note that `add()` now
  raises on duplicate
- `docs/changelog.md` — add entry under `## Unreleased`

No new `docs/api/` pages are needed — all new exports either belong to existing
modules or are re-exports from internal modules that already have documentation.
`PeriodType` and `VALID_PERIOD_TYPES` can be documented inline in the
`api/index.md` overview table or in a new "Constants & Types" row.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/utils/exceptions.py` | Rename `ConnectionError` → `AitaemConnectionError` |
| `aitaem/utils/__init__.py` | Update renamed class |
| `aitaem/connectors/ibis_connector.py` | Drop import alias |
| `aitaem/connectors/base.py` | Docstring update |
| `aitaem/connectors/README.md` | Prose update |
| `aitaem/query/builder.py` | Add `PeriodType`, derive `VALID_PERIOD_TYPES`, rename private symbol |
| `aitaem/insights.py` | Update `period_type` annotation |
| `aitaem/specs/loader.py` | Add properties; enforce uniqueness in all three loading paths |
| `aitaem/__init__.py` | Re-export all exceptions, `PeriodType`, `VALID_PERIOD_TYPES` |
| `tests/test_public_api.py` | **New** — smoke-test all new top-level exports |
| `tests/test_specs/test_spec_loader.py` | Update 2 tests; add duplicate/property tests |
| `tests/test_connectors/test_ibis_connector.py` | Drop import alias |
| `docs/api/index.md` | Add new exports |
| `docs/api/specs.md` | Document new properties and `add()` behaviour change |
| `docs/changelog.md` | Add `## Unreleased` entry |

---

## Testing Strategy

1. Run existing tests before starting to confirm green baseline.
2. After each SF, run `python -m pytest` and check coverage for the changed module.
3. Final run: `python -m pytest --cov=aitaem --cov-report=term-missing` to confirm
   overall coverage is not regressed.
4. Commit after all SFs pass, with a single descriptive commit message.
