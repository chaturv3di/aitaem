# Plan 19: Spec Validation — Column Extraction

## Overview

This plan extends `ValidationResult` with a `referenced_columns` map that captures every column
name referenced in a metric or slice spec, keyed by the field that references it. The downstream
use case is warehouse column-existence checking: a consumer who holds a live connection can
iterate the map and confirm that every column the spec refers to is present in the source table.

Two design decisions constrain the implementation:

1. **Column extraction is only performed when the spec is fully valid.** An invalid spec may have
   unparseable expressions; partial extraction could produce a misleading map. Consumers are
   expected to gate on `result.valid` before using `result.referenced_columns`.

2. **Full-SQL validation is out of scope.** The current per-expression checks
   (`SELECT {expr}` / `SELECT 1 WHERE {expr}`) already cover expression-level syntax. Building
   the complete query SQL via `QueryBuilder` adds no additional error class for user-authored
   fields and is therefore not included in this plan.

---

## Scope

| # | Feature |
|---|---------|
| SF-1 | `_extract_columns_from_sql` private helper in `validation.py` |
| SF-2 | `referenced_columns` field on `ValidationResult` |
| SF-3 | Populate `referenced_columns` in `validate_metric_spec` |
| SF-4 | Populate `referenced_columns` in `validate_slice_spec` |
| SF-5 | Tests |
| SF-6 | Documentation |

`validate_segment_spec` is **not** in scope; the user's explicit request covers metric and slice
specs only.

---

## Background & Critical Observations

### What the current validation covers

| Field | Validation today |
|-------|-----------------|
| `numerator` | `sqlglot.parse_one(f"SELECT {expr}")` — syntax + aggregate presence |
| `denominator` | Same as numerator |
| `values[i].where` (slice/segment) | `sqlglot.parse_one(f"SELECT 1 WHERE {expr}")` — syntax |
| `timestamp_col` | Required non-empty string — no SQL parsing |
| `entities` | Required non-empty list of strings — no SQL parsing |
| Wildcard slice `where` | Plain column identifier regex — no SQL parsing |

All expression syntax is already validated. This plan **adds** column extraction on top; it does
not replace or extend the existing syntax checks.

### Column extraction via sqlglot AST

`sqlglot.parse_one()` returns an AST. Walking it for `sqlglot.expressions.Column` nodes gives
every column reference in the expression. `node.name` returns the unqualified column name
(e.g. for `t.revenue`, `.name == "revenue"`). Since each `MetricSpec` points to a single
source table, unqualified names are sufficient for the downstream use case.

Edge cases:
- `COUNT(*)` — `*` is `exp.Star`, not `exp.Column`; nothing is extracted. Correct.
- `SUM(CASE WHEN status = 'active' THEN revenue ELSE 0 END)` — extracts both `status` and
  `revenue`. Correct.
- Column referenced multiple times in one field (e.g. `SUM(a) / COUNT(a)`) — deduplicated
  to `["a"]` within that field entry.

### `referenced_columns` map structure

```python
{
    "numerator":     ["revenue"],
    "denominator":   ["impressions"],
    "timestamp_col": ["created_at"],       # plain string field, not SQL-parsed
    "entities":      ["user_id", "org_id"],# plain list field, not SQL-parsed
}
```

For a slice leaf spec with two values:
```python
{
    "values[0].where": ["industry", "region"],
    "values[1].where": ["industry"],
}
```

For a wildcard slice spec:
```python
{
    "where": ["industry"],   # the bare column name
}
```

For a composite slice spec: `{}` (no SQL expressions, no columns).

The value `None` (not an empty dict) is used when the spec is **invalid**, to distinguish
"nothing extracted because invalid" from "extracted and found no columns".

---

## Sub-Features

### SF-1 — `_extract_columns_from_sql` helper

**File:** `aitaem/utils/validation.py`

Add a private helper after `_validate_sql_expression`:

