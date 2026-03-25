# Wildcard Slice Specs Plan

## Overview

Introduce a **wildcard slice** — a new `SliceSpec` variant where the slice values are not
enumerated in the YAML but are instead discovered automatically at query time from the distinct
values present in a named column.

A wildcard slice is declared with a top-level `where` field containing only a column name:

```yaml
slice:
  name: industry
  where: industry        # bare column name → wildcard
```

At compute time this emits SQL that groups by `industry`, producing one output row per distinct
value without any pre-query or pre-fetch step.

---

## Motivation

For high-cardinality or frequently-changing categorical columns (e.g. `country`, `plan_tier`,
`campaign_type`), maintaining an explicit list of `name`/`where` pairs in the spec is fragile:

- New values in the column silently go missing from reports.
- Removing a value requires a spec edit and redeployment.
- Large cardinality columns (50+ values) make specs unreadable.

The wildcard variant eliminates enumeration entirely — the spec author declares only *which*
column drives the slice, and the query engine discovers the values at runtime via GROUP BY.

---

## Design Decisions

### YAML field: top-level `where` on the slice spec (not inside `values`)

The existing `where` field lives inside each `SliceValue` under `values[i].where`. The new
field is a top-level sibling of `name`, `values`, and `cross_product`:

```yaml
slice:
  name: industry
  where: industry        # top-level — declares wildcard column
```

This is syntactically unambiguous: a top-level `where` alongside `name` signals wildcard;
`where` nested inside `values[i]` remains an explicit SQL predicate.

Accepted column name forms (validated at parse time):
- Simple: `industry`, `user_id`
- Dot-qualified: `public.campaigns.industry`, `t.country`

SQL expressions (operators, quotes, spaces, functions) are rejected with a clear error.

### Internal field: `column` on `SliceSpec`

The YAML key `where` is stored internally as `SliceSpec.column: str = ""`. The name `column`
avoids conflating it with a SQL WHERE clause throughout the codebase.

```python
@dataclass(frozen=True)
class SliceSpec:
    name: str
    values: tuple[SliceValue, ...] = ()
    cross_product: tuple[str, ...] = ()
    column: str = ""         # NEW — non-empty iff this is a wildcard spec
    description: str = ""

    @property
    def is_wildcard(self) -> bool:
        return bool(self.column)
```

### Three mutually exclusive spec variants

| Variant   | YAML field        | `is_composite` | `is_wildcard` |
|-----------|-------------------|----------------|---------------|
| Leaf      | `values`          | False          | False         |
| Composite | `cross_product`   | True           | False         |
| Wildcard  | `where: col_name` | False          | True          |

Validation rejects any combination of two or more of `values`, `cross_product`, `where`.

### SQL generation: direct column reference instead of CASE WHEN

For **leaf** slices the builder emits a CASE WHEN expression:

```sql
CASE
    WHEN industry = 'SaaS'       THEN 'SaaS'
    WHEN industry = 'E-commerce' THEN 'E-commerce'
    ELSE NULL
END AS _slice_industry
```

For **wildcard** slices the builder emits a direct cast:

```sql
CAST(industry AS VARCHAR) AS _slice_industry
```

The rest of the query structure is unchanged:

- `_slice_industry IS NOT NULL` in the outer WHERE (filters NULLs from the column)
- `_slice_industry` in GROUP BY (one row per distinct value)
- `_slice_industry` as `slice_value` in the outer SELECT
- `slice_type` carries the slice name (`'industry'`), as for leaf slices

### No runtime pre-fetch of distinct values

Values are not fetched before query execution. The SQL GROUP BY discovers them naturally.
This keeps `QueryBuilder` as a pure SQL-string builder with no database dependency — consistent
with the existing design.

### Wildcard slices in `cross_product` composites

Wildcard slices can be referenced by composite (cross_product) slice specs. Each wildcard
component contributes its column as a direct cast in the CTE, and the composite
`slice_value` is the concatenation of the individual column values (e.g. `SaaS|Search`).
No special handling is needed beyond what already exists for the cross-product path.

---

## API Changes

### `SliceSpec` — `specs/slice.py`

New field and property:

```python
@dataclass(frozen=True)
class SliceSpec:
    name: str
    values: tuple[SliceValue, ...] = ()
    cross_product: tuple[str, ...] = ()
    column: str = ""          # NEW — bare column name; non-empty iff is_wildcard
    description: str = ""

    @property
    def is_wildcard(self) -> bool:   # NEW
        """True if this spec auto-discovers values from a column at query time."""
        return bool(self.column)
```

New YAML variant (parsed in `from_yaml`):

