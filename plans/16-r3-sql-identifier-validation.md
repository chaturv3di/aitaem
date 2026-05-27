# Plan 16 — R-3: Validate Spec `name` as a SQL Identifier at Load Time

Addresses R-3 from `REQ01-api-improvement-requests.md`.

---

## Problem

`QueryBuilder` generates bare, unquoted SQL column aliases from spec names:

```python
# aitaem/query/builder.py
alias = f"_slice_{ss.name}"   # e.g. _slice_English speaking countries  ← DuckDB error
```

A `SliceSpec` with `name: "English speaking countries"` loads successfully from YAML
but fails at query execution with a DuckDB parse error. The error message references
generated SQL, not the spec name, making it hard to debug.

`MetricSpec.name` and `SegmentSpec.name` are only ever embedded in quoted SQL string
literals (`'...'`), so they cannot cause a SQL syntax error — but they are validated
for consistency and to prevent any latent SQL injection risk via string literal content.

---

## Scope

Validate the `name` field of **all three spec types** at load time. The constraint:

```
^[A-Za-z_][A-Za-z0-9_]*$
```

- Must start with a letter or underscore
- May contain only letters, digits, and underscores
- No spaces, hyphens, dots, or other characters

`SliceValue.name` and `SegmentValue.name` (the per-value sub-names within a spec)
are used only as string literals in generated SQL and are **not** in scope.

---

## Critical Observations and Edge Cases

### 1. Breaking change for consumers
Any existing spec with a `name` that does not match the regex will start failing at
load time. Examples that will break:
- `name: "English speaking countries"` (spaces)
- `name: "revenue-2024"` (hyphen)
- `name: "Net Revenue"` (capital letter + space — *capital letters alone are fine*)
- `name: "2024_signups"` (starts with digit)

The changelog entry must clearly document this as a breaking change with a migration
note.

### 2. Relationship to the existing `_is_valid_column_identifier` helper
`validation.py` already has:
```python
def _is_valid_column_identifier(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", value))
```

This is used for the wildcard `where` field in `SliceSpec` and **intentionally allows
dots** (e.g. `schema.table.column`). Spec names must **not** contain dots — they
become part of SQL alias tokens like `_slice_my.name`, which is invalid.

A new, separate helper `_is_valid_spec_name` must be added with the stricter pattern
(`[A-Za-z0-9_]` only, no dots). Do not reuse or extend `_is_valid_column_identifier`.

