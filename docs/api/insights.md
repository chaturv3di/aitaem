# MetricCompute

::: aitaem.insights.MetricCompute

---

## `scan()`

```python
mc.scan() -> ScanResult
```

Introspects source table schemas and returns a compatibility matrix for all specs loaded in the
`SpecCache`. For each metric, every loaded slice and segment is checked:

- **Slice** — compatible when all columns referenced in `values[].where` (or the bare `column`
  field for wildcard slices) exist in the metric's source table. Composite slices are resolved
  transitively via their component leaf/wildcard slices.
- **Segment** — compatible when at least one join-key candidate exists in the metric's source
  table. Candidates are taken from `join_keys` (if non-empty) or `entity_id`. Only the
  fact-table side is checked; DIM-table columns are not.

Schema introspection is batched by unique source URI — each table is queried once regardless of
how many metrics share it. Metrics whose source connection is unavailable are skipped with a
warning; all other metrics are still processed.

Returns a `ScanResult` with one `CompatibilityResult` per metric × slice and per metric × segment.
See the [Specs API reference](specs.md#compatibility) for field descriptions.