```yaml
slice:
  name: industry
  where: industry
  description: "Breakdown by industry (auto-populated)"   # optional
```

### `validate_slice_spec` — `utils/validation.py`

Accept `where` as a third valid spec form. Mutually exclusive with `values` and
`cross_product`. The value must be a non-empty string that matches the pattern
`^[A-Za-z_][A-Za-z0-9_.]*$` (simple or dot-qualified identifier). SQL expressions
are rejected.

### `QueryBuilder._build_slice_case_when_expr` — `query/builder.py`

No change to the existing method. A new helper is added:

```python
@staticmethod
def _build_wildcard_slice_expr(slice_spec: SliceSpec, alias: str) -> str:
    """Build direct column cast expression for a wildcard SliceSpec."""
    return f"CAST({slice_spec.column} AS VARCHAR) AS {alias}"
```

In `_build_metric_segment_query`, the dispatch becomes:

```python
if ss.is_wildcard:
    cte_extra_cols.append(QueryBuilder._build_wildcard_slice_expr(ss, alias))
else:
    cte_extra_cols.append(QueryBuilder._build_slice_case_when_expr(ss, alias))
```

No other changes to builder logic — the GROUP BY, WHERE, and slice_value expression
paths are already correct for the wildcard case.

---

## Full SQL Example

### Input spec

```yaml
slice:
  name: industry
  where: industry
```

### Emitted SQL (all_time, no segment)

```sql
WITH _labeled AS (
    SELECT
        *,
        CAST(industry AS VARCHAR) AS _slice_industry
    FROM campaigns
)
SELECT
    'all_time'          AS period_type,
    NULL                AS period_start_date,
    NULL                AS period_end_date,
    NULL                AS entity_id,
    'total_spend'       AS metric_name,
    'industry'          AS slice_type,
    _slice_industry     AS slice_value,
    'none'              AS segment_name,
    'all'               AS segment_value,
    SUM(spend)          AS metric_value
FROM _labeled
WHERE _slice_industry IS NOT NULL
GROUP BY _slice_industry
```

### Cross-product wildcard example

```yaml
# campaign_type wildcard slice
slice:
  name: campaign_type
  where: campaign_type

# cross-product of industry (wildcard) × campaign_type (wildcard)
slice:
  name: industry_x_campaign
  cross_product: [industry, campaign_type]
```

Emitted CTE columns:

```sql
CAST(industry      AS VARCHAR) AS _slice_industry,
CAST(campaign_type AS VARCHAR) AS _slice_campaign_type
```

Outer slice_value: `_slice_industry || '|' || _slice_campaign_type`

---

## Files to Modify

```
aitaem/specs/slice.py               # Add column field; is_wildcard property; parse top-level where
aitaem/utils/validation.py          # Accept where: col_name variant in validate_slice_spec()
aitaem/query/builder.py             # Add _build_wildcard_slice_expr(); dispatch on is_wildcard
docs/user-guide/specs.md            # Document wildcard slice syntax and example
docs/changelog.md                   # Add entry under Unreleased
```

No new files. No new dependencies. No changes to `insights.py`, `executor.py`,
`formatting.py`, or the segment/metric spec modules.

---

## Sub-Feature Implementation Order

### 1. Add `column` field and `is_wildcard` property to `SliceSpec`; parse top-level `where`

**Files**: `aitaem/specs/slice.py`, `aitaem/utils/validation.py`

Changes to `SliceSpec`:
- Add `column: str = ""` field (after `cross_product`, before `description`)
- Add `is_wildcard` property
- In `from_yaml`: after checking for `cross_product` and `values`, check for top-level `where`;
  parse the value as the column name and pass to `cls(column=...)`
- Update `validate()` to include `where: self.column` in the spec_dict passed to
  `validate_slice_spec` when `is_wildcard` is True
- Update unknown-fields allowlist to include `"where"`

Changes to `validate_slice_spec`:
- If `where` is present alongside `values` or `cross_product`, emit a conflict error
- If `where` is present alone, validate the value is a non-empty string matching
  `^[A-Za-z_][A-Za-z0-9_.]*$`; emit `ValidationError` if it looks like a SQL expression
  (contains spaces, operators, quotes, parentheses)
- If none of `values`, `cross_product`, `where` is present, keep the existing error

**Tests** (`tests/test_specs/`):
- `where: industry` (valid) → `SliceSpec(column='industry', is_wildcard=True)`
- `where: "public.orders.country"` (dot-qualified, valid) → accepted
- `where: "industry = 'SaaS'"` (SQL expression) → `SpecValidationError`
- `where: industry` + `values: [...]` → `SpecValidationError` (conflict)
- `where: industry` + `cross_product: [...]` → `SpecValidationError` (conflict)
- Existing leaf and composite specs → no regression
- `SliceSpec.validate()` on a wildcard spec → returns valid `ValidationResult`

