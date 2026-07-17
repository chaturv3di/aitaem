# Section 2 — Architectural Decisions

This section captures the architecture-level decisions reached during alignment. Each decision carries context (why the question came up), the decision, and consequences (what it locks in or rules out).

---

## AD-01: `aitaem.agent` is an optional install of the `aitaem` package

**Context.** `aitaem` core is a quiet library with a narrow dependency set (ibis, pydantic, etc.). Adding LLM machinery as a hard dependency would change that — pydantic-ai brings in LLM SDKs, prompt scaffolding, JSON-mode handling, model adapters. Users who only want the deterministic compute layer should not pay for any of this.

**Decision.** The agent module ships as `aitaem.agent`, gated by an optional install:
```
pip install aitaem            # core only, no agent
pip install aitaem[agent]     # core + agent module
```

**Consequences.**
- `aitaem.agent.__init__` may import from `aitaem` freely; the reverse is forbidden.
- An import-graph CI check enforces the one-way dependency.
- Users who try `import aitaem.agent` without the extra installed get a clear `ModuleNotFoundError` for the LLM dependencies.
- No risk of agent-specific bugs affecting non-agent users.

---

## AD-02: pydantic-ai is the agent runtime

**Context.** Multiple agent frameworks exist (LangChain agents, LlamaIndex, Haystack, raw provider SDKs). pydantic-ai is the choice with the strongest type-safety story, structured-output handling, and an active development community.

**Decision.** The agent module is built on pydantic-ai. Model strings (e.g. `"anthropic:claude-haiku-4-5"`, `"openai:gpt-4o"`) are passed to the bot constructor; pydantic-ai routes to the appropriate provider.

**Consequences.**
- pydantic-ai is a hard dep of `aitaem[agent]`.
- Provider support inherits from pydantic-ai — currently OpenAI, Anthropic, Bedrock, Gemini, Groq, Mistral, Together, Cohere, DeepSeek, Perplexity, Azure, Vertex, Ollama, LiteLLM, and others.
- The agent module accepts an upgrade treadmill — pydantic-ai breaking changes flow through.

---

## AD-03: Module shape — primitives + convenience bots

**Context.** Three shapes were considered: specialized bots only, one unified bot, or primitives + convenience bots. The convenience-bots-only shape forces subclassing/monkey-patching for extension; the unified-bot shape loses surface clarity per capability.

**Decision.** Both layers are first-class:
- **Convenience bots** — `QueryBot`, `DefinitionBot`, `SetupBot` as opinionated assemblies, each constructed in two lines.
- **Primitives** — composable lower-level constructs (the underlying agent, tool, prompt, and response types) for users who want to build custom bots.

The convenience classes are designed from day one as if primitives are available — internal structure is composition, not inheritance.

**Consequences.**
- Two parallel public surfaces to document and version.
- Onboarding pitch ("import a bot") is preserved.
- Downstream apps build their own bots on the public primitives — same surface the convenience bots use.
- New convenience bots can be added without breaking primitives.

---

## AD-04: Bot is the session

**Context.** Two options: (a) a separate `Session` object owns conversation state, with a bot that is reusable across users; (b) the bot itself carries the session — constructed per user, garbage-collected per conversation.

**Decision.** Option (b). The bot carries the session. A bot instance is constructed per user / per workspace / per conversation as appropriate to the caller's lifecycle.

**Rationale.**
- `ConnectionManager` is bound to user identity (RBAC/RLS support) and must be passed at construction. Sharing a bot across users while swapping connection managers is fighting the grain.
- Bot construction is cheap (SpecCache is typed Python data; ConnectionManager is caller-owned).
- The "one bot, many sessions" optimization isn't needed for measured use cases; can be revisited if a daemon scenario demands it.

**Consequences.**
- `ask()` does not touch internal history. `chat()` does.
- `dump_history()` / constructor `history=...` are the persistence primitives.
- No separate `Session` type.

---

## AD-05: Result store is a bot field; responses carry result IDs; history serialization includes artifacts

**Context.** Tools produce artifacts (DataFrames, configs). Where they live across turns, how they survive serialization, and how the caller retrieves them are tied questions.

**Decision.**
- Result store is a `dict[result_id: str, Artifact]` field on the bot.
- Lifetime is session-scoped — created when the bot is constructed; cleared on GC or `bot.reset()`. Not cleared between turns.
- Tool responses sent to the LLM carry only minimal summaries.
- Bot responses to the caller carry result IDs; the caller dereferences via `bot.get_result(result_id)`.
- `dump_history()` serializes both the messages *and* the result artifacts, keyed by result ID. `load_history()` restores both.
- Artifacts must be serializable. DataFrames/Arrow tables/configs are. Open connections and lambdas are not — and they wouldn't be in the result store anyway.

