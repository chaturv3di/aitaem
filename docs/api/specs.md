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
