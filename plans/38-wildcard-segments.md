# Plan 38 — Wildcard Segments

**Scope:** Extend `SegmentSpec` to support wildcard-style definitions — one or more DIM-table
columns whose distinct values (or value combinations, for more than one column) become
segments automatically at query time, without enumerating `values[]`/CASE-WHEN clauses by
hand.

`SegmentSpec`/`SegmentValue` are part of released `v2.0.0` and are not renamed or removed —
this is a purely additive extension.

---

## 1. Key decisions

| Decision | Choice |
|---|---|
| `SegmentSpec` field shape | `values: tuple[SegmentValue, ...] = ()` (now optional), new `column: tuple[str, ...] = ()`, derived `is_wildcard -> bool` property. Field order: `name, source, entity_id, values=(), column=(), description="", join_keys=()`. All existing `SegmentSpec(...)` call sites use keyword arguments. |
| YAML key | `where:` accepts a scalar (`where: store_type`) or a list (`where: [store_type, region]`), parsed into `column` as a 1-tuple or n-tuple. |
| Where the wildcard column(s) live | On the segment's DIM table (`segment_spec.source`); added onto `dim_src` before the join. |
| Multi-column: group by raw columns | Each listed column gets its own alias (`_segment_0`, `_segment_1`, ...); `GROUP BY` runs on these columns; the `'\|'`-joined display value is built only after aggregation, so distinct combinations can't collide into one group. |
| Wildcard column casting | Ibis's `.cast("string")`, applied via `.mutate()` — not raw SQL. CASE-WHEN (enumerated) stays SQL-spliced (predicates are raw user text). |
| `segment_value`/`segment_name` | Built by reusing `_build_slice_value_expr`, renamed `_build_joined_value_expr` (now serves both slices and segments), against the post-aggregation table. `segment_name` is always `segment_spec.name`. |
| Composite segments (named-spec form) | Out of scope — single-spec, single-table mechanism only. |
| Validation dispatch | `validate_segment_spec` requires exactly one of `values`/`column` non-empty. Each `column` entry validated with `_is_valid_column_identifier`, checked for duplicates (mirrors `values`/`cross_product`'s existing dedup checks), and checked for overlap with `entity_id`/`join_keys` (rejected — wildcarding the join key turns every DIM row into its own segment). |
| DIM-side column existence checking | `referenced_columns["where"] = list(column)` flows through `validate_spec` Check 5, which already checks referenced columns against the DIM table's live schema. |
| Cardinality / dot-qualified column names | No cardinality limit, matching wildcard slices. `_is_valid_column_identifier` accepts dot-qualified names, but `dim_src[col]` requires a bare name — a dot-qualified entry fails with an Ibis `KeyError` at query-build time. |

---

## 2. Scope

**In scope:**
- SF-1 — `SegmentSpec` dataclass shape (`aitaem/specs/segment.py`).
- SF-2 — `validate_segment_spec` dispatch + `referenced_columns` (`aitaem/utils/validation.py`).
- SF-3 — Query builder: per-column Ibis-native casting, post-aggregation join, `_build_joined_value_expr` rename (`aitaem/query/builder.py`).
- SF-4 — Tests.
- SF-5 — Docs.

**Out of scope:**
- Composite segments referencing other named `SegmentSpec`s.
- Column-name quoting/escaping (pre-existing, shared with `entity_id`/`join_keys`).
- `_build_wildcard_slice_expr`'s raw-SQL `VARCHAR` cast (existing, released fact-table wildcard code) — not touched here.
- `aitaem/agent/` changes — DefinitionBot's authoring guidance for wildcard segments is Plan 39.

---

## 3. Sub-features

### SF-1 — `SegmentSpec` dataclass

**File:** `aitaem/specs/segment.py`

```python
@dataclass(frozen=True)
class SegmentSpec:
    name: str
    source: str
    entity_id: str
    values: tuple[SegmentValue, ...] = ()
    column: tuple[str, ...] = ()
    description: str = ""
    join_keys: tuple[str, ...] = ()

    @property
    def is_wildcard(self) -> bool:
        return bool(self.column)
```

- `from_yaml`: `where` present and a scalar → `column = (raw_where,)`; a list → `column = tuple(raw_where)`; either way `values` stays `()`. Else → `values` parsed from the `values` key (empty tuple if absent).
- `validate()`: round-trips `column` back into `spec_dict["where"]` — a bare string when `len(column) == 1`, a list otherwise.
- Unknown-fields allowlist gains `"where"`.

### SF-2 — Validation

**File:** `aitaem/utils/validation.py`, `validate_segment_spec`

- Presence check: exactly one of `values`/`column` non-empty.
- Each `column` entry validated via `_is_valid_column_identifier`.
- Duplicate entries in `column` rejected (mirrors `_validate_values_list`/`cross_product`'s dedup checks, same file).
- Any `column` entry equal to `entity_id` or a `join_keys` entry rejected.
- `referenced_columns`: wildcard branch emits `{"where": list(column)}`.
- `ValidationResult.referenced_columns`' docstring updated to list the new `"where"` key.

### SF-3 — Query builder

**File:** `aitaem/query/builder.py`, `_build_metric_segment_query`

- `segment_alias: str | None` becomes `segment_aliases: list[str]`. `dim_labeled`'s construction branches on `segment_spec.is_wildcard`:
  - CASE-WHEN: unchanged — one alias, via `_build_segment_case_when_expr`, spliced with `dim_src.sql(f"SELECT *, {segment_col_sql} FROM {dim_src_name}")`.
  - Wildcard: one alias per `column` entry, via `dim_src.mutate(**QueryBuilder._build_wildcard_segment_mutations(segment_spec, dim_src))` — new `_build_wildcard_segment_mutations(segment_spec, dim_table) -> dict[str, ibis.Value]` returning `{alias: dim_table[col].cast("string"), ...}`.
- Join: `joined.select(*fact_cols, *[joined[a] for a in segment_aliases])`.
- Null-filter: extend `null_filters` with `labeled[a].notnull() for a in segment_aliases`.
- `group_by_cols`: `group_by_cols.extend(segment_aliases)`.
- Outer projection: `segment_value_expr = QueryBuilder._build_joined_value_expr(aggregated, segment_aliases)`. `segment_name_val` unchanged.

### SF-4 — Tests

- `tests/test_specs/test_segment_spec.py` — wildcard YAML parsing (scalar, list), `is_wildcard` property, `validate()` round-trip, mutual-exclusivity errors.
- `tests/test_utils/test_validation.py` — valid single- and multi-column, invalid column identifier, duplicate-entry rejection, `entity_id`/`join_keys`-overlap rejection, `referenced_columns["where"]` shape.
- `tests/test_query/test_builder.py`:
  - `_build_wildcard_segment_mutations` unit tests (single- and multi-column aliases and expressions).
  - `_build_joined_value_expr` (renamed): regression test, existing slice-composite behavior unchanged.
  - New `TestWildcardSegmentExecution`: NULL exclusion, one aggregate row per distinct value/combination, correct aggregates, `segment_name == spec.name`, `'\|'`-join order.
  - Collision regression: two DIM rows whose column values differ but whose naive `'\|'`-joined string would match (e.g. `("A|B", "C")` vs. `("A", "B|C")`) → two separate aggregate rows.
  - Empty DIM table / no matching rows → zero segment rows.
  - Dot-qualified `column` entry → `KeyError` at query-build time.
  - `ibis.to_sql(expr, dialect="bigquery")`: `.cast("string")` emits `STRING`.
  - Multi-entity metric + explicit `by_entity` + wildcard segment.
- `tests/test_agent/test_definition_tools.py` — `validate_spec` Check 5 coverage, single- and multi-column.

### SF-5 — Docs

- `docs/api/specs.md` — new "Keys for wildcard segment specs" table (`"where"` → `[col1, col2, ...]`), plus a short multi-column note.
- `aitaem/specs/README.md` — refresh the `SegmentSpec(...)` signature block, including `column`.
- `docs/changelog.md` → `## Unreleased` → `### Added`: wildcard segment support, including multi-column.

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/specs/segment.py` | SF-1: `column: tuple[str, ...]` field, `is_wildcard` property, `values` defaulted, scalar-or-list `from_yaml`/`validate()` dispatch |
| `aitaem/utils/validation.py` | SF-2: `validate_segment_spec` mutual-exclusivity, per-entry/duplicate/overlap `column` validation, wildcard `referenced_columns`, docstring update |
| `aitaem/query/builder.py` | SF-3: `segment_aliases` list threading through join/null-filter/group-by/outer-projection; `_build_wildcard_segment_mutations`; `_build_slice_value_expr` → `_build_joined_value_expr` rename |
| `tests/test_specs/test_segment_spec.py` | SF-4: wildcard parsing/validation tests |
| `tests/test_utils/test_validation.py` | SF-4: `validate_segment_spec` wildcard tests |
| `tests/test_query/test_builder.py` | SF-4: expr unit tests, `TestWildcardSegmentExecution`, collision regression, BigQuery dialect check, slice-rename regression |
| `tests/test_agent/test_definition_tools.py` | SF-4: `validate_spec` Check 5 wildcard-segment coverage |
| `docs/api/specs.md` | SF-5: wildcard segment `referenced_columns` table + multi-column note |
| `aitaem/specs/README.md` | SF-5: `SegmentSpec` signature refresh |
| `docs/changelog.md` | SF-5: `Unreleased` entry |
