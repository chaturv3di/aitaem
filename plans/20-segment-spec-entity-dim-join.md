# Plan: Segment Spec Redesign — Entity-Attribute DIM Join Model

## Context

Segment specs are currently structurally identical to leaf slice specs: a `source` URI and a list of `values[].where` clauses applied inline as a `CASE WHEN` against the metric's **fact table**. This has two problems:

1. The `source` field in `SegmentSpec` is parsed and validated but **never used** by `QueryBuilder` — it is dead code.
2. The `where` clauses are assumed to reference columns on the fact table, which defeats the purpose of star-schema segmentation (where classification attributes live in DIM tables, not the fact table).

The intent is for segments to be based on entity attributes from DIM tables (e.g., `dim_users`, `dim_customers`). A segment must know:
- Which DIM table to join (its existing `source`)
- The DIM table's primary key (`entity_id`)
- Which fact-table foreign keys are valid join points (`join_keys`)

The join key to actually use is supplied at `compute()` time (since the same segment can apply to different entity columns on the same fact table — e.g., `buyerID` vs `sellerID`).

**Goal:** Make segments first-class DIM-join constructs. `entity_id` is a **required** field — a segment spec without it is malformed. Columns that live on the fact table should be expressed as slices instead.

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Entity binding location | `entity_id` in spec (DIM PK), join key in `compute()` | Spec is reusable; binding is contextual |
| `join_keys` field | Optional whitelist on spec; default = `[entity_id]` | Validation without over-constraining |
| Multi-segment per call | Max 1 segment per `compute()` call | Analytical clarity; users can call twice |
| `segments` param type | `dict[str, str] \| str \| None` | Dict = explicit binding; str = default binding |
| `entity_id` required | Missing `entity_id` is a validation error | Enforces the DIM-join model; fact-table predicates belong in slices |
| DIM column qualification | Auto-qualify unqualified column refs in `where` with `_dim.` via sqlglot | Transparent to spec authors |

---

## Changes

### 1. `aitaem/specs/segment.py`

Add `entity_id` as a required field and `join_keys` as optional:

```python
@dataclass(frozen=True)
class SegmentSpec:
    name: str
    source: str
    entity_id: str                        # PK column on the DIM table (required)
    values: tuple[SegmentValue, ...]
    description: str = ""
    join_keys: tuple[str, ...] = ()       # valid FK columns on fact tables (whitelist)
```

- `from_yaml()`: read `entity_id` and `join_keys` from YAML dict; pass to `validate_segment_spec`
- `validate()`: round-trip new fields through `validate_segment_spec`

### 2. `aitaem/utils/validation.py` — `validate_segment_spec()`

Add validation for new fields:

- `entity_id`: **required**; must be a non-empty string and a valid column identifier (reuse `_is_valid_column_identifier`). Missing → `"'entity_id' is required and must be a non-empty string"`
- `join_keys`: if present, must be a non-empty list of valid column identifiers
- `referenced_columns` (already returned on valid specs): extend to include `entity_id` and `join_keys` entries when present

### 3. `aitaem/insights.py`

**`compute()` signature change** (breaking change — `list[str]` no longer accepted):

```python
segments: dict[str, str] | str | None = None
```

Where `dict[str, str]` maps exactly one segment name → join key override.

Validation at compute time (before calling `QueryBuilder`):
- If dict has >1 entry → `QueryBuildError("Only one segment per compute() call is supported")`
- Resolve segment spec via `spec_cache.get_segment(name)`
- Determine effective `join_key`:
  - If dict: use the provided value; if spec has non-empty `join_keys`, validate it's in the whitelist
  - If str: use `spec.entity_id` as default join key (always set since it is required)
- Pass `segment_spec` and `segment_join_key` to `QueryBuilder.build_queries()`

**Updated call:**
```python
query_groups = QueryBuilder.build_queries(
    metric_specs=metric_specs,
    slice_specs=slice_specs,
    segment_spec=segment_spec,           # SegmentSpec | None  (was: segment_specs list)
    segment_join_key=segment_join_key,   # str | None
    ...
)
```

### 4. `aitaem/query/builder.py`

**Signature changes** (propagate through `build_queries` → `_build_queries_for_metric` → `_build_metric_segment_query`):

```python
# Replace:  segment_specs: list[SegmentSpec] | None
# With:
segment_spec: SegmentSpec | None
segment_join_key: str | None = None
```

Remove the `all_segment_specs` loop and no-segment baseline list construction — only one segment now. Preserve the `None` baseline (no-segment query still generated).

**`_build_metric_segment_query()` — DIM join path:**

