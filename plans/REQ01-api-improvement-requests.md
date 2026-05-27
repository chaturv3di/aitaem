# REQ01 - AITAEM API Improvement Requests

This document catalogs every gap, instability, and missing feature in the AITAEM v0.1.5 public API that currently forces consumers to duplicate knowledge, use internal module paths, or work around bugs.

---

## Context

Recent incidents surfaced the systemic nature of these gaps:

1. **Unquoted SQL alias bug** AITAEM generates `_slice_{name}` as a bare, unquoted SQL identifier. If the YAML `name` field contained spaces (accidentally or intentionally), DuckDB rejected the query. AITAEM silently accepted the bad spec at load time and only failed at SQL execution.

2. **`period_type` duplication**: Consumers need to hard-code `_VALID_PERIOD_TYPES = Literal["all_time", "daily", "weekly", "monthly", "yearly"]` because does not expose allowed values.

---

## Current AITAEM Public API (v0.1.5)

### Top-level `__all__`
```python
["SpecCache", "ConnectionManager", "MetricCompute"]
```

### What a consumer typically imports

| Symbol | Import path used | Internal or public? |
|--------|-----------------|---------------------|
| `SpecCache` | `aitaem` | Public |
| `ConnectionManager` | `aitaem` | Public |
| `MetricCompute` | `aitaem` | Public |
| `SpecNotFoundError` | `aitaem.specs.loader` | **Internal** |
| `QueryBuildError` | `aitaem.query.builder` | **Internal** |
| `QueryExecutionError` | `aitaem.query.executor` | **Internal** |
| `validate_metric_spec` | `aitaem.utils.validation` | **Internal** |
| `validate_slice_spec` | `aitaem.utils.validation` | **Internal** |
| `validate_segment_spec` | `aitaem.utils.validation` | **Internal** |
| `IbisConnector` | `aitaem.connectors.ibis_connector` | **Internal** |
| `load_csvs_to_duckdb` | `aitaem.helpers` | Semi-public |

---

## Improvement Requests

### R-1 — Expose all exceptions at the top level

**Priority: High**

**Current state:** Exception classes (`SpecNotFoundError`, `QueryBuildError`,
`QueryExecutionError`, `SpecValidationError`, `ConnectionNotFoundError`, etc.)
live in `aitaem.utils.exceptions` but are not re-exported from the top-level
`aitaem` package. Consumers import them from internal submodule paths.

**Impact:** Any internal refactor that moves or renames `exceptions.py` silently
breaks consumers' error handling. The imports will fail at startup.

**Request:** Add all exception classes to `aitaem/__init__.py`'s `__all__`:

```python
from aitaem.utils.exceptions import (
    AitaemError,
    SpecNotFoundError,
    SpecValidationError,
    QueryBuildError,
    QueryExecutionError,
    ConnectionNotFoundError,
    ConnectionError,
    TableNotFoundError,
    InvalidURIError,
    UnsupportedBackendError,
    ConfigurationError,
)

__all__ = [
    "SpecCache", "ConnectionManager", "MetricCompute",
    # exceptions
    "AitaemError", "SpecNotFoundError", "SpecValidationError",
    "QueryBuildError", "QueryExecutionError", "ConnectionNotFoundError",
    ...
]
```

---

### R-2 — Make `VALID_PERIOD_TYPES` a public constant

**Priority: High**

**Current state:** Valid `period_type` values are defined as a private frozenset
in `aitaem/query/builder.py`:

```python
_VALID_PERIOD_TYPES = frozenset({"all_time", "daily", "weekly", "monthly", "yearly"})
```

Consumers duplicate these five strings verbatim in two files as a `Literal` type.
If AITAEM adds or renames a period type (e.g., adds `"quarterly"`), consumers will
silently accept the old set until a runtime failure surfaces the mismatch.

**Note:** The scripts directory already uses `"quarterly"` in `st_data_layer.py`,
which would currently raise a `QueryBuildError` at runtime. This suggests
`"quarterly"` may be a planned addition — consumers cannot know without a public
constant.

**Request:** Export a public constant from `aitaem`:

```python
# aitaem/query/builder.py
VALID_PERIOD_TYPES: frozenset[str] = frozenset(
    {"all_time", "daily", "weekly", "monthly", "yearly"}
)
```