```python
def _extract_columns_from_sql(expr: str, context: str = "select") -> list[str]:
    """Extract unqualified column names from a SQL expression via sqlglot AST.

    Returns an empty list if sqlglot is not installed or the expression cannot be parsed.
    Columns are deduplicated while preserving their order of first appearance.
    """
    try:
        import sqlglot
        import sqlglot.expressions as exp
    except ImportError:
        return []

    try:
        if context == "select":
            tree = sqlglot.parse_one(f"SELECT {expr}")
        else:
            tree = sqlglot.parse_one(f"SELECT 1 WHERE {expr}")
    except Exception:
        return []

    seen: dict[str, None] = {}
    for node in tree.walk():
        if isinstance(node, exp.Column):
            seen[node.name] = None
    return list(seen)
```

**Validation of SF-1:**
- `_extract_columns_from_sql("SUM(revenue)")` → `["revenue"]`
- `_extract_columns_from_sql("SUM(amount) / NULLIF(SUM(impressions), 0)")` → `["amount", "impressions"]`
- `_extract_columns_from_sql("COUNT(*)")` → `[]`
- `_extract_columns_from_sql("SUM(a) + SUM(a)")` → `["a"]` (deduplicated)
- `_extract_columns_from_sql("SUM(CASE WHEN status = 'active' THEN revenue ELSE 0 END)")` →
  `["status", "revenue"]`
- `_extract_columns_from_sql("industry = 'tech'", context="where")` → `["industry"]`
- `_extract_columns_from_sql("amt > 0 AND channel = 'email'", context="where")` →
  `["amt", "channel"]`

---

### SF-2 — Extend `ValidationResult`

**File:** `aitaem/utils/validation.py`

```python
@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError]
    referenced_columns: dict[str, list[str]] | None = None
    """
    Maps each spec field to the column names it references.

    Populated only when ``valid is True``. ``None`` when the spec is invalid.
    Consumers must check ``result.valid`` before relying on this field.

    Example (metric spec)::

        {
            "numerator":     ["revenue"],
            "denominator":   ["impressions"],
            "timestamp_col": ["created_at"],
            "entities":      ["user_id"],
        }

    Example (slice leaf spec)::

        {
            "values[0].where": ["industry"],
            "values[1].where": ["industry", "region"],
        }
    """
```

No other code changes needed to callers — `referenced_columns` defaults to `None`, so all
existing call sites continue to work without modification.

**Validation of SF-2:**
- `ValidationResult(valid=True, errors=[])` → `result.referenced_columns is None` (default)
- `ValidationResult(valid=False, errors=[...])` → `result.referenced_columns is None` (default)
- `ValidationResult(valid=True, errors=[], referenced_columns={"numerator": ["x"]})` →
  `result.referenced_columns == {"numerator": ["x"]}`

---

### SF-3 — Populate `referenced_columns` in `validate_metric_spec`

**File:** `aitaem/utils/validation.py`

At the **end** of `validate_metric_spec`, after all checks are complete, add a population
block that runs only when `errors` is empty:

```python
    referenced_columns: dict[str, list[str]] | None = None
    if not errors:
        col_map: dict[str, list[str]] = {}

        # SQL-expression fields
        col_map["numerator"] = _extract_columns_from_sql(numerator, context="select")

        if denominator:
            col_map["denominator"] = _extract_columns_from_sql(denominator, context="select")

        # Plain-string fields (already validated as non-empty strings above)
        col_map["timestamp_col"] = [timestamp_col.strip()]

        if entities:
            col_map["entities"] = [e.strip() for e in entities if isinstance(e, str) and e.strip()]

        referenced_columns = col_map

    return ValidationResult(valid=True, errors=[], referenced_columns=referenced_columns)
```

Note: at the block entry, `numerator`, `denominator`, `timestamp_col`, and `entities` are
guaranteed non-empty / valid because the preceding error checks would have added to `errors`
otherwise.

**Validation of SF-3:**

| Spec | Expected `referenced_columns` |
|------|-------------------------------|
| Minimal valid metric (no denominator, no entities) | `{"numerator": [...], "timestamp_col": ["created_at"]}` |
| Valid metric with denominator | adds `"denominator": [...]` |
| Valid metric with entities | adds `"entities": ["user_id", "org_id"]` |
| `COUNT(*)` numerator | `"numerator": []` |
| Invalid metric (e.g. bad syntax) | `referenced_columns is None` |
| `MetricSpec.validate()` on a valid spec | propagates `referenced_columns` from `validate_metric_spec` |

