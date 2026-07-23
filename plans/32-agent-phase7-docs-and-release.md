# Plan 32 — Agent Phase 7: Docs and v1.0 Release

Implements Phase 7 of `plans/agent_module/08-implementation-order.md` (P7.1, P7.2).

## Scope

- **v1.0 ships with two convenience bots: `QueryBot` and `DefinitionBot`.** `SetupBot`
  (Phase 4) is not implemented and is deferred to v1.x — matches what Phase 6 (evals)
  already shipped. No SetupBot implementation work is in this plan.
- **Nav structure.** A top-level **Agent** section holds four manually-authored guides
  (Getting Started, Building Your Own Bot, Evaluating Your Agent, Stability &
  Limitations). The auto-generated agent API reference joins the existing **API
  Reference** section as a new page, consistent with how `MetricCompute`/`Specs`/
  `Connectors`/`Helpers` are organized today.

## Current-state gaps this plan closes

- `aitaem.agent` has zero docs pages and zero mkdocs nav entries today.
- `pyproject.toml` has no `agent` extra — only `agent-anthropic` and `agent-evals`. See
  P7.1a for the shape it needs.
- `examples/` has three example pairs with no execution-order signal in their filenames,
  and the README's listed order (query_bot → intent_resolution → definition_bot) doesn't
  match the conceptual dependency of defining specs before querying them.
- `DefinitionBot`'s prompt-cache setting doesn't match its own documented intent — a
  pre-existing bug, fixed in P7.0.
- Design docs (`ARCHITECTURE.md`, `08-implementation-order.md`, AD-03) present `SetupBot`
  as v1.0 content — fixed in P7.1e.
- No stability-guarantee page exists; the content already lives in `ARCHITECTURE.md` §6
  but isn't shipped — fixed in P7.1f.
- `tenant_id` is undocumented in shipped docs, and its name invites readers to infer
  isolation guarantees the module doesn't provide — covered in P7.1d and P7.1f.

---

## P7.0 — Pre-release bugfix: `DefinitionBot` prompt-cache setting (blocking, do first)

**Diagnosis:** `aitaem/agent/query_bot.py::_provider_cache_config` (Anthropic branch)
returns `{"anthropic_cache_instructions": "5m"}`. `aitaem/agent/definition_bot.py:285`
(`_provider_cache_config_definition`, Anthropic branch) returns
`{"anthropic_cache": "5m"}` instead, despite its docstring claiming to mirror
`_provider_cache_config`.

Both keys are real, distinct `AnthropicModelSettings` fields (verified against the
installed pydantic-ai 2.2.0 source, `pydantic_ai/models/anthropic.py`), not a typo:
- `anthropic_cache_instructions` places the cache breakpoint immediately after the last
  *static* instruction block, which is what both bots need — Layer A + Layer B (static)
  cached, Layer C (per-turn dynamic date context) excluded.
- `anthropic_cache` is a separate "automatic caching" mode, mutually exclusive with
  `anthropic_cache_messages`, documented as a fallback for Anthropic-compatible gateways
  — not the targeted static-instructions breakpoint DefinitionBot's docstring claims.

`tests/test_agent/test_query_bot.py::test_provider_cache_config_anthropic` pins
QueryBot's return value exactly; `tests/test_agent/test_definition_bot.py` has no
equivalent test, so the divergence shipped unverified.

**Impact:** `QueryBot` and `DefinitionBot` have different actual cache-hit/cost behavior
for a mechanism presented as shared — a customer-visible cost surface that P7.1d/P7.1f
would otherwise document incorrectly.

**Fix:**
```python
# aitaem/agent/definition_bot.py — _provider_cache_config_definition, Anthropic branch
    if provider == "anthropic":
-       return {"anthropic_cache": "5m"}
+       return {"anthropic_cache_instructions": "5m"}
```
```python
# tests/test_agent/test_definition_bot.py — new test, mirrors
# test_query_bot.py::test_provider_cache_config_anthropic
def test_provider_cache_config_definition_anthropic():
    cfg = _provider_cache_config_definition(
        "anthropic:claude-haiku-4-5-20251001", "t1"
    )
    assert cfg == {"anthropic_cache_instructions": "5m"}
```