And re-export from `aitaem/__init__.py`:

```python
from aitaem.query.builder import VALID_PERIOD_TYPES
__all__ = [..., "VALID_PERIOD_TYPES"]
```

Ideally also provide a `PeriodType` `Literal` or `StrEnum` so downstream can use
it in Pydantic models:

```python
PeriodType = Literal["all_time", "daily", "weekly", "monthly", "yearly"]
```

---

### R-3 — Validate spec `name` as a SQL identifier at load time

**Priority: High**

**Current state:** AITAEM generates column aliases as bare unquoted SQL identifiers:

```python
# aitaem/query/builder.py:225
alias = f"_slice_{ss.name}"
```

No validation occurs at `SpecCache.from_yaml()` or `SliceSpec.from_yaml()` to
confirm that `name` is a valid unquoted SQL identifier (i.e., no spaces, hyphens,
or special characters). A spec with `name: "English speaking countries"` loads
successfully but causes a `QueryExecutionError` at compute time when DuckDB
encounters `END AS _slice_English speaking countries`.

**Impact:** The error surfaces late (at query execution) rather than early
(at spec load), making debugging harder. The error message from DuckDB does not
mention spec names.

**Request:** Add identifier validation in `SliceSpec.validate()` (and similarly
for `MetricSpec` and `SegmentSpec`):

```python
import re
_VALID_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def validate(self) -> ValidationResult:
    errors = []
    if not _VALID_IDENTIFIER.match(self.name):
        errors.append(
            f"name '{self.name}' is not a valid SQL identifier "
            "(must match [A-Za-z_][A-Za-z0-9_]*)"
        )
    ...
```

Alternatively, AITAEM could quote identifiers in generated SQL (e.g.,
`"_slice_{ss.name}"` → `f'"_slice_{ss.name}"'`) — but that changes SQL semantics
and is a larger change.

---

### R-4 — Add `SpecCache` introspection methods

**Priority: High**