**Consequences.**
- Responses are lightweight (no embedded DataFrames).
- Cross-turn analysis on prior results works (Plan 023 pattern).
- History portability is complete: a serialized history loaded into a new bot has full access to prior artifacts.
- Tool result store is **not** in the response object; the response and the store live on the bot.

---

## AD-06: Per-bot response types over a common base

**Context.** Either one canonical response type carries all possible fields (with most being optional per bot), or each bot has a typed response that subclasses a small common base.

**Decision.** Per-bot response types. The base type carries `status`, `narrative`, `trace`, `reason` (all bots have these). Bot-specific fields — including the typed payload and any bot-specific extras — live on the subclass.

**Consequences.**
- Callers know exactly what `payload` contains by the bot they're talking to.
- Discriminated unions across bots are natural (`QueryResponse | DefinitionResponse | SetupResponse`).
- Bot-specific helpers (e.g. `QueryResponse.primary_result()`) have somewhere to live.
- Cost is small: subclasses primarily exist for type-narrowing.

---

## AD-07: Status is an enum, not a bool

**Context.** A common pattern is `success: bool` + `error: str | None`. This collapses four distinct end states (succeeded with data, succeeded empty, refused as out-of-scope, errored) into two bools, forcing every consumer to re-derive the four cases.

**Decision.** A `Status` enum with four values: `ok | empty | refused | error`. The `refused` value captures the "I considered this and chose not to act" case — distinct from both success and failure. This is the same case that surfaces when a question doesn't precisely match any spec and the agent refuses to substitute an approximation.

**Consequences.**
- Callers branch cleanly on status; no need to re-derive the four cases from tuples like `(success, error_message, answerable)`.
- Mutually exclusive states are unrepresentable (no `success=True, refused=True`).

---

## AD-08: Aggregated trace now, event-stream hooks later

**Context.** Trace serves debugging, transparency/audit, and evals. Two flavors: (A) standardized aggregated `RunTrace` on the response; (B) event-stream protocol that callers subscribe to.

**Decision.** Flavor A for v1. Each response carries a `RunTrace` with tools_called, model, usage, duration, run_id. The bot's internals are designed so an event-stream hook can be added later without breaking the response shape.

**Consequences.**
- Simple, eval-friendly, debuggable from day one.
- Logfire / OTel / custom observability is a downstream consumer concern in v1; the trace exposes enough that downstream observability can be wired without library changes.
- Path open for v1.1+ event hooks (`@bot.on_tool_called`, etc.).

---

## AD-09: Eval substrate is the architecture's job; framework choice is not

**Context.** Multiple eval frameworks exist (pydantic-evals, deepeval, Inspect AI, others). Choosing one bakes that choice into the library.

**Decision.** The architecture commits to an eval substrate — `RunTrace` is OTel-compatible by design; `dump_history()` is JSON; `bot.get_result()` exposes artifacts. The library does **not** import any eval framework. Eval harness construction is a downstream consumer concern.

**Recommendation (non-binding; user decides).** `pydantic-evals` as primary; deepeval as RAG-flow complement; Inspect AI for capability/safety benchmarks if/when needed. See Section 5 for the trade-off analysis.

**Consequences.**
- Switching eval frameworks later costs nothing in the agent module.
- The substrate's OTel compatibility makes downstream observability and evals first-class without library changes.

---

## AD-10: Tools return minimal LLM-facing summaries; full artifacts live in the result store

**Context.** A naive implementation has tools return their full output (e.g. an entire DataFrame) as the tool result the LLM ingests. For a 200-row result, this is many tokens spent on data the LLM only needs to narrate. Worse, the LLM is then sometimes asked to copy this data verbatim into its structured response — adding error surface.

**Decision.** Tools return:
- **To the LLM:** the smallest sufficient summary to ground narrative (headline stat, top-N rows, summary stats — varies by tool).
- **To the result store:** the full artifact (DataFrame, config, etc.), addressed by a result ID.

The bot's response to the caller composes both: narrative from the LLM, full artifact dereferenced from the result store.

**Consequences.**
- Token-efficient by construction.
- Tools become slightly opinionated about "what does the LLM need to talk about this well?" — this is correctly each tool's concern, not the agent loop's.
- The architecture documents but does not specify what "minimal summary" looks like per tool; that's a tool-design decision per analysis tool.