A Phase 3 defect, not Phase 7 scope, but a one-line fix with a pinning test — resolved
here rather than in a separate cycle since it was caught while preparing docs that would
otherwise ship the wrong claim.

---

## P7.1 — Public API docs

### P7.1a — `pyproject.toml`: add `agent` extra (provider-neutral, non-drifting, tested)

**What:** Per `08-implementation-order.md` P1.1 (`agent` extra should include
`pydantic-ai[anthropic,openai,...]`) and G7 (provider-agnostic LLM layer), `agent` must
not alias a single provider. Aliasing it to `agent-anthropic` outright would bake one
provider into the semver-stable install contract, and widening it later would be a
breaking change for every v1.0 install. But two independent dependency lists would drift
(a dep added to one silently misses the other), so both derive from a shared base:
```toml
agent-core = [
    "pydantic-ai-slim>=2.2.0,<3",
]
agent-anthropic = [
    "aitaem[agent-core]",
    "pydantic-ai-slim[anthropic]>=2.2.0,<3",
]
agent = [
    "aitaem[agent-anthropic]",
    "pydantic-ai-slim[openai]>=2.2.0,<3",
]
agent-evals = [
    "pydantic-ai-slim[evals]>=2.2.0,<3",
]
```
`agent` is `agent-anthropic` plus the OpenAI extra — still provider-neutral in effect
(the union covers both providers), but a dependency added to `agent-core` or
`agent-anthropic` is automatically picked up by `agent`. Update the `all` extra to
depend on `agent` instead of `agent-anthropic` (a strict superset). Cap `dev`'s existing
`pydantic-ai-slim[evals]>=2.2.0` at `<3` too.

Nothing in the bot code is Anthropic-specific — `_provider_cache_config`/
`_provider_cache_config_definition` already branch on `openai:`-prefixed model strings —
so OpenAI is already a first-class code path, just not an installable extra until now.

