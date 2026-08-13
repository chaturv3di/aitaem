# Plan 39 — DefinitionBot: Catalog Parity, Wildcard-vs-Enumeration Guidance, Join-Gap Detection

**Scope:** Give `DefinitionBot` three related spec-authoring intelligence capabilities it
currently lacks:

1. Check whether a spec already satisfying the user's intent exists in the catalog, before
   drafting a new one — built on the same underlying resolution mechanism `QueryBot` uses.
2. Decide between a wildcard spec (Plan 38) and an enumerated one for slices/segments, using
   the distribution tooling already wired onto `DefinitionBot` (Plan 37).
3. Detect when the columns a request needs are spread across multiple tables, and recommend
   the user create a backend view instead of attempting (or silently failing at) a
   single-table spec.

None of `DefinitionBot`'s 9 existing tools are renamed or removed. `resolver.py`'s
`SpecResolver.resolve()` keeps its existing signature and return shape — this plan refactors
its internals, not its contract.

---

## 1. Key decisions

| Decision | Choice |
|---|---|
| Existing-spec discovery mechanism | A new `check_existing_spec` tool, built on a shared name-resolution primitive extracted from `SpecResolver.resolve()` — not a bespoke reimplementation. `resolve()`'s own docstring already calls this exact-match + `difflib`-suggestion logic the "v0 → v1 swap point" for a future RAG upgrade; extracting it once means both `resolve()` and `check_existing_spec` inherit that upgrade later with no second migration. |
| Shared primitive shape | New `SpecResolver.resolve_name(spec_type: Literal["metric","slice","segment"], proposed_name: str, spec_cache: SpecCache) -> NameResolution` (`NameResolution`: `exact: MetricSpec \| SliceSpec \| SegmentSpec \| None`, `suggestions: list[str]`). `resolve()` is refactored internally (no signature/behavior change) to call this. Semantic/description-based matching is explicitly not added — stays v0 exact-name+typo-suggestion on both bots, deferred to the planned RAG work. |
| Scope of matching | Per-`spec_type` only — a name that exists under a *different* `spec_type` than requested is reported as not found. Documented as a limitation in the tool's docstring, not fixed here (per-type name uniqueness is how `SpecCache`/`validate_spec` already work). |
| What `check_existing_spec` returns on a match | `name`/`description`/`source` only — **not** the spec's full definition. No spec class has a YAML/dict serialization method today (`to_yaml()` or equivalent doesn't exist on `MetricSpec`/`SliceSpec`/`SegmentSpec`), so there is nothing lossless to return yet; adding one is real, cross-cutting core-library work, out of scope here (noted as a future option below). |
| Preventing fabricated `existing_yaml` | The prompt explicitly forbids constructing `existing_yaml` from `check_existing_spec`'s summary or from memory. If the user wants a matched spec changed, the LLM asks the user to paste its current YAML. Drafting a "revision" from a 2–3-field paraphrase would silently invent whatever fields weren't in that paraphrase (numerator, `entities`, `values`, `join_keys`, ...) — exactly the failure mode this property exists to prevent. |
| Where it sits in the tool-call order | Advisory only — grouped with `list_tables`/`describe_table`/`date_range`/`column_distribution` as callable any time before `draft_spec`, prompted to be called first. `validate_spec` Check 3 remains the authoritative, late-binding conflict check, consistent with `commit_spec`'s existing "cache may have changed" posture (Plan 33). |
| Wildcard-vs-enumeration guidance | Prompt-only, not a new tool or a hard-coded cardinality threshold. `column_distribution` (Plan 37) already returns `distinct_count` and is already wired onto `DefinitionBot`. It only matters when the user hasn't already named the values they want — if they have, `DefinitionBot` enumerates exactly those; cardinality only decides the shape when `DefinitionBot` has to pick the value set itself. Soft, LLM-judgment guardrail, same shape as the Metric Precision Rule — not validation-enforced, since a rigid cutoff would misfire on legitimate small-but-open-ended or large-but-controlled cases. |
| Multi-table join-gap output | Reuse `status="refused"` + `narrative`/`reason` — no `DefinitionPayload` schema change. While exploring schema, if the needed columns aren't co-located on one table, refuse and recommend a backend view rather than draft a spec that's structurally impossible (every spec has exactly one `source:`) or silently wrong. Matches the project's minimal-status-enum philosophy and the Metric Precision Rule / Plan 37 SF-7 refusal precedent. |
| Docs timing | `docs/changelog.md` only — no `docs/api/agent.md` edits now, per the convention Plan 37's SF-9 established (release-time pass, not per-plan). |

---

## 2. Scope

