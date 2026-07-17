# Plan 28 — Phase 5.2: Composition Primitives

**Prerequisites:** Plan 23 (Phase 1), Plan 24 (Phase 2 QueryBot), Plan 26 (QueryBot v0.2), and
Plan 27 (Phase 3 DefinitionBot) are fully implemented and passing.

**Architecture reference:** `plans/agent_module/ARCHITECTURE.md` Section 2 (AD-11), Section 6
(EP1), Section 7 (ND-11), Section 8 (Phase 5.2). See `plans/agent_module/07-non-decisions.md`
ND-11 for why `Bot.as_tool()` / `add_bot()` are out of scope here.

**Reordering note:** `08-implementation-order.md` normally sequences Phase 5 after Phase 4
(SetupBot). This plan is intentionally pulled ahead of Phase 4 and ahead of Phase 5.1
(`Bot.as_tool()`, deferred per ND-11) at the user's direction, for two reasons:

1. Comprehensive user testing of `tools=`/`add_tool()`/`extra_tools=` hasn't happened yet —
   this plan is what establishes whether bot composition is shippable at all, even as an MVP.
   Sequencing it before Phase 4 gets that signal sooner rather than after sinking more work into
   a fourth bot.
2. `SetupBot`'s utility is not clear at this time. It isn't being cancelled — it's simply not
   assumed necessary by default. Phase 4 is revisited if there's an explicit ask for it.

It applies only to `QueryBot` and `DefinitionBot` — the two convenience bots that exist in the
codebase today. If `SetupBot` is built later, it inherits this mechanism from `Bot` with no
extra composition work.

---

## Decisions Resolved (Pre-flight)

