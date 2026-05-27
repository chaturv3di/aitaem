# Plan 17 — R-5, R-7, R-9: Top-Level Exports

Addresses R-5, R-7, and R-9 from `REQ01-api-improvement-requests.md`.

---

## Problem

Three categories of symbols are useful to consumers but only accessible via internal
module paths:

| R | Symbol(s) | Internal path |
|---|-----------|---------------|
| R-5 | `STANDARD_COLUMNS` | `aitaem.utils.formatting` |
| R-7 | `MetricSpec`, `SliceSpec`, `SliceValue`, `SegmentSpec`, `SegmentValue` | `aitaem.specs.*` |
| R-9 | `IbisConnector` | `aitaem.connectors.ibis_connector` |

All three are purely additive re-exports. No existing behaviour changes.

---

## Scope

Export all of the above from `aitaem/__init__.py` and add each to `__all__`.

### R-7 scope (validators excluded)

The REQ01 originally requested exporting the `validate_*_spec` functions, but these are
**not included**. Rationale: the validators are implementation details of `SpecCache`'s
loading mechanism. Every consumer workflow that would call a validator is better served
by calling `SpecCache.add()` / `from_string()` and catching `SpecValidationError`:

```python
# Don't: validate then add (double validation, two error types to handle)
result = validate_metric_spec(spec_dict)
if not result.valid: ...
cache.add(metric=MetricSpec(**spec_dict))

# Do: add and catch — same structured errors, one step
try:
    cache.add(metric=MetricSpec.from_string(yaml_str))
except SpecValidationError as e:
    return [{"field": e.field, "message": e.message} for e in e.errors]
```

`SpecValidationError.errors` already carries the same `field` / `message` / `suggestion`
structure as `ValidationResult.errors`. There is no information lost by dropping the
standalone validators. `ValidationResult` and `ValidationError` are therefore also excluded
(they were only needed as return types of the validators).

- **`SliceValue` and `SegmentValue`**: included. Consumers working with `SliceSpec.values`
  or `SegmentSpec.values` need these types. Both are already in `aitaem.specs.__all__`.

---

## Critical Observations and Edge Cases

### 1. No circular import risk
`aitaem/__init__.py` already imports from `aitaem.specs.loader`, `aitaem.connectors.connection`,
`aitaem.insights`, `aitaem.query.builder`, and `aitaem.utils.exceptions`. All new imports come
from the same or sibling modules. No new dependency cycles are introduced.

### 2. `ValidationError` naming
`aitaem.utils.validation.ValidationError` is a simple `@dataclass`, not an `Exception`
subclass. It must **not** be added to the exceptions section of `__all__` and docs — it belongs
in a separate "Validation types" group. This distinction must be clear in the index docs to avoid
consumer confusion with `pydantic.ValidationError` or Python's own `ValueError`.

### 3. `STANDARD_COLUMNS` type annotation
The current definition in `formatting.py` is untyped:
```python
STANDARD_COLUMNS = [...]
```
Add `list[str]` annotation before export so the type is machine-readable:
```python
STANDARD_COLUMNS: list[str] = [...]
```

### 4. `IbisConnector` is already in `aitaem.connectors.__all__`
It is exported from the `aitaem.connectors` sub-package but not from the top-level `aitaem`
package. The fix is a single import + `__all__` entry. No changes to `ibis_connector.py` itself.

### 5. Grouping in `__all__`
New entries should be added to existing or new logical comment groups:
```python
# spec types
# validators and validation types
# constants and types      ← already exists, add STANDARD_COLUMNS here
# connectors               ← already has ConnectionManager, add IbisConnector
```

### 6. `SpecCache` already exported — no duplication
`SpecCache` is already in `__all__`. The spec type exports (`MetricSpec`, etc.) are additions,
not replacements. The existing `SpecCache` entry is untouched.

---

## Implementation Sub-Features

### SF-1: R-9 — Export `IbisConnector` (simplest, implement first)

**File changed:** `aitaem/__init__.py`

Add one import and one `__all__` entry:
```python
from aitaem.connectors.ibis_connector import IbisConnector
```
Add `"IbisConnector"` to `__all__` under the connectors section (alongside `ConnectionManager`).

**Test (in `tests/test_public_api.py`):**
```python
class TestIbisConnectorExport:
    def test_ibis_connector_in_all(self):
        assert "IbisConnector" in aitaem.__all__

    def test_ibis_connector_same_object(self):
        from aitaem.connectors.ibis_connector import IbisConnector as _IbisConnector
        assert aitaem.IbisConnector is _IbisConnector

    def test_ibis_connector_is_class(self):
        import inspect
        assert inspect.isclass(aitaem.IbisConnector)
```

---

### SF-2: R-5 — Export `STANDARD_COLUMNS`

**Files changed:** `aitaem/utils/formatting.py`, `aitaem/__init__.py`

In `formatting.py`, add the `list[str]` annotation:
```python
STANDARD_COLUMNS: list[str] = [
    "period_type",
    "period_start_date",
    "period_end_date",
    "entity_id",
    "metric_name",
    "slice_type",
    "slice_value",
    "segment_name",
    "segment_value",
    "metric_value",
]
```

In `__init__.py`:
```python
from aitaem.utils.formatting import STANDARD_COLUMNS
```
Add `"STANDARD_COLUMNS"` to `__all__` under `# constants and types`.