When `segment_spec is not None` (always applies since `entity_id` is always set):

```sql
-- all_time path:
WITH _labeled AS (
    SELECT t.*,
        CASE
            WHEN _dim.{col} ... THEN '{val}'
            ELSE NULL
        END AS _segment
    FROM {fact_table} t
    JOIN {dim_table} _dim ON t.{join_key} = _dim.{entity_id}
    WHERE {time_filter}   -- if present
)
SELECT ... FROM _labeled WHERE _segment IS NOT NULL GROUP BY _segment
```

- `dim_table` = `_parse_table_name_from_uri(segment_spec.source)`
- `join_key` = `segment_join_key`
- `entity_id` = `segment_spec.entity_id`
- The `where` expressions from `segment_spec.values` are auto-qualified with `_dim.` (see below)
- Fact table is aliased as `t`; DIM table as `_dim`

For the **non-all_time path**, the fact table is already aliased as `t` (for the periods JOIN). Add the DIM join as a second `JOIN`:
```sql
FROM {fact_table} t
JOIN _periods p ON CAST(t.{ts_col} AS TIMESTAMP) >= p.period_start ...
JOIN {dim_table} _dim ON t.{join_key} = _dim.{entity_id}
```

**New helper: `_qualify_where_with_dim_alias(where_expr: str) -> str`**

Uses sqlglot to rewrite unqualified column references in a WHERE expression to be qualified with `_dim.`:

```python
@staticmethod
def _qualify_where_with_dim_alias(where_expr: str) -> str:
    import sqlglot, sqlglot.expressions as exp
    tree = sqlglot.parse_one(f"SELECT 1 WHERE {where_expr}")
    for node in tree.walk():
        if isinstance(node, exp.Column) and not node.table:
            node.set("table", exp.to_identifier("_dim"))
    return str(tree.find(exp.Where).this)
```

Called as a pre-processing step in `_build_metric_segment_query` before building the CASE WHEN expressions.

---

## Documentation Changes

### `docs/user-guide/specs.md` — SegmentSpec section (full rewrite)

The current SegmentSpec description ("A segment is similar to a slice but includes a `source` field") is misleading and must be replaced. The new section should:

- Open with a clear conceptual statement: segments classify entities using attributes from DIM tables, joined to the fact table at query time.
- Show an updated YAML example with `entity_id` and `join_keys`:

```yaml
segment:
  name: user_tier
  description: Customer value tier based on lifetime spend
  source: duckdb://analytics.db/dim_users
  entity_id: user_id
  join_keys: [buyer_id, seller_id]
  values:
    - name: premium
      where: "lifetime_value > 1000"
    - name: standard
      where: "lifetime_value <= 1000"
```

- Replace the fields table with:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier |
| `source` | Yes | URI of the DIM table to join |
| `entity_id` | Yes | Primary key column on the DIM table — the DIM side of the join condition |
| `values` | Yes | List of `{name, where}` filter definitions; `where` expressions reference DIM table columns |
| `join_keys` | No | Whitelist of valid fact-table FK columns that can be used as the join key at `compute()` time. When omitted, any column name is accepted. |
| `description` | No | Human-readable description |

- Add a note explaining that `where` expressions reference DIM columns by bare name — they are automatically qualified with the DIM table alias at query time. Users should write `"lifetime_value > 1000"`, not `"_dim.lifetime_value > 1000"`.

- Add a note explaining the slice-vs-segment distinction: "If the classification predicate references columns that already live on the fact table, use a `SliceSpec` instead."

Also update the **Column introspection** sub-section to document new keys for segment specs:

| Key | Source |
|-----|--------|
| `"entity_id"` | Plain string field |
| `"join_keys"` | Plain list field, present only if the field is set |
| `"values[i].where"` | SQL WHERE expression (AST-parsed), one key per value |

### `docs/user-guide/computing-metrics.md`

**`segments` parameter section** — full rewrite:

- New type: `dict[str, str] | str | None`
  - `dict`: maps one segment name → the fact-table FK column to join on (join key override)
  - `str`: segment name only; uses `entity_id` from the spec as the default join key
- Only **one segment per `compute()` call** is supported; passing a dict with more than one entry raises `QueryBuildError`.

```python
# Explicit join key: join dim_users on buyer_id
df = mc.compute(metrics="revenue", segments={"user_tier": "buyer_id"})

# Default join key: uses entity_id declared in the spec
df = mc.compute(metrics="revenue", segments="user_tier")
```

**"Combining Slices and Segments" section** — update the prose and example to reflect the single-segment constraint. The example should show one segment (not `segments="platform"` which implied multiple could stack).