| Question | Decision |
|---|---|
| **Scope** | Implement `Bot(tools=[...])`, `Bot.add_tool()`, and per-call `extra_tools=[...]` on `chat()`/`ask()`. **`add_bot()` and `as_tool()` are out of scope** — deferred per ND-11. |
| **Which bots** | `QueryBot` and `DefinitionBot` only. `SetupBot` (Phase 4) doesn't exist yet. |
| **Where `add_tool()` mutates** | Each bot's `_build_agent()` stores the `FunctionToolset` it constructs as `self._toolset` (new attribute on `Bot`). `add_tool()` mutates `self._toolset` in place. **Verified empirically** (see below): mutating a `FunctionToolset` after `Agent(toolsets=[toolset])` construction is picked up by that same `Agent` instance's next `.run()` call — no agent rebuild needed. |
| **How `extra_tools` reaches `agent.run()`** | **Verified empirically:** `Agent.run(toolsets=[...])` is *additive* to construction-time toolsets, not a replacement. `chat()`/`ask()` build a fresh ephemeral `FunctionToolset` from `extra_tools` and pass it as `toolsets=[ephemeral]`; the bot's own persistent toolset does not need to be re-listed. |
| **Accepting both plain functions and `Tool` instances** | One dispatch rule, reused everywhere a tool is registered: `isinstance(tool, pydantic_ai.Tool)` → `toolset.add_tool(tool)`; otherwise (plain callable) → `toolset.add_function(tool)`. |
| **Persistence semantics** | `tools=[...]` (constructor) and `add_tool()` are **persistent** — folded into `self._toolset`, present on every subsequent `chat()`/`ask()` call. `extra_tools=[...]` is **ephemeral** — scoped to one call, never touches `self._toolset`. |
| **Enforcing the `self._toolset` contract** | `Bot.__init__` checks `self._toolset is not None` immediately after `_build_agent()` returns and raises `TypeError` (naming the offending subclass) if a subclass forgot to set it. Fails at `Bot(...)` construction time, not lazily at first `add_tool()` call with a bare `AttributeError` on `None`. Explicit `if`/`raise`, not `assert` — asserts are stripped under `-O`. |
| **`add_bot()` stub in `base.py`** | Left as-is (`raise NotImplementedError` inside `as_tool()`; `add_bot()` is already correctly wired as `self.add_tool(bot.as_tool())` from Phase 1). No code change to `add_bot()` in this plan — it becomes reachable once a future plan implements `as_tool()` per ND-11's trigger condition. |
| **Tool-name collisions** | **Fail loud — no collision prevention.** pydantic-ai already raises `pydantic_ai.exceptions.UserError` on every collision case (verified empirically, see below); this plan lets that propagate unmodified rather than adding a `PrefixedToolset`-based auto-namespacing layer. Consistent with this architecture's existing no-silent-workaround stance (e.g. the Metric Precision Rule refuses rather than substitutes). A collision is a caller mistake; renaming a tool out from under the LLM would trade a clear error for a confusing one (LLM calling a tool under a name the caller never specified). See SF-2/SF-3/SF-4/SF-5/SF-6 below for where each case's `UserError` surfaces. |
| **Sync vs. async custom tools** | `_register_tool()` needs no sync/async branching — pydantic-ai's `Tool`/`FunctionToolset.add_function()` handle both uniformly (**verified empirically**, see the "Test fixture convention" section below). Since every existing default tool is async, this plan's tests explicitly parametrize invocation-level tests over a sync and an async fixture to avoid shipping the sync path with zero coverage. |
| **`add_tool()` vs. `dump_history()`/`load_history()`** | `load_history()` reconstructs a bot via `cls(**kwargs)` — a fresh `_build_agent()` call. Tools added via `add_tool()` on the original bot are not part of `**kwargs` and are silently absent after reload; this predates this plan (`load_history()` already existed) but was undocumented and untested. **Decision:** track runtime-added tool *names* (not the callables — those aren't portably serializable, same reasoning AD-05 already applies to open connections/lambdas in the result store) and `warnings.warn()` at `load_history()` time if any are missing after reload. See SF-7. This directly addresses the EP1 audit-logging use case, where a tool added post-construction silently vanishing across a dump/load boundary (e.g. a process restart) would be a serious, easy-to-miss gap. |

**Empirical verification performed during planning** (not part of the implementation, recorded
here so the "how" step doesn't have to re-derive it): using `pydantic_ai.models.function.FunctionModel`,
confirmed that (a) `agent._user_toolsets[0] is toolset` holds after construction — the `Agent`
keeps the same object, not a copy; (b) calling `toolset.add_function(new_fn)` *after* `Agent(...)`
construction, then calling `.run()` again on the *same* `Agent` instance, surfaces `new_fn` to the
model; (c) passing `toolsets=[extra_toolset]` to `agent.run()` surfaces the union of the
construction-time toolset's tools and the extra toolset's tools, not just the extra ones; (d)
`FunctionToolset.add_tool()` raises `UserError("Tool name conflicts with existing tool: '<name>'")`
synchronously when registering a name already present in *that same* toolset — this is the path
both `add_function()` and this plan's `_register_tool()` dispatch go through, so it covers
same-toolset collisions uniformly; (e) combining two *different* `FunctionToolset` objects with
an overlapping name via `agent.run(toolsets=[...])` raises a distinctly-worded `UserError`
(`"FunctionToolset defines a tool whose name conflicts with existing tool from FunctionToolset:
'<name>'. Rename the tool or wrap the toolset in a PrefixedToolset..."`) at `.run()` time, not at
toolset-construction time.

### Where each collision case's error surfaces

| Case | Where the collision is registered | Error path |
|---|---|---|
| (a) `tools=[fn]` collides with a default tool | `_build_agent()`'s `for tool in self._tools: _register_tool(toolset, tool)` loop (SF-3/SF-4), called from `Bot.__init__` | `UserError` raised synchronously, **uncaught** — propagates out of `QueryBot(...)`/`DefinitionBot(...)` construction. The bot object is never created. |
| (b) `add_tool(fn)` collides with an already-registered tool (default, prior `tools=[...]`, or prior `add_tool()`) | `Bot.add_tool()` → `_register_tool(self._toolset, fn)` (SF-2) | `UserError` raised synchronously, **uncaught** — propagates directly out of the `bot.add_tool(fn)` call. |
| (c) `extra_tools=[fn]` collides with a persistent tool | Ephemeral toolset combined with `self._toolset` inside `self._agent.run(..., toolsets=[ephemeral])` (SF-5/SF-6) | `UserError` raised inside `agent.run()`, which is already inside `chat()`/`ask()`'s existing `try/except Exception` block — **caught**, surfaces as `BotResponse(status=error, reason=str(exc))` via the existing `_error_response()` path. No code change needed for this to work correctly; it already does, by virtue of the existing exception handling in `chat()`/`ask()`. |

Cases (a) and (b) crash loudly by design (fail at the point of the caller's mistake). Case (c)
degrades to a normal-looking error response by the *pre-existing* behavior of `chat()`/`ask()`
(their docstrings already commit to "always returns a Response, never raises") — this plan does
not change that contract, it just needs to verify a `UserError` from a tool collision takes the
same path as any other tool-time exception.

---

## Terminology

| Term | Meaning |
|---|---|
| **Persistent toolset** | `self._toolset` — the `FunctionToolset` a bot holds for its lifetime; folds in default tools + constructor `tools=[...]` + anything added via `add_tool()`. |
| **Ephemeral toolset** | A `FunctionToolset` built fresh inside a single `chat()`/`ask()` call from `extra_tools=[...]`, passed via `agent.run(toolsets=[...])`, discarded after the call returns. |

---

## Test fixture convention: sync and async custom tools

Every default tool on `QueryBot` and `DefinitionBot` is async (`compute_metrics`, `rank_by_value`,
etc.). If this plan's tests for `tools=[...]`, `add_tool()`, and `extra_tools=[...]` only ever
register async fixture functions, the sync-callable path through `_register_tool()` →
`FunctionToolset.add_function()` ships with **zero test coverage**, despite being explicitly
in-contract — EP1 describes attaching "their own logic" with no async requirement, and the
`tool: Any` / `fn: Any` signatures don't constrain it either.

**Verified empirically:** a `FunctionModel`-driven `Agent` with both a sync and an async function
registered on the same `FunctionToolset` correctly invoked each and received the correct return
value from both — pydantic-ai's `Tool` class handles the sync/async distinction internally;
`_register_tool()` requires no sync/async branching of its own.

**Convention for this plan's tests:** wherever a test registers a custom tool and then verifies
it is actually *invoked* by the model (not just present in the schema), the test fixture is
parametrized over a sync and an async variant of the same tool (e.g. `custom_tool_sync()` /
`custom_tool_async()`, both returning the same literal so the assertion is identical). Tests that
only check registration bookkeeping (toolset membership, object identity, collision detection,
list assignment) are not parametrized this way — sync/async is irrelevant to those code paths.
Each Sub-Feature below marks which of its tests carry this parametrization.

---

## File Structure

### Modified files
```
aitaem/agent/base.py                    # SF-1, SF-2, SF-7: store tools, _toolset contract, add_tool(), runtime-added tracking + load_history() warning
aitaem/agent/history.py                 # SF-7: make_bundle()/load_bundle() gain runtime_added_tool_names
aitaem/agent/query_bot.py               # SF-3, SF-5: wire tools=/_toolset and extra_tools=
aitaem/agent/definition_bot.py          # SF-4, SF-6: wire tools=/_toolset and extra_tools=
tests/test_agent/test_definition_bot.py # SF-4: update internal-API test call site
tests/test_agent/test_history.py        # SF-7: runtime_added_tool_names round-trip + warning tests
docs/changelog.md                       # SF-9: Unreleased entry
plans/agent_module/08-implementation-order.md  # SF-11: mark P5.2 implemented, link this plan
```

### New files
```
tests/test_agent/test_composition.py    # SF-10: composition-surface test suite
```

No new public symbols are introduced (see SF-8), so `aitaem/agent/__init__.py` and
`docs/api/` are unaffected.

---

## Implementation Sub-Features

### SF-1: `Bot.__init__` — store `tools`; add the `_toolset` contract

`Bot.__init__` currently accepts `tools` but never assigns it to `self`, and `_build_agent()`
has no way to see it. Store it before `_build_agent()` runs, and reserve a `self._toolset` slot
that subclasses are contractually required to populate:

```python
def __init__(
    self,
    *,
    model: str,
    tools: list[Any] | None = None,
) -> None:
    self._model = model
    self._tools: list[Any] = list(tools or [])
    self._store = ResultStore()
    self._message_history: list[Any] = []
    # Contract: subclass _build_agent() MUST assign a FunctionToolset here before
    # returning — enforced by the check below. If you're reading this in a
    # debugger because self._toolset is None, your _build_agent() didn't set it.
    self._toolset: Any = None
    self._agent = self._build_agent()
    if self._toolset is None:
        raise TypeError(
            f"{type(self).__name__}._build_agent() did not set self._toolset. "
            "Concrete Bot subclasses must build a FunctionToolset, register "
            "self._tools onto it, and assign it to self._toolset before "
            "returning the Agent."
        )
```

**Post-init contract check.** `self._toolset` starts as `None` and must be set by the
subclass's `_build_agent()`. Without a check, a subclass that forgets to set it fails silently
at construction time and only surfaces as a bare `AttributeError: 'NoneType' object has no
attribute 'add_tool'` the first time someone calls `add_tool()` — far from where the actual
mistake was made, and uninformative about the fix. The check above runs immediately after
`_build_agent()` returns (inside `__init__`, not lazily inside `add_tool()`), so a subclass that
violates the contract fails at `Bot(...)` construction time, naming the offending class and the
missing step. Implemented as an explicit `if`/`raise` rather than a bare `assert` — asserts are
stripped under Python's `-O` flag, which would silently defeat this check for library consumers
running the interpreter with optimizations on.

Update the class docstring's "standard pattern" example (currently shows `_build_agent()`
returning `pydantic_ai.Agent(...)` directly with no toolset handling) to document the new
contract: subclasses build a `FunctionToolset`, register `self._tools` onto it via
`_register_tool()` (SF-2), assign the toolset to `self._toolset`, then construct and return the
`Agent` with `toolsets=[self._toolset]`. Note in the docstring that skipping this step raises
`TypeError` at construction.

#### Tests
- `Bot.__init__` with `tools=[some_fn]` results in `self._tools == [some_fn]` on a concrete subclass
- `Bot.__init__` with `tools=None` results in `self._tools == []`
- A minimal `Bot` subclass whose `_build_agent()` does **not** set `self._toolset` raises
  `TypeError` on construction, with the class's own name in the message (not a generic
  `AttributeError` deferred to first `add_tool()` call)
- `QueryBot` and `DefinitionBot` construct without raising (once SF-3/SF-4 land) — regression
  guard that the real subclasses satisfy the contract

---

### SF-2: Shared tool-registration helper and `Bot.add_tool()`

Module-level helper in `base.py`, used by `add_tool()` and by both bots' `_build_agent()` /
`chat()` / `ask()`:

```python
def _register_tool(toolset: Any, tool: Any) -> None:
    """Add one tool (plain callable or pydantic-ai Tool instance) to a FunctionToolset."""
```
Dispatch: `isinstance(tool, pydantic_ai.Tool)` → `toolset.add_tool(tool)`; else → `toolset.add_function(tool)`.

`Bot.add_tool()` (replaces the current `NotImplementedError` stub):
```python
def add_tool(self, tool: Any) -> None:
    """Add a tool to this bot's persistent tool set at runtime.

    Takes effect on the next chat()/ask() call. Mutations during an
    in-progress run() are undefined.
    """
```
Body is `_register_tool(self._toolset, tool)`.

#### Tests
- `add_tool(plain_function)` — a subsequent `ask()` call actually invokes the registered
  function and its return value reaches the model, tested for both a **sync** and an **async**
  fixture function (sync/async parametrization; see "Test fixture convention" above)
- `add_tool(pydantic_ai.Tool(...))` — a `Tool` instance is also accepted (single variant;
  Tool-vs-callable dispatch is orthogonal to sync/async, not parametrized)
- `add_tool()` called between two `chat()` calls — turn 1's model sees the tool absent, turn 2's sees it present
- `add_tool()` mutates `bot._toolset` in place (same object identity before/after)
- **Collision case (b):** `add_tool(fn)` where `fn.__name__` matches an already-registered tool
  (a default tool, a prior `tools=[...]` entry, or a prior `add_tool()` call) raises
  `pydantic_ai.exceptions.UserError`, uncaught, directly from the `add_tool()` call — assert with
  `pytest.raises(UserError, match="conflicts with existing tool")`
- Collision above leaves `bot._toolset` unchanged (the pre-existing tool is not evicted, no
  partial mutation) — verify tool count / registered names before and after the raised call

---

### SF-3: Wire `tools=` and `_toolset` into `QueryBot._build_agent()`

After the existing default-tool registration block (`toolset.add_function(record_intent)` through
`toolset.add_function(contribution_share)`), register constructor-supplied tools and save the
toolset reference:

```python
for tool in self._tools:
    _register_tool(toolset, tool)
...
self._toolset = toolset
return agent
```

No change to `QueryBot.__init__`'s signature — `tools` is already accepted and forwarded to
`super().__init__()`.

#### Tests
- `QueryBot(..., tools=[custom_fn])` — `custom_fn` appears in `bot._toolset.tools` alongside the eight default tools (registration/schema check; single variant)
- `QueryBot(..., tools=[custom_fn])` followed by `ask(...)` — the model actually invokes
  `custom_fn` and its return value reaches the response, tested for both a **sync** and an
  **async** `custom_fn` (sync/async parametrization — this is the invocation-level check the
  membership check above doesn't cover, and it establishes behaviorally that `self._toolset` is
  the live object driving the agent, without reaching into pydantic-ai's private
  `_user_toolsets` — the coupling SF-4 separately removes from the pre-existing
  `test_definition_bot_agent_has_five_tools`. No identity-check-via-private-attribute test is
  included here; the invocation test already proves the same thing through public behavior.)
- **Collision case (a):** `QueryBot(..., tools=[fn])` where `fn.__name__` equals a default tool
  name (e.g. `"compute_metrics"`) raises `pydantic_ai.exceptions.UserError` out of the
  `QueryBot(...)` constructor call itself — `pytest.raises(UserError)` wrapping the constructor
  call, not a later method call
- Collision above also covers two entries within `tools=[...]` colliding with each other
  (same underlying `_register_tool()` loop, second entry raises on the same name as the first)

---

### SF-4: Wire `tools=` and `_toolset` into `DefinitionBot._build_agent()`

Same change as SF-3, applied to `DefinitionBot._build_agent()`'s five-tool `FunctionToolset`
block.

Also update the existing test `test_definition_bot_agent_has_five_tools`
(`tests/test_agent/test_definition_bot.py:205-213`), which currently reaches into
`bot._agent._user_toolsets[0].tools.keys()` (a pydantic-ai internal attribute) — the assertion
becomes `bot._toolset.tools.keys()`, same check, no longer coupled to a private API now that the
bot holds its own reference.

#### Tests
Mirror SF-3's tests (including the collision case) against `DefinitionBot`, plus the
`test_definition_bot_agent_has_five_tools` update above.

---

### SF-5: Per-call `extra_tools=[...]` on `QueryBot.chat()` / `QueryBot.ask()`

Both methods already accept `extra_tools` in their signature but never read it. When
`extra_tools` is non-empty, build an ephemeral `FunctionToolset` (via `_register_tool()` per
entry) and pass `toolsets=[ephemeral]` to `self._agent.run(...)`. When `extra_tools` is
`None`/empty, omit `toolsets` from the call entirely — preserves current behavior byte-for-byte.

Signatures are unchanged:
```python
async def chat(self, message: str, *, extra_tools: list[Any] | None = None) -> QueryResponse
async def ask(self, message: str, *, extra_tools: list[Any] | None = None) -> QueryResponse
```

#### Tests
- `ask(..., extra_tools=[fn])` — the model actually invokes `fn` and its return value reaches
  the response, tested for both a **sync** and an **async** `fn` (sync/async parametrization)
- `chat(..., extra_tools=[fn])` on turn 1, plain `chat(...)` on turn 2 — turn 2 does not see `fn` (ephemeral, never persisted to `self._toolset`; single variant, timing behavior doesn't depend on sync/async)
- `ask()`/`chat()` with `extra_tools=None` — unchanged from pre-plan behavior (regression guard against existing `test_query_bot*.py` suites)
- **Collision case (c):** `ask(..., extra_tools=[fn])` / `chat(..., extra_tools=[fn])` where
  `fn.__name__` matches a persistent tool (default or previously `add_tool()`-ed) does **not**
  raise to the caller — it returns a `QueryResponse` with `status=Status.error` and
  `reason` containing the underlying `UserError`'s message (via the existing
  `_error_response()` path). Assert on the response, not on a raised exception — this is the one
  collision case with different externally-visible behavior from (a)/(b).

---

### SF-6: Per-call `extra_tools=[...]` on `DefinitionBot.chat()` / `DefinitionBot.ask()`

Same change as SF-5, applied to `DefinitionBot`.

#### Tests
Mirror SF-5's tests (including the collision case) against `DefinitionBot`.

---

### SF-7: `add_tool()` tracking + `dump_history()`/`load_history()` missing-tool warning

**Problem.** `Bot.load_history()` reconstructs a bot via `cls(**kwargs)`, calling `_build_agent()`
fresh. Only tools reachable from `kwargs` (i.e. re-passed via `tools=[...]`) exist on the
reloaded bot; anything added via `add_tool()` on the original bot is gone, with no signal to the
caller. Callable tools can't be round-tripped through a JSON bundle at all (not portably
serializable — same limitation AD-05 already accepts for open connections/lambdas in the result
store), so this plan does not attempt to restore the tool itself — only to make its absence
loud instead of silent.