**Test (in `tests/test_public_api.py`):**
```python
class TestStandardColumnsExport:
    def test_standard_columns_in_all(self):
        assert "STANDARD_COLUMNS" in aitaem.__all__

    def test_standard_columns_same_object(self):
        from aitaem.utils.formatting import STANDARD_COLUMNS as _SC
        assert aitaem.STANDARD_COLUMNS is _SC

    def test_standard_columns_is_list_of_strings(self):
        assert isinstance(aitaem.STANDARD_COLUMNS, list)
        assert all(isinstance(c, str) for c in aitaem.STANDARD_COLUMNS)

    def test_standard_columns_contains_expected_columns(self):
        expected = {
            "period_type", "period_start_date", "period_end_date",
            "entity_id", "metric_name", "slice_type", "slice_value",
            "segment_name", "segment_value", "metric_value",
        }
        assert expected == set(aitaem.STANDARD_COLUMNS)
```

---

### SF-3: R-7 — Export spec types

**File changed:** `aitaem/__init__.py`

Add imports:
```python
from aitaem.specs.metric import MetricSpec
from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.specs.segment import SegmentSpec, SegmentValue
```

Add to `__all__`:
```python
# spec types
"MetricSpec",
"SliceSpec",
"SliceValue",
"SegmentSpec",
"SegmentValue",
```

**Test (in `tests/test_public_api.py`):**
```python
class TestSpecTypeExports:
    def test_spec_types_in_all(self):
        for name in ["MetricSpec", "SliceSpec", "SliceValue", "SegmentSpec", "SegmentValue"]:
            assert name in aitaem.__all__

    def test_spec_types_are_same_objects(self):
        from aitaem.specs.metric import MetricSpec as _MS
        from aitaem.specs.slice import SliceSpec as _SS, SliceValue as _SV
        from aitaem.specs.segment import SegmentSpec as _SeS, SegmentValue as _SeV
        assert aitaem.MetricSpec is _MS
        assert aitaem.SliceSpec is _SS
        assert aitaem.SliceValue is _SV
        assert aitaem.SegmentSpec is _SeS
        assert aitaem.SegmentValue is _SeV

    def test_spec_types_are_classes(self):
        import inspect
        for cls in [aitaem.MetricSpec, aitaem.SliceSpec, aitaem.SliceValue,
                    aitaem.SegmentSpec, aitaem.SegmentValue]:
            assert inspect.isclass(cls)
```

---

### SF-4: Documentation updates

**Files changed:**
- `docs/api/index.md`
- `docs/changelog.md`

#### `docs/api/index.md`

1. Update the code block in the intro to include new imports:
   ```python
   from aitaem import SpecCache, ConnectionManager, MetricCompute, IbisConnector
   from aitaem import PeriodType, VALID_PERIOD_TYPES, STANDARD_COLUMNS
   from aitaem import MetricSpec, SliceSpec, SliceValue, SegmentSpec, SegmentValue
   from aitaem import AitaemError, SpecNotFoundError, QueryBuildError  # etc.
   ```

2. Add `IbisConnector` to the Class Overview table:
   | `IbisConnector` | `aitaem.connectors.ibis_connector` | Ibis-based multi-backend connector |

3. Add `STANDARD_COLUMNS` to the Constants and Types table:
   | `STANDARD_COLUMNS` | `list[str]` | Ordered list of column names that `MetricCompute.compute()` always returns |

4. Extend Class Overview with the spec nested types:
   | `SliceValue` | `aitaem.specs.slice` | Individual slice value within a `SliceSpec` |
   | `SegmentValue` | `aitaem.specs.segment` | Individual segment value within a `SegmentSpec` |

   (`MetricSpec`, `SliceSpec`, `SegmentSpec` rows are already present in the table.)

#### `docs/changelog.md`

Add under `## Unreleased` → `### Added`:
```markdown
- `STANDARD_COLUMNS: list[str]` is now importable directly from `aitaem`. Contains the
  ordered list of column names that `MetricCompute.compute()` always returns
  (`period_type`, `period_start_date`, `period_end_date`, `entity_id`, `metric_name`,
  `slice_type`, `slice_value`, `segment_name`, `segment_value`, `metric_value`).
- Spec types (`MetricSpec`, `SliceSpec`, `SliceValue`, `SegmentSpec`, `SegmentValue`) are
  now importable directly from `aitaem` (previously only from `aitaem.specs`).
- `IbisConnector` is now importable directly from `aitaem` (previously only from
  `aitaem.connectors` or `aitaem.connectors.ibis_connector`).
```

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/__init__.py` | Add 7 new imports and `__all__` entries |
| `aitaem/utils/formatting.py` | Add `list[str]` type annotation to `STANDARD_COLUMNS` |
| `tests/test_public_api.py` | 3 new test classes (13 tests) |
| `docs/api/index.md` | Update intro code block, Class Overview, Constants table |
| `docs/changelog.md` | 3 new `### Added` entries under `## Unreleased` |

No changes to `aitaem.specs.*`, `aitaem.utils.validation`, or `aitaem.connectors.*`.

---

## Testing Strategy

1. Baseline: `python -m pytest -q` — all existing tests pass.
2. After SF-1: `python -m pytest tests/test_public_api.py -q -k TestIbisConnector` — new tests pass.
3. After SF-2: `python -m pytest tests/test_public_api.py -q -k TestStandardColumns` — new tests pass.
4. After SF-3: `python -m pytest tests/test_public_api.py -q -k TestSpecType` — new tests pass.
5. Final: `python -m pytest -q` — full suite green; `ruff check aitaem/` clean.
6. Commit once all pass.