---

## AD-11: Tool composition surface — three layered patterns

**Context.** Pydantic-ai supports tool registration at construction (`tools=[...]`), persistent runtime addition (`FunctionToolset.add_tool()`), per-call injection (`agent.run(toolsets=[...])`), and dynamic per-step (`@agent.toolset(per_run_step=True)`). The architecture must pick a public surface.

**Decision.** Three layered patterns, all using clean library-level idioms:

| Pattern | API | Use |
|---|---|---|
| Construction-time, permanent | `Bot(tools=[...])` | Bot's baseline identity |
| Persistent runtime addition | `bot.add_tool(...)` | Plugin-style attachment |
| Per-call ephemeral | `bot.chat(..., extra_tools=[...])`, `bot.ask(..., extra_tools=[...])` | Scoped capabilities for one turn |

A fourth pattern — a generic `bot.as_tool()` / `bot.add_bot(other_bot)` that
auto-converts any bot into a callable tool, so an orchestrator is just a bot
whose tools include other bots' `as_tool()` outputs — is deferred. See
Section 7, ND-11. In v1, cross-bot composition is achieved by wrapping the
target bot's `ask()` in a plain function and registering it via `add_tool()`,
which the three patterns above already support.

**Consequences.**
- No `remove_tool()` API — per-call injection already covers the scoped case.
- Pydantic-ai's `@toolset(per_run_step=True)` decorator is not exposed at v1 (power-user territory; users can subclass to access it).
- Cross-bot composition in v1 is a hand-written wrapper function per pairing, not a zero-code `as_tool()` call — see ND-11 for when that's revisited.

---

## AD-12: Ibis-based result handling

**Context.** As of AITAEM v0.4.0, `MetricCompute.compute()` returns an `ibis.Table` reference (lazy) instead of a `pandas.DataFrame`. Materialization is explicit via `.to_pandas()` / `.to_pyarrow()`. The agent module is designed against this shape.

**Decision.** Tools in the agent module assume Ibis-return `compute()`. The result store stores **dual representation**:
- A materialized artifact (Arrow table, lazily promoted to pandas only when requested) for portability and serialization.
- A live Ibis table reference for downstream tool chaining, when available.

Analysis tools prefer the Ibis ref (predicate pushdown to the warehouse, or pushdown to the cross-backend tmp DuckDB introduced in v0.4.0) when alive; fall back to `ibis.memtable(artifact)` when not (e.g., after history reload, or after the producing `MetricCompute` is gone).

**Consequences.**
- Memory bloat from full materialization avoided in lazy mode.
- Live Ibis refs are valid only while the `MetricCompute` that produced them is alive. AD-16 commits the bot to holding one `MetricCompute` instance for its lifetime, so refs are live for the bot's lifetime by construction.
- Analysis tools can be implemented in either lazy or eager mode per tool; pushdown is the preference but not the requirement.

---

## AD-13: STANDARD_COLUMNS accessed by name, never by index

**Context.** AITAEM v0.2.1 added the `metric_format` column at index 5, shifting later columns. Position-dependent code in downstream applications has broken on additive AITAEM column changes historically.

**Decision.** All references to STANDARD_COLUMNS in the agent module use column names. A `NON_VALUE_COLS` set (or equivalent) identifies metadata columns; `metric_value` is identified by name.

**Consequences.**
- Future additive AITAEM column changes don't break the agent module.
- Tools that need to know which column is the value column ask by name.

---

## AD-14: No raw YAML parsing in the agent module

**Context.** Early downstream usage of AITAEM parsed YAML directly to extract spec metadata (entities, period types, etc.) for system-prompt assembly. AITAEM v0.2.0 exposed typed attributes (`SpecCache.metrics[name].entities` and similar), making YAML re-parsing unnecessary and brittle.

**Decision.** The agent module accesses spec metadata only via typed attributes from `SpecCache`. Any future metadata access needed is requested as an AITAEM API addition, not solved by YAML re-parsing.

**Consequences.**
- The agent module never depends on YAML structure beyond what AITAEM exposes.
- AITAEM's typed API is the contract; the YAML is implementation detail.

---

## AD-15: Multi-turn is the architectural default; `ask()` is a subset

**Context.** Single-turn-first architectures consistently struggle to retrofit multi-turn (history threading, tool result store continuity, prompt context window management).

**Decision.** Multi-turn (`chat()`) is the primary API. `ask()` is the stateless variant — implemented in terms of the same machinery, but does not retain history.