**Error Handling table** — add two rows:

| Exception | Raised when |
|-----------|-------------|
| `QueryBuildError` | More than one segment is passed (dict with >1 entry) |
| `QueryBuildError` | The join key provided in the `segments` dict is not in the spec's `join_keys` whitelist |

### `docs/api/specs.md` — `referenced_columns` section

Add a **"Keys for segment specs"** subsection (currently absent) after the composite slice entry:

| Key | Source |
|-----|--------|
| `"entity_id"` | Plain string field |
| `"join_keys"` | Plain list field, present only if set |
| `"values[i].where"` | SQL WHERE expression (AST-parsed), one key per value |

### `docs/changelog.md` — Unreleased section

Add under `## Unreleased`:

```markdown
### Changed (Breaking)

- **`SegmentSpec.entity_id` is now required.** Segment specs without `entity_id` raise
  `SpecValidationError` at load time. A segment without `entity_id` has no way to join its
  DIM table to the fact table. Predicates that reference fact-table columns should be expressed
  as `SliceSpec` values instead.

  **Migration:** add `entity_id: <dim_pk_column>` to every segment YAML.

- **`MetricCompute.compute(segments=...)` parameter type changed.** The old `str | list[str]`
  type is replaced by `dict[str, str] | str | None`. The dict form maps exactly one segment
  name to the fact-table FK column to use as the join key. The `str` form retains its meaning
  (segment name) but now uses the spec's `entity_id` as the default join key.
  Only **one segment per `compute()` call** is supported; passing a dict with more than one
  entry raises `QueryBuildError`.

  **Migration:** replace `segments="my_segment"` (unchanged) or `segments=["my_segment"]`
  (broken) with `segments={"my_segment": "<fact_fk_col>"}` when a non-default join key
  is needed.

### Added

- **`SegmentSpec.join_keys`** — optional list of fact-table FK column names that the segment
  is permitted to join on. When provided, the join key supplied at `compute()` time is
  validated against this whitelist and raises `QueryBuildError` if not listed. When omitted,
  any column name is accepted.

### Fixed

- **`SegmentSpec.source` is now used.** Previously, the `source` field on a segment spec was
  parsed and validated but never used in query generation — segment predicates were silently
  applied to the metric's fact table regardless. Segments now generate a SQL `JOIN` from the
  fact table to the DIM table declared in `source`, keyed on `entity_id`.
```

---

## Files Modified

| File | Nature of change |
|---|---|
| `aitaem/specs/segment.py` | Add `entity_id`, `join_keys` fields; update `from_yaml()` and `validate()` |
| `aitaem/utils/validation.py` | Extend `validate_segment_spec()` for new fields |
| `aitaem/insights.py` | Change `segments` param type; add binding resolution logic |
| `aitaem/query/builder.py` | Signature change; DIM join SQL generation; `_qualify_where_with_dim_alias` helper |
| `tests/test_specs/test_segment_spec.py` | Add tests for `entity_id`/`join_keys` parsing and validation |
| `tests/test_specs/fixtures/valid_segment.yaml` | Add `entity_id` to fixture |
| `examples/segments/platform.yaml` | Add `entity_id` and `join_keys` |
| `tests/test_query/` (existing builder tests) | Update for new signature; add DIM join SQL generation tests |
| `tests/test_insights.py` (or new file) | Integration test: segment with DIM join produces correct SQL structure |
| `docs/user-guide/specs.md` | Rewrite SegmentSpec section; update Column introspection for new segment keys |
| `docs/user-guide/computing-metrics.md` | Rewrite `segments` param docs; update Combining section; extend Error Handling table |
| `docs/api/specs.md` | Add "Keys for segment specs" subsection to `referenced_columns` |
| `docs/changelog.md` | Add `## Unreleased` entry with breaking changes, added, and fixed entries |

---

## Verification

1. **Unit — spec parsing**: `pytest tests/test_specs/test_segment_spec.py` — new `entity_id`/`join_keys` fields round-trip through YAML; validation catches malformed cases (join_keys without entity_id, invalid identifiers).

2. **Unit — query builder**: `pytest tests/test_query/` — assert generated SQL contains `JOIN {dim_table} _dim ON t.{join_key} = _dim.{entity_id}`; assert `_dim.` prefix is applied to segment `where` column references.

3. **Integration — insights**: `pytest tests/test_insights.py` — compute with a segment that has `entity_id` executes without error and returns rows with `segment_name` / `segment_value` populated.

4. **Coverage**: `pytest --cov=aitaem --cov-report=term-missing` — no regressions in existing slice or metric tests.
