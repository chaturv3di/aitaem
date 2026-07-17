# Section 6 — Extensibility Surface

## Purpose

The agent module's second goal (G2 in Section 1) is to serve as a blueprint for custom agentic applications on AITAEM. This section names what users can extend, how, and what guarantees the architecture provides about stability.

---

## 1. Extension points

The architecture exposes five extension points, each with a representative use case.

### EP1 — Custom tools

The most common extension. A user adds a tool that wraps their own logic — a Slack notifier, a CRM lookup, a domain-specific calculator — and attaches it to a stock bot.

- **Via constructor:** `bot = QueryBot(..., tools=[my_tool])`
- **At runtime:** `bot.add_tool(my_tool)`
- **Per call:** `bot.chat(..., extra_tools=[my_tool])`

Tools follow the pydantic-ai tool contract. The architecture imposes one additional convention: if the tool produces an artifact, write it to the result store and return a result_id in the summary. This convention is enforced by example, not by the type system — power users can opt out if they have a good reason.

**Example use case:** an audit-logging tool that records every agent run to the caller's database. Adding it via `add_tool()` keeps logging a tool-level concern rather than wrapping the bot at the application boundary.

### EP2 — Cross-bot delegation (plain-function wrapping)

A generic `bot.as_tool()` / `bot.add_bot(other_bot)` that automatically converts any bot into a callable tool is **deferred** — see ND-11 in [`07-non-decisions.md`](./07-non-decisions.md). Only two convenience bots exist today (`QueryBot`, `DefinitionBot`); a generic version built against two data points risks the wrong shape for questions a third, structurally different bot (`SetupBot`) would raise — how a wrapped bot's trace and result store nest into the parent's, how a non-`str` payload collapses into one tool result, how `refused`/`error` status propagates through the wrapping call.

Today's escape valve — already sufficient for cross-bot delegation — is wrapping the target bot's `ask()` in a plain function and attaching it via EP1's `add_tool()`:

```python
async def ask_definition_bot(question: str) -> str:
    response = await definition_bot.ask(question)
    return response.narrative

query_bot.add_tool(ask_definition_bot)
```

This covers the same use cases a generic `as_tool()` would (cross-bot delegation, orchestration without a dedicated orchestrator class, domain-specific sub-bots) with library-level idioms already shipping in Phase 5.2 — no new API surface. A generic `as_tool()` / `add_bot()` can be layered on top later, purely additively, since it would itself reduce to `add_tool()` under the hood.

**Example use case:** a unified "analyst" bot that fronts QueryBot and DefinitionBot via hand-written wrapper functions — letting one conversation handle question-answering and spec authoring without the user choosing a sub-bot upfront.

### EP3 — Custom bots from primitives

Users who want a bot for a domain not covered by the convenience bots build their own from the primitives:

- Subclass `Bot` (or compose the primitives directly)
- Set the default tool list
- Provide the default system prompt
- Define a payload type for the response

The architecture commits that the primitives layer is a first-class public surface, not an implementation detail. This is the blueprint promise.

**Example use case:** an application that needs a `WorkspaceBot` wrapping QueryBot with workspace-aware logging, multi-tenant lifecycle, and UI-specific response augmentation — same primitives, the application's own opinionated assembly.

### EP4 — Prompt overrides

Each convenience bot ships with a default system prompt. Users override via subclass (overriding a `_default_system_prompt()` method) or constructor parameter.

The architecture is intentionally vague on the *mechanism* — subclass-only, kwarg, or registered-overrides. The decision is deferred to v1.x to avoid foreclosing a path. What's committed:

- Default prompts are inspectable and copyable (not encrypted, not generated).
- Users who override are responsible for preserving any guardrails embedded in the default (e.g. the Metric Precision Rule).
- A future, more granular prompt-fragment-override API may emerge.

### EP5 — Response payload extension

A user who wants a bot to return UI-specific metadata (e.g. a `visualization_type` hint for a chart toggle, or a `display_priority` field for prioritizing rendering) subclasses the bot's response payload type and configures the bot to use the extended type.

The base `BotResponse[PayloadT]` is generic. Subclasses parameterize `PayloadT` with their own model. App-specific UX fields live in the app's own subclass (e.g. `MyAppQueryPayload(QueryPayload)`), never leaking into the AITAEM payload type.

**Example use case:** any consuming application that has UI requirements not covered by the base `QueryPayload` — chart type hints, custom "no answer" copy, A/B-test markers — defines a payload subclass and keeps the agent module's types free of UI assumptions.

---

## 2. Stability guarantees

Versioning and stability discipline:

- **Convenience bot constructor signatures** — public, semver-stable. Adding optional parameters is a minor; removing or renaming parameters is a major.
- **Primitives base classes** — public, semver-stable. Same rules.
- **Default tool input/output schemas** — public, semver-stable. Changing the JSON schema the LLM sees is a major version change.
- **Default prompts** — public but **not** semver-stable in content. Prompts are tuned; tuning is expected. Users who override prompts opt out of these updates.
- **Internal pydantic-ai version** — pinned to a minor range in `pyproject.toml`. The agent module insulates users from pydantic-ai breaking changes within a major.
- **`RunTrace` / `BotResponse` field shapes** — public, semver-stable. The eval substrate is contract.

---

## 3. What extension does NOT enable

Architectural limits — things the extension surface deliberately does not support, and why:

| Not supported | Why |
|---|---|
| Modifying the result store schema | Internal to the bot; would break artifact retrieval for unrelated callers |
| Hooking pre- or post-tool execution arbitrarily | Out of scope for v1; event-hook surface is a v1.1+ extension |
| Changing the LLM-runtime from pydantic-ai | Forks the library; not supported via configuration |
| Adding tools mid-run from inside another tool | pydantic-ai supports this; the architecture doesn't expose it (too easy to misuse) |
| Removing or replacing default tools by name | Reorder/subclass instead; default tools are part of the bot's identity |
| Persistent cross-process state owned by the bot | NG1; caller owns persistence |

---

## 4. Versioning of `aitaem.agent` itself

`aitaem.agent` is a subpackage of `aitaem` — they ship together, same version. There is no version skew possible between the agent module and the core it depends on.

External stability follows semver:

- v1.0 — first stable release, public API frozen per the rules above.
- v1.x — additive: new bots, new default tools, new optional params, prompt tuning.
- v2.0 — breaking — major API rework. Migration guide required.

Streaming, event hooks, error taxonomy refinement, and prompt-override API are candidates for v1.x or v2.0 depending on whether they can be added compatibly.