**Current state:** `SpecCache` has `get_metric(name)`, `get_slice(name)`,
`get_segment(name)` — but no way to enumerate the names of loaded specs. To build
the spec catalog (names and descriptions for the agent's system prompt), consumers
must re-parse the YAML content of every spec from Postgres, e.g. using their own
`_extract_yaml_name` helpers.

**Impact:** Consumers maintain a parallel YAML-parsing path that duplicates
AITAEM's own parsing logic. If AITAEM changes its YAML schema, both must be
updated.

**Request:** Add read-only properties or methods over SpecCache:

```python
@property
def metric_names(self) -> list[str]: ...

@property
def slice_names(self) -> list[str]: ...

@property
def segment_names(self) -> list[str]: ...
```

Or expose the underlying dicts:

```python
@property
def metrics(self) -> dict[str, MetricSpec]: ...

@property
def slices(self) -> dict[str, SliceSpec]: ...

@property
def segments(self) -> dict[str, SegmentSpec]: ...
```

The latter is more useful because downstream can then access `MetricSpec.entities`
directly (see R-6).


---

### R-5 — Expose `STANDARD_COLUMNS` at the top level

**Priority: Medium**

**Current state:** `STANDARD_COLUMNS` is defined in `aitaem/utils/formatting.py` but not exported from the top-level package. Consumers currently has no stable way to discover the column names that `MetricCompute.compute()` always returns (e.g., `period_start_date`, `period_type`, `metric_value`, etc.).

**Impact:** Any consumers writing agentic system prompts tell the agent to use `period_start_date` and ignore `period_end_date`. This guidance was derived by manually inspecting AITAEM's output — it is not machine-readable. If AITAEM renames a column, consumer prompts silently gives the agents wrong guidance.

**Request:**

```python
# aitaem/utils/formatting.py
STANDARD_COLUMNS: list[str] = [
    "period_type", "period_start_date", "period_end_date",
    "entity_id", "metric_name", "slice_type", "slice_value",
    "segment_name", "segment_value", "metric_value",
]
```

Re-export from `aitaem/__init__.py`.

---

### R-6 — Add `period_type` to `MetricCompute.compute()` type signature

**Priority: Medium**

**Current state:**

```python
def compute(
    self,
    ...
    period_type: str = "all_time",   # bare str, no Literal
    ...
) -> pd.DataFrame
```

A caller passing `period_type="quarterly"` (or a typo) receives a `QueryBuildError`
at runtime. No IDE warning, no Pydantic pre-validation.

**Request:** Change the type annotation to use `PeriodType` (from R-2):

```python
def compute(
    self,
    ...
    period_type: PeriodType = "all_time",
    ...
) -> pd.DataFrame
```

---

### R-7 — Export spec types and validation functions from top level

**Priority: Medium**

**Current state:** `validate_metric_spec`, `validate_slice_spec`,
`validate_segment_spec` are imported from `aitaem.utils.validation` — an
internal path. The spec dataclasses (`MetricSpec`, `SliceSpec`, `SegmentSpec`)
are importable from `aitaem.specs` but not from the top-level package.

**Request:** Re-export from `aitaem/__init__.py`:

```python
from aitaem.specs import MetricSpec, SliceSpec, SegmentSpec
from aitaem.utils.validation import (
    validate_metric_spec, validate_slice_spec, validate_segment_spec
)
```

---

### R-8 — Fix `SpecCache.from_string()` OSError on long YAML (macOS)

**Priority: Medium** *(previously reported 2026-04-21)*

**Current state:** `from_string()` calls `SliceSpec.from_yaml(yaml_string)`
internally. `from_yaml` calls `Path(yaml_string).exists()` to detect whether
the argument is a file path or raw YAML content. On macOS (HFS+, 255-character
filename limit), YAML strings longer than 255 characters raise
`OSError: [Errno 63] File name too long` before YAML parsing begins.

**Workaround in a consumer:** `build_spec_cache` writes each YAML string to a
temporary file and calls `from_yaml()` with explicit paths.

**Request:** In `from_yaml()`, check `len(yaml_input) <= 255` (or use a
heuristic like `"\n" in yaml_input`) before calling `Path(yaml_input).exists()`.
Or separate the path-detection check from the length-unsafe OS call:

```python
def from_yaml(cls, yaml_input: str | Path) -> "MetricSpec":
    path = Path(yaml_input) if isinstance(yaml_input, Path) else None
    if path is None and len(yaml_input) <= 255:
        candidate = Path(yaml_input)
        try:
            path = candidate if candidate.exists() else None
        except OSError:
            path = None
    if path and path.exists():
        return cls(**yaml.safe_load(path.read_text())["metric"])
    return cls(**yaml.safe_load(yaml_input)["metric"])
```

---

### R-9 — Export `IbisConnector` from top level

**Priority: Low**

**Current state:** `IbisConnector` is imported from
`aitaem.connectors.ibis_connector` — an internal path used consumers
to type the return value of `load_csvs_to_duckdb()`.

**Request:** Add `IbisConnector` to `aitaem/__init__.py`'s `__all__`.

---

## Summary Table

| ID | Description | Priority | Status | Consumer pain point |
|----|-------------|----------|--------|---------------------|
| R-1 | Export all exceptions at top level | High | ✅ Done (plan 15) | Internal import paths break on refactor |
| R-2 | Public `VALID_PERIOD_TYPES` constant | High | ✅ Done (plan 15) | Duplicated `Literal` in 2 files |
| R-3 | Validate `name` as SQL identifier at load time | High | 🔲 Planned (plan 16) | SQL errors surface late, hard to debug |
| R-4 | `SpecCache` introspection (`metrics`, `slices`, `segments` properties) | High | ✅ Done (plan 15) | Consumers re-parse YAML to build catalog |
| R-5 | Export `STANDARD_COLUMNS` at top level | Medium | 🔲 Open | Hardcoded column names in system prompt |
| R-6 | `period_type` typed as `PeriodType` on `compute()` | Medium | ✅ Done (plan 15) | No IDE/static-analysis guard on invalid values |
| R-7 | Export spec types and validators at top level | Medium | 🔲 Open | Internal import paths |
| R-8 | Fix `from_string()` OSError on long YAML (macOS) | Medium | ✅ Done (plan 13) | Forces workaround via temp files |
| R-9 | Export `IbisConnector` at top level | Low | 🔲 Open | Internal import path |
| +U | `SpecCache` duplicate name enforcement | — | ✅ Done (plan 15) | Silent overwrites masked configuration errors |