---

### SF-4 — Populate `referenced_columns` in `validate_slice_spec`

**File:** `aitaem/utils/validation.py`

At the **end** of `validate_slice_spec`, after all checks are complete, add:

```python
    referenced_columns: dict[str, list[str]] | None = None
    if not errors:
        col_map: dict[str, list[str]] = {}

        if values is not None and isinstance(values, list):
            # Leaf spec: extract from each where clause
            for i, value in enumerate(values):
                if isinstance(value, dict):
                    where_expr = value.get("where", "")
                    if where_expr and isinstance(where_expr, str):
                        col_map[f"values[{i}].where"] = _extract_columns_from_sql(
                            where_expr, context="where"
                        )
        elif where is not None and isinstance(where, str) and where.strip():
            # Wildcard spec: the 'where' field is a bare column name
            col_map["where"] = [where.strip()]
        # Composite spec: cross_product contains slice names, not SQL — col_map stays empty

        referenced_columns = col_map

    return ValidationResult(valid=len(errors) == 0, errors=errors, referenced_columns=referenced_columns)
```

**Validation of SF-4:**

| Spec type | Expected `referenced_columns` |
|-----------|-------------------------------|
| Leaf with 2 values | `{"values[0].where": [...], "values[1].where": [...]}` |
| Wildcard (`where: industry`) | `{"where": ["industry"]}` |
| Composite (`cross_product: [...]`) | `{}` (empty dict, not `None`) |
| Invalid slice (any error) | `referenced_columns is None` |
| `SliceSpec.validate()` on valid spec | propagates `referenced_columns` |

---

### SF-5 — Tests

**New test file:** `tests/test_utils/test_column_extraction.py`

Unit tests for `_extract_columns_from_sql`:
- All cases listed in SF-1 above
- Context `"where"` with compound expressions

**Updates to `tests/test_utils/test_validation.py`:**

*Metric:*
- Valid minimal metric → `referenced_columns == {"numerator": [...], "timestamp_col": [...]}`
- Valid metric with denominator → includes `"denominator"` key
- Valid metric with entities → includes `"entities"` key with correct list
- `COUNT(*)` numerator → `"numerator": []`
- Invalid metric → `referenced_columns is None`
- `referenced_columns` dict has no extra unexpected keys

*Slice:*
- Valid leaf slice (1 value) → `{"values[0].where": [...]}`
- Valid leaf slice (2 values) → keys `"values[0].where"` and `"values[1].where"`
- Valid wildcard slice → `{"where": ["col_name"]}`
- Valid composite slice → `referenced_columns == {}`
- Invalid slice → `referenced_columns is None`

**Updates to `tests/test_specs/test_metric_spec.py`:**
- `MetricSpec.validate()` on a valid spec returns `ValidationResult` with populated `referenced_columns`
- `MetricSpec.from_yaml()` succeeds without error (existing tests should still pass)

**Updates to `tests/test_specs/test_slice_spec.py`:**
- `SliceSpec.validate()` on a valid leaf/wildcard/composite returns correct `referenced_columns`

---

### SF-6 — Documentation

**`docs/changelog.md`** — under `## Unreleased`:

```markdown
### Added
- `ValidationResult.referenced_columns` — populated on successful validation; maps each spec
  field to the list of column names it references. Useful for downstream column-existence checks
  against a live warehouse. `None` when the spec is invalid.
```

**`docs/api/specs.md`** — add a subsection under `ValidationResult`:

```
#### `referenced_columns`

`dict[str, list[str]] | None`

Maps each spec field to the unqualified column names it references.

Only populated when `valid is True`. When the spec is invalid, this field is `None`.
Consumers must check `result.valid` before using this field.

**Metric spec fields extracted:**

| Key | Source |
|-----|--------|
| `"numerator"` | SQL expression (AST-parsed) |
| `"denominator"` | SQL expression (AST-parsed), present only if field is set |
| `"timestamp_col"` | Plain string field |
| `"entities"` | Plain list field, present only if field is set |

**Slice spec fields extracted:**

| Key | Source |
|-----|--------|
| `"values[i].where"` | SQL WHERE expression (AST-parsed), one key per leaf value |
| `"where"` | Bare column identifier (wildcard spec only) |
| *(no keys)* | Composite spec — no SQL expressions, empty dict returned |

**Example usage (metric):**

```python
result = validate_metric_spec(spec_dict)
if result.valid:
    for field, columns in result.referenced_columns.items():
        print(f"{field}: {columns}")
    # numerator: ['revenue']
    # timestamp_col: ['created_at']
