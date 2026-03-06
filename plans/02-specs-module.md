# Implementation Plan: `specs/` Module

## Overview

This plan covers the implementation of the `aitaem/specs/` module, which is responsible for:

- Parsing YAML specification files into strongly-typed Python objects
- Validating specs at load time with clear, actionable error messages
- Providing lazy, cached loading of specs from files, directories, or YAML strings

The specs module is a **pure parsing/validation layer** — it produces typed objects from YAML but does not build Ibis expressions or execute queries. Expression building is handled downstream in `query/builder.py`.

**Key constraints**:
- No Ibis or database dependencies in this module; imports must be minimal
- Spec classes are pure data objects: parse, validate, and store fields only
- Conversion of specs to Ibis expressions/filters is handled entirely by `query/builder.py`
- All spec fields are validated at construction time; invalid specs raise `SpecValidationError`
- Specs are immutable after creation (frozen dataclasses or equivalent)

---

## Architecture Summary

```
aitaem/specs/
├── __init__.py        # Exports: MetricSpec, SliceSpec, SegmentSpec, SpecCache
├── metric.py          # MetricSpec dataclass + validation
├── slice.py           # SliceSpec dataclass + SliceValue + validation
├── segment.py         # SegmentSpec dataclass + validation
└── loader.py          # load_spec_from_file, load_spec_from_string,
                       # load_specs_from_directory, SpecCache
```

Supporting modules (must exist before specs can raise errors):
```
aitaem/utils/exceptions.py   # SpecValidationError, SpecNotFoundError
aitaem/utils/validation.py   # validate_metric_spec, validate_slice_spec, validate_segment_spec
```

---

## 1. Prerequisites

### 1.1 Exceptions (`aitaem/utils/exceptions.py`)

The following exceptions must be defined before any spec classes. If they already exist, confirm the signatures match.

```python
class AitaemError(Exception):
    """Base exception for all aitaem errors."""

class SpecValidationError(AitaemError):
    """Raised when a YAML spec fails validation."""
    def __init__(self, spec_type: str, name: str | None, errors: list[ValidationError]):
        self.spec_type = spec_type
        self.name = name
        self.errors = errors

class SpecNotFoundError(AitaemError):
    """Raised when a named spec cannot be found in configured paths."""
    def __init__(self, spec_type: str, name: str, searched_paths: list[str]):
        self.spec_type = spec_type
        self.name = name
        self.searched_paths = searched_paths
```

### 1.2 Validation Utilities (`aitaem/utils/validation.py`)

```python
@dataclass
class ValidationError:
    field: str
    message: str
    suggestion: str | None = None

@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError]

def validate_metric_spec(spec_dict: dict) -> ValidationResult: ...
def validate_slice_spec(spec_dict: dict) -> ValidationResult: ...
def validate_segment_spec(spec_dict: dict) -> ValidationResult: ...
```

**Two-tier validation approach**:

| Tier | What | How |
|------|------|-----|
| Structural | Required fields, enum values, URI format, non-empty strings, list constraints | Pure Python checks |
| SQL syntax | `numerator`, `denominator`, `where` clauses | `sqlglot` parser (see below) |

Semantic validation (column existence, type compatibility) is **not** performed in the specs layer — specs are decoupled from any table or schema. Semantic errors surface implicitly at query execution time.

**SQL syntax validation via `sqlglot`**:

`sqlglot` is a pure Python SQL parser with no database dependency. SQL fields are validated by wrapping them in a dummy statement and parsing:

- `numerator` / `denominator` → wrapped as `SELECT {expr}` (aggregation expression context)
- `where` → wrapped as `SELECT 1 WHERE {expr}` (predicate context)

If `sqlglot` raises a parse error, a `ValidationError` is recorded for that field with the parse failure message. DuckDB-specific syntax that deviates from standard SQL may occasionally produce false positives/negatives, but this is acceptable for Phase 1.

**Structural validation rules**:

| Spec | Required Fields | Optional Fields | Field Constraints |
|------|----------------|-----------------|-------------------|
| Metric | `name`, `source`, `aggregation`, `numerator` | `description`, `denominator` | `aggregation` in `{sum, avg, count, ratio, min, max}`; `denominator` required when `aggregation == 'ratio'`; `source` must match `scheme://...` URI format |
| Slice | `name` | `description`, `values`, `cross_product` | Exactly one of `values` or `cross_product` must be present; `values` must be non-empty list when present; each value must have `name` and `where`; `cross_product` must be a list of at least 2 unique non-empty strings |
| Segment | `name`, `source`, `values` | `description` | `source` must match `scheme://...` URI format; `values` must be non-empty list; each value must have `name` and `where` |

---

## 2. `MetricSpec` (`specs/metric.py`)

### 2.1 YAML Schema

Two variants are supported:

**Ratio metric**:
```yaml
metric:
  name: homepage_ctr
  description: Click-through rate for homepage impressions
  source: duckdb://analytics.db/events
  aggregation: ratio
  numerator: "SUM(CASE WHEN event_type = 'click' AND page = 'home_page' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' AND page = 'home_page' THEN 1 ELSE 0 END)"
```

