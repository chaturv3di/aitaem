# Section 7 — Explicit Non-Decisions

## Purpose

The architecture takes positions on what *is* in scope for v1. This section names what is *deliberately not* — what we're punting on, why, and what makes the punt safe (i.e., why we can defer without painting ourselves into a corner).

Each non-decision is paired with an "escape valve" — the path forward we are leaving open.

---

## ND-01: Streaming response API

**What's deferred.** No streaming surface on `Bot.chat()` / `Bot.ask()` in v1. Responses are returned whole.

**Why safe.** Streaming is incremental delivery of the same response shape — chunks of narrative, then a final structured payload. The architecture commits to a structured response; streaming can be added later as an alternative method (e.g. `Bot.chat_stream()` yielding partial responses) without changing the existing API.

**Escape valve.** pydantic-ai supports streaming. When users need it, expose `chat_stream()` returning an async iterator of partial responses. No architectural changes required.

**Trigger to revisit.** First real user need (interactive UI with token-by-token rendering, or progress UX during long-running tool chains).

---

## ND-02: Event-stream observability hooks

**What's deferred.** No `@bot.on_tool_called` / `@bot.on_model_request` / event-emitter surface. Observability in v1 is "consume the aggregated `RunTrace` after the turn."

**Why safe.** AD-08 commits to aggregated trace now, designed so an event stream can be added later. The aggregated trace is OTel-compatible; observability backends (Logfire, Datadog, anything OTel-aware) can consume the same data via spans even in v1.

**Escape valve.** v1.x can add observability hooks as a parallel mechanism — `bot.on(...)` decorator, or `bot.events` async iterator — without touching the response shape.

**Trigger to revisit.** When real-time visibility into in-flight tool calls becomes a customer requirement (e.g. a "live transparency panel" that updates as the agent works, rather than rendering after the response).

---

## ND-03: Error taxonomy

**What's deferred.** `Status.error` with a free-text `reason: str`. No typed `error_kind` (`connection_failed`, `tool_timeout`, `validation_failed`, `model_overloaded`, etc.).

**Why safe.** Adding a `reason_kind` enum to the response in v1.x is additive — old callers who only read `reason` keep working; new callers can branch on `reason_kind`. No callers depend on the absence of typed kinds.

**Escape valve.** Type the kinds when they earn their cost (i.e., when downstream code routinely branches on string parsing of `reason`).

**Trigger to revisit.** First time we see callers writing `if "timeout" in response.reason:` — that's the signal the taxonomy is being demanded.

---

## ND-04: Default prompt customization API

**What's deferred.** Bots have default prompts. Users can override via subclass; we don't commit to a richer customization mechanism (kwarg override, fragment-level override, registry).

**Why safe.** Subclass-override is sufficient for advanced users today; convenience kwargs are pure additions later. The architecture doesn't lock in either path.

**Escape valve.** When users want it: `Bot(system_prompt=...)` constructor kwarg, or a `prompt_fragments={"precision_rule": "..."}` overrides dict, or a registered-fragments system. All purely additive.

**Trigger to revisit.** First user who reports that subclassing-to-override-one-prompt is too heavy. Likely soon.

---

## ND-05: Default analysis tool catalogue composition

**What's deferred.** The exact list of analysis tools that ships in `QueryBot`'s default tool set. Section 3 names a working set (`rank_by_value`, `filter_by_threshold`, `distribution_summary`, `period_over_period`, `contribution_share`) based on common analytical patterns, but the architecture does not commit that exact composition.

**Why safe.** Bots in v1 accept `tools=[...]` and ship with a sensible default. Adding, renaming, or deprecating default tools is a backward-compatible operation as long as the existing default tool *names and schemas* are stable.

**Escape valve.** Real-world experience and user feedback during early v1 use will surface the right default set. We can ship the working set as v1.0 and refine in v1.x.

**Trigger to revisit.** Usage data showing which tools the LLM actually uses, and which it ignores or misuses.

