# Section 1 — Goals & Non-Goals

## Purpose

The `aitaem.agent` module is an opt-in extension that ships agent primitives and opinionated convenience bots on top of AITAEM's deterministic compute layer. It exists to make AITAEM consumable as "import a bot, get going" while staying composable enough to serve as a blueprint for custom agentic applications.

This section defines the module's intent at the architecture level. Everything downstream — components, contracts, implementation order — derives from it.

---

## 1. Goals

### G1 — Onboarding velocity for AITAEM

Today, an AITAEM user must hand-roll the assembly of `SpecCache.from_string(...) → ConnectionManager.add_connection(...) → MetricCompute(...).compute(...)`, plus any LLM glue they want, plus any tool-calling scaffolding. `aitaem.agent` reduces this to:

```python
from aitaem.agent import QueryBot
bot = QueryBot(spec_cache=..., connection_manager=..., model="anthropic:claude-...")
response = await bot.ask("What was Q4 revenue?")
```

Two lines, not a hundred.

### G2 — Self-evidencing reference for AITAEM extension

The agent module's structure is intentionally legible. Primitives are exposed underneath the convenience bots so that anyone who wants to build a custom agentic application on AITAEM can compose their own bots without forking the library or copying boilerplate. Convention exposed: the convenience bots are themselves built on the public primitives; downstream apps build the same way.

### G3 — One-way dependency, optional install

`aitaem.agent` depends on `aitaem` core. `aitaem` core never depends on `aitaem.agent`. Users who don't install the `agent` extra must still be able to import `aitaem` and use its core APIs. The agent module's existence is invisible to non-users.

### G4 — Multi-turn from day one

The architecture assumes multi-turn conversation as the primary mode. Single-turn (`ask()`) is a subset of the multi-turn API, not the other way around. This avoids the retrofit problem.

### G5 — Eval-substrate compatibility

Every bot exposes a structured trace and a portable conversation history, designed from day one to support offline eval harnesses, regression testing, and prompt iteration. See Section 5 for the substrate, Section 5 of the evals research for framework choice analysis.

### G6 — Token-efficient by construction

Tools return minimal summaries to the LLM; full artifacts (DataFrames, configs) live in a session-scoped result store. The LLM narrates; it does not round-trip bulk data. This is a hard architectural commitment, not an optimization.

### G7 — Provider-agnostic LLM layer

The agent module uses pydantic-ai under the hood. pydantic-ai supports OpenAI, Anthropic, Bedrock, Gemini, Groq, Mistral, Together, and others via model strings. Users pick their provider; the agent module does not constrain.

### G8 — Composable tool surface

Three layered patterns for tool attachment:
- Constructor `tools=[...]` for baseline identity
- `bot.add_tool()` for persistent runtime addition
- `bot.chat(..., extra_tools=[...])` for per-call ephemeral injection

A generic `bot.as_tool()` / `bot.add_bot()` for zero-code bot-as-tool composition is deferred
(see Section 7, ND-11). In v1, an orchestrator is built as a bot whose tool set includes other
bots' `ask()` wrapped in plain functions and attached via `add_tool()`.

---

## 2. Non-Goals

### NG1 — Credential and persistence ownership

The agent module never:
- Stores credentials
- Persists conversation history to disk or remote storage
- Manages multi-tenancy or RBAC
- Holds connections to databases other than via the caller-provided `ConnectionManager`

These are caller concerns. A `SetupBot` that helps a user configure a connection returns a config dict; it does not call `ConnectionManager.add_connection()` itself.

### NG2 — Streaming response API

Not in v1. The architecture doesn't preclude streaming — the response shape is the same; chunks arrive incrementally — but the bot API surface for streaming is deferred.

### NG3 — Observability event hooks

Section 5 commits to an aggregated trace on the response object now, designed eval-friendly. An event-stream observability hook (`@bot.on_tool_called`, etc.) is a future extension point, not in v1.

### NG4 — Error taxonomy

A `Status.error` is signaled in v1 with a free-text `reason` string. A typed error_kind (connection_failed, tool_timeout, validation_failed, etc.) may follow, but the type system isn't part of the v1 architecture.

### NG5 — Default prompt customization API

Bots ship with default prompts. Whether users override via subclass, constructor kwarg, or registered overrides is a v1.1+ question. The architecture must not foreclose any path, but doesn't commit to one.

### NG6 — Default tool catalogue beyond QueryBot's compute + analysis tools

The architecture defines what tool surfaces look like; it does not specify which analysis tools ship by default. A working set (`rank_by_value`, `filter_by_threshold`, `distribution_summary`, `period_over_period`, `contribution_share`) is a strong starting catalogue based on common analytical patterns, but its exact composition is a Section 8 ordering decision, not an architectural one.

### NG7 — Removal of tools at runtime

`add_tool()` is supported; `remove_tool()` is not. Per-call `extra_tools=[...]` already covers the "this tool for one turn only" case. Adding `remove_tool` introduces history-consistency questions that don't earn their cost.

### NG8 — Hosting AITAEM

The user has explicitly opted for AITAEM as a pure PyPI library. Hosting is a separate, post-MVP question. The agent module's design must not assume a hosted backend exists.

### NG9 — Hot-reload of SpecCache

A bot is constructed with a `SpecCache` instance; the bot does not poll or refresh it. If the caller updates specs, they construct a new bot. This keeps in-memory state coherent.

---

## 3. Audience

The architecture document — and the agent module it describes — is intended for:

1. **The user (architect / lead).** For high-level design alignment and downstream Claude Code planning.
2. **Claude Code.** For generating detailed, low-level implementation plans grounded in the architecture.
3. **(Eventually) third-party AITAEM users.** Once the module ships, the convenience-bot surface and primitives surface form the public API contract.

The agent module's *test suite* — including the reference eval harness if we ship one (see Section 5, open question 6) — also serves as documentation by example.