**Simple aggregation** (`sum`, `avg`, `count`, `min`, `max`):
```yaml
metric:
  name: total_revenue
  description: Sum of all transaction amounts
  source: duckdb://analytics.db/transactions
  aggregation: sum
  numerator: "SUM(amount)"
```

### 2.2 Class Definition

```python
@dataclass(frozen=True)
class MetricSpec:
    name: str
    source: str          # URI: scheme://path/table
    aggregation: str     # sum | avg | count | ratio | min | max
    numerator: str       # SQL expression
    description: str = ""
    denominator: str | None = None   # Required when aggregation == 'ratio'

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> 'MetricSpec':
        """
        Load and validate a MetricSpec from a YAML file path or YAML string.

        If yaml_input is a valid file path (exists on disk), it is read as a file.
        Otherwise, it is treated as a YAML string.

        Calls validate() and raises SpecValidationError if result.valid is False.

        Raises:
            SpecValidationError: if validation fails
            FileNotFoundError: if path provided but file does not exist
        """

    def validate(self) -> ValidationResult:
        """
        Validate spec fields and return a ValidationResult.
        Called by from_yaml(), which raises SpecValidationError if result.valid is False.
        Can also be called directly to inspect errors without catching exceptions.
        """
```

### 2.3 Implementation Details