**`Bot.__init__`** (extends SF-1's version): add `self._runtime_added_tool_names: list[str] = []`.

**`Bot.add_tool()`** (extends SF-2's version): record which tool name(s) `_register_tool()`
actually added, by diffing the toolset's registered names before/after the call — avoids
re-deriving pydantic-ai's own name-inference rules (explicit `name=` override on a `Tool`
instance vs. `func.__name__` for a plain callable):

```python
def add_tool(self, tool: Any) -> None:
    before = set(self._toolset.tools)
    _register_tool(self._toolset, tool)
    self._runtime_added_tool_names.extend(sorted(set(self._toolset.tools) - before))
```

**`aitaem/agent/history.py`:**
- `make_bundle(messages, store, runtime_added_tool_names=None)` — adds one key to the returned
  dict: `"runtime_added_tool_names": list(runtime_added_tool_names or [])`. Additive field; no
  `_SCHEMA_VERSION` bump needed (same backward-compat approach `load_store()` already uses for
  the `kind` field: read with `bundle.get("runtime_added_tool_names", [])`, so older bundles
  without the key still load cleanly).
- `load_bundle()` is unchanged — the new field is read directly by `Bot.load_history()`, not by
  `load_bundle()` itself, since only `Bot` has access to the freshly-constructed `self._toolset`
  needed to compute what's missing.

**`Bot.dump_history()`:** passes `self._runtime_added_tool_names` through to `make_bundle()`.

**`Bot.load_history()`:** after `bot = cls(**kwargs)` and loading messages, compare the bundle's
`runtime_added_tool_names` against the freshly-built `bot._toolset`; warn on anything missing:

```python
@classmethod
def load_history(cls, data: dict[str, Any], **kwargs: Any) -> Bot:
    bot = cls(**kwargs)
    bot._message_history = load_bundle(data, bot._store)
    missing = set(data.get("runtime_added_tool_names", [])) - set(bot._toolset.tools)
    if missing:
        warnings.warn(
            f"load_history() bundle references runtime-added tool(s) not present "
            f"after reload: {sorted(missing)}. Pass them again via tools=[...] or "
            f"call add_tool() to restore them.",
            stacklevel=2,
        )
    return bot
```

`warnings.warn` (stdlib `warnings` module), not a raised exception — the bot is still usable;
this is a caller-visible nudge, not a fatal condition. A caller who supplies the same tool via
`tools=[...]` in `**kwargs` on reload satisfies the check (the name ends up in `bot._toolset`
regardless of *how* it got there) and gets no warning.

**Not carried forward across multiple reload hops:** if a caller reloads with `tools=[fn]` to
silence the warning, `fn` is now part of the new bot's baseline identity (supplied via
constructor, not `add_tool()`), so `bot._runtime_added_tool_names` is empty for that bot unless
`add_tool()` is called on it again. This is a deliberate simplification, not an oversight — each
bot's `_runtime_added_tool_names` reflects tools added at runtime *relative to its own
construction*.