**In scope:**
- SF-1 — Extract `SpecResolver.resolve_name` from `resolve()` (`aitaem/agent/resolver.py`); refactor `resolve()` to use it internally.
- SF-2 — New `check_existing_spec` tool (`aitaem/agent/definition_tools.py` + `definition_types.py`).
- SF-3 — Register `check_existing_spec` on `DefinitionBot`; Layer A prompt: call-it-first guidance, fabrication-prevention guidance.
- SF-4 — Layer A prompt: wildcard-vs-enumeration guidance for slices/segments, incl. a `SegmentSpec` wildcard YAML example.
- SF-5 — Layer A prompt: multi-table join-gap refusal guidance.
- SF-6 — Tests.
- SF-7 — Docs (changelog only).

**Out of scope:**
- Any semantic/embedding/RAG-based matching — `resolve_name` stays exact-match + typo-suggestion.
- Any change to `QueryBot`'s `resolve_intent`/`record_intent` behavior or prompt — SF-1's refactor is internals-only, verified by regression tests asserting identical `resolve()` output before/after.
- A structured `view_recommendation` field on `DefinitionPayload` (prose/`refused` only).
- A hard cardinality threshold enforced anywhere in code for wildcard-vs-enumeration.
- A `to_yaml()`/serialization method on spec classes that would let `check_existing_spec` return a matched spec's full definition — a real fix to the same underlying gap, but cross-cutting core-library work; the fabrication risk is closed here via prompt guidance instead (see Key decisions).
- `docs/api/agent.md` edits (deferred to release-time pass).

---

## 3. Sub-features

### SF-1 — Shared name-resolution primitive

**File:** `aitaem/agent/resolver.py`

```python
@dataclass
class NameResolution:
    exact: MetricSpec | SliceSpec | SegmentSpec | None
    suggestions: list[str]

class SpecResolver:
    @staticmethod
    def resolve_name(
        spec_type: Literal["metric", "slice", "segment"],
        proposed_name: str,
        spec_cache: SpecCache,
    ) -> NameResolution: ...
```

- Dispatches to `spec_cache.metrics` / `.slices` / `.segments` based on `spec_type`; exact match via dict `.get(proposed_name)`; on miss, `difflib.get_close_matches(proposed_name, <bucket>.keys(), n=3, cutoff=0.75)` for `suggestions` — same parameters `resolve()` uses today, generalized across all three spec types (today only the metric-name path gets suggestions on `resolve()`; the primitive supports it for slice/segment too, which only `check_existing_spec` will exercise).
- `resolve()`: internal-only refactor — its metric-name lookup and its `slice_name`/`segment_name` membership checks are rewritten to call `resolve_name(...)`.
- Execution downstream of a match (`QueryBot`/`DefinitionBot` `compute_metrics`) is unaffected by this refactor: both already execute via a fresh `spec_cache.get_metric(name)`-style lookup by name at call time (`_execute_metric_compute`/`MetricCompute.compute`), not by holding a reference from `resolve()`'s return value — so there's nothing for this refactor to accidentally decouple.
- Regression requirement (SF-6): `resolve()`'s observable output (exact_match/near_misses shapes, suggestion lists) byte-for-byte identical to pre-refactor behavior for every existing `test_resolver.py` case.

### SF-2 — `check_existing_spec` tool

**File:** `aitaem/agent/definition_tools.py`

```python
def check_existing_spec(
    ctx: RunContext[DefinitionDeps],
    spec_type: Literal["metric", "slice", "segment"],
    proposed_name: str,
) -> CheckExistingSpecResult:
```

- Calls `SpecResolver.resolve_name(spec_type, proposed_name, ctx.deps.spec_cache)`.
- On `.exact` found: `found=True`, `name`, `description`, `source` (metrics/segments only). Read-only — no draft/token minted.
- On miss: `found=False`, `suggestions`.
- Docstring covers: when to call it, that it's advisory only (`validate_spec` Check 3 is authoritative), that it only checks the given `spec_type`'s bucket, and that a match's `description`/`source` must not be used to construct `existing_yaml` — ask the user for the current YAML instead.

**File:** `aitaem/agent/definition_types.py`

```python
class CheckExistingSpecResult(BaseModel):
    spec_type: str
    proposed_name: str
    found: bool
    name: str | None = None
    description: str | None = None
    source: str | None = None
    suggestions: list[str] = []
```

### SF-3 — Wiring + guidance

**File:** `aitaem/agent/definition_bot.py`

- `_build_agent()`: register `check_existing_spec`.
- `_build_layer_a_definition()`: new section, before schema exploration — call `check_existing_spec` with a best-guess name first; if found, don't draft a duplicate, tell the user what exists by name/description; if they want changes, ask them to paste the spec's current YAML to pass as `existing_yaml` rather than reconstructing it from the tool's summary or from memory.

**File:** `aitaem/agent/definition_tools.py` module docstring — add `check_existing_spec` to the tool list, in the "callable any time before `draft_spec`" bucket alongside `date_range`/`compute_metrics`/`column_distribution`.

