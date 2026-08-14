# Plan 40 — DefinitionBot: Multi-Turn Spec Refinement (ND-10 Escape Valve)

**Scope:** Close the gap `plans/agent_module/07-non-decisions.md` ND-10 documents:
`DefinitionBot.chat()` accumulates message history across turns, but a spec's full YAML does
not — each turn re-derives it from the LLM's own memory, which is lossy for specs whose YAML
exceeds `draft_spec`'s 800-char preview truncation. Implement ND-10's own documented fix: a
`get_prior_spec(spec_draft_token)` tool that recovers the full validated YAML from
`ResultStore`, which already holds it durably across turns (`commit_spec` already reads a
`validate_spec` token across turns via `store.get_text()`). The gap is the absence of an
LLM-facing read path onto that data.

This plan is `DefinitionBot`-only. ND-10 separately floats an analogous
`get_prior_result(result_id)` for `QueryBot` (same structural gap: `spec_registry` is
per-run, `ResultStore`-backed results are not) — left out of scope as an independent
follow-on.

---

## 1. Key decisions

| Decision | Choice |
|---|---|
| What the tool exposes | `store.get_text(spec_draft_token)`. No new storage, no new lifetime semantics: `ResultStore` is already `self._store`, constructed once per bot instance and untouched across `chat()` turns. |
| Draft-registry (`draft_id`, pre-validation) parity | Not added — the LLM only ever needs the last *validated* YAML to revise from; an unvalidated intermediate draft is never the thing worth recovering. |
| Failure modes | Unknown token → not-found error. Token pointing at a `TabularEntry` → a distinct, kind-naming error — stricter than `commit_spec`'s current single `except` clause covering both cases with one generic message. The LLM needs to know whether to retry with a different token or stop entirely. |
| Prompt guidance | Layer A: when revising a spec from earlier in the conversation, call `get_prior_spec` with that turn's `validate_spec` token, rather than reconstructing YAML from memory. |
| Bucket placement | Token-gated, like `commit_spec` — not the no-token grounding-tools group (`date_range`/`compute_metrics`/`column_distribution`) and not standalone like `delete_spec`. |
| Stale-base / lost-update exposure | Not mitigated — flagged as an accepted, pre-existing risk this plan widens. `commit_spec`'s add-vs-update routing is derived purely from live `spec_cache` state at commit time, with no comparison against what the spec looked like when `get_prior_spec` last returned it (`test_commit_spec_drift_validated_new_but_now_exists_routes_to_update` already proves a silent overwrite happens today with no warning). Multi-turn `get_prior_spec` usage has an unbounded validate→commit gap, which only widens the window for this to bite — no detection/locking is added here, consistent with the project's existing late-binding "cache may have changed" posture (Plan 33, Plan 39's `check_existing_spec`). |
| ND-10 status | Marked implemented (`DefinitionBot`-side) in `plans/agent_module/07-non-decisions.md` and `ARCHITECTURE.md`'s Section 7 table; `QueryBot`'s `get_prior_result` stays explicitly deferred. |

---

## 2. Scope

**In scope:**
- SF-1 — `get_prior_spec` tool (`aitaem/agent/definition_tools.py` + `definition_types.py`).
- SF-2 — Register on `DefinitionBot`; Layer A prompt guidance, including the required `record_definition_intent` re-issue step.
- SF-3 — `plans/agent_module/` doc updates (ND-10 status).
- SF-4 — Tests.
- SF-5 — Docs (changelog).

**Out of scope:**
- `QueryBot`'s analogous `get_prior_result(result_id)`.
- Any change to `draft_spec`'s 800-char `yaml_preview` truncation.
- Any change to `ResultStore`'s lifetime/eviction semantics (none exist today; this plan makes long multi-turn sessions — and therefore more, larger, accumulated entries per session — a first-class supported pattern rather than an edge case, but doesn't add bounding).
- Stale-base/lost-update detection or locking (see Key decisions).
- A dedicated multi-turn refinement *workflow* tool (e.g. auto-diffing old vs. new YAML) — the LLM edits the retrieved text itself, same as any other draft.

---

## 3. Sub-features

### SF-1 — `get_prior_spec` tool

**File:** `aitaem/agent/definition_tools.py`

```python
def get_prior_spec(
    ctx: RunContext[DefinitionDeps],
    spec_draft_token: str,
) -> GetPriorSpecResult:
```

- `ctx.deps.store.get_text(spec_draft_token)` — same call site pattern as `commit_spec` (`definition_tools.py:543`).
- `KeyError` / `WrongEntryKindError` → `GetPriorSpecResult(error=...)`, with distinct messages (see Key decisions).
- On success: returns the full, untruncated `yaml_string`, plus `spec_type`/`spec_name`/`referenced_columns`/`warnings` — deserialized from `TextEntry.metadata` the same way `_assemble_payload` already does (`referenced_columns`/`warnings` are stored there as JSON-encoded strings, not native types; reuse its `json.loads` + `None`-guard handling rather than re-implementing it).