- Use `pyyaml` to parse YAML. Expect top-level key `metric:` in the dict.
- `from_yaml()` calls `validate()`, which internally calls `validate_metric_spec()` from `utils/validation.py` and returns the `ValidationResult`.
- `from_yaml()` checks `result.valid`; if `False`, raises `SpecValidationError('metric', name_or_none, result.errors)`.
- `source` URI format: `scheme://rest-of-uri`. Validate that `://` is present and `scheme` is non-empty.
- `aggregation` must be one of the allowed values (case-insensitive, normalize to lowercase).
- `denominator` is required when `aggregation == 'ratio'`; forbidden for other aggregation types (log a warning, don't error, for forward compatibility).
- `numerator` and `denominator` are SQL syntax-validated by wrapping in `SELECT {expr}` and parsing with `sqlglot`. A parse failure adds a `ValidationError` for the respective field.

---

## 3. `SliceSpec` (`specs/slice.py`)

### 3.1 YAML Schema

```yaml
slice:
  name: geography
  description: Geographic breakdown by major regions
  values:
    - name: North America
      where: "country_code IN ('US', 'CA', 'MX')"
    - name: Europe
      where: "country_code IN ('DE', 'FR', 'UK', 'ES', 'IT')"
    - name: Rest of World
      where: "country_code NOT IN ('US', 'CA', 'MX', 'DE', 'FR', 'UK', 'ES', 'IT')"
```

```yaml
# Composite slice — cross-product of two referenced leaf slices
slice:
  name: industry_geo
  description: Cross-product of industry and geo breakdowns
  cross_product: [industry, geo]
```

> **Note**: Exactly one of `values` or `cross_product` must be present. A composite slice references other leaf SliceSpecs by name. Referenced specs must be available in the SpecCache at query-build time.

### 3.2 Class Definition

```python
@dataclass(frozen=True)
class SliceValue:
    name: str     # Display name, e.g. "North America"
    where: str    # SQL WHERE condition, e.g. "country_code IN ('US', 'CA')"

@dataclass(frozen=True)
class SliceSpec:
    name: str
    values: tuple[SliceValue, ...] = ()       # Leaf spec — direct WHERE-based values
    cross_product: tuple[str, ...] = ()       # Composite spec — names of other SliceSpecs
    description: str = ""

    @property
    def is_composite(self) -> bool:
        """True if this spec references other SliceSpecs via cross_product."""
        return bool(self.cross_product)

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> 'SliceSpec':
        """Load and validate a SliceSpec. Expects top-level key 'slice:'.
        Accepts either values (leaf) or cross_product (composite), not both.
        """

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult."""
```

### 3.3 Implementation Details

- Parse YAML, extract `slice:` top-level key.
- Exactly one of `values` or `cross_product` must be present; having both or neither is a `SpecValidationError`.
- `values` list must be non-empty when `cross_product` is absent; each item must have `name` and `where`.
- `cross_product` must be a list of at least 2 unique, non-empty strings (spec names). A single-name cross_product is rejected: just use a leaf spec.
- When `cross_product` is present, `values` is stored as an empty tuple. `from_yaml()` skips `SliceValue` construction.
- Reference resolution (looking up the named specs) is NOT performed in `from_yaml()`. Resolution happens at query-build time via `SpecCache.get_global()`.
- Convert `values` list to `tuple[SliceValue, ...]` for frozen dataclass compatibility.
- `SliceValue.where` is SQL syntax-validated by wrapping in `SELECT 1 WHERE {expr}` and parsing with `sqlglot`. A parse failure adds a `ValidationError` for `values[N].where`.
- Duplicate `name` values in `values` list: raise `SpecValidationError` with clear message.

---

## 4. `SegmentSpec` (`specs/segment.py`)

### 4.1 YAML Schema

A segment groups rows from a **source table** into named cohorts, each defined by a SQL `WHERE` condition — analogous to how a `SliceSpec` groups event rows, but tied to a specific backing table (like a customers or users dimension).

```yaml
segment:
  name: customer_value_tier
  description: Customer segmentation by lifetime value and status
  source: duckdb://analytics.db/customers
  values:
    - name: high_value
      where: "lifetime_value > 1000 AND customer_status = 'active'"
    - name: medium_value
      where: "lifetime_value BETWEEN 100 AND 1000 AND customer_status = 'active'"
    - name: low_value
      where: "lifetime_value < 100 OR customer_status != 'active'"
```

### 4.2 Class Definition

```python
@dataclass(frozen=True)
class SegmentValue:
    name: str    # Display name, e.g. "high_value"
    where: str   # SQL WHERE condition, e.g. "lifetime_value > 1000"

@dataclass(frozen=True)
class SegmentSpec:
    name: str
    source: str                        # URI: scheme://path/table
    values: tuple[SegmentValue, ...]   # Tuple for immutability (frozen dataclass)
    description: str = ""

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> 'SegmentSpec':
        """Load and validate a SegmentSpec. Expects top-level key 'segment:'."""

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult."""
```

### 4.3 Implementation Details

- Parse YAML, extract `segment:` top-level key.
- `source` URI format: `scheme://rest-of-uri`. Validate that `://` is present and `scheme` is non-empty (same rule as `MetricSpec.source`).
- `values` list must be non-empty; each item must have `name` and `where`.
- Convert `values` list to `tuple[SegmentValue, ...]` for frozen dataclass compatibility.
- `SegmentValue.where` is SQL syntax-validated by wrapping in `SELECT 1 WHERE {expr}` and parsing with `sqlglot`. A parse failure adds a `ValidationError` for `values[N].where`.
- Duplicate `name` values in `values` list: raise `SpecValidationError` with clear message.
- `validate_segment_spec()` in `utils/validation.py` mirrors the structure of `validate_slice_spec()`, with the addition of the `source` URI check.

---

## 5. `loader.py` — Spec Loading and Caching

### 5.1 Module-level Functions

```python
from pathlib import Path

SpecType = type[MetricSpec] | type[SliceSpec] | type[SegmentSpec]

def load_spec_from_file(path: str | Path, spec_type: SpecType) -> MetricSpec | SliceSpec | SegmentSpec:
    """
    Load a single spec from a YAML file.
    Delegates to spec_type.from_yaml(path).
    Raises FileNotFoundError if file does not exist.
    Raises SpecValidationError if spec is invalid.
    """

def load_spec_from_string(yaml_string: str, spec_type: SpecType) -> MetricSpec | SliceSpec | SegmentSpec:
    """
    Load a single spec from a YAML string.
    Delegates to spec_type.from_yaml(yaml_string).
    Raises SpecValidationError if spec is invalid.
    """

def load_specs_from_directory(
    directory: str | Path,
    spec_type: SpecType
) -> dict[str, MetricSpec | SliceSpec | SegmentSpec]:
    """
    Load all YAML files (*.yaml, *.yml) from a directory.
    Returns a dict mapping spec name → spec object.
    Skips files with parse/validation errors, logging warnings.
    Raises ValueError if directory does not exist.
    """
```

### 5.2 `SpecCache`

```python
class SpecCache:
    """
    Lazy, session-scoped cache for specs loaded from configured directories.

    Specs are loaded on first access (by name) and cached for the duration
    of the session. Supports metrics, slices, and segments.
    """

    def __init__(
        self,
        metric_paths: list[str | Path] | None = None,
        slice_paths: list[str | Path] | None = None,
        segment_paths: list[str | Path] | None = None,
    ):
        """
        Initialize with paths to search.
        Paths may be individual YAML files or directories.
        Loading is deferred until first access.
        """

    def get_metric(self, name: str) -> MetricSpec:
        """
        Return MetricSpec for the given name.
        Loads and caches from configured metric_paths on first call.
        Raises SpecNotFoundError if name not found in any path.
        """

    def get_slice(self, name: str) -> SliceSpec:
        """Return SliceSpec by name. Same lazy-load semantics."""

    def get_segment(self, name: str) -> SegmentSpec:
        """Return SegmentSpec by name. Same lazy-load semantics."""

    def clear(self) -> None:
        """Clear all cached specs (useful for testing)."""

    # --- Singleton / global instance ---

    _global_instance: ClassVar['SpecCache | None'] = None

    @classmethod
    def set_global(cls, cache: 'SpecCache') -> None:
        """Set global singleton instance (mirrors ConnectionManager.set_global)."""

    @classmethod
    def get_global(cls) -> 'SpecCache':
        """Get global singleton instance.

        Raises:
            RuntimeError: If set_global() has not been called.
        """

    # --- Manual spec registration ---

    def add_spec(self, new_spec: MetricSpec | SliceSpec | SegmentSpec) -> None:
        """Add a spec to the cache if its name is not already present.

        Triggers lazy-load of the relevant spec type if not yet loaded,
        so manually added specs coexist with path-loaded specs.
        A spec whose name already exists in the cache is silently ignored
        (first-write-wins, consistent with explicit registration intent).
        """
```

### 5.3 Implementation Details

**Loading logic** in `get_metric / get_slice / get_segment`:
1. If internal cache dict already has the name, return it.
2. If the internal cache dict has not been populated yet for this spec type, trigger a full directory scan:
   - Iterate over configured paths
   - For each path: if it's a `.yaml`/`.yml` file, load it; if it's a directory, scan it with `load_specs_from_directory()`
   - Populate the internal `dict[str, Spec]`
3. After population, look up `name`; if missing raise `SpecNotFoundError(spec_type_str, name, searched_paths)`

**Cache invalidation**: None in Phase 1. Cache lives for the lifetime of the `SpecCache` instance.

**Thread safety**: Not required in Phase 1. Note in docstring.

**`add_spec()` implementation:**
1. Determine the spec type (MetricSpec, SliceSpec, SegmentSpec) via `isinstance`.
2. Trigger lazy-load for that spec type if its internal cache dict is still `None` (call `_load_all()` to populate it first, so path-loaded specs are present).
3. Call `setdefault(new_spec.name, new_spec)` — if the name already exists, the existing spec is kept (silent no-op).

**Cross-reference validation (composite SliceSpecs):**
After all slices are loaded in `_load_all()` for the SliceSpec type, call `_validate_slice_cross_references()`:
- For each composite SliceSpec in `_slices`, verify every name in `cross_product` maps to a key in `_slices`.
- If any name is missing, raise `SpecValidationError('slice', composite_spec_name, [ValidationError(field='cross_product', message="Referenced slice '{name}' not found")])`.
- Composite specs referencing other composite specs (nesting) are NOT supported in Phase 1; raise a `SpecValidationError` if a referenced spec is itself composite.

**`set_global()` / `get_global()` implementation:**
Mirror `ConnectionManager` exactly:
- `set_global(cache)`: `cls._global_instance = cache`
- `get_global()`: if `_global_instance is None`, raise `RuntimeError("No global SpecCache set. Call SpecCache.set_global() first.")`. Otherwise return `cls._global_instance`.

---

## 6. `specs/__init__.py`

```python
from aitaem.specs.metric import MetricSpec
from aitaem.specs.slice import SliceSpec, SliceValue
from aitaem.specs.segment import SegmentSpec, SegmentValue
from aitaem.specs.loader import SpecCache, load_spec_from_file, load_spec_from_string, load_specs_from_directory

__all__ = [
    "MetricSpec",
    "SliceSpec",
    "SliceValue",
    "SegmentSpec",
    "SegmentValue",
    "SpecCache",
    "load_spec_from_file",
    "load_spec_from_string",
    "load_specs_from_directory",
]
```

---

## 7. Edge Cases and Error Handling

### 7.1 YAML Parsing Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| Valid YAML, top-level key missing (e.g., `name: foo` instead of `metric: {name: foo}`) | FAIL | `SpecValidationError` with message: "Expected top-level key 'metric', got: ['name']" |
| File path does not exist | FAIL | `FileNotFoundError` (not wrapped) |
| File contains invalid YAML syntax | FAIL | `SpecValidationError` wrapping `yaml.YAMLError`, with file path in message |
| YAML string is empty or whitespace only | FAIL | `SpecValidationError` with message: "YAML content is empty" |
| YAML with extra unknown fields | SUCCESS (Phase 1) | Unknown fields ignored silently; logged at DEBUG level |
| YAML with `null` / `~` for required field | FAIL | `SpecValidationError` listing the null field |

### 7.2 SQL Syntax Validation Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| Valid aggregation expression: `"SUM(amount)"` | SUCCESS | Parses cleanly via `SELECT SUM(amount)` |
| Valid CASE expression: `"SUM(CASE WHEN x = 1 THEN 1 ELSE 0 END)"` | SUCCESS | Parses cleanly |
| Valid predicate: `"country_code IN ('US', 'CA')"` | SUCCESS | Parses cleanly via `SELECT 1 WHERE ...` |
| Malformed expression: `"SUM(amount"` (unclosed paren) | FAIL | `ValidationError` on the field with sqlglot parse error message |
| Malformed predicate: `"country_code IN ('US'"` | FAIL | `ValidationError` on `where` field |
| DuckDB-specific syntax not in sqlglot's model | WARN (Phase 1) | Logged at DEBUG; spec accepted. False negatives are acceptable. |
| Empty string after non-null check passes | FAIL | Caught by structural validation before SQL parsing is attempted |

### 7.3 MetricSpec Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| `aggregation: ratio` with no `denominator` | FAIL | `SpecValidationError`: "'denominator' is required when aggregation is 'ratio'" |
| `aggregation: sum` with `denominator` present | SUCCESS with WARNING | Spec created; warning logged: "'denominator' is ignored for 'sum' aggregation" |
| `source` without URI scheme (e.g., `events` instead of `duckdb://...`) | FAIL | `SpecValidationError`: "Invalid source URI: must include scheme (e.g., 'duckdb://...')" |
| `aggregation: RATIO` (uppercase) | SUCCESS | Normalized to `ratio` |
| `aggregation: window_function` (unsupported) | FAIL | `SpecValidationError`: "Unsupported aggregation type. Must be one of: sum, avg, count, ratio, min, max" |
| `numerator` is empty string | FAIL | `SpecValidationError`: "'numerator' must be a non-empty SQL expression" |

### 7.4 SliceSpec Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| `values` is empty list | FAIL | `SpecValidationError`: "'values' must contain at least one slice value" |
| A slice value missing `name` | FAIL | `SpecValidationError`: "Slice value at index N is missing required field 'name'" |
| A slice value missing `where` | FAIL | `SpecValidationError`: "Slice value 'X' is missing required field 'where'" |
| Duplicate `name` values in `values` list | FAIL | `SpecValidationError`: "Duplicate slice value name: 'X'" |
| Both `values` and `cross_product` present | FAIL | `SpecValidationError`: "SliceSpec must have exactly one of 'values' or 'cross_product', not both" |
| Neither `values` nor `cross_product` present | FAIL | `SpecValidationError`: "SliceSpec must have exactly one of 'values' or 'cross_product'" |
| `cross_product` with only 1 name | FAIL | `SpecValidationError`: "'cross_product' must reference at least 2 slice specs" |
| `cross_product` with duplicate names | FAIL | `SpecValidationError`: "Duplicate name in 'cross_product'" |
| `cross_product` references unknown spec name (at SpecCache load time) | FAIL | `SpecValidationError`: "Referenced slice 'X' not found" |
| `cross_product` references another composite spec | FAIL | `SpecValidationError`: "Nested composite slices not supported (Phase 1)" |
| Valid composite with 2 names | SUCCESS | `SliceSpec(name=..., cross_product=('industry', 'geo'), values=())` |

### 7.5 SegmentSpec Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| `values` is empty list | FAIL | `SpecValidationError`: "'values' must contain at least one segment value" |
| A segment value missing `name` | FAIL | `SpecValidationError`: "Segment value at index N is missing required field 'name'" |
| A segment value missing `where` | FAIL | `SpecValidationError`: "Segment value 'X' is missing required field 'where'" |
| Duplicate `name` values in `values` list | FAIL | `SpecValidationError`: "Duplicate segment value name: 'X'" |
| `source` without URI scheme (e.g., `customers` instead of `duckdb://...`) | FAIL | `SpecValidationError`: "Invalid source URI: must include scheme (e.g., 'duckdb://...')" |
| Malformed `where` SQL in any segment value | FAIL | `SpecValidationError` with field `values[N].where` |

### 7.6 `SpecCache` / Loader Edge Cases

| Input | Handling | Result |
|-------|----------|--------|
| `load_specs_from_directory()` on empty directory | SUCCESS | Returns empty dict; no error |
| `load_specs_from_directory()` with non-existent path | FAIL | `ValueError`: "Directory does not exist: /path/to/dir" |
| `load_specs_from_directory()` with a file path (not dir) | FAIL | `ValueError`: "Expected a directory, got a file: /path/to/file.yaml" |
| `SpecCache.get_metric('unknown')` | FAIL | `SpecNotFoundError('metric', 'unknown', [...searched_paths])` |
| One file in directory has invalid YAML | PARTIAL SUCCESS | File skipped with warning; valid files loaded normally |
| `SpecCache` with no paths configured, `.get_metric()` called | FAIL | `SpecNotFoundError` with `searched_paths=[]` and message "No metric paths configured" |
| Two files in same directory define the same spec name | LAST-WINS | Second file's spec overrides first; WARNING logged |

---

## 8. Test Strategy

### 8.1 Test Structure

```
tests/test_specs/
├── __init__.py
├── conftest.py                     # Shared fixtures (tmp_dir, sample YAML strings)
├── test_metric_spec.py             # MetricSpec unit tests
├── test_slice_spec.py              # SliceSpec unit tests
├── test_segment_spec.py            # SegmentSpec unit tests
├── test_spec_loader.py             # load_spec_from_* + SpecCache tests
└── fixtures/
    ├── valid_metric_ratio.yaml
    ├── valid_metric_sum.yaml
    ├── valid_slice.yaml
    ├── valid_segment.yaml
    ├── invalid_metric_no_denominator.yaml
    ├── invalid_segment_no_source.yaml
    └── invalid_yaml_syntax.yaml
```

### 8.2 Key Test Cases

**MetricSpec (`test_metric_spec.py`)**:
- ✓ `validate()` on valid spec → returns `ValidationResult(valid=True, errors=[])`
- ✓ `validate()` on invalid spec → returns `ValidationResult(valid=False, errors=[...])` without raising
- ✓ Valid `numerator` SQL expression → no validation error
- ✗ Malformed `numerator` SQL (e.g., unclosed paren) → `SpecValidationError` with field `numerator`
- ✗ Malformed `denominator` SQL → `SpecValidationError` with field `denominator`
- ✓ Load valid ratio metric from YAML string → all fields populated correctly
- ✓ Load valid ratio metric from YAML file path → same
- ✓ Load valid sum metric without denominator → `denominator is None`
- ✓ `aggregation` normalized to lowercase (RATIO → ratio)
- ✗ Missing `name` field → `SpecValidationError`
- ✗ Missing `source` field → `SpecValidationError`
- ✗ Invalid source URI format (no scheme) → `SpecValidationError`
- ✗ `aggregation: ratio` with no denominator → `SpecValidationError`
- ✗ Unsupported aggregation type → `SpecValidationError`
- ✗ YAML string with missing top-level `metric:` key → `SpecValidationError`
- ✗ Non-existent file path → `FileNotFoundError`
- ✗ Invalid YAML syntax → `SpecValidationError`

**SliceSpec (`test_slice_spec.py`)**:
- ✓ Load valid slice with 3 values → `values` is tuple of `SliceValue`
- ✓ Each `SliceValue` has correct `name` and `where`
- ✓ Valid `where` SQL predicate → no validation error
- ✗ Malformed `where` SQL in any slice value → `SpecValidationError` with field `values[N].where`
- ✗ Empty `values` list → `SpecValidationError`
- ✗ A value missing `name` → `SpecValidationError`
- ✗ A value missing `where` → `SpecValidationError`
- ✗ Duplicate slice value names → `SpecValidationError`
- ✗ Missing top-level `slice:` key → `SpecValidationError`
- ✓ Load composite slice with `cross_product: [industry, geo]` → `is_composite == True`, `cross_product == ('industry', 'geo')`, `values == ()`
- ✓ Composite spec YAML string parses correctly without error
- ✗ Both `values` and `cross_product` present → `SpecValidationError`
- ✗ Neither `values` nor `cross_product` present → `SpecValidationError`
- ✗ `cross_product` with only 1 element → `SpecValidationError`
- ✗ `cross_product` with duplicate names → `SpecValidationError`

**SegmentSpec (`test_segment_spec.py`)**:
- ✓ Load valid segment with 3 values → `values` is tuple of `SegmentValue`
- ✓ Each `SegmentValue` has correct `name` and `where`
- ✓ `description` optional; defaults to empty string
- ✓ `source` URI stored correctly
- ✓ Valid `where` SQL predicate in each value → no validation error
- ✗ Missing `source` field → `SpecValidationError`
- ✗ Invalid source URI format (no scheme) → `SpecValidationError`
- ✗ Empty `values` list → `SpecValidationError`
- ✗ A value missing `name` → `SpecValidationError`
- ✗ A value missing `where` → `SpecValidationError`
- ✗ Malformed `where` SQL in any value → `SpecValidationError` with field `values[N].where`
- ✗ Duplicate segment value names → `SpecValidationError`
- ✗ Missing top-level `segment:` key → `SpecValidationError`

**Loader + SpecCache (`test_spec_loader.py`)**:
- ✓ `load_spec_from_file()` with valid file → correct spec returned
- ✓ `load_spec_from_string()` with valid string → correct spec returned
- ✓ `load_specs_from_directory()` with directory of 3 yaml files → dict with 3 entries
- ✓ `load_specs_from_directory()` with mixed valid/invalid → valid loaded, invalid skipped with warning
- ✓ `load_specs_from_directory()` on empty dir → empty dict
- ✗ `load_specs_from_directory()` on non-existent dir → `ValueError`
- ✓ `SpecCache.get_metric()` returns correct spec on first call (lazy load)
- ✓ `SpecCache.get_metric()` returns same object on second call (cached)
- ✗ `SpecCache.get_metric('nonexistent')` → `SpecNotFoundError` with searched paths
- ✓ `SpecCache` accepts single file path as `metric_paths`
- ✓ `SpecCache` accepts directory as `metric_paths`
- ✓ `SpecCache.clear()` causes re-load on next access
- ✓ `SpecCache.set_global(cache)` → `SpecCache.get_global()` returns same instance
- ✗ `SpecCache.get_global()` without `set_global()` → `RuntimeError`
- ✓ `add_spec(SliceSpec.from_yaml(...))` adds spec to cache; second call with same name is silent no-op
- ✓ `add_spec` triggers lazy-load before inserting (path-loaded specs coexist)
- ✗ SpecCache with composite slice referencing unknown name → `SpecValidationError` at first `get_slice()` call
- ✗ SpecCache with composite referencing another composite → `SpecValidationError`

### 8.3 Test Fixtures (`conftest.py`)

```python
VALID_METRIC_RATIO_YAML = """
metric:
  name: homepage_ctr
  description: Click-through rate
  source: duckdb://analytics.db/events
  aggregation: ratio
  numerator: "SUM(CASE WHEN event_type = 'click' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' THEN 1 ELSE 0 END)"
"""

VALID_METRIC_SUM_YAML = """
metric:
  name: total_revenue
  source: duckdb://analytics.db/transactions
  aggregation: sum
  numerator: "SUM(amount)"
"""

VALID_SLICE_YAML = """
slice:
  name: geography
  description: Regional breakdown
  values:
    - name: North America
      where: "country_code IN ('US', 'CA')"
    - name: Europe
      where: "country_code IN ('DE', 'FR')"
"""

VALID_SEGMENT_YAML = """
segment:
  name: customer_value_tier
  description: Customer segmentation by value
  source: duckdb://analytics.db/customers
  values:
    - name: high_value
      where: "lifetime_value > 1000 AND customer_status = 'active'"
    - name: low_value
      where: "lifetime_value <= 1000 OR customer_status != 'active'"
"""
```

### 8.4 Coverage Goals

- Line coverage ≥ 90% for all files in `aitaem/specs/` and `aitaem/utils/validation.py`
- All `SpecValidationError` raise paths covered
- All `SpecNotFoundError` raise paths covered

---

## 9. Example YAML Specs

### `examples/metrics/homepage_ctr.yaml`
```yaml
metric:
  name: homepage_ctr
  description: Click-through rate for homepage impressions
  source: duckdb://analytics.db/events
  aggregation: ratio
  numerator: "SUM(CASE WHEN event_type = 'click' AND page = 'home_page' THEN 1 ELSE 0 END)"
  denominator: "SUM(CASE WHEN event_type = 'impression' AND page = 'home_page' THEN 1 ELSE 0 END)"
```

### `examples/metrics/total_revenue.yaml`
```yaml
metric:
  name: total_revenue
  description: Sum of all transaction amounts
  source: duckdb://analytics.db/transactions
  aggregation: sum
  numerator: "SUM(amount)"
```

### `examples/slices/geography.yaml`
```yaml
slice:
  name: geography
  description: Geographic breakdown by major regions
  values:
    - name: North America
      where: "country_code IN ('US', 'CA', 'MX')"
    - name: Europe
      where: "country_code IN ('DE', 'FR', 'UK', 'ES', 'IT')"
    - name: Asia Pacific
      where: "country_code IN ('CN', 'JP', 'IN', 'AU', 'SG')"
    - name: Rest of World
      where: "country_code NOT IN ('US', 'CA', 'MX', 'DE', 'FR', 'UK', 'ES', 'IT', 'CN', 'JP', 'IN', 'AU', 'SG')"
```

### `examples/segments/customer_value_tier.yaml`
```yaml
segment:
  name: customer_value_tier
  description: Customer segmentation by lifetime value and status
  source: duckdb://analytics.db/customers
  values:
    - name: high_value
      where: "lifetime_value > 1000 AND customer_status = 'active'"
    - name: medium_value
      where: "lifetime_value BETWEEN 100 AND 1000 AND customer_status = 'active'"
    - name: low_value
      where: "lifetime_value < 100 OR customer_status != 'active'"
```

---

## 10. Implementation Sequence

Implement in this order (each step is testable before moving to the next):

### Phase 1: Validation Utilities
1. `aitaem/utils/exceptions.py` — Add `SpecValidationError`, `SpecNotFoundError`
2. `aitaem/utils/validation.py` — Add `ValidationError`, `ValidationResult`, and three `validate_*_spec()` functions

### Phase 2: Spec Classes (each independently testable)
3. `aitaem/specs/metric.py` — `MetricSpec` with `from_yaml()` and `validate()`
4. `aitaem/specs/slice.py` — `SliceValue` + `SliceSpec` with `from_yaml()` and `validate()`
5. `aitaem/specs/segment.py` — `SegmentSpec` with `from_yaml()` and `validate()`

### Phase 3: Loader and Cache
6. `aitaem/specs/loader.py` — `load_spec_from_file`, `load_spec_from_string`, `load_specs_from_directory`, `SpecCache`

### Phase 4: Package Init and Examples
7. `aitaem/specs/__init__.py` — Wire up all exports
8. `examples/` — Create example YAML files (metrics, slices, segments)

### Phase 5: Tests
9. `tests/test_specs/` — Write and run all test cases per Section 8

---

## 11. Verification Checklist

### Spec Classes
- [ ] `MetricSpec.from_yaml()` accepts both file path and YAML string
- [ ] `MetricSpec` fields match architecture: `name`, `source`, `aggregation`, `numerator`, `description`, `denominator`
- [ ] `MetricSpec` validates ratio requires denominator
- [ ] `MetricSpec` normalizes `aggregation` to lowercase
- [ ] `SliceSpec.values` is a tuple (immutable)
- [ ] `SliceSpec` rejects duplicate value names
- [ ] `SegmentSpec.source` validated as URI (same rule as `MetricSpec.source`)
- [ ] `SegmentSpec.values` is a tuple (immutable)
- [ ] `SegmentSpec` rejects duplicate value names
- [ ] `SegmentSpec.from_yaml()` raises on empty `values` list
- [ ] All three spec classes are `frozen=True` dataclasses

### Validation
- [ ] `validate()` returns `ValidationResult` on all three spec classes (does not raise)
- [ ] `from_yaml()` raises `SpecValidationError` when `validate()` returns `valid=False`
- [ ] `SpecValidationError` includes `spec_type`, `name`, and `errors` list
- [ ] Each `ValidationError` has `field`, `message`, and optional `suggestion`
- [ ] Unknown YAML fields are silently ignored
- [ ] Missing required fields produce field-specific error messages

### Loader and Cache
- [ ] `load_specs_from_directory()` scans both `.yaml` and `.yml` files
- [ ] Invalid files in directory are skipped with a WARNING (not an error)
- [ ] `SpecCache` loads lazily (no file I/O at `__init__` time)
- [ ] `SpecCache` returns cached instance on second call
- [ ] `SpecNotFoundError.searched_paths` contains all paths that were searched
- [ ] `SpecCache.clear()` forces re-scan on next access

### SliceSpec Composite Support
- [ ] `SliceSpec.is_composite` returns `True` when `cross_product` is non-empty
- [ ] Leaf SliceSpec: `values` non-empty, `cross_product` empty tuple
- [ ] Composite SliceSpec: `cross_product` non-empty, `values` empty tuple
- [ ] `from_yaml()` handles both leaf and composite correctly
- [ ] Mutual exclusivity of `values` and `cross_product` enforced

### SpecCache Singleton and Manual Registration
- [ ] `SpecCache.set_global()` / `get_global()` work correctly
- [ ] `SpecCache.add_spec()` coexists with path-loaded specs
- [ ] Cross-reference validation fires at first `get_slice()` call

### Imports
- [ ] `from aitaem.specs import MetricSpec, SliceSpec, SegmentSpec` works (depth-2 import)
- [ ] `from aitaem.specs import SpecCache` works

### Tests
- [ ] All test cases in Section 8.2 implemented
- [ ] Line coverage ≥ 90% for `aitaem/specs/` and `aitaem/utils/validation.py`
- [ ] All tests pass with `python -m pytest tests/test_specs/ -v`

---

## 12. Critical Files Summary

| Priority | File | Purpose | Est. LOC |
|----------|------|---------|----------|
| P0 | `aitaem/utils/exceptions.py` | `SpecValidationError`, `SpecNotFoundError` | 30 |
| P0 | `aitaem/utils/validation.py` | `ValidationError`, `ValidationResult`, validate functions | 80 |
| P1 | `aitaem/specs/metric.py` | `MetricSpec` dataclass + `from_yaml()` | 70 |
| P1 | `aitaem/specs/slice.py` | `SliceValue` + `SliceSpec` dataclass + `from_yaml()` | 70 |
| P1 | `aitaem/specs/segment.py` | `SegmentValue` + `SegmentSpec` dataclass + `from_yaml()` | 70 |
| P2 | `aitaem/specs/loader.py` | `SpecCache` + loading functions | 120 |
| P3 | `aitaem/specs/__init__.py` | Package exports | 15 |
| P4 | `tests/test_specs/conftest.py` | Shared fixtures | 50 |
| P4 | `tests/test_specs/test_metric_spec.py` | MetricSpec tests | 120 |
| P4 | `tests/test_specs/test_slice_spec.py` | SliceSpec tests | 100 |
| P4 | `tests/test_specs/test_segment_spec.py` | SegmentSpec tests | 80 |
| P4 | `tests/test_specs/test_spec_loader.py` | Loader + SpecCache tests | 150 |

---

## 13. Dependencies

No new entries required in `pyproject.toml`.

```toml
# Already present — confirm only
"pyyaml>=6.0"
```

`sqlglot` (used for SQL syntax validation) is a transitive dependency of `ibis-framework` and will already be present in the environment. It does not need to be declared explicitly. No Ibis, DuckDB, or pandas imports anywhere in `aitaem/specs/`. Conversion to Ibis expressions is the responsibility of `query/builder.py`.

---

## 14. Error Message Examples

### SpecValidationError

```
SpecValidationError: Invalid metric spec 'homepage_ctr':
  - Field 'denominator': required when aggregation is 'ratio' (suggestion: add denominator field)
```

```
SpecValidationError: Invalid metric spec (unknown name):
  - Field 'aggregation': unsupported value 'window_function'. Must be one of: sum, avg, count, ratio, min, max
  - Field 'source': invalid URI format. Expected 'scheme://...' (e.g., 'duckdb://analytics.db/events')
```

```
SpecValidationError: Invalid slice spec 'geography':
  - Field 'values[1].name': missing required field 'name'
  - Field 'values': duplicate slice value name 'North America'
```

### SpecNotFoundError

```
SpecNotFoundError: Metric 'revenue' not found.
Searched paths:
  - /workspace/metrics/
  - /workspace/config/metrics/
```

---

## 15. Testing Commands Reference

```bash
# Run all specs tests
python -m pytest tests/test_specs/ -v

# Run with coverage
python -m pytest tests/test_specs/ --cov=aitaem/specs --cov=aitaem/utils/validation --cov-report=term-missing

# Run a single test file
python -m pytest tests/test_specs/test_metric_spec.py -v

# Run linting
ruff check aitaem/specs/ aitaem/utils/
ruff format aitaem/specs/ aitaem/utils/
```

---

## 16. Future Enhancements (Phase 2+)

- HAVING clause support in `SegmentSpec` and `SliceSpec` for aggregate-level filtering
- Subquery support in `SegmentValue.where` and `SliceValue.where`
- Multi-table metric support (joins) in `MetricSpec`
- Database-backed spec storage (`SpecCache` reading from a DB rather than files)
- Spec versioning and schema evolution
- Auto-discovery of spec directories (convention over configuration)
- Nested composite SliceSpecs (composites referencing other composites)
- Inline sub-spec values in composite YAML (alternative to name references)

---

## Notes

1. Spec classes have no Ibis-related methods. Unlike the original architecture doc, `to_ibis_expression()`, `to_ibis_filters()`, and `to_ibis_filter()` are **not** defined on spec classes. All conversion from specs to Ibis expressions is the responsibility of `query/builder.py`, which accepts spec objects as pure data inputs.
2. The specs module has **no database imports**. `import ibis` must not appear anywhere in `aitaem/specs/`.
3. The existing `aitaem/utils/exceptions.py` may already have some exceptions from the connectors module. Extend it, do not replace it.
4. Use `pathlib.Path` consistently (not raw `str`) internally; accept both `str` and `Path` in public APIs.
5. YAML files in `examples/` directories should be created as part of this plan but do not block testing.

---

**Plan Status**: Ready for Implementation
**Target module**: `aitaem/specs/`
**Blocked by**: None (specs module has no upstream aitaem dependencies beyond utils/exceptions.py)
