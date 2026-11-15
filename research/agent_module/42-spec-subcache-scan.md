# Compatibility checks on a sub-cache of specs

**Date:** 2026-11-10
**Sources:** aitaem source at commit 281c92d (`aitaem/specs/loader.py` `SpecCache.add`/`_validate`,
`aitaem/insights.py` `_run_scan`), executed on Python 3.12 with the example specs and
`examples/data/ad_campaigns.duckdb`
**Used by:** `plans/42-aitaem-agent-v3.md` SF-7 (`check_compatibility`)

---

Plan 42 SF-7 marked as **[verify]**: "`SpecCache.add()` accepts a composite slice without the
slices it references, and `scan()` on that cache gives the same verdicts as on the full cache."

## Findings (executed)

1. **The first half is false.** `SpecCache.add()` runs `_validate()`, which raises
   `SpecValidationError` ("Referenced slice 'geo' not found in loaded slices") when a composite
   slice's `cross_product` names a slice absent from the cache, and rolls the add back.
2. `_run_scan` resolves a composite slice through `spec_cache.get_slice(name)` for each
   `cross_product` entry and checks the union of their referenced columns, so the components must
   be in the cache for the scan itself too.
3. **A sub-cache holding the named specs plus each named composite slice's components** (added
   before the composite) gives, for every named metric × named slice/segment pair, a
   `CompatibilityResult` equal to the full-cache `scan()` result. Executed for metrics
   `ctr`, `roas`, composite slice `geo × industry`, and segment `platform`: 4 of 4 results equal.
   The sub-cache scan also reports the component slices, which are not among the named specs.
