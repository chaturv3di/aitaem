# Plan 30 — Reconcile AD-16/AD-17 with Shipped `MetricCompute` Lifecycle

**Scope:** Primarily documentation — the shipped code is already correct; the architecture docs describing it are stale. SF-6 adds one regression test, verifying a claim the docs make about `MetricCompute`'s GC behavior rather than asserting it from reasoning alone.
**Depends on:** Plan 25 (`tmp-dir-to-connection-manager`), Plan 26 (`querybot-v0.2`) — both shipped, still in `## Unreleased`.
**Not in scope:** an MCP-server architecture doc doesn't exist anywhere in this repository — confirmed via the working tree and `git log --all` across every branch. Revisit AD-16/AD-17 references there once it's written.

---

## 0. Key Decision

**What's wrong:** `02-architectural-decisions.md`'s AD-16 and AD-17 — and their restatements across `ARCHITECTURE.md`, `03-component-architecture.md`, `04-aitaem-contracts.md`, and `08-implementation-order.md` — describe `QueryBot` holding one `MetricCompute` instance for its lifetime and forwarding AITAEM operational parameters via an opaque `compute_kwargs` dict. Neither is true of the shipped code.

**Confirmed against the code, not assumed:**
- `MetricCompute.__init__(self, spec_cache, connection_manager)` (`aitaem/insights.py:106`) — two required args only. No `tmp_dir`, no operational kwargs.
- `QueryBot.__init__` (`aitaem/agent/query_bot.py:276`) — no `compute_kwargs` parameter, holds no `MetricCompute` instance; only `_spec_cache`/`_connection_manager`.
- `compute_metrics()` (`aitaem/agent/query_tools.py:224`) constructs `MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)` fresh, inside the tool, on every call.
- `QueryBot`'s own docstring already documents this: *"Tools create a MetricCompute instance per call from the held spec_cache and connection_manager."*
- `DefinitionBot`/`SetupBot` never constructed `MetricCompute` — unaffected.

**Root cause:** AD-16's premise was that AITAEM v0.4.0's `tmp_dir` parameter made `MetricCompute` "effectively stateful — it now owns an on-disk resource." That's what forced bot-lifetime holding (AD-16) and opaque-dict forwarding (AD-17). `docs/changelog.md`'s `## Unreleased` → `### Breaking changes` (still unreleased, same development window as Plans 26/28/29):

> **`MetricCompute.__init__` `tmp_dir` parameter removed.** Pass `tmp_dir` to `ConnectionManager()` instead.

Plan 25 moved `tmp_dir` onto `ConnectionManager` — caller-owned, not bot-owned. `MetricCompute` is stateless again; AD-17 has nothing left to forward, since `MetricCompute` now takes only `spec_cache`/`connection_manager`, both already top-level bot constructor parameters. QueryBot's v0.2 redesign (Plan 26 / `09-querybot-v0.2-design.md` §4.4) adapted correctly. AD-16/AD-17 and every restatement were never revisited — Plan 25 itself never mentions the agent-module docs.

**AD-16 gave two reasons for bot-held `MetricCompute` — both are addressed by the same underlying change, not just the first:**

1. **tmp_dir / GC-reclaim.** Resolved above — `ConnectionManager` owns the tmp file now, not `MetricCompute`.
2. **Live Ibis refs would be invalidated if `MetricCompute` is torn down** (`02-architectural-decisions.md:202`, restated `04-aitaem-contracts.md:154`). This needs active disproof, not just "moot" — a reader would reasonably worry that constructing-then-discarding `MetricCompute` per call still risks invalidating a stored `ibis.Table` ref:
   - `MetricCompute.__init__` (`aitaem/insights.py:106-121`) stores only plain references (`self.spec_cache`, `self.connection_manager`) — no `__del__`, no owned resource, nothing to tear down on GC.
   - The cross-backend scratch DuckDB connection a multi-backend `ibis.Table` depends on lives in `ConnectionManager._cross_backend_conn` (`aitaem/connectors/connection.py:441-450`) — cached on `ConnectionManager`, torn down only by `close_all()` or `ConnectionManager.__del__` (line 499). Both scoped to `ConnectionManager`'s lifetime, which the bot holds persistently.
   - GC'ing the per-call `MetricCompute` wrapper touches none of that. This concern was never actually live once `MetricCompute` stopped owning the connection itself — it doesn't just become moot, it was never really at risk.
   - Backed by SF-6's regression test, not left as reasoning alone.