### SF-4 — Wildcard-vs-enumeration guidance

**File:** `aitaem/agent/definition_bot.py`, `_build_layer_a_definition()`

- New section: if the user names specific values, enumerate exactly those (`column_distribution` afterward only to catch typos/mismatches, not to override the shape). If the value set is unspecified, call `column_distribution` first: prefer a wildcard (`where:`) when distinct values are numerous, open-ended, or expected to grow; prefer enumerated `values` when the mapping isn't 1:1 with raw values (bucketing, numeric thresholds, a controlled vocabulary that should drop out-of-list values). Covers both `where:` forms — a single column and a list of columns for a cross-product segment (Plan 38) — since the existing YAML format reference has no segment-wildcard example of either form.
- Same section (or immediately after): add a `SegmentSpec` wildcard example, single- and multi-column, to the existing YAML format-reference block — today it has wildcard/composite examples for `SliceSpec` but none for `SegmentSpec`.

### SF-5 — Multi-table join-gap guidance

**File:** `aitaem/agent/definition_bot.py`, `_build_layer_a_definition()`

- New section: every spec has exactly one `source:` table; if `list_tables`/`describe_table` exploration shows the needed columns aren't all on one table, don't draft — refuse, naming the tables/columns, and recommend the user create a joined view combining them.

### SF-6 — Tests

- `tests/test_agent/test_resolver.py` — `resolve_name` direct tests (exact match per spec_type, suggestions on miss); full regression pass confirming `resolve()`'s public output is unchanged pre/post refactor.
- `tests/test_agent/test_definition_tools.py` — `check_existing_spec`: exact match per spec_type (asserting `description`/`source` echoed, not full YAML), near-miss suggestions, unknown `spec_type` handling, same-name-under-a-different-`spec_type` reports `found=False`.
- `tests/test_agent/test_definition_bot.py` — tool-registration set; Layer A prompt-content assertions for all three new sections, including the fabrication-prevention instruction and the new `SegmentSpec` wildcard YAML example.
- `tests/evals/test_definition_bot_evals.py` — one scripted-model `Case` calling `check_existing_spec` as part of the wiring dataset.
- `tests/evals/test_definition_bot_discovery_evals.py` (new, live-model, `skipif` no API key):
  - A request phrased differently from an existing metric's name but matching its description → `check_existing_spec` is called before `draft_spec`; no duplicate spec is drafted; narrative references the existing spec; no `record_definition_intent(existing_yaml=...)` call is made from the match summary alone.
  - A segment request against a high-cardinality DIM column, with no values named → `column_distribution` is called before drafting; the committed spec uses `where:` (wildcard).
  - A segment request where the user names specific values → the committed spec enumerates exactly those; `column_distribution`, if called, doesn't override the named shape.
  - A request needing columns from two different tables → no `draft_spec` call; `status="refused"`; narrative names both tables and recommends a view.

### SF-7 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Added`: `check_existing_spec`; wildcard-vs-enumeration and join-gap guidance (prompt behavior, noted as such).

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/agent/resolver.py` | SF-1: `NameResolution`, `SpecResolver.resolve_name`; `resolve()` internal refactor |
| `aitaem/agent/definition_tools.py` | SF-2: `check_existing_spec`; module docstring update |
| `aitaem/agent/definition_types.py` | SF-2: `CheckExistingSpecResult` |
| `aitaem/agent/definition_bot.py` | SF-3: tool registration + guidance; SF-4: wildcard-vs-enumeration prompt + YAML example; SF-5: join-gap prompt |
| `tests/test_agent/test_resolver.py` | SF-6: `resolve_name` tests + `resolve()` regression |
| `tests/test_agent/test_definition_tools.py` | SF-6: `check_existing_spec` tests |
| `tests/test_agent/test_definition_bot.py` | SF-6: registration + prompt-content tests |
| `tests/evals/test_definition_bot_evals.py` | SF-6: scripted wiring case |
| `tests/evals/test_definition_bot_discovery_evals.py` | SF-6: new — live-model discovery/guidance evals |
| `docs/changelog.md` | SF-7: `Unreleased` entry |

---

## 5. Sequencing note

Depends on Plan 38 for SF-4's `SegmentSpec` wildcard example and SF-6's wildcard-segment eval
case (both need Plan 38's `column` field to exist) — sequence after Plan 38, or hedge both with
"once available" wording if landing first.

Independent of Plan 40 in mechanism, but both edit the same three points: `_build_layer_a_definition()`,
the `definition_tools.py` module docstring, and the `toolset.add_function(...)` registration
block inside `definition_bot.py`'s `_build_agent()` (the actual tool-registration list — not
`definition_tools.py`, which only carries the descriptive docstring). That third point is the
one most likely to produce a real merge conflict, since both plans insert a new registration
line into the same block. Whichever of Plan 39/40 lands second rebases its insertions onto the
first.