---

## ND-06: Cost/token-budget controls

**What's deferred.** No built-in per-bot budget enforcement (e.g. "stop after $0.50 of inference" or "max 10 tool calls"). pydantic-ai exposes `UsageLimits`; we don't expose a higher-level interface.

**Why safe.** pydantic-ai's mechanism is reachable via subclass / advanced configuration. The bot's response already carries usage in `RunTrace`; callers can enforce budgets at their level.

**Escape valve.** Expose a `limits=...` constructor kwarg in v1.x mapping to pydantic-ai's UsageLimits.

**Trigger to revisit.** When users explicitly ask for this.

---

## ND-07: Hot-reload of SpecCache

**What's deferred.** A bot constructed with a `SpecCache` instance uses that instance for its lifetime. If the caller updates specs upstream (e.g. a user creates a new metric), the existing bot does not see the change. The caller constructs a new bot.

**Why safe.** Bot construction is cheap. The "new bot per session" pattern (AD-04) already implies fresh `SpecCache` lookups at construction time.

**Escape valve.** `bot.reset(spec_cache=new_cache)` could be added in v1.x without breaking the in-construction lifetime model.

**Trigger to revisit.** When users describe long-lived bot instances that need to track spec changes (probably never for `aitaem.agent`'s expected use; possibly for a hosted variant).

---

## ND-08: Concurrency / async semantics inside a single bot

**What's deferred.** Whether two concurrent `chat()` calls on the *same bot instance* are safe is not committed. The architecture's expectation is one call at a time per bot; concurrent calls require separate bot instances.

**Why safe.** This matches every real use case — a user is talking to one bot in one session. Concurrency lives at the caller level (a multi-tenant application with N users → N bot instances, one per active session).

**Escape valve.** If anyone wants concurrent calls on one bot, document it as unsupported and let them serialize at their layer. Or add an explicit lock if that ever becomes a real ask.

**Trigger to revisit.** Unlikely. Mentioned for completeness.

---

## ND-09: A formal `aitaem.agent` test/eval harness shipped in the repo — ✅ Resolved (Plan 29)

**Resolution.** Resolved in favor of shipping. `tests/evals/` ships in the repo, using `pydantic-evals`, covering both `QueryBot` and `DefinitionBot` (the original open question named `QueryBot` only; `DefinitionBot` exists now, so the harness covers it too). Runs in CI via the dedicated `evals` job against scripted `FunctionModel`s — no live LLM calls or API keys required. Demonstrates that the substrate (`RunTrace`, `ResultStore`, `BotResponse`) is wired correctly for `pydantic_evals.Evaluator`s to consume — not a behavioral/quality evaluation of either bot's actual decisions. See `plans/29-agent-phase6-evals.md`.

The rest of this entry is kept as historical record of the original open question and its framing.

**What was deferred (open question to user).** Whether the agent module ships a reference test/eval harness — e.g. `tests/evals/test_query_bot.py` demonstrating pydantic-evals wiring — or whether the eval substrate is documented and users build their own.

**Why this was non-deciding-able by architecture alone.** Either path works at the architecture level; the decision is about library scope and maintenance burden. Flagged in Section 5 as an open question for the user.

**Escape valve (as originally framed).** Start without; add later. Or start with, remove if maintenance burden outweighs value.

---

## ND-10: Multi-turn interactive refinement for DefinitionBot

**What's deferred.** `DefinitionBot.chat()` is implemented and accumulates message
history, but dedicated multi-turn UX for iterative spec refinement — where each
user turn adds a new constraint or correction and the bot revises the previous draft
across turns — is not the primary interaction model in Phase 3.

**Why single-turn for Phase 3.** Phase 3 DefinitionBot is a single-turn bot:
one `ask()` call, all schema exploration / drafting / validation loop within one
`agent.run()`, one `DefinitionResponse` returned. The user may pass existing/partial
YAML in the message text; the LLM picks it up and includes it in
`record_definition_intent(existing_yaml=...)`. This covers the most common case
without multi-turn complexity.

**User decision (verbatim).** "We will implement multi-turn support for both query
and definition bots at a later time." — confirmed in Phase 3 planning session.

**Scope of deferral.** Both `QueryBot` and `DefinitionBot` multi-turn refinement
flows are deferred to v1.x once the single-turn implementations are battle-tested
and usage patterns are understood.

**Escape valve.** `chat()` is already provided; enabling multi-turn refinement
is an additive prompt and workflow-instruction change — no API surface change required.
The natural v1.1 addition is a `get_prior_spec(spec_draft_token: str)` LLM-facing tool
that calls `store.get_text(token).text` to recover the full validated YAML from a prior
turn, making cross-turn revision lossless regardless of spec size. This also unblocks
multi-turn refinement for QueryBot (e.g. re-running a prior query with adjusted parameters)
via an analogous `get_prior_result(result_id)` tool.

---

## ND-11: `Bot.as_tool()` and `Bot.add_bot(other_bot)` — generic bot-as-tool composition

**What's deferred.** A generic `bot.as_tool()` that introspects a bot's `ask()`
signature/output type and produces a pydantic-ai `Tool` automatically, plus
`bot.add_bot(other_bot)` (sugar for `bot.add_tool(other_bot.as_tool())`). Both
exist today as `NotImplementedError` stubs on `Bot`, added during Phase 1
foundations. Phase 5.2 implements `Bot(tools=[...])`, `add_tool()`, and
per-call `extra_tools=[...]` (AD-11's other two patterns) but not these two.

**Why safe.** Only two convenience bots exist today (`QueryBot`,
`DefinitionBot`); `SetupBot` (Phase 4) hasn't been built yet. A generic
`as_tool()` has to make cross-cutting design decisions — how the wrapped
bot's `RunTrace` and result store nest into the parent bot's, how a
non-`str` payload collapses into a single tool-call result, how errors and
`refused` status propagate up through the wrapping tool call — and there
are only two bots' shapes to design against right now. Building the generic
version before a third, structurally different bot exists risks guessing
the wrong shape and having to break it later. `add_tool()` (shipping in
Phase 5.2) is the one primitive any bot-as-tool wrapper would be built on
regardless, so nothing about the generic version is blocked by deferring it.

**Escape valve.** Wrap `ask()` as a plain function and register it via
`add_tool()`:

```python
async def ask_definition_bot(question: str) -> str:
    response = await definition_bot.ask(question)
    return response.narrative

query_bot.add_tool(ask_definition_bot)
```

This covers today's cross-bot delegation need with the library-level idiom
already shipping in Phase 5.2 — no new API surface required. A generic
`as_tool()` / `add_bot()` can be layered on top later, purely additively,
since it would itself reduce to `add_tool()` under the hood.

**Trigger to revisit.** Once `SetupBot` exists and there's a concrete
three-bot orchestration scenario to design against, or once hand-writing the
wrapper function per bot pairing becomes repetitive enough (three or more
pairings) that the boilerplate itself becomes the complaint.

---

## What's NOT a non-decision

For clarity: these are decisions the architecture *has* taken, not punts:

- Multi-turn (`chat()`) is the primary API. Single-turn (`ask()`) is a subset.
- The result store is on the bot, not on responses; lifetime is session-scoped.
- Responses carry result IDs, not artifacts; artifacts are retrieved via `bot.get_result(id)`.
- History serialization includes result store artifacts.
- The trace structure is OTel-compatible.
- pydantic-ai is the runtime.
- AITAEM core never imports from `aitaem.agent`.
- The agent module is gated by an optional install (`aitaem[agent]`).
- Tools return minimal LLM-facing summaries; never round-trip bulk data through the LLM.
- AITAEM's Ibis-return `compute()` is the assumed shape; migration is a hard prerequisite.

These are committed in Section 2 and the architecture follows from them.