3. AD-16's original "bot-as-session (AD-04) alignment" framing is dropped, not carried forward — it was never a hard constraint, and there's nothing left for the bot to align lifecycles *with*.

**AD-12's own consequences section restates the same now-corrected claim** (`02-architectural-decisions.md:202`: "Live Ibis refs are valid only while the `MetricCompute` that produced them is alive... so refs are live for the bot's lifetime by construction") — same claim as AD-16's second rationale, different section, same fix: refs are live for `ConnectionManager`'s lifetime, not `MetricCompute`'s.

**Decision — update the docs to match shipped behavior; keep the AD-16/AD-17 IDs (avoid cross-reference churn); preserve original rationale as marked history, not silently delete it.** Same treatment Plan 29 gave `05-evals.md`'s stale field names and ND-09's resolution.

- **AD-16 (revised):** `MetricCompute` constructed fresh per `compute_metrics` call — not bot-held. It's a cheap, stateless wrapper now; no resource-lifecycle reason to hold it, and per-call construction removes any "does the held instance need invalidating" question entirely.
- **AD-17 (revised, dormant rather than retired):** `compute_kwargs` isn't part of `QueryBot`'s actual constructor today — `MetricCompute` has no operational parameters to forward. The opaque-dict *design* stays documented as reactivation guidance if AITAEM ever reintroduces an operational `MetricCompute` parameter: the original reasoning (opaque dict beats a typed wrapper, since the bot has no opinion on AITAEM's parameter surface) is still sound, just relabeled "not currently implemented" rather than "current behavior." (The alternative — retiring AD-17 outright with no reactivation guidance — was considered and rejected; dormant-with-guidance preserves more value.)

---

## 1. Scope

**In scope:**
- SF-1 — Rewrite AD-16 and AD-17 in `02-architectural-decisions.md`, including AD-12's stale restatement.
- SF-2 — Fix stale AD-16/AD-17 restatement sites in `ARCHITECTURE.md`.
- SF-3 — Fix stale AD-16/AD-17 restatement sites in `03-component-architecture.md`.
- SF-4 — Fix stale sites in `04-aitaem-contracts.md` — the densest file, and the only one with stale content that doesn't literally cite "AD-16"/"AD-17" (e.g. "v0.4.0 adds a `tmp_dir` parameter to `MetricCompute`" is flatly false post-Plan-25 but wouldn't be caught by an ID-only grep).
- SF-5 — Fix stale sites in `08-implementation-order.md`.
- SF-6 — New regression test proving live `ibis.Table` refs survive the per-call `MetricCompute`'s garbage collection.

**Out of scope:**
- Any change to production code paths — the shipped behavior is already correct; SF-6 is a new *test*, not a behavior change.
- `DefinitionBot`/`SetupBot` docs — never referenced `MetricCompute`/`compute_kwargs`, unaffected.
- Re-litigating whether per-call `MetricCompute` construction is the right design — it's already shipped; this plan documents and verifies reality, it doesn't redesign it.
- `docs/changelog.md` — Plan 25's `tmp_dir` changelog entry already exists and is accurate; no new entry needed for a pure architecture-doc correction with no user-facing behavior change.
- The MCP-server architecture doc — doesn't exist yet (see header).

---

## 2. Sub-features

### SF-1 — Rewrite AD-16 and AD-17 in `02-architectural-decisions.md`

**AD-16 — new text:**
- **Heading:** `AD-16: MetricCompute is constructed fresh per compute_metrics call — revised (Plan 30)`.
- **Superseded-context line:** the original decision (bot holds one instance for its lifetime) was made when v0.4.0's `tmp_dir` made `MetricCompute` stateful. Plan 25 moved `tmp_dir` to `ConnectionManager`, making it stateless again — decision revised accordingly; original context/rationale kept below as historical record.
- **Revised decision:** no bot-held `MetricCompute` instance; `compute_metrics` constructs `MetricCompute(ctx.deps.spec_cache, ctx.deps.connection_manager)` fresh on every call.
- **Revised rationale:** stateless wrapper around already-held references — nothing to construct once and reuse for. Addresses both of AD-16's original rationales (tmp_dir/GC and live-ibis-ref validity — see §0), not just the tmp_dir one.
- **Revised consequences:** no `self._metric_compute` on the bot; `bot.reset()` has nothing `MetricCompute`-related to rebuild; live `ibis.Table` refs remain valid for `ConnectionManager`'s lifetime (bot-held), independent of how often `MetricCompute` itself is constructed and discarded.
- Original AD-16 context/decision/rationale/consequences retained below the revision, marked "Original decision (v0.4.0-era, superseded above)."

