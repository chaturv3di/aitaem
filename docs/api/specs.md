# Specs

!!! tip "Looking for YAML spec syntax?"
    For the full YAML format, required fields, and examples for each spec type, see the
    [Writing Specs](../user-guide/specs.md) user guide.

## SpecCache

::: aitaem.specs.loader.SpecCache

### Introspection

`SpecCache` exposes three read-only properties for iterating over loaded specs without
needing `get_metric()` / `get_slice()` / `get_segment()` lookups:

```python
cache = SpecCache.from_yaml(metric_paths="metrics/", slice_paths="slices/")

# Iterate names
for name in cache.metrics:
    print(name, cache.metrics[name].description)

# Access a spec directly
spec = cache.slices["geography"]
```

The returned `Mapping` is a live read-only view of the internal dict (`MappingProxyType`).
Mutation attempts raise `TypeError`.

### Spec name constraints

All spec names (`MetricSpec`, `SliceSpec`, `SegmentSpec`) must be valid SQL identifiers:

- Match `^[A-Za-z_][A-Za-z0-9_]*$`
- Letters, digits, and underscores only
- Must start with a letter or underscore (not a digit)

Names are validated at load time. Invalid names raise `SpecValidationError` with the
offending name and a suggested replacement. This constraint exists because `SliceSpec`
names are used as bare SQL column aliases (`_slice_{name}`), and all names are
validated consistently for simplicity.

### Duplicate name enforcement

All loading paths (`from_yaml`, `from_string`, `add`) raise `SpecValidationError` if a
spec with the same name has already been loaded into the same cache. Uniqueness is
enforced **per spec type** — a metric and a slice may share a name without conflict.

---

## ValidationResult

`ValidationResult` is returned by `MetricSpec.validate()`, `SliceSpec.validate()`,
`SegmentSpec.validate()`, and the lower-level `validate_metric_spec()` /
`validate_slice_spec()` / `validate_segment_spec()` functions.

| Field | Type | Description |
|-------|------|-------------|
| `valid` | `bool` | `True` if the spec passed all validation checks |
| `errors` | `list[ValidationError]` | List of validation errors (empty when valid) |
| `referenced_columns` | `dict[str, list[str]] \| None` | Column map — see below |

### `referenced_columns`

Maps each spec field to the unqualified column names it references. Populated only when
`valid is True`; `None` when the spec is invalid.

!!! warning
    Always check `result.valid` before using `result.referenced_columns`. When the spec is
    invalid, the field is `None` — not an empty dict.

**Keys for metric specs:**

| Key | Source |
|-----|--------|
| `"numerator"` | SQL expression (AST-parsed) |
| `"denominator"` | SQL expression (AST-parsed), present only if the field is set |
| `"timestamp_col"` | Plain string field |
| `"entities"` | Plain list field, present only if the field is set |

**Keys for slice leaf specs:**

| Key | Source |
|-----|--------|
| `"values[i].where"` | SQL WHERE expression (AST-parsed), one key per value |

**Keys for wildcard slice specs:**

| Key | Source |
|-----|--------|
| `"where"` | The bare column name |

**Keys for composite slice specs:** empty dict `{}` — no SQL expressions to extract from.

**Example — metric spec:**

```python
result = metric_spec.validate()
if result.valid:
    for field, columns in result.referenced_columns.items():
        print(f"{field}: {columns}")
    # numerator:     ['revenue']
    # denominator:   ['impressions']
    # timestamp_col: ['created_at']
    # entities:      ['user_id']
```

**Example — slice spec:**

```python
result = slice_spec.validate()
if result.valid:
    print(result.referenced_columns)
    # {'values[0].where': ['region'], 'values[1].where': ['region', 'country']}
```

Column names are **unqualified** — for `SUM(t.revenue)` the extracted name is `"revenue"`,
not `"t.revenue"`. This is sufficient for single-source specs where each `MetricSpec.source`
points to one table.

---

## MetricSpec

::: aitaem.specs.metric.MetricSpec

---

## SliceSpec

::: aitaem.specs.slice.SliceSpec

---

## SliceValue

::: aitaem.specs.slice.SliceValue

---

## SegmentSpec

::: aitaem.specs.segment.SegmentSpec

---

## SegmentValue

::: aitaem.specs.segment.SegmentValue
