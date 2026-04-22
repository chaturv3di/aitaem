# Plan 13 — Bugfix: from_yaml() fails on long YAML strings (PATH_MAX)

## Problem

All three spec classes (`MetricSpec`, `SliceSpec`, `SegmentSpec`) share this pattern in
`from_yaml()`:

```python
is_path = isinstance(yaml_input, Path)
path: Path = yaml_input if isinstance(yaml_input, Path) else Path(str(yaml_input))

if is_path or path.exists():   # <— BUG is here
    ...
else:
    raw = str(yaml_input)
```

When `yaml_input` is a YAML string longer than the OS `PATH_MAX` value (~4 KB on Linux,
~1 KB on some systems), the call to `path.exists()` issues a `stat` syscall with that
overlong string as the path. The kernel rejects it and `path.exists()` raises `OSError`
instead of returning `False`. This causes valid YAML specs to fail with an unhandled
exception.

`isinstance(yaml_input, Path)` itself does **not** raise; only `path.exists()` does.

## Root Cause

`pathlib.Path.exists()` propagates `OSError` for reasons other than "file not found",
including `ENAMETOOLONG` (errno 36). The fix must wrap `path.exists()` in a
`try/except OSError` and treat any `OSError` as "this is not a file path", i.e. return
`False`.

## Code Duplication Opportunity

The file-reading preamble is copy-pasted verbatim across all three `from_yaml()` methods:

1. Detect whether input is a file path or a YAML string.
2. Read the file (or pass the string through as-is).
3. Guard against empty input.
4. Parse YAML via `yaml.safe_load`, raising `SpecValidationError` on `YAMLError`.
5. Assert the expected top-level key is present, raising `SpecValidationError` otherwise.
6. Assert the value under that key is a `dict`, raising `SpecValidationError` otherwise.

Only steps after step 6 are spec-specific (validation + dataclass construction). Steps 1–6
can be extracted into a single private helper that all three classes share.

## Why Not Put the Helper in `loader.py`?

`loader.py` already imports from all three spec modules. Adding an import in the reverse
direction (`metric.py` → `loader.py`) creates a circular import.

## Why `utils/yaml_validation.py`?

`utils/` already holds spec-domain code — `utils/exceptions.py` defines
`SpecValidationError` and `utils/validation.py` defines `validate_metric_spec` and
friends. The import direction `specs/ → utils/` is already established. The new helper
fits naturally alongside the existing validation utilities, and `yaml_validation.py`
accurately describes its primary job: validating the structure of incoming YAML (empty
check, parse errors, top-level key, dict assertion). File I/O is just a pre-step.

## Out of Scope

- No changes to validation logic.
- No changes to public API or documentation.
- No changes to `SpecCache` or `loader.py`.

---

## Implementation Steps

Steps are ordered so that each item depends only on previously completed items.

### Step 1 — Create `aitaem/utils/yaml_validation.py` with `load_yaml_spec_dict`

This is the foundational change that the three spec-class refactors all depend on.
Create a new module with one public function:

```python
def load_yaml_spec_dict(
    yaml_input: str | Path,
    spec_type_name: str,   # e.g. "metric", "slice", "segment"
) -> dict:
    """Resolve yaml_input to a raw spec dict.

    1. Detects whether yaml_input is a file path or a YAML string,
       handling OSError from path.exists() (e.g. ENAMETOOLONG) by treating
       the input as a YAML string.
    2. Reads the file or uses the string as-is.
    3. Guards against empty input.
    4. Parses YAML, raising SpecValidationError on YAMLError.
    5. Validates the top-level key equals spec_type_name.
    6. Validates the value under that key is a dict.

    Returns the dict under the top-level key.

    Raises:
        FileNotFoundError: if input is a Path and the file does not exist.
        SpecValidationError: for empty input, bad YAML, missing/wrong top-level key.
    """
```

The key fix lives inside this function — the path detection logic:

```python
# isinstance(yaml_input, Path) is safe; path.exists() is not
if isinstance(yaml_input, Path):
    is_file = True
else:
    try:
        is_file = Path(str(yaml_input)).exists()
    except OSError:
        # ENAMETOOLONG or similar — string exceeds PATH_MAX,
        # so it cannot be a file path; treat as YAML content.
        is_file = False
```

`yaml_validation.py` should be added to `aitaem/utils/__init__.py` only if other modules
outside `specs/` are expected to use it; otherwise leave it unexported for now.

### Step 2 — Write unit tests for `load_yaml_spec_dict` in `tests/test_utils/`

Write tests for the helper in isolation before refactoring the spec classes, so
regressions are caught at the lowest level. Cover:

- Valid file path (Path object) → returns correct dict
- Valid file path (string) → returns correct dict
- Non-existent Path object → raises `FileNotFoundError`
- Valid YAML string → returns correct dict
- YAML string longer than PATH_MAX → returns correct dict (the bug scenario)
- Empty string → raises `SpecValidationError`
- Malformed YAML string → raises `SpecValidationError`
- YAML string missing expected top-level key → raises `SpecValidationError`
- YAML string where top-level key value is not a dict → raises `SpecValidationError`

### Step 3 — Refactor `MetricSpec.from_yaml()` to use `load_yaml_spec_dict`

Replace the duplicated preamble with a single call to `load_yaml_spec_dict(yaml_input,
"metric")`. The remainder of the method (validation, unknown-field logging, dataclass
construction) is unchanged. Verify all existing `MetricSpec` tests still pass.

### Step 4 — Refactor `SliceSpec.from_yaml()` to use `load_yaml_spec_dict`

Same pattern as Step 3 for `"slice"`. Verify all existing `SliceSpec` tests still pass.

### Step 5 — Refactor `SegmentSpec.from_yaml()` to use `load_yaml_spec_dict`

Same pattern as Step 3 for `"segment"`. Verify all existing `SegmentSpec` tests still pass.

### Step 6 — Add regression tests for the PATH_MAX bug (one per spec type)

Add a parametrized test (or three explicit cases) that passes a valid YAML string padded
to exceed PATH_MAX to each of `MetricSpec.from_yaml()`, `SliceSpec.from_yaml()`, and
`SegmentSpec.from_yaml()`. Assert the spec loads correctly and all fields are populated
as expected.

### Step 7 — Run full test suite with coverage and commit

```
pytest tests/test_specs/ tests/test_utils/ --cov=aitaem/specs --cov=aitaem/utils
```

All tests must pass. Confirm coverage of `yaml_validation.py` is 100%. Then create a git commit.

---

## Implementation Checklist

### `aitaem/utils/yaml_validation.py` (new file)
- [ ] Module created with module-level docstring
- [ ] `load_yaml_spec_dict(yaml_input, spec_type_name)` function implemented
- [ ] Path detection wraps `path.exists()` in `try/except OSError`
- [ ] `isinstance(yaml_input, Path)` always treated as a file path (no OSError risk)
- [ ] Empty input raises `SpecValidationError`
- [ ] `YAMLError` is caught and re-raised as `SpecValidationError`
- [ ] Missing top-level key raises `SpecValidationError` with descriptive message
- [ ] Non-dict top-level value raises `SpecValidationError`
- [ ] `FileNotFoundError` raised when a Path/file-string points to a non-existent file

### `tests/test_utils/test_yaml_validation.py` (new file)
- [ ] Test: valid Path object → correct dict returned
- [ ] Test: valid file path as string → correct dict returned
- [ ] Test: non-existent Path → `FileNotFoundError`
- [ ] Test: valid YAML string → correct dict returned
- [ ] Test: YAML string > PATH_MAX → correct dict returned (bug regression)
- [ ] Test: empty string → `SpecValidationError`
- [ ] Test: malformed YAML → `SpecValidationError`
- [ ] Test: missing top-level key → `SpecValidationError`
- [ ] Test: top-level value is not a dict → `SpecValidationError`

### `aitaem/specs/metric.py`
- [ ] `from_yaml()` calls `load_yaml_spec_dict(yaml_input, "metric")`
- [ ] All duplicated preamble lines removed
- [ ] Spec-specific logic (validation, unknown fields, dataclass construction) unchanged
- [ ] All existing `MetricSpec` tests pass

### `aitaem/specs/slice.py`
- [ ] `from_yaml()` calls `load_yaml_spec_dict(yaml_input, "slice")`
- [ ] All duplicated preamble lines removed
- [ ] Spec-specific logic unchanged
- [ ] All existing `SliceSpec` tests pass

### `aitaem/specs/segment.py`
- [ ] `from_yaml()` calls `load_yaml_spec_dict(yaml_input, "segment")`
- [ ] All duplicated preamble lines removed
- [ ] Spec-specific logic unchanged
- [ ] All existing `SegmentSpec` tests pass

### Regression tests (PATH_MAX bug)
- [ ] `MetricSpec.from_yaml()` with YAML string > PATH_MAX → loads correctly
- [ ] `SliceSpec.from_yaml()` with YAML string > PATH_MAX → loads correctly
- [ ] `SegmentSpec.from_yaml()` with YAML string > PATH_MAX → loads correctly

### Final validation
- [ ] `pytest tests/test_specs/ tests/test_utils/ --cov=aitaem/specs --cov=aitaem/utils` passes with 100% coverage on `yaml_validation.py`
- [ ] `ruff check` and `ruff format` pass with no errors
- [ ] Git commit created
