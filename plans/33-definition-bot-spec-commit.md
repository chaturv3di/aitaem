# Plan 33 — DefinitionBot Spec Commit, Update, and Delete

**Scope:** Extends `DefinitionBot`, part of the released `v1.0.0` (Plan 32; `docs/changelog.md`'s `## v1.0.0`; tag `v1.0.0` on `main`). The semver contract in `docs/agent/stability.md` applies, and everything here is additive: two *new* tools (`commit_spec`, `delete_spec`, no schema change to the five existing ones), new *optional* fields on `DefinitionOutput`/`DefinitionPayload`, and prompt-copy tuning (SF-6, explicitly not semver-stable).

`docs/api/*.md` is not touched (see SF-8) — reference docs update at release-cut time (`CLAUDE.md` Release Process step 4); the changelog `Unreleased` entry and this plan doc are the record until then.

**Also touches `QueryBot`** (SF-9): a caller may share one `SpecCache` between a `DefinitionBot` and a `QueryBot`. Once `DefinitionBot` can mutate that shared instance, `QueryBot`'s own Layer B — built once at construction (`query_bot.py:315`) — silently diverges from the cache it's holding. SF-9 applies the same fix SF-5 applies to `DefinitionBot`: rebuild the agent when `SpecCache.version` moves, instead of only at construction.

---

## 0. The gap

`validate_spec` (`aitaem/agent/definition_tools.py:220-404`) mints a `spec_draft_token`, but the validated YAML only lands in `ResultStore` (`store.store_text(...)`, line 394) — nothing ever calls `spec_cache.add()`. The caller must manually parse the YAML and add it to their own `SpecCache`.

Worse for update/delete than add:
- `SpecCache.add()` (`loader.py:167-196`) always raises on a duplicate name — no overwrite path.
- There is no `remove()` — only `clear()`, which wipes everything.
- `validate_spec` already has a partial *update* concept (`record_definition_intent(existing_yaml=...)` sets `is_update=True` and locks the name, `definition_tools.py:46-92`, `274-289`) but nowhere to deliver it.

**Second, independent gap:** the spec catalog the LLM sees (Layer B, `_build_layer_b_definition`) is built once at `_build_agent()` time into a static `instructions=` string (`definition_bot.py:348-352`); only Layer C (today's date) is per-run dynamic. A spec committed on turn 1 of a `chat()` session stays invisible on turn 2 of the same bot instance — defeating the catalog's purpose ("all spec names are always listed to avoid name-conflict round-trips," `definition_bot.py:204`). Distinct from **ND-07** (`plans/agent_module/07-non-decisions.md:83-89`), which covers a caller updating specs upstream of the bot, not the bot losing sight of its own update.

---

## 1. Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Commit trigger | New LLM tool `commit_spec(spec_draft_token)`, not automatic on `validate_spec` success | A validated draft may go through corrections before the user wants it saved. `spec_draft_token` already persists across turns via `self._store` — no new registry needed. |
| Update mechanism | `SpecCache.update(spec)` — overwrites a same-name entry; raises `SpecNotFoundError` if the name doesn't exist | Distinct failure from `add()`'s "name conflict." |
| Add-vs-update dispatch | `commit_spec` derives it from live cache state at commit time (name present → `update()`, else `add()`), not from anything captured at `validate_spec` time | The cache can change between validate and commit (e.g. a parallel draft for the same name commits first, or a validated update's target is deleted before commit); deriving from live state always answers correctly and needs nothing persisted. |
| Delete workflow | Immediate single-call `delete_spec(spec_type, name)`, no draft/token/confirm step | A spec loaded via `from_yaml()`/`from_string()` can be recovered by reloading its source; one created purely in-session via `commit_spec` cannot. Acceptable because `delete_spec` is only ever called on explicit user request (SF-6), and `remove()`'s referential-integrity check still prevents a delete from corrupting other specs. |
| Referential integrity | `add()`/`update()`/`remove()` all route through one consolidated validator, not per-method checks: mutate the dict, run the existing full-cache validator (`_validate_slice_cross_references()`, `loader.py:310-343`, generalized into `_validate()`), roll back on failure | One "is the cache still self-consistent" pass after every mutation covers existence, non-nested-composite, and self-reference uniformly — including a leaf slice, referenced by an existing composite, being updated into a composite itself. Reuses `SpecValidationError`; no new exception type. `validate_spec` Check 4 (`definition_tools.py:313-317`) gets the same nested-composite check for early, draft-time rejection, but `SpecCache._validate()` is authoritative regardless. |
| SpecCache mutation signal | `version: int` on `SpecCache`, starting at 0; incremented once at the end of every successful `add()`/`update()`/`remove()`/`clear()` (after `_validate()` passes for the first three — not on rollback) | Cheap, monotonic "has the cache changed" signal; avoids diffing rendered Layer B text to detect staleness. |
| Layer B visibility | Layer B stays in the static `instructions=` string (not moved to `@agent.instructions`); `DefinitionBot`/`QueryBot` compare `self._spec_cache.version` against the version captured at the last agent build, once at the top of `ask()`/`chat()` before `self._agent.run(...)` starts, and rebuild `self._agent` only when it has moved | A fully dynamic Layer B is permanently excluded from pydantic_ai's cache-breakpoint region (detailed in the Anthropic prompt-cache impact row below), costing full input-token price every turn even when nothing mutated — in every deployment, not just sessions pairing a mutating `DefinitionBot` with a `QueryBot`. Layer B is piecewise-stable: identical between mutations. Gating the rebuild on `version` keeps turns byte-identical (and cache-eligible) except the one turn right after a commit. Checked once before the run starts: a spec committed mid-run becomes catalog-visible on the *next* turn, not intra-turn — within the same run, the LLM sees its own commit's outcome via the tool result, not an updated catalog. |
| Agent rebuild must not drop runtime-added tools | The rebuild path reuses the existing `self._toolset` instead of re-running `_build_agent()`'s toolset construction — guarded by `if self._toolset is None:` | `_build_agent()` populates the toolset from `self._tools` (constructor-time only); tools added later via `add_tool()` (`base.py:129-137`) live solely on the in-place-mutated `self._toolset` object. Re-running the unguarded construction on every version-triggered rebuild would silently drop them. |
| Reattaching the same `FunctionToolset` to a fresh `Agent(...)` | Safe — verified against installed pydantic-ai source, no wrap/mutate step | `Agent.__init__` stores toolsets by reference (`self._user_toolsets`, `pydantic_ai/agent/__init__.py:481`), not copied or bound. Every `run()` call — on any `Agent`, rebuilt or not — already wraps the same toolset instance in a fresh, ephemeral `CombinedToolset` (`agent/__init__.py:2469-2474`); this is the existing per-turn pattern, not something rebuild introduces. `AbstractToolset.for_run()`'s documented default is "shared across runs" (`toolsets/abstract.py:109-113`), and `FunctionToolset` overrides neither `for_run` nor `__aenter__`/`__aexit__` (no connection state to double-bind); `_id` is a display label only, not an identity registry. |
| Anthropic prompt-cache impact | Warm across no-mutation turns; one cache-miss/rewrite turn immediately after a commit, then warm again | `_get_instruction_parts` (`pydantic_ai/models/anthropic.py:1683-1725`; corroborated by the in-repo comment at `query_bot.py:328-333`) places the cache breakpoint after the last *static* instruction block. Layer A+B both stay static, so unchanged Layer A+B content (the common case) is cache-eligible across turns; a version bump changes Layer B's text, producing one miss, then stability resumes. |
| OpenAI prompt-cache impact | Accepted rough edge, not fixed here — applies identically to `QueryBot` | `openai_prompt_cache_key` is derived from a fingerprint of the cache once at construction and won't reflect mid-session commits. Routing hint, not a correctness mechanism; `version` is a future keying hook but wiring it in is out of scope. |
| Caller-side visibility | No new `DefinitionBot` accessor needed | `DefinitionBot.__init__` stores the caller's `SpecCache` by reference; mutating it in place is already visible through the caller's own variable, including a `QueryBot` holding the same instance. |
| Shared `SpecCache` across bots | Fix `QueryBot`'s Layer B too (SF-9), not just document a caveat | `QueryBot`'s tool-level machinery (`resolve_intent`/`compute_metrics`, `query_tools.py:126-245`) already reads `ctx.deps.spec_cache` live — a spec committed by `DefinitionBot` is immediately queryable. The gap is LLM-facing only: `QueryBot`'s static Layer B means its LLM never learns a newly committed spec exists. |

---

## 2. Scope

**In scope:**
- SF-1 — `SpecCache.update()`, `remove()`, transactional `add()`, a monotonic `version` counter, all via one consolidated validator; matching nested-composite check in `validate_spec` Check 4. No new exception type.
- SF-2 — `commit_spec` tool + `CommitSpecResult`. Add-vs-update derived from live cache state at commit time — no metadata persistence.
- SF-3 — `delete_spec` tool + `DeleteSpecResult`.
- SF-4 — Wire both tools into `DefinitionBot`; extend `DefinitionOutput`/`DefinitionPayload` with a structured commit/delete outcome.
- SF-5 — `DefinitionBot`: version-gated `self._agent` rebuild so Layer B reflects committed specs without losing Anthropic prompt-cache eligibility on unchanged turns.
- SF-6 — Prompt copy: commit_spec step, a "Deleting a spec" section, updated Final Response fields.
- SF-7 — Tests.
- SF-8 — Docs: changelog `Unreleased` entry; ND-07 amendment; `definition_tools.py` module docstring update.
- SF-9 — `QueryBot`: same version-gated rebuild as SF-5, mirrored, for the shared-`SpecCache` case.

**Out of scope:**
- Writing `SpecCache` back to a filesystem — stays in-memory, private per session.
- OpenAI prompt-cache-key staleness fix (both bots).
- Two-step confirm/preview for delete.
- Any change to `record_definition_intent`, `draft_spec`, or the rest of `validate_spec`'s gate logic beyond SF-1's Check 4 addition.
- Persisting `is_update`/`original_name` metadata at `validate_spec` time — superseded by the live-derivation dispatch in §1. `validate_spec`'s own Check 3 (`definition_tools.py:274-289`) is unaffected, since it operates synchronously within the same run via the ephemeral `DefinitionIntent`.

---

## 3. Sub-features

### SF-1 — `SpecCache.update()` / `remove()`, transactional `add()`, nested-composite check in `validate_spec`

**File:** `aitaem/specs/loader.py`
- `__init__` (`loader.py:94-98`): add `self.version: int = 0`, a public monotonic counter.
- Rename `_validate_slice_cross_references()` → `_validate()` (no logic change) — the single validation entry point, used by `from_yaml()`/`from_string()` (updated call sites) and now every mutator.
- `add()`: keep the existing duplicate-name precondition. Insert, call `self._validate()`; on exception, remove the inserted entry and re-raise; on success, `self.version += 1`.
- `update()`: raise `SpecNotFoundError` if the name isn't present. Save the existing entry, overwrite, call `self._validate()`; on exception, restore and re-raise; on success, `self.version += 1`.
- `remove(spec_type, name)`: raise `SpecNotFoundError` if absent. Pop, call `self._validate()`; on exception, restore and re-raise; on success, `self.version += 1`.
- `clear()` (`loader.py:243-247`): also `self.version += 1` — it's a mutation like any other, and the version signal should reflect any cache change regardless of source.
- All three mutators share the same mutate → validate → keep-and-bump-or-restore shape. Small `_bucket_for(spec)` / `_bucket_for_type(spec_type)` helpers avoid repeating the three-way type dispatch.
- No new exception type — rollback failures surface as the existing `SpecValidationError`.

**File:** `aitaem/agent/definition_tools.py`
- `validate_spec` Check 4 (`definition_tools.py:309-329`): reject a referenced name that resolves to an already-composite spec, alongside the existing missing-name check. Draft-time convenience only — `SpecCache._validate()` remains authoritative.

### SF-2 — `commit_spec` tool

**File:** `aitaem/agent/definition_tools.py`
```python
def commit_spec(ctx: RunContext[DefinitionDeps], spec_draft_token: str) -> CommitSpecResult
```
- Look up the token via `ctx.deps.store.get_text(...)`; missing/wrong-kind → `CommitSpecResult(error=...)`.
- Re-parse via `_parse_yaml_to_spec(metadata["spec_type"], entry.text)`.
- Check live existence: `spec.name in _get_spec_cache_bucket(spec_cache, spec_type)` (existing helper, `definition_tools.py:428-435`) → `spec_cache.update(spec)`; else → `spec_cache.add(spec)`.
- Routing to `update()` when the name already exists is a silent overwrite — no diff, no confirmation, no merge. Benign under ND-08's one-call-at-a-time-per-bot expectation, extended across a shared `DefinitionBot`/`QueryBot` pair (SF-8's ND-07 amendment): a race between two writers has the second commit silently win, with no warning.
- Catch `SpecValidationError`/`SpecNotFoundError` → `CommitSpecResult(error=...)`; these come only from `_validate()`'s cache-consistency checks (e.g. a nested-composite conflict introduced since `validate_spec` ran).
- Success → `CommitSpecResult(spec_type=..., spec_name=spec.name, action="added" | "updated")`.

**File:** `aitaem/agent/definition_types.py`
```python
class CommitSpecResult(BaseModel):
    spec_type: Literal["metric", "slice", "segment"] | None = None
    spec_name: str | None = None
    action: Literal["added", "updated"] | None = None
    error: str | None = None
```

### SF-3 — `delete_spec` tool

**File:** `aitaem/agent/definition_tools.py`
```python
def delete_spec(ctx: RunContext[DefinitionDeps], spec_type: Literal["metric","slice","segment"], name: str) -> DeleteSpecResult
```
- Calls `spec_cache.remove(spec_type, name)` directly — no draft/token step.
- Catches `SpecNotFoundError`/`SpecValidationError` → `DeleteSpecResult(deleted=False, error=...)`. The `SpecValidationError` message names the dependent composite and the broken reference.
- Success → `DeleteSpecResult(spec_type=spec_type, spec_name=name, deleted=True)`.

**File:** `aitaem/agent/definition_types.py`
```python
class DeleteSpecResult(BaseModel):
    spec_type: Literal["metric", "slice", "segment"]
    spec_name: str
    deleted: bool
    error: str | None = None
```

### SF-4 — Wire into `DefinitionBot`, extend structured output

**File:** `aitaem/agent/definition_bot.py`
- `_build_agent()`: register `commit_spec`/`delete_spec` on the `FunctionToolset` alongside the existing five tools.
- `DefinitionOutput` gains:
  ```python
  committed_spec_type: Literal["metric", "slice", "segment"] | None = None
  committed_spec_name: str | None = None
  committed_action: Literal["added", "updated", "deleted"] | None = None
  ```
  Set only when `commit_spec`/`delete_spec` succeeded during the run. `spec_draft_token` keeps its existing meaning (drafted-and-validated, not necessarily committed).
- `_assemble_payload()`: surface these three fields onto `DefinitionPayload` for non-LLM callers.

### SF-5 — `DefinitionBot` Layer B: version-gated rebuild

**File:** `aitaem/agent/definition_bot.py`
- `_build_agent()` (`definition_bot.py:332-375`): Layer A+B stay combined in `static_instructions` (`348-352`), unchanged. Guard the toolset construction (`337-346`) with `if self._toolset is None:` so a rebuild reuses the existing toolset — preserving anything added via `add_tool()` — instead of reconstructing it from `self._tools` alone. After computing `static_instructions`, set `self._layer_b_version = self._spec_cache.version`.
- `ask()` and `chat()` (`377-...`, `417-...`): at the top of each, before building `deps`, `if self._spec_cache.version != self._layer_b_version: self._agent = self._build_agent()`.
- Intra-turn boundary: same as the "Layer B visibility" decision row — a `commit_spec` mid-run bumps `version`, but that run's Layer B is already fixed; visibility starts turn N+1.
- No change to `_provider_cache_config_definition`'s Anthropic path — the cache-eligible region depends on the static string's content and its static/dynamic classification, not on when it was last computed.
- Conversation history (`self._message_history`, `base.py:88`) lives on the bot, not on the `Agent` instance, and is passed to `self._agent.run(...)` as a `message_history` kwarg (`definition_bot.py:440`) each call — a mid-session `self._agent` rebuild is history-invariant by construction.
- Reattaching `self._toolset` to the freshly constructed `Agent` in `_build_agent()` is safe — see the "Reattaching the same `FunctionToolset` to a fresh `Agent(...)`" decision row.

### SF-6 — Prompt copy

**File:** `aitaem/agent/definition_bot.py`, `_build_layer_a_definition()`
- "### Step 5 — commit_spec": call only after the user explicitly confirms they want the draft saved, passing `spec_draft_token`.
- "## Deleting a spec": call `delete_spec` only on explicit user request; note deletion isn't reversible within the session — confirm first if the request is ambiguous; relay a validation-failure message rather than retrying blindly.
- "## Final Response": document the three new optional `DefinitionOutput` fields.

### SF-7 — Tests

**Files:** `tests/test_specs/test_spec_loader.py` (`SpecCache` mutation), `tests/test_agent/test_definition_tools.py` (`commit_spec`/`delete_spec`), `tests/test_agent/test_definition_bot.py` (Layer B visibility, multi-turn commit), `tests/test_agent/test_definition_bot_smoke.py` (new — live-API cache-breakpoint guard)

- `SpecCache`: `update()` success/overwrite and missing-name; `update()`/`add()` reject a composite referencing a missing name or an already-composite slice; `update()` rejects self-reference; updating a leaf slice — referenced by an existing composite — into a composite itself, asserting rejection names the referrer; `remove()` success, missing-name, and blocked-by-dependent-composite (assert the dependent's name appears in the error). Every rejection case asserts the cache is restored to its exact pre-call state, not just that an exception was raised.
- `validate_spec` Check 4: a composite referencing an already-composite slice is rejected at validation time, not just later at commit.
- `commit_spec`: add path, update path, stale/unknown token; drift — validated as new but the name now exists at commit → routes to `update()`; drift — validated as an update but the target was deleted before commit → routes to `add()`; a nested-composite conflict introduced since `validate_spec` ran still surfaces as an error.
- `delete_spec`: success, unknown name, blocked-by-dependent-composite.
- Layer B rebuild: (a) two consecutive no-mutation turns reuse the same `self._agent` (byte-identical static instructions) — the cache-eligibility regression test; (b) commit a spec, assert the next turn rebuilds (`self._layer_b_version` matches the new `version`) and its rendered instructions include the new spec — the visibility regression test for §0's second gap; (c) a tool added via `add_tool()` before a commit is still present after the commit-triggered rebuild — regression test for the toolset-reuse guard.
- Multi-turn: draft+validate in turn 1, commit in turn 2 of the same `chat()` session, using only `spec_draft_token`.
- History survives a mid-`chat()` rebuild: commit a spec on one turn (forcing a rebuild on the next), then assert the following turn's model request still carries the prior turns' messages — the regression test for the bot-held-history invariant above.
- Cache-breakpoint guard (new `test_definition_bot_smoke.py`, live API, skipped without credentials — mirrors `test_query_bot_smoke.py::test_query_bot_smoke_prompt_cache_hit_on_turn_2`): two `chat()` turns with no `commit_spec`/`delete_spec` in between; assert `response2.trace.usage.cache_read_tokens > 0` on turn 2. The mocked-model tests above can confirm the agent wasn't rebuilt, but only a live call confirms Anthropic actually served Layers A+B from cache.

### SF-8 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Added`: `commit_spec`/`delete_spec`, `SpecCache.update()`/`remove()`. → `### Fixed`: `QueryBot`'s Layer B no longer goes stale when it shares a `SpecCache` with a `DefinitionBot` that commits/updates/deletes a spec (SF-9). No `docs/api/*.md` edits (see §2).
- `plans/agent_module/07-non-decisions.md`: amend ND-07 — "bot mutates its own held `SpecCache` and sees the change within the same instance" is now handled; ND-07's original scope ("caller updates specs upstream") stays deferred. Also note that ND-08's "one call at a time per bot" now extends across a shared `DefinitionBot`/`QueryBot` pair: a `commit_spec`/`delete_spec` call must not run concurrently with a `QueryBot` run on the same cache; serializing that is the caller's responsibility, per ND-08's existing escape valve.
- `definition_tools.py` module docstring: "Five tools in 4-step gate order" → seven tools; `commit_spec`/`delete_spec` described as tools 6–7, callable any time after `validate_spec` mints a token (or standalone, for `delete_spec`).

### SF-9 — `QueryBot` mirrors SF-5's version-gated rebuild

**File:** `aitaem/agent/query_bot.py`
- `_build_agent()` (`query_bot.py:293-350`): same treatment as SF-5 — guard the toolset construction (`298-310`) with `if self._toolset is None:`; Layer A+B stay combined in `static_instructions` (`315`, unchanged); after computing it, set `self._layer_b_version = self._spec_cache.version`.
- `chat()` and `ask()` (`352-...`, `396-...`): same top-of-method version check as SF-5.
- Comments at `query_bot.py:312-314` and `328-333` remain accurate under this design — no change needed.
- No change to `_provider_cache_config`'s Anthropic path — same mechanism as SF-5.
- Toolset reattachment safety: same as SF-5 — see the decision-table row.
- Intra-turn boundary: same as SF-5 — a spec committed by `DefinitionBot` mid-`QueryBot`-run doesn't apply within that `QueryBot` run; it becomes Layer-B-visible starting the `QueryBot`'s next turn.
- Test: mirrors SF-7's Layer B rebuild case — construct a `QueryBot` and `DefinitionBot` sharing one `SpecCache`, commit a spec via the `DefinitionBot`, assert the `QueryBot`'s next-turn rendered instructions include it.
- Cache-breakpoint guard: the existing `test_query_bot_smoke.py::test_query_bot_smoke_prompt_cache_hit_on_turn_2` (two `chat()` turns, no mutation, asserts `cache_read_tokens > 0` on turn 2) now also doubles as SF-9's regression guard — under the version-gated design, both turns see the same `spec_cache.version`, so `self._agent` is never rebuilt between them, the same code path this test has always exercised. Must keep passing unmodified as SF-9 lands.

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/specs/loader.py` | SF-1: `_validate()` (renamed); transactional `add()`/`update()`/`remove()`/`clear()`; `version` counter; bucket-dispatch helpers |
| `aitaem/agent/definition_tools.py` | SF-1: Check 4 nested-composite check; SF-2: `commit_spec`; SF-3: `delete_spec`; SF-8: module docstring |
| `aitaem/agent/definition_types.py` | SF-2: `CommitSpecResult`; SF-3: `DeleteSpecResult`; SF-4: `DefinitionOutput`/`DefinitionPayload` fields |
| `aitaem/agent/definition_bot.py` | SF-4: tool registration, `_assemble_payload`; SF-5: version-gated agent rebuild; SF-6: prompt copy |
| `aitaem/agent/query_bot.py` | SF-9: version-gated agent rebuild (mirrors SF-5) |
| `tests/test_specs/test_spec_loader.py` | SF-7: `SpecCache` mutation tests |
| `tests/test_agent/test_definition_tools.py` | SF-7: `commit_spec`/`delete_spec` tests |
| `tests/test_agent/test_definition_bot.py` | SF-7: Layer B visibility, multi-turn commit |
| `tests/test_agent/test_definition_bot_smoke.py` | SF-7: new — live-API cache-breakpoint guard |
| `tests/test_agent/test_query_bot.py` | SF-9: shared-`SpecCache` Layer B visibility test |
| `docs/changelog.md` | SF-8: Unreleased entry |
| `plans/agent_module/07-non-decisions.md` | SF-8: ND-07 amendment |
