# Specs

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