**AD-12's consequences section gets the matching fix:** `02-architectural-decisions.md:202` — "Live Ibis refs are valid only while the `MetricCompute` that produced them is alive. AD-16 commits the bot to holding one `MetricCompute` instance for its lifetime, so refs are live for the bot's lifetime by construction." → "Live Ibis refs are valid only while the `ConnectionManager` that produced them is alive — bot-held for the bot's lifetime regardless of how `MetricCompute` (AD-16, revised) is scoped."

**AD-17 — new text:**
- **Heading:** `AD-17: compute_kwargs passthrough — dormant, not currently implemented (Plan 30)`.
- **Revised status line:** `MetricCompute` currently takes only `spec_cache`/`connection_manager` — both already top-level bot constructor parameters. No operational parameter exists today for `compute_kwargs` to forward, so `QueryBot` doesn't implement one.
- **Kept as reactivation guidance:** if AITAEM reintroduces an operational `MetricCompute` parameter, the original design reasoning (opaque dict beats a typed wrapper) still applies and should be reactivated, not re-derived. Original context/decision/rationale/consequences retained below, marked "Reactivation design, not current behavior."

### SF-2 — Fix `ARCHITECTURE.md`'s stale sites

- **Executive Summary (~line 40):** "AITAEM operational parameters pass through opaquely via `compute_kwargs: dict | None`..." → `MetricCompute` is constructed per call by `compute_metrics`; no operational-parameter passthrough exists today (AD-17 dormant).
- **Decisions table (~lines 108–109):** AD-16/AD-17 row text updated to SF-1's revised one-line summaries.
- **§4 AITAEM contracts (~lines 180, 183):** `MetricCompute(spec_cache, connection_manager, **compute_kwargs) — bot constructs one per its lifetime (AD-16).` → `MetricCompute(spec_cache, connection_manager) — constructed fresh per compute_metrics call (AD-16, revised).` Remove the `tmp_dir`/`compute_kwargs` forwarding line (183), or replace with a pointer to AD-17's dormant status.
- **§8 Phase 2 description (~line 311):** "`compute_metrics` against bot-held `MetricCompute` (AD-16)" → "`compute_metrics` constructing `MetricCompute` per call (AD-16, revised)."
- **OQ-4 "closed" note (~line 374):** light touch — describes historical reasoning accurate at the time v0.4.0 shipped. Append a trailing pointer rather than rewriting: "(AD-16/AD-17 revised since — see Plan 30.)"

### SF-3 — Fix `03-component-architecture.md`'s stale sites

- **Primitives-layer bot description (~line 76):** "For bots that compute metrics: a single `MetricCompute` instance held for the bot's lifetime (AD-16)" → "For bots that compute metrics: `MetricCompute` constructed fresh per compute call (AD-16, revised) — no long-lived instance held."
- **"Bot constructor shape — the universal pattern" (~lines 144–159):** remove `compute_kwargs: dict[str, Any] | None = None` from the shown constructor pattern — it's not part of any bot's actual constructor today. Replace the explanatory paragraph with a short note: AD-17 is dormant (no bot currently exposes `compute_kwargs`); see AD-17 for reactivation conditions.
- **QueryBot description (~line 165):** "Holds a `MetricCompute` instance at construction... reused across all tool calls in the bot's lifetime (AD-16)." → "Constructs a fresh `MetricCompute` per `compute_metrics` call (AD-16, revised), from the held `spec_cache`/`connection_manager`."
- **`compute_metrics` tool description (~line 196):** "Calls `.compute(...)` on the bot's held `MetricCompute` instance (AD-16); does not instantiate one per call." → inverted: "Constructs `MetricCompute(spec_cache, connection_manager)` fresh per call (AD-16, revised) — cheap, since `MetricCompute` no longer owns any on-disk resource (Plan 25 moved `tmp_dir` to `ConnectionManager`)."