```
```

**`docs/user-guide/specs.md`** — add a new section "Column introspection":

> After loading a spec, you can inspect every column it references via `spec.validate()`:
>
> ```python
> result = metric_spec.validate()
> if result.valid:
>     print(result.referenced_columns)
>     # {'numerator': ['revenue'], 'timestamp_col': ['created_at']}
> ```
>
> `referenced_columns` is `None` when the spec is invalid. Always check `result.valid` first.
> Column names are unqualified (the table prefix is stripped if present in the expression).
> This field is intended for downstream consumers who want to verify column existence against
> a live warehouse before computing metrics.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/utils/validation.py` | Add `_extract_columns_from_sql`; extend `ValidationResult`; update `validate_metric_spec` and `validate_slice_spec` |
| `tests/test_utils/test_column_extraction.py` | New unit tests for `_extract_columns_from_sql` |
| `tests/test_utils/test_validation.py` | Add `referenced_columns` assertions to metric and slice cases |
| `tests/test_specs/test_metric_spec.py` | Add `referenced_columns` propagation test via `MetricSpec.validate()` |
| `tests/test_specs/test_slice_spec.py` | Add `referenced_columns` propagation test via `SliceSpec.validate()` |
| `docs/changelog.md` | Add entry under `## Unreleased` |
| `docs/api/specs.md` | Document `ValidationResult.referenced_columns` |
| `docs/user-guide/specs.md` | Add "Column introspection" section |

No changes to `aitaem/__init__.py`, `aitaem/specs/metric.py`, `aitaem/specs/slice.py`, or
any query/connector modules.

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| `referenced_columns` is `None` (not `{}`) on invalid specs | Distinguishes "failed to extract" from "extracted and found nothing" (composite slices) |
| Extraction only when `valid is True` | Partial extraction from invalid expressions risks misleading column maps |
| Unqualified column names (`node.name`) | `MetricSpec.source` is a single table; table-qualified names add noise without utility |
| Plain-string fields (`timestamp_col`, `entities`) included in the map | They are column references, even though they are not SQL-parsed. Consumers need the full picture for warehouse column checks |
| `"entities"` key omitted when the field is `None` | Matches the field being optional; consumers should not assume the key is present |
| `"denominator"` key omitted when the field is `None` | Same rationale |
| `validate_segment_spec` not updated | Not requested; can be added in a follow-up if needed |

---

## Edge Cases

| Case | Handling |
|------|----------|
| `COUNT(*)` numerator | `exp.Star` is not `exp.Column`; `"numerator": []` |
| `numerator` references a table-qualified column (e.g. `SUM(t.revenue)`) | `node.name == "revenue"`; table prefix stripped |
| Same column in multiple fields (e.g. `revenue` in numerator and denominator) | Both keys get `["revenue"]`; no cross-field deduplication — provenance is preserved |
| `entities` has a duplicate entry | Preserved as-is; deduplication is the caller's responsibility |
| sqlglot not installed | `_extract_columns_from_sql` returns `[]`; column map keys still present but with empty lists |
| Expression that passes syntax validation but AST walk finds no columns | Empty list `[]` for that key — valid and expected |
| Composite slice | `referenced_columns == {}` (empty dict, not `None`) |

---

## Out of Scope

- `validate_segment_spec` column extraction
- Cross-field validation (e.g. asserting `timestamp_col` appears in `entities`)
- Fully-qualified column extraction (table + column) for multi-table expressions
- Full SQL validation via `QueryBuilder` (assessed as providing no additional syntax coverage
  beyond the current per-expression checks)
- Validating that referenced columns actually exist in the warehouse (connection-dependent;
  left to the downstream consumer)