**Upper bound, `<3`:** the two `_provider_cache_config*` functions depend on pydantic-ai
*internals* (the anthropic adapter's static-instruction sort order in `_agent_graph.py`),
not just its public API. An unbounded `>=2.2.0` risks a future major version silently
changing that internal behavior with no signal until a cache regression is noticed in
production.

**Docs and examples install `"aitaem[agent-anthropic]"`:** every example hard-codes an
Anthropic model string and is only tested against Anthropic. `docs/agent/
getting-started.md` and every example prerequisite block use `agent-anthropic` as the
concrete, tested path, with one parenthetical mention of `"aitaem[agent]"` as the
multi-provider superset for readers who plan to swap model providers.

**CI must exercise `aitaem[agent]`'s resolution before it's frozen.** No CI job or doc
currently installs it — everything uses `agent-anthropic` or `agent-evals` — so v1.0
would otherwise freeze an extra whose dependency resolution has never actually run. Add
a job to `.github/workflows/ci.yml`:
```yaml
  agent-extra-smoke:
    name: agent-extra-smoke
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install uv
        run: pip install uv
      - name: Install aitaem[agent]
        run: uv pip install --system -e ".[agent]"
      - name: Import smoke test
        run: python -c "import aitaem.agent"
```

**Model-string convention for every new docs page:** use
`anthropic:claude-haiku-4-5-20251001` throughout — the only model string used by any
example script/notebook in this repo. Do not copy `anthropic:claude-sonnet-4-6` from the
`QueryBot`/`DefinitionBot`/`Bot` constructor docstrings — that string is an illustrative
placeholder in code docstrings and test fixtures (no date suffix, never exercised
against a live API here). Applies to all of P7.1d and P7.1f; called out here because
`building-your-own-bot.md` is the page most likely to pull a snippet from `Bot`'s
docstring verbatim.

### P7.1b — `docs/api/agent.md` (new, auto-generated via mkdocstrings)

**What:** One page documenting the full public surface of `aitaem.agent.__all__`
(33 symbols), grouped into subsections matching the module's own structure:
- Primitives: `Bot`, `BotResponse`, `Status`, `RunTrace`, `ToolCall`, `Usage`,
  `ResultStore`, `ResultEntry`, `TabularEntry`, `TextEntry`, `WrongEntryKindError`
- QueryBot: `QueryBot`, `QueryResponse`, `QueryPayload`, `MetricIntent`, `ResolvedSpec`,
  `ExactMatch`, `NearMiss`, `SpecMatchResult`, `RecordIntentResult`,
  `ResolveIntentResult`, `SpecResolver`
- DefinitionBot: `DefinitionBot`, `DefinitionResponse`, `DefinitionPayload`,
  `DefinitionIntent`, `SpecDraft`, `ColumnInfo`, `ListTablesResult`,
  `DescribeTableResult`, `DraftSpecResult`, `ValidateSpecResult`, `ValidationIssue`

Each subsection uses `::: aitaem.agent.<module>` mkdocstrings directives, same pattern as
`docs/api/specs.md` / `docs/api/connectors.md`.

`mkdocs build --strict` fails on a broken nav link or an unresolvable symbol reference,
but not on a symbol that resolves and renders with an empty body (a class/field with no
docstring just emits a bare signature). A green `--strict` build is necessary but not
sufficient evidence this page is complete — see the Validation section's manual-eyeball
step, and P7.1g for a test that catches drift going forward.

**mkdocs.yml nav change:**
```yaml
- API Reference:
    - Overview: api/index.md
    - MetricCompute: api/insights.md
    - Specs: api/specs.md
    - Connectors: api/connectors.md
    - Helpers: api/helpers.md
    - Agent: api/agent.md          # new
```

### P7.1c — `docs/api/index.md` update

**What:** Add a short "Agent module (optional install)" section — one paragraph plus a
pointer to `api/agent.md` and the `Agent` nav section, not a full duplicate class table
(33 symbols would overwhelm the existing overview page). Mirrors how the page already
keeps helpers to a compact table rather than repeating full docstrings.

### P7.1d — Four new guides under a new `docs/agent/` directory

**mkdocs.yml nav change:**
```yaml
- Agent:
    - Getting Started: agent/getting-started.md
    - Building Your Own Bot: agent/building-your-own-bot.md
    - Evaluating Your Agent: agent/evaluating-your-agent.md
    - Stability & Limitations: agent/stability.md
```

**`docs/agent/getting-started.md`:**
- Install: `pip install "aitaem[agent-anthropic]"` + `ANTHROPIC_API_KEY` setup, with the
  single `"aitaem[agent]"` mention from P7.1a.
- Two-line construction pitch for `QueryBot` and `DefinitionBot`, adapted from the
  existing "QueryBot quick start" / "DefinitionBot quick start" snippets in
  `examples/README.md`.
- Pointer to `examples/` for full runnable scripts + notebooks.
- Note that `SetupBot` is planned but not yet shipped (v1.x).
- `tenant_id` constructor parameter (on both bots): an optional OpenAI prompt-cache
  routing key, not a security or isolation boundary. When omitted, falls back to
  `_permission_fingerprint(spec_cache)` — an 8-char hash of the visible
  metric/slice/segment names, so RBAC-differentiated `spec_cache`s land in different
  cache routing lanes automatically. Paired with an explicit limitations note:
  - **NG1** — the agent module never stores credentials, persists history beyond what
    the caller explicitly serializes via `dump_history()`, manages multi-tenancy or RBAC,
    or holds connections other than the caller-provided `ConnectionManager`. `tenant_id`
    does not create isolation; separate tenants need separate `ConnectionManager`/
    `SpecCache`/bot instances, enforced by the caller.
  - **ND-08** — concurrent `chat()`/`ask()` calls on the same bot instance are
    unsupported; one call at a time per instance. Concurrency lives at the caller level
    (N users → N bot instances).
  Links forward to `docs/agent/stability.md` for the full stability/limitations
  reference.

**`docs/agent/building-your-own-bot.md`:**
- When to reach for primitives directly instead of a convenience bot.
- `Bot` subclassing contract: `_build_agent()` must build a `FunctionToolset`, register
  `self._tools` via the module's `_register_tool()` pattern, and set `self._toolset`
  (adapted from `Bot`'s existing docstring in `aitaem/agent/base.py` into prose plus a
  worked mini-example, not restated verbatim).
- `BotResponse[PayloadT]` / `Status` / `RunTrace` / `ResultStore` contracts.
- Tool composition surface: `Bot(tools=[...])`, `add_tool()`, per-call `extra_tools=`
  (Phase 5.2).
- History serialization: `dump_history()` / `load_history()` round-trip.
- Context-window management via `ProcessHistory` (reference pattern per OQ-A1 — no
  built-in trimmer ships, so this documents the pattern, not a shipped utility).

**`docs/agent/evaluating-your-agent.md`:**
- Two-track framing, matching the docstring in `tests/evals/test_query_bot_evals.py`:
  1. **Substrate validation** (CI-safe, no API key) — points at `tests/evals/` as the
     reference harness: scripted `FunctionModel`s prove `RunTrace`/`ResultStore`/
     `BotResponse` are consumable by `pydantic_evals.Evaluator`s. States plainly that
     this tests wiring, not agent quality (ND-09).
  2. **Behavioral evaluation against a live model** — points at
     `examples/04_evaluating_agents_example.py`/`.ipynb` (P7.3) as the pattern to copy:
     real model, real dataset, `Case`/`Evaluator`/`Dataset` from `pydantic_evals`, plus a
     repeated-run `pass_rate()` helper for confidence given LLM non-determinism.
- Install note: `pip install "aitaem[agent-evals]"`.

**`docs/agent/stability.md`:** see P7.1f.

### P7.1e — Reconcile design docs: `SetupBot` is deferred, not v1.0

**What:** One-line status edits, no restructuring:
- `plans/agent_module/ARCHITECTURE.md`:
  - Executive summary phase list (line 44) — mark SetupBot deferred to v1.x in the phase
    sequence.
  - §8 Implementation Order, Phase 4 bullet (line 315) — add a deferred-status marker,
    matching how other phases in the same list already carry a status (e.g. "✅
    Shipped").
  - Module description (line 28, "three opinionated convenience bots — `QueryBot`,
    `DefinitionBot`, `SetupBot`") — add a parenthetical noting SetupBot is v1.x.
- `plans/agent_module/08-implementation-order.md`: `## Phase 4 — SetupBot` heading gets
  a status line ("**Status:** Deferred to v1.x — not implemented for v1.0."), matching
  the pattern already used for shipped phases (Phase 5.2, Phase 6).
- `plans/agent_module/02-architectural-decisions.md` (AD-03): one clause on the
  `SetupBot` bullet noting it's a v1.x deliverable. The architectural decision itself
  (primitives + convenience bots as first-class layers) is unchanged — only SetupBot's
  ship date is corrected.

These are the documents a prospective contributor reads first, ahead of any generated
API docs, so they need to state the deferral directly rather than read as an oversight.

### P7.1f — `docs/agent/stability.md` (new)

**What:** Lift `ARCHITECTURE.md` §6's stability guarantees into shipped docs, since
0.4.0 → 1.0.0 is a stability promise:
- Convenience bot constructors and primitives base classes: semver-stable.
- Default tool input/output schemas: semver-stable.
- Default prompts: public but explicitly not semver-stable in content — tuning is
  expected across patch/minor releases. Don't pin application behavior to exact
  default-prompt wording; override via the primitives layer
  (`building-your-own-bot.md`) if stability is required there. Changing the static
  instruction text (Layer A/B) also invalidates the provider-side prompt cache P7.0
  just made consistent across both bots — a prompt tune is a cache-warmup reset too.
- `RunTrace` and `BotResponse` field shapes: semver-stable (the eval substrate is a
  contract — see `evaluating-your-agent.md`).
- Not enabled: modifying the result-store schema, removing default tools by name,
  hot-swapping the LLM runtime away from pydantic-ai, persistent state owned by the bot.
- Cross-links to the NG1/ND-08 limitations note in `getting-started.md` rather than
  repeating it — this page is guarantees, that section is limitations.

**mkdocs.yml nav change:** included in P7.1d's nav block above (`Stability &
Limitations: agent/stability.md`).

### P7.1g — Test: pin `__all__` against the documented symbol set

**What:** P7.1b enumerates `aitaem.agent.__all__`'s 33 symbols by hand into
`docs/api/agent.md`'s three subsections; the Validation section's manual-eyeball check
only catches drift once, at write time. Add a test that keeps catching it:
```python
# tests/test_agent/test_public_api.py
from aitaem import agent

# Keep in sync with docs/api/agent.md's three subsections (Primitives / QueryBot /
# DefinitionBot) — this is the enforcement mechanism for that sync, not a duplicate.
DOCUMENTED_SYMBOLS = {
    # Primitives
    "Bot", "BotResponse", "Status", "RunTrace", "ToolCall", "Usage",
    "ResultStore", "ResultEntry", "TabularEntry", "TextEntry", "WrongEntryKindError",
    # QueryBot
    "QueryBot", "QueryResponse", "QueryPayload", "MetricIntent", "ResolvedSpec",
    "ExactMatch", "NearMiss", "SpecMatchResult", "RecordIntentResult",
    "ResolveIntentResult", "SpecResolver",
    # DefinitionBot
    "DefinitionBot", "DefinitionResponse", "DefinitionPayload", "DefinitionIntent",
    "SpecDraft", "ColumnInfo", "ListTablesResult", "DescribeTableResult",
    "DraftSpecResult", "ValidateSpecResult", "ValidationIssue",
}


def test_all_matches_documented_symbols():
    assert set(agent.__all__) == DOCUMENTED_SYMBOLS
```
A future export added to `__all__` without a matching `docs/api/agent.md` update now
fails CI instead of shipping as a silent gap in a later minor release.

---

## P7.2 — Examples folder cleanup (numbered execution order)

**What:** Prefix every example script/notebook pair with a two-digit order number.
Config/fixture files (`connections.yaml`, `connections.template.yaml`, `data/`,
`metrics/`, `slices/`, `segments/`, `README.md`) are unchanged.

| New name | Old name |
|---|---|
| `01_definition_bot_example.py` / `.ipynb` | `definition_bot_example.py` / `.ipynb` |
| `02_query_bot_example.py` / `.ipynb` | `query_bot_example.py` / `.ipynb` |
| `03_intent_resolution_example.py` / `.ipynb` | `intent_resolution_example.py` / `.ipynb` |
| `04_evaluating_agents_example.py` / `.ipynb` | *(new, P7.3)* |

**Rationale for order:** DefinitionBot (defining specs) conceptually precedes QueryBot
(querying against specs), even though the files don't literally chain — both read the
same static spec YAML today. Intent-resolution is a deep-dive supplement to QueryBot, so
it follows. Evaluating agents comes last since it evaluates the bots introduced in
01–03.

A repo-wide grep confirmed no file outside `examples/` references these filenames, so
this is a self-contained rename.

All three existing example scripts' docstrings and all three notebooks' prerequisite
cells already say `pip install aitaem[agent-anthropic]` consistently — no content
changes needed there.

**`examples/README.md` updates:**
- Reorder the "Examples" table to match the new numbering/filenames.
- Add a row for `04_evaluating_agents_example.py` / `.ipynb`.
- No content changes needed to the "quick start" snippets.

---

## P7.3 — New example: `04_evaluating_agents_example.py` / `.ipynb`

Standalone script + notebook pair, following the existing example conventions
(`_check_api_key()`, DuckDB-from-CSV bootstrap, `SpecCache.from_yaml(...)` against
`examples/metrics/` + `examples/slices/`, `ConnectionManager` against
`examples/data/ad_campaigns.duckdb`, `asyncio.run(main())` — see
`examples/query_bot_example.py`). Runs against the live Anthropic model, not
`FunctionModel`, since its purpose is behavioral evaluation with repeated-run confidence
— `tests/evals/` already covers the mocked/CI-safe substrate case.

Imports and types use the real package surface: `Status` and `QueryResponse` come from
`aitaem.agent` (top-level), not `aitaem.agent.base`; the model id is
`"anthropic:claude-haiku-4-5-20251001"`; `QOut.response` is typed as `QueryResponse`
directly.

**Task + I/O types:**
```python
@dataclass
class QIn:
    question: str

@dataclass
class QOut:
    response: QueryResponse   # aitaem.agent.QueryResponse
    bot: QueryBot             # kept so evaluators can call get_result()

async def query_task(inputs: QIn) -> QOut: ...
```
Fresh `QueryBot` per case (per-run state — intents, spec registry — must not leak
between cases), built against module/notebook-level `SPEC_CACHE` / `CONN_MGR` (safe to
share: read-only catalog + connection). Uses `bot.ask()` — single-turn, no history.

**Evaluators** (as `Evaluator[QIn, QOut, None]` dataclasses):
- `StatusIs(expected: Status)` → `bool`, checks `ctx.output.response.status`.
- `CalledTool(tool_name: str)` → `bool`, checks `ctx.output.response.trace.tool_calls`.
- `ToolSequenceIs(expected: list[str])` → `bool`, exact-sequence check validating the
  Metric Precision Rule gate order (`record_intent → resolve_intent →
  compute_metrics`).

**Dataset:** two `Case`s against the real `examples/metrics/` catalog
(`total_revenue`, `avg_revenue`, `max_revenue`, `campaign_count`, `ctr`, `roas` — none
named `sales_velocity`, so the refusal case is genuine):
- `in_catalog_metric` — "What was total revenue in Q1 2024?" →
  `StatusIs(Status.ok)`, `ToolSequenceIs(["record_intent", "resolve_intent", "compute_metrics"])`.
- `out_of_catalog_metric` — "What was sales velocity last month?" →
  `StatusIs(Status.refused)`, `CalledTool("resolve_intent")`.

`ToolSequenceIs` stays exact-match, deliberately, and the example says so in its
prose. Against a live model, a run that self-corrects (e.g. calls `resolve_intent` twice
after a near-miss) behaves correctly but fails an exact-sequence assertion — a
subsequence check would fix that but would also let a model that calls
`compute_metrics` before `resolve_intent` pass as long as it calls `resolve_intent`
later, defeating the one case that actually exercises the Metric Precision Rule gate
order. The example states directly that a single failed run on this assertion isn't
necessarily a bug, and `pass_rate()` — not the single-run `report.print()` — is how the
case should be read.

**Execution:**
- `report = await dataset.evaluate(query_task)` then `report.print(...)` — single run.
- `pass_rate(dataset, task, n=5) -> dict[str, float]` — repeats the full dataset `n`
  times against the live model, tallies per-case pass fraction. Runtime/cost caveat noted
  in both the script's docstring and the notebook's markdown cell (n=5 runs × 2 cases =
  10 API calls).

**Prerequisites** (docstring / first notebook cell) mirror `query_bot_example.py`:
`ANTHROPIC_API_KEY`, `pip install "aitaem[agent-evals]"`.

**Never wired into CI or the docs build.** This notebook makes 10+ live Anthropic API
calls. `.github/workflows/docs.yml` runs plain `mkdocs gh-deploy`; no `mkdocs-jupyter`
plugin is configured and no `nbconvert --execute` step exists anywhere in the repo.
Never add notebook execution to `docs.yml` or any test workflow — verification is
manual-only (see Validation).

---

## P7.4 — Changelog entry

**What:** `docs/changelog.md`, under `## Unreleased`: `DefinitionBot` prompt-cache fix
(P7.0, under "Fixed"), agent module docs including the new Stability & Limitations page
(`docs/agent/*`, `docs/api/agent.md`), new `agent` install extra, new evaluating-agents
example, examples renumbering. Becomes part of the v1.0.0 section when the release is
cut (CLAUDE.md release process, not repeated here).

One sentence at the top of the `1.0.0` entry states that `1.0.0` is a stability
declaration (per P7.1f) with no breaking changes to the existing core `aitaem` API — the
version jump reflects the agent module's graduation to a stable, documented surface, not
an API break. A `0.4.0` → `1.0.0` jump otherwise reads as a signal to look for a
migration section that isn't there.

---

## P7.5 — v1.0 release

Not detailed here — CLAUDE.md's "Release Process" section is the authoritative procedure
(branch, version bump, changelog rename, PR, tag, GitHub release, PyPI publish via
Trusted Publishing). Version bump target: `1.0.0` (from current `0.4.0`).

---

## Out of scope

- `SetupBot` implementation (Phase 4) — deferred to v1.x.
- Any change to `tests/evals/` (P6.1) — stays as the CI-safe substrate reference; P7.3's
  new example is the live-model companion, not a replacement.
- Streaming, event hooks, error-taxonomy refinement, prompt-fragment overrides,
  hot-reload — tracked in `07-non-decisions.md`, unaffected by this plan.
- No `DefinitionBot`/`QueryBot` behavior changes beyond the P7.0 cache-key fix — a
  narrow, deliberate exception to "Phase 7 is docs-only," not a general invitation to fix
  other things found along the way.

## Validation

- `mkdocs build --strict` — catches broken nav references and mkdocstrings symbol
  resolution failures. Does not catch a symbol that resolves but renders empty (P7.1b).
- Manually eyeball the rendered `api/agent.md` (`mkdocs serve`, not just a green
  `--strict` build) and confirm all 33 symbols in `aitaem.agent.__all__` show descriptive
  content, not a bare signature. Any symbol with a missing/thin docstring gets one added
  in `aitaem/agent/` as a small follow-up.
- `ruff check` + `mypy aitaem/ tests/evals/` — regression check (no `aitaem/` source
  changes beyond P7.0's one-line cache-key fix).
- `pytest tests/test_agent/test_definition_bot.py -k cache` — confirms the P7.0 pinning
  test passes.
- `pytest tests/test_agent/test_public_api.py` — confirms the P7.1g `__all__`-vs-docs
  pinning test passes.
- CI green on the new `agent-extra-smoke` job (P7.1a) — confirms `aitaem[agent]`
  actually resolves and imports before the extra is frozen at `1.0.0`.
- Manually run `python examples/04_evaluating_agents_example.py` against a live key to
  confirm both cases pass and `pass_rate()` executes end-to-end. Deliberately manual,
  never wired into CI (see P7.3).
- Execute the new notebook top-to-bottom locally (`jupyter nbconvert --execute` or
  manual run) to confirm no broken cells. Also never wired into CI.
- Grep repo for old example filenames post-rename to confirm no dangling references.
- Grep `plans/agent_module/ARCHITECTURE.md`, `08-implementation-order.md`, and
  `02-architectural-decisions.md` for `SetupBot` post-edit to confirm every remaining
  mention reads as deferred/v1.x, not v1.0-scoped (P7.1e).