### SF-4 — Fix `04-aitaem-contracts.md`'s stale sites (the densest file)

Confirmed via `grep -n "AD-16\|AD-17\|compute_kwargs\|tmp_dir.*MetricCompute\|MetricCompute.*tmp_dir" plans/agent_module/04-aitaem-contracts.md` plus manual reading — the ID-only grep misses some of these:

- **Line 10:** intro line framing "the v0.4.0 `tmp_dir` parameter to `MetricCompute` and its lifetime implications" as a topic the doc covers — reframe as historical (v0.4.0-era) context.
- **Line 28 (contracts table):** "holds one instance per bot (AD-16), constructed with `spec_cache`, `connection_manager`, and optionally `tmp_dir` and other operational kwargs (AD-17)" → constructed fresh per call, from `spec_cache`/`connection_manager` only.
- **Line 138:** "v0.4.0 adds a `tmp_dir` parameter to `MetricCompute`:" — flatly false post-Plan-25. Reframe as historical: v0.4.0 added it; Plan 25 subsequently moved it to `ConnectionManager`.
- **Line 141 (code block):** shown `MetricCompute(...)` construction example likely still includes `tmp_dir` — update to the two-arg form, or move the `tmp_dir` example to `ConnectionManager(tmp_dir=...)`.
- **Line 148:** "the file is reclaimed when the `MetricCompute` instance is garbage-collected" — the reclaim story belongs to `ConnectionManager` (`__del__`/`close_all()`) now.
- **Line 154:** the two-reasons rationale for bot-held `MetricCompute` — both corrected per §0 (tmp_dir/GC no longer applies; live-ibis-ref concern actively disproven).
- **Line 155:** `compute_kwargs` description — dormant, not current, per AD-17's revision.
- **Line 156:** "it forwards via `compute_kwargs={'tmp_dir': ...}` and is done" — this mechanism no longer exists; `tmp_dir` goes directly on `ConnectionManager()`, which the caller already constructs.
- **Lines 216–222 (component diagram/text block):** repeats the same `MetricCompute(spec_cache, connection_manager, **compute_kwargs)` / "held one instance per bot (AD-16)" / "forwarded via `compute_kwargs` (AD-17)" claims — update to match.

### SF-5 — Fix `08-implementation-order.md`'s stale sites

- **Line 15:** "AD-12, AD-16, AD-17 in Section 2 reflect the v0.4.0 reality." → no longer true for AD-16/AD-17 or AD-12's restatement — reword to note they were subsequently revised (Plan 30).
- **Line 64:** "The tool that calls `.compute(...)` on the bot's held `MetricCompute` instance (AD-16)." → "constructs `MetricCompute` fresh per call (AD-16, revised)."
- **Line 66:** "Construction of the `MetricCompute` itself happens in the `QueryBot` constructor (P2.4), using `spec_cache`, `connection_manager`, and `compute_kwargs` (AD-17)." — wrong on two counts now (construction happens per-call inside `compute_metrics`, not the constructor; no `compute_kwargs` exists) — rewrite to match.
- **Line 13** (optional, light touch): "Also delivered in v0.4.0: `tmp_dir` parameter for cross-backend scratch DuckDB." — accurate as history; a footnote noting the later move to `ConnectionManager` (Plan 25) is a nice-to-have, not required for consistency.

### SF-6 — Regression test: live `ibis.Table` refs survive per-call `MetricCompute`'s GC

**New/extended test** (likely `tests/test_agent/test_query_bot.py`; reuse multi-backend fixtures from `tests/test_connectors/test_connection_manager.py` if a cross-backend scenario is easy to stand up there — a single-backend variant is an acceptable fallback that still proves the core invariant, just not the cross-backend-scratch-DB path specifically):

- Construct a `QueryBot` with a real (or realistically faked) `ConnectionManager` that produces a lazy `ibis.Table` from `compute_metrics`.
- Call `compute_metrics` via the bot (through a `FunctionModel`-driven turn, or by invoking the tool function directly against a `QueryDeps` fixture). The tool's internal `MetricCompute` instance goes out of scope when the call returns.
- Force garbage collection (`gc.collect()`) after the call returns — makes "does discarding `MetricCompute` break anything" concrete, rather than relying on CPython's refcounting happening to run.
- Retrieve the stored entry from `ResultStore` via `bot.get_result(result_id)`, get its `ibis_ref`, execute it (e.g. `.to_pyarrow()`) — assert it succeeds and returns the expected data.