**Consequences.**
- History serialization is a first-class concern from v1.
- Tool result store lifetime decisions consider multi-turn use from the start.
- Existing single-turn implementations of similar agent stacks can adopt `aitaem.agent` and gain multi-turn for free.

---

## AD-16: `MetricCompute` lifetime matches bot lifetime

**Context.** AITAEM v0.4.0 added a `tmp_dir` parameter to `MetricCompute`, used for a scratch DuckDB file when a compute spans multiple source backends. The file is reclaimed when the `MetricCompute` instance is garbage-collected. This makes `MetricCompute` effectively stateful — it now owns an on-disk resource.

The agent module must take a position on `MetricCompute` lifetime. Three options were considered: per-tool-call (instantiate, compute, discard), per-turn (instantiate at start of `chat()`/`ask()`, discard at end), or per-bot (one instance for the bot's lifetime).

**Decision.** One `MetricCompute` instance per bot, constructed at bot construction time and held until the bot is garbage-collected or `bot.reset()` is called.

**Rationale.**
- Bot-as-session (AD-04) commits to "bot lifecycle = session lifecycle." `MetricCompute` lifecycle aligning is natural.
- Live Ibis refs (AD-12 dual representation) reference into the `MetricCompute`'s execution context — including its cross-backend tmp DuckDB. Per-call or per-turn lifetimes would invalidate refs prematurely, defeating the lazy-mode pushdown benefit.
- The bot is constructed per user/conversation, so the scratch DuckDB scope matches a single active user's session — clean for multi-tenant deployments.
- AITAEM's "GC reclaims the tmp file" promise works correctly: the bot holds a single reference; when the bot is GC'd, so is `MetricCompute`, so is the tmp file.

**Consequences.**
- The bot holds `self._metric_compute` from construction onward.
- `bot.reset()` (already in the primitives API) tears down and rebuilds the `MetricCompute` along with the rest of session state.
- For long-running daemon callers, the tmp directory is per-bot — the caller's bot lifecycle governs filesystem footprint. If they construct one bot per user request and discard, files churn but stay small per request. If they hold a long-lived bot, the file grows with cross-backend compute volume.
- `DefinitionBot` and `SetupBot` do not construct a `MetricCompute` (they don't compute metrics). No tmp file for them.

---

## AD-17: AITAEM operational parameters pass through opaquely via `compute_kwargs`

**Context.** AITAEM v0.4.0 added one operational parameter (`tmp_dir`); future minors may add more (connection pool sizes, query timeouts, retry policies, cache TTLs). Each addition could require updating the bot constructor signature, creating version coupling between `aitaem.agent` and `aitaem` core that we want to avoid even though both ship together.

Three designs were considered: explicit pass-through (list every `MetricCompute` parameter on the bot), typed wrapper dataclass (e.g. `ComputeOptions`), and opaque dict pass-through.

**Decision.** Opaque dict pass-through. Bots that construct `MetricCompute` accept:

```python
compute_kwargs: dict[str, Any] | None = None
```

Internally, the bot constructs `MetricCompute(spec_cache=..., connection_manager=..., **(compute_kwargs or {}))`.

**Why not the typed wrapper.** A wrapper dataclass would need to mirror AITAEM's parameter list — exactly the coupling we're trying to escape. A wrapper would only earn its complexity if the bot had opinions about AITAEM parameters (overriding defaults, computing values, etc.). It doesn't: the bot's relationship to `tmp_dir` (and every plausible future operational parameter) is "forward whatever the caller passes; use AITAEM's default otherwise."

**Consequences.**
- AITAEM owns its parameter surface, its defaults, and its versioning. New AITAEM parameters work the day they ship; no `aitaem.agent` release required.
- No type safety at the bot boundary for these parameters. Invalid keys surface as `TypeError` from `MetricCompute()` — error is immediate and the message points at the right place.
- Users discover available parameters via AITAEM's docs, not via an agent-module wrapper. Honest about who owns the API.
- `spec_cache` and `connection_manager` stay as top-level bot constructor parameters — they're operational inputs the bot uses directly (the SpecCache feeds prompt catalog assembly; the ConnectionManager may be needed by other bots for schema introspection independent of `MetricCompute`), not passthrough configuration.
- If the bot ever develops a genuine opinion about a specific AITAEM parameter (architectural reason to override the default, derive the value, or expose a bot-side default different from AITAEM's), that parameter is promoted to a named constructor kwarg at that point — a deliberate, semantically meaningful change.