### 3. Where to add validation
All spec name validation lives in `aitaem/utils/validation.py` in the three
`validate_*_spec` functions. Each already validates `name` for presence and
non-emptiness. The SQL identifier check must come **after** the presence check
(we don't want to emit a second error if the name is empty or missing).

### 4. Error message quality
The error should identify the invalid name, state the constraint clearly, and
give a concrete suggestion:

```
name 'English speaking countries' is not a valid SQL identifier.
Must match ^[A-Za-z_][A-Za-z0-9_]*$ (letters, digits, underscores only;
must start with a letter or underscore).
Suggestion: use 'english_speaking_countries' instead.
```

The `suggestion` field of `ValidationError` should be populated.

### 5. `re.compile` vs inline `re.match`
The existing `_is_valid_column_identifier` imports `re` inline. The new helper
should define `_SPEC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")` at
module level to avoid recompiling on every validation call.

### 6. No changes to `builder.py`
The validation is added exclusively at load time (`from_yaml` / `from_string` /
`add` all call the `validate_*_spec` functions). `builder.py` does not need to
change — by the time a spec reaches the query builder it is already validated.

### 7. Fixture names in existing tests
All current test fixture names (`homepage_ctr`, `total_revenue`, `geography`,
`customer_value_tier`, `geo`, `device`, `geo_x_device`, etc.) already match the
regex. No existing passing tests will break.

---

## Implementation Sub-Features

### SF-1: Add `_is_valid_spec_name` and apply it in all three validators

**File changed:** `aitaem/utils/validation.py`

Add at module level (after the existing `import re` inside `_is_valid_column_identifier`
is refactored out — or simply add a top-level import):

```python
import re

_SPEC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_valid_spec_name(name: str) -> bool:
    """Return True if name is a valid SQL identifier for use as a spec name."""
    return bool(_SPEC_NAME_RE.match(name))
```

In each of `validate_metric_spec`, `validate_slice_spec`, `validate_segment_spec`,
add after the existing presence check:

```python
# After: if not name or not isinstance(name, str) or not name.strip(): ...
elif not _is_valid_spec_name(name):
    errors.append(
        ValidationError(
            field="name",
            message=f"name '{name}' is not a valid SQL identifier "
                    f"(must match ^[A-Za-z_][A-Za-z0-9_]*$)",
            suggestion=f"use '{name.replace(' ', '_').replace('-', '_')}' instead",
        )
    )
```

The `elif` ensures the message is only emitted when the name is present but invalid,
not when it is missing (the previous check would have already appended an error).

**Validation (tests to add):**

New test class `TestSpecNameValidation` in `tests/test_utils/test_validation.py`
covering all three validators:

| Scenario | Expected |
|---|---|
| `name: "my_metric"` | valid |
| `name: "MyMetric"` | valid |
| `name: "_private"` | valid |
| `name: "a"` | valid (single char) |
| `name: "english speaking"` | invalid — space |
| `name: "revenue-2024"` | invalid — hyphen |
| `name: "2024_signups"` | invalid — starts with digit |
| `name: "schema.metric"` | invalid — dot |
| `name: "my!metric"` | invalid — special char |

Test each invalid case for all three spec types. Test that `errors[].field == "name"`
and that the `suggestion` field is populated.

---

### SF-2: Integration test — invalid name rejected at `SpecCache` load time

**File changed:** `tests/test_specs/test_spec_loader.py`

Add a new test class `TestSpecNameIdentifierValidation` verifying that
`SpecCache.from_string()` raises `SpecValidationError` for each spec type when the
name contains a space or hyphen:

```python
INVALID_NAME_METRIC_YAML = """
metric:
  name: "my invalid metric"
  source: duckdb://db/t
  numerator: "SUM(x)"
  timestamp_col: ts
"""

def test_from_string_rejects_metric_with_invalid_name(self):
    with pytest.raises(SpecValidationError) as exc_info:
        SpecCache.from_string(metric_yaml=INVALID_NAME_METRIC_YAML)
    assert any("not a valid SQL identifier" in e.message for e in exc_info.value.errors)
```

Equivalent tests for SliceSpec and SegmentSpec.

Also add a test confirming `from_yaml()` rejects invalid names when loading from a file,
to verify the validation runs on the full load path (not just unit-level).

---

### SF-3: Documentation updates

**Files changed:**
- `docs/changelog.md` — add breaking change entry under `## Unreleased`
- `docs/api/specs.md` — add a note under spec name constraints

**Changelog entry** (must be clearly marked as a breaking change):

```markdown
### Changed (Breaking)
- `MetricSpec`, `SliceSpec`, `SegmentSpec`: spec `name` is now validated as a SQL
  identifier at load time. Names must match `^[A-Za-z_][A-Za-z0-9_]*$` — letters,
  digits, and underscores only, starting with a letter or underscore. Specs with
  names containing spaces, hyphens, dots, or other characters will raise
  `SpecValidationError` at load time rather than `QueryExecutionError` at compute time.

  **Migration:** rename any affected specs. For example, `"English speaking countries"`
  → `"english_speaking_countries"`, `"revenue-2024"` → `"revenue_2024"`.
```

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/utils/validation.py` | Add `_SPEC_NAME_RE`, `_is_valid_spec_name()`; apply check in all three `validate_*_spec` functions |
| `tests/test_utils/test_validation.py` | New `TestSpecNameValidation` class — unit tests for each validator |
| `tests/test_specs/test_spec_loader.py` | New `TestSpecNameIdentifierValidation` class — integration tests via `SpecCache` |
| `docs/changelog.md` | Breaking change entry |
| `docs/api/specs.md` | Name constraint documentation |

---

## Testing Strategy

1. Run baseline before starting: `python -m pytest -q`
2. After SF-1: run `python -m pytest tests/test_utils/ -q` — new unit tests pass,
   all existing validation tests still pass
3. After SF-2: run `python -m pytest tests/test_specs/ -q` — integration tests pass
4. Final: `python -m pytest -q` — full suite green; `ruff check aitaem/` clean
5. Commit once all pass