This directly verifies §0's disproof of AD-16's original second rationale, rather than leaving it as prose.

---

## 3. Ordering

SF-1 first — source of truth for what AD-16/AD-17 (and AD-12's restatement) now say. SF-2 through SF-5 restate SF-1's conclusions in each doc; all four can proceed in parallel once SF-1's wording is settled, and must stay textually consistent with it. SF-6 is independent of the doc work and can happen in parallel — it verifies a claim SF-1 makes, but doesn't depend on the doc edits landing first.

---

## 4. Verification

**Grep across all of `plans/`, not a fixed per-file checklist** — a fixed list is exactly how the original AD-16/AD-17 drift went unnoticed for two plans' worth of development, and how this plan's own first draft under-counted the affected files. Before considering SF-1–SF-5 done, run:

```
grep -rn "AD-16\|AD-17\|compute_kwargs" plans/
```

and separately, since some stale content doesn't cite the AD IDs at all:

```
grep -rn "MetricCompute" plans/ | grep -i "tmp_dir\|held\|instance per bot\|bot.s lifetime\|garbage.collect"
```

Every hit outside `02-architectural-decisions.md`'s revised AD-16/AD-17 text must be consistent with the revised decisions — either updated, or (for genuinely historical context, like `ARCHITECTURE.md`'s OQ-4 note) explicitly marked as describing a past state with a pointer to this plan. Zero hits should describe current behavior incorrectly. Run this grep again against the MCP-server architecture doc once it exists, before considering it consistent with the rest of the doc set.

**Additional verification:**
- Original decision rationale preserved as marked historical record in `02-architectural-decisions.md`, not deleted.
- SF-6's test passes — an executable check, not just prose, for the live-ibis-ref claim.

---

## 5. Files changed summary

| File | Change |
|---|---|
| `plans/agent_module/02-architectural-decisions.md` | SF-1: AD-16, AD-17 rewritten; AD-12's stale restatement (line 202) corrected; original text kept as marked history |
| `plans/agent_module/ARCHITECTURE.md` | SF-2: Executive Summary, decisions table, §4 AITAEM contracts, §8 Phase 2 description, OQ-4 note |
| `plans/agent_module/03-component-architecture.md` | SF-3: primitives-layer description, constructor-shape pattern + `compute_kwargs` block, QueryBot description, `compute_metrics` tool description |
| `plans/agent_module/04-aitaem-contracts.md` | SF-4: ~9 sites — the densest file, including content stale about `MetricCompute`/`tmp_dir` that doesn't cite the AD IDs |
| `plans/agent_module/08-implementation-order.md` | SF-5: 3 sites (v0.4.0-reality status line, `compute_metrics` tool description, QueryBot-construction description) |
| `tests/test_agent/test_query_bot.py` (or new file) | SF-6: new — GC-then-execute regression test |

---

## 6. Success criteria

- [ ] AD-16 describes the actual shipped `MetricCompute` lifecycle (constructed per call, not bot-held), and its rationale addresses **both** original reasons (tmp_dir/GC, and live-ibis-ref validity) — not just the tmp_dir one.
- [ ] AD-12's consequences section no longer claims live Ibis refs depend on `MetricCompute`'s lifetime.
- [ ] AD-17 clearly states `compute_kwargs` is not currently implemented, with reactivation guidance preserved rather than deleted.
- [ ] `grep -rn "AD-16\|AD-17\|compute_kwargs" plans/` and the `MetricCompute`/`tmp_dir`/"held"/"bot's lifetime" grep from §4 return zero hits that describe current behavior incorrectly, across every file in `plans/` — not just the five files enumerated above.
- [ ] SF-6's regression test passes: a live `ibis.Table` ref retrieved from `ResultStore` after its producing `MetricCompute` instance has been garbage-collected still executes correctly.
- [ ] A reader following "AD-16" or "AD-17" by ID through any of the five files sees one consistent story.
- [ ] Original decision context/rationale is preserved as marked historical record in `02-architectural-decisions.md`, not silently erased.