#### Tests
- `add_tool(fn)` then `dump_history()` — bundle's `runtime_added_tool_names` contains `fn`'s
  registered name
- `dump_history()` on a bot with no `add_tool()` calls — `runtime_added_tool_names` is `[]`
  (regression guard: bundle shape for bots that never use this feature is unaffected)
- `add_tool(fn)` → `dump_history()` → `load_history(bundle, ...)` **without** re-passing `fn`
  via `tools=` — emits a `UserWarning` naming `fn`, via `pytest.warns(UserWarning, match=...)`
- Same round-trip but **with** `tools=[fn]` passed to `load_history()` — no warning emitted
- A bundle produced before this plan (no `runtime_added_tool_names` key) loads via
  `load_history()` without raising or warning — `bundle.get(..., [])` backward-compat path
- `load_bundle()`/`make_bundle()` unit tests (in `test_history.py`) for the new field's
  presence/absence round-tripping through `_arrow_to_b64`-style JSON serialization (the field
  itself is a plain list of strings, no special encoding needed)

---

### SF-8: Public exports

No new public symbols. `add_tool()`, `tools=`, and `extra_tools=` already exist in
`Bot`/`QueryBot`/`DefinitionBot`'s public signatures today (as inert stubs / no-ops); this plan
makes them functional without adding new exported names. No `aitaem/agent/__init__.py` change.
No new `docs/api/` page — `aitaem.agent` has no `docs/api/` pages yet at all (confirmed none
exist); full public API docs are Phase 7 (P7.1) scope.