---

### 2. Add `_build_wildcard_slice_expr` to `QueryBuilder`; dispatch on `is_wildcard`

**File**: `aitaem/query/builder.py`

Changes:
- Add static method `_build_wildcard_slice_expr(slice_spec, alias) -> str` that returns
  `f"CAST({slice_spec.column} AS VARCHAR) AS {alias}"`
- In `_build_metric_segment_query`, inside the `if slice_specs:` block, replace the direct
  call to `_build_slice_case_when_expr` with a dispatch:
  ```python
  if ss.is_wildcard:
      cte_extra_cols.append(QueryBuilder._build_wildcard_slice_expr(ss, alias))
  else:
      cte_extra_cols.append(QueryBuilder._build_slice_case_when_expr(ss, alias))
  ```

**Tests** (`tests/test_query/`):
- SQL string test — wildcard slice: CTE contains `CAST(industry AS VARCHAR) AS _slice_industry`;
  outer WHERE contains `_slice_industry IS NOT NULL`; outer GROUP BY contains `_slice_industry`
- SQL string test — leaf slice: no regression (still emits CASE WHEN)
- SQL string test — cross-product of two wildcard slices: both columns cast; slice_value is
  `_slice_a || '|' || _slice_b`
- SQL string test — cross-product of one wildcard + one leaf slice: mixed dispatch works
- Integration test with in-memory DuckDB:
  - Table has `industry` column with values `['SaaS', 'Fintech', 'EdTech', NULL]`
  - Wildcard slice on `industry` produces exactly 3 rows (NULL excluded)
  - `slice_type == 'industry'` on all rows
  - `slice_value` contains exactly `{'SaaS', 'Fintech', 'EdTech'}`
  - New column values discovered after insert (add `'Healthcare'` row) appear automatically
    in the next `compute()` call — no spec change required

---

### 3. Update documentation

**Files**: `docs/user-guide/specs.md`, `docs/changelog.md`

`docs/user-guide/specs.md` — add a **Wildcard slices** sub-section under SliceSpec:

> Instead of enumerating values, declare only the column name in a top-level `where` field.
> Values are discovered automatically at query time.
>
> ```yaml
> slice:
>   name: industry
>   where: industry
> ```
>
> The column name must be a plain identifier (alphanumeric and underscores, optionally
> dot-qualified). SQL expressions are not accepted.

Include comparison table:

| Variant   | When to use                                         |
|-----------|-----------------------------------------------------|
| Leaf      | Fixed, well-known values; custom WHERE predicates   |
| Wildcard  | Dynamic/high-cardinality columns; equality slices   |
| Composite | Cross-product of two or more existing slice specs   |

`docs/changelog.md` — add to `## Unreleased`:

```
- `SliceSpec`: new wildcard variant — set `where: <column_name>` at the spec level
  (instead of listing `values`) to auto-populate slice values from the column's distinct
  values at query time. Supports simple and dot-qualified column names.
```

---

## Integration Test Scenario

```python
# Table: campaigns
# Columns: campaign_id (str), industry (str | NULL), spend (float)
# Data:
#   ('c1', 'SaaS',     100.0)
#   ('c2', 'Fintech',  200.0)
#   ('c3', 'SaaS',     150.0)
#   ('c4', 'EdTech',    50.0)
#   ('c5', None,        80.0)   # NULL industry
#
# Metric: total_spend = SUM(spend)
# Wildcard slice: industry (where: industry)
```

Assertions:

1. Output has exactly 3 rows (NULL industry excluded by IS NOT NULL filter)
2. `slice_type == 'industry'` on all rows
3. `slice_value` values are `{'SaaS', 'Fintech', 'EdTech'}`
4. `metric_value` for `'SaaS'` equals 250.0 (100 + 150)
5. `metric_value` for `'Fintech'` equals 200.0
6. `metric_value` for `'EdTech'` equals 50.0
7. No-slice baseline row also present: `slice_type='none'`, `slice_value='all'`,
   `metric_value=580.0` (all rows including NULL-industry)

Adding a new industry value (`'Healthcare'`) to the table and re-running `compute()`
produces a 4th slice row without any spec change.

---

## Out of Scope

- Pre-fetching distinct values at spec-load time (keeping QueryBuilder DB-free is a core design constraint)
- Filtering wildcard values at spec time (e.g. exclude a specific value) — use a leaf spec for this
- Wildcard slices on computed expressions (only bare column references are supported)
- Segment specs with wildcard-style auto-population — Phase 2 if needed