**File:** `aitaem/agent/definition_types.py`

```python
class GetPriorSpecResult(BaseModel):
    spec_draft_token: str
    spec_type: str | None = None
    spec_name: str | None = None
    yaml_string: str | None = None
    referenced_columns: dict[str, list[str]] | None = None
    warnings: list[str] = []
    error: str | None = None
```

### SF-2 — Wiring + prompt guidance

**File:** `aitaem/agent/definition_bot.py`

- `_build_agent()`: register `get_prior_spec`.
- `_build_layer_a_definition()`: new section — to revise a spec from earlier in the conversation: call `get_prior_spec(spec_draft_token)` (the token from that turn's `validate_spec` result) to retrieve the full YAML; edit it; **re-call `record_definition_intent(existing_yaml=<edited yaml>)` before calling `draft_spec`/`validate_spec` again** — required because `definition_intent` (including `is_update`/name-lock state) doesn't survive across turns, so skipping this re-call routes the revision through `validate_spec`'s new-spec path instead of its update/name-lock path, risking either a spurious name-conflict rejection or, if the LLM also changed the name while revising, a silent duplicate `add()` instead of the intended `update()`.

**File:** `aitaem/agent/definition_tools.py` module docstring — add `get_prior_spec` to the tool list, grouped with `commit_spec` (token-gated), not the no-token grounding tools or standalone `delete_spec`.

### SF-3 — Non-decision doc updates

**File:** `plans/agent_module/07-non-decisions.md`, ND-10 — status line: escape valve implemented for `DefinitionBot` via `get_prior_spec`; `QueryBot`'s `get_prior_result` remains deferred.

**File:** `plans/agent_module/ARCHITECTURE.md`, Section 7 table (ND-10 row) — same update.

### SF-4 — Tests

- `tests/test_agent/test_definition_tools.py` — `get_prior_spec`: success (round-trips exact YAML + metadata, including YAML longer than 800 chars), unknown token, token pointing at a `TabularEntry` (distinct error), a token that was already used to `commit_spec` still resolves (nothing deletes the store entry).
- `tests/test_agent/test_definition_bot.py`:
  - Tool-registration set updated.
  - Scripted multi-turn test (`FunctionModel`, following the `_make_multi_turn_commit_model`/`_collect_tool_returns` pattern): turn 1 drafts + validates a spec whose YAML exceeds 800 chars; turn 2 calls `get_prior_spec(token)` and asserts `yaml_string` is exactly equal to what was validated in turn 1 (not the truncated preview). Use a source that doesn't trigger `_normalize_source_in_yaml`'s rewrite, so "what was validated" is unambiguous.
  - Full revise-and-recommit regression: turn 1 validates and commits a spec (`is_update=False`); turn 2 retrieves it via `get_prior_spec`, edits a field, re-issues `record_definition_intent(existing_yaml=...)`, re-drafts, re-validates, re-commits — asserts the result is `commit_spec`'s `update()` path (same catalog entry revised), not a duplicate `add()`.
  - Layer A prompt-content assertion for the new guidance section, including the `record_definition_intent` re-issue instruction.

### SF-5 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Added`: `get_prior_spec`, DefinitionBot multi-turn spec revision.

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/agent/definition_tools.py` | SF-1: `get_prior_spec`; module docstring update |
| `aitaem/agent/definition_types.py` | SF-1: `GetPriorSpecResult` |
| `aitaem/agent/definition_bot.py` | SF-2: tool registration + prompt guidance (incl. `record_definition_intent` re-issue step) |
| `plans/agent_module/07-non-decisions.md` | SF-3: ND-10 status update |
| `plans/agent_module/ARCHITECTURE.md` | SF-3: ND-10 row update |
| `tests/test_agent/test_definition_tools.py` | SF-4: `get_prior_spec` tests |
| `tests/test_agent/test_definition_bot.py` | SF-4: multi-turn regression tests, registration, prompt test |
| `docs/changelog.md` | SF-5: `Unreleased` entry |

---

## 5. Sequencing note

Independent of Plan 38. Overlaps Plan 39 in three shared edit points: `_build_layer_a_definition()`,
the `definition_tools.py` module docstring, and the `toolset.add_function(...)` registration
block inside `definition_bot.py`'s `_build_agent()` — the last is the most likely real merge
conflict, since both plans insert a new registration line into the same block. Whichever of
Plan 39/40 lands second rebases its insertions onto the first.