---

### SF-9: Changelog entry

Add a bullet under the existing `## Unreleased` section of `docs/changelog.md` (an `### Added`
or `### Changed` subsection as appropriate) noting: `Bot.add_tool()`, the constructor
`tools=[...]` parameter, and the `extra_tools=[...]` parameter on `chat()`/`ask()` are now
functional for `QueryBot` and `DefinitionBot` (previously accepted but inert, or raised
`NotImplementedError`); and that `load_history()` now warns when a reloaded bundle references
`add_tool()`-added tools not present after reload (SF-7).

Also add a short note mirroring the plan header's reordering rationale, so a future reader of
the changelog (who won't necessarily see this plan document) understands why composition
primitives shipped ahead of SetupBot: this release establishes whether bot composition is
shippable at all — SetupBot is not being skipped outright, its need just isn't assumed by
default; it's picked back up on an explicit ask.

---

### SF-10: Composition test suite (`tests/test_agent/test_composition.py`)

New file holding the FunctionModel-driven, cross-bot-parametrized versions of the tests listed
under SF-2 through SF-6 (those sub-sections list the assertions; this file is where the
parametrized-across-`QueryBot`/`DefinitionBot` versions live so the behavior is verified
symmetrically rather than duplicated ad hoc in each bot's own test file). Test names:

- `test_constructor_tools_registered` (parametrized: QueryBot, DefinitionBot — schema/membership check only)
- `test_constructor_tools_invoked` (parametrized: QueryBot, DefinitionBot × **sync, async** — actual invocation, return value reaches the response)
- `test_add_tool_invoked` (parametrized: QueryBot, DefinitionBot × **sync, async** — actual invocation, return value reaches the response)
- `test_add_tool_persists_across_turns` (parametrized: QueryBot, DefinitionBot — presence/timing check, not parametrized over sync/async)
- `test_add_tool_not_visible_before_call` (parametrized: QueryBot, DefinitionBot)
- `test_add_tool_accepts_tool_instance_and_plain_function` (parametrized: QueryBot, DefinitionBot — Tool-vs-callable dispatch, orthogonal to sync/async)
- `test_extra_tools_ephemeral_on_ask` (parametrized: QueryBot, DefinitionBot × **sync, async** — actual invocation)
- `test_extra_tools_ephemeral_on_chat` (parametrized: QueryBot, DefinitionBot — presence/timing check, not parametrized over sync/async)
- `test_extra_tools_none_is_noop_regression` (parametrized: QueryBot, DefinitionBot)
- `test_toolset_contract_violation_raises_typeerror_at_construction` (minimal non-`QueryBot`/`DefinitionBot` `Bot` subclass fixture)
- `test_collision_constructor_tools_vs_default_raises_usererror_at_construction` (parametrized) — case (a)
- `test_collision_add_tool_vs_existing_raises_usererror` (parametrized) — case (b)
- `test_collision_add_tool_leaves_toolset_unmodified` (parametrized) — case (b), post-condition
- `test_collision_extra_tools_vs_persistent_surfaces_as_error_status` (parametrized, covers both `ask()` and `chat()`) — case (c)

---

### SF-11: Architecture doc follow-up

`08-implementation-order.md`'s P5.2 section currently (as of this plan's own edits) points to
this plan's filename in a reordering note. Once implementation lands, update that section's
status line to mark P5.2 complete (matching the pattern used for Phase 2/3 in that file), no
further content change needed since P5.2's text already reflects this plan's scope.

---

## Files Changed Summary

| File | Change |
|---|---|
| `aitaem/agent/base.py` | SF-1: store `self._tools`, add `self._toolset` slot, update docstring contract. SF-2: `_register_tool()` helper, implement `add_tool()`. SF-7: `self._runtime_added_tool_names` tracking, `load_history()` missing-tool warning. |
| `aitaem/agent/history.py` | SF-7: `make_bundle()`/`load_bundle()` gain `runtime_added_tool_names`. |
| `aitaem/agent/query_bot.py` | SF-3: register `self._tools` into the default toolset, store `self._toolset`. SF-5: ephemeral toolset for `extra_tools=` in `chat()`/`ask()`. |
| `aitaem/agent/definition_bot.py` | SF-4, SF-6: same as `query_bot.py`. |
| `tests/test_agent/test_definition_bot.py` | SF-4: update `test_definition_bot_agent_has_five_tools` to read `bot._toolset` instead of `bot._agent._user_toolsets[0]`. |
| `tests/test_agent/test_history.py` | SF-7: `runtime_added_tool_names` round-trip + `load_history()` warning tests. |
| `tests/test_agent/test_composition.py` | **New** — SF-10 tests. |
| `docs/changelog.md` | SF-9: Unreleased entry. |
| `plans/agent_module/08-implementation-order.md` | SF-11: mark P5.2 complete post-implementation. |

---

## Testing Strategy

1. **After SF-1/SF-2:** `python -m pytest tests/test_agent/test_primitives.py -v`
2. **After SF-3/SF-4:** `python -m pytest tests/test_agent/test_query_bot.py tests/test_agent/test_definition_bot.py -v`
3. **After SF-7:** `python -m pytest tests/test_agent/test_history.py -v`
4. **After SF-5/SF-6/SF-10:** `python -m pytest tests/test_agent/test_composition.py -v`

**Full suite before commit:**
```bash
python -m pytest tests/test_agent/ --cov=aitaem/agent --cov-report=term-missing
python -m pytest tests/ --ignore=tests/test_agent/    # core suite must stay green
python scripts/check_import_graph.py
ruff check aitaem/agent/
```

---

## Success Criteria

- [ ] `QueryBot(..., tools=[fn])` and `DefinitionBot(..., tools=[fn])` register `fn` alongside default tools
- [ ] `bot.add_tool(fn)` makes `fn` callable on the next `chat()`/`ask()` call, on both bots
- [ ] Every invocation-level test (constructor `tools=`, `add_tool()`, `extra_tools=`) is parametrized over both a sync and an async custom tool fixture — the sync-callable path through `_register_tool()` has explicit, non-zero test coverage, not just the async path all default tools happen to use
- [ ] `bot.add_tool()` accepts both plain callables and `pydantic_ai.Tool` instances
- [ ] `bot.chat(..., extra_tools=[fn])` / `bot.ask(..., extra_tools=[fn])` make `fn` callable only for that call
- [ ] A tool added via `extra_tools=` on turn N is not visible on turn N+1 without re-passing it
- [ ] A tool added via `add_tool()` between turns is visible from that point forward
- [ ] A `Bot` subclass that fails to set `self._toolset` in `_build_agent()` raises `TypeError` at construction time, not a bare `AttributeError` at first `add_tool()` call
- [ ] Collision case (a): `tools=[fn]` colliding with a default tool raises `UserError` out of the bot constructor
- [ ] Collision case (b): `add_tool(fn)` colliding with an existing tool raises `UserError` directly, and leaves the persistent toolset unmodified
- [ ] Collision case (c): `extra_tools=[fn]` colliding with a persistent tool does **not** raise to the caller — surfaces as `BotResponse(status=error, reason=...)` via the existing exception handling in `chat()`/`ask()`
- [ ] `add_tool(fn)` followed by `dump_history()` records `fn`'s name in the bundle's `runtime_added_tool_names`
- [ ] `load_history()` on a bundle whose `runtime_added_tool_names` are not satisfied after `cls(**kwargs)` reconstruction emits a `UserWarning` naming the missing tool(s); satisfied names (re-passed via `tools=`) emit no warning
- [ ] A pre-existing bundle without a `runtime_added_tool_names` key still loads via `load_history()` without raising or warning
- [ ] `extra_tools=None` (the default) produces identical agent behavior to before this plan — no `toolsets=` passed to `agent.run()` when there are no extra tools
- [ ] All Phase 1–3 tests remain green, unmodified except the one internal-API call-site update in SF-4
- [ ] `python -m pytest tests/test_agent/ --cov=aitaem/agent` coverage does not regress
- [ ] `ruff check aitaem/agent/` passes
- [ ] `python scripts/check_import_graph.py` exits 0

---

## Known Deviations from Architecture Doc

| Architecture says | Implementation does | Rationale |
|---|---|---|
| AD-11 originally listed `bot.add_bot(other_bot)` under "persistent runtime addition" | Not implemented in this plan | User-directed scope cut, recorded as ND-11; `add_bot()` depends on `as_tool()`, which is also deferred |
| `08-implementation-order.md` sequenced Phase 5 after Phase 4 (SetupBot) | This plan runs before Phase 4 | User-directed reordering; Phase 5.2 has no dependency on SetupBot, only on `QueryBot`/`DefinitionBot` |

---

## Explicitly out of scope (context only, not part of this plan)

- `SetupBot` will inherit `tools=` / `add_tool()` / `extra_tools=` automatically once built (Phase 4) — no extra composition work needed then.
- Generic `Bot.as_tool()` / `add_bot()` — tracked as ND-11; revisit once `SetupBot` exists or hand-written delegation wrappers become repetitive.
