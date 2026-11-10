# pydantic-ai 2.55 mechanics relevant to `aitaem.agent`

**Date:** 2026-10-12
**Sources:** pydantic-ai-slim **2.55.0** and pydantic-ai-harness **0.55.0** (source read from the
PyPI wheels). An earlier pass against the locally installed 2.2.0 was superseded; differences in §6.
**Used by:** `plans/agent_module/ARCHITECTURE.md` §6, §11, §14, `plans/42-aitaem-agent-v3.md`

Related notes: [42-anthropic-prompt-caching](42-anthropic-prompt-caching.md) ·
[42-history-stubbing](42-history-stubbing.md) · [42-subagent-mechanics](42-subagent-mechanics.md)

---

## 1. Multi-turn state: `Conversation`

`pydantic_ai.conversation` (2.55) carries messages, accumulated `RunUsage`, `conversation_id`,
and pending deferred-tool requests. Passing `conversation=` on each run means `UsageLimits`
persist across turns, which bare `message_history=` does not give. Serialization uses
`ConversationTypeAdapter`.

## 2. Deferred capabilities (the core primitive for skills)

Core has **no** Skills API. The core primitive is
`Capability(id=…, description=…, instructions=…, tools=[…]/toolsets=[…], defer_loading=True)`.
Mechanics read in `capabilities/_deferred_capability_loader.py`,
`toolsets/_deferred_capability_loader.py`, `_deferred_capabilities.py`:

1. The prompt carries a catalog of every deferred capability (`- id: description`), listing
   loaded and unloaded ones alike, so the rendering is byte-stable and the cached prefix never
   changes on load. Descriptions must be static strings.
2. The framework adds a reserved `load_capability(id)` tool. Its result is the capability's
   instructions as an **ordinary tool-return in message history** (append-only), plus
   `ToolReturn(tools=[…])` naming the tools it reveals, recorded as a `ToolAvailabilityDeltaPart`.
3. A deferred capability's tools are **hard-gated** (2.30+): they cannot be called or seen until
   the capability is loaded and the tool revealed.
4. Loaded state is **reconstructed from history** every request
   (`LoadCapabilityCallPart`/`LoadCapabilityReturnPart` pairs since the last `CompactionPart`).
   Consequence: any history rewrite that drops a load pair or a `ToolAvailabilityDeltaPart`
   silently un-loads the capability; the model must reload it. Persisting the `Conversation`
   restores loaded capabilities across turns.
5. On Anthropic Messages models that support it (Opus 5.x, Sonnet 5.5, Haiku 5.5, Fable/Mythos;
   **not** `claude-sonnet-5`, Bedrock, or Vertex), revealed tools are delivered as a
   `tool_addition` system block, so the cache is not busted. Elsewhere pydantic-ai falls back to
   local tool-search exchanges (still append-only, but the tool array grows). **[verify]** per
   target model.
6. Attaching native tools to a deferred capability busts the cached prefix on load.

## 3. Harness components assessed

pydantic-ai-harness 0.55.0 pins `pydantic-ai-slim==` exactly, so adopting any harness component
forces an exact slim pin.

| Component | What it does | Fit issues for aitaem |
|---|---|---|
| `Skills` (`pydantic-ai-harness[skills]`) | SKILL.md-style skills | Needs a workspace; instructions-only (no tools, resources, or scripts) |
| `SubAgents` (`delegate_task`) | LLM-driven delegation | See [42-subagent-mechanics §3](42-subagent-mechanics.md#3-harness-subagents-assessment) |
| `ClearToolResults` | Tool-result clearing | See [42-history-stubbing §4](42-history-stubbing.md#4-build-vs-adopt-harness-cleartoolresults) |
| `TieredCompaction`, `SummarizingCompaction` | Compaction strategies | Usable as an escalation path; compaction resets loaded capabilities |
| `ToolOutputLimits(Spill)` | Spills oversized tool payloads out of context | Solves generically what an artifact-ID-only tool result guarantees by construction |
| `SystemReminders` | Request-only tail injection | Pattern reused for the tail state block ([caching §4](42-anthropic-prompt-caching.md#4-volatile-state-tail-block)) |
| `pin()` | Pins content against compaction | Semantics reusable for the session-state block |

## 4. Testing

- `FunctionModel` for deterministic parent behavior (capability loads, fan-out); `TestModel` or
  `FunctionModel` per worker via `override`.
- Loaded capabilities can be asserted with `parse_loaded_capabilities` (private module; pin the
  version if used).
- Presence of `models.ALLOW_MODEL_REQUESTS` in 2.55 is unconfirmed. **[verify]** or use a failing
  default model.

## 5. Observability

- The `Instrumentation` capability nests child runs under the tool-call span when agents are named
  (`name=`).
- 2.44+ reports per-run (not conversation-total) usage on run spans.
- Cache-health span attributes: see [caching §6](42-anthropic-prompt-caching.md#6-observability).

## 6. Changes vs. 2.2

| Topic | 2.2 assumption | 2.55 reality |
|---|---|---|
| History hook | `Agent(history_processors=…)` / `ProcessHistory` | Argument removed; `ProcessHistory` capability, edits persist into run history |
| Orphaned tool pairs (#2050) | Must hand-roll atomic pair dropping | Automatic repair at run start and on the wire; public `repair_messages`, `sanitize_messages` |
| Multi-turn state | `message_history` | `Conversation` (messages + usage + conversation_id + deferred requests) |
| Skills / progressive disclosure | Needed custom design | `Capability(defer_loading=True)` + `load_capability`, hard gating, `ToolAvailabilityDeltaPart`, state rebuilt from history |
| Caching | Per-provider `anthropic_cache_*` only | Unified `cache` setting + `Caching` capability; `prompt_cache_outlook`; cache-health span attributes; `anthropic_cache_diagnostics` |
| Dynamic state injection | Dynamic instructions | Tail block via `wrap_model_request` (instructions would bust the message cache) |
| Compaction | Not available | `CompactionPart`, `AnthropicCompaction`, `OpenAICompaction`; harness strategies (`ClearToolResults`, `TieredCompaction`, …) |
| Thinking blocks | Not considered | Client-side rewrites break binding; library retries with `drop_block` |
| Subagents | Hand-rolled delegation | Still hand-rolled in core; harness `SubAgents` exists but untyped returns / shared deps |
| Concurrency | Shared limiter | Sharing a limiter between parent and child wrappers now raises (2.53) |
| Deprecated | — | `RunContext.capability_loaded`/`available_capability_ids` → `capability_active`/`active_capability_ids`; appending to `request_context.messages` in `before_model_request` |

## 7. Minimum versions per feature

| Feature | Minimum |
|---|---|
| `ToolAvailabilityDeltaPart` / `ToolReturn.tools` | 2.26 |
| `CancellationToken` | 2.26 |
| Hard-gated `load_capability` | 2.30 |
| `ctx.context_window_used` | 2.38 |
| `prompt_cache_outlook` | 2.52 |
| `repair_messages` public; parent/child shared-limiter `UserError` | 2.53 |
| `Conversation` | 2.55 |

---

## Update (2026-11-10): Python floor and mechanics confirmed by execution

**Sources:** pydantic-ai-slim 2.55.0 in an isolated `uv run --no-project` environment (Python
3.12), with `FunctionModel`; `uv` resolution of `pydantic-ai-slim[anthropic]` on Python 3.10.

- **Python floor:** pydantic-ai-slim 2.55.0 requires Python ≥ 3.11. On Python 3.10 the newest
  release that resolves is 2.54.0, which predates `Conversation` (§7).
- **`Conversation` is returned, not mutated (§1):** a run reads the `Conversation` passed as
  `conversation=` and returns the next state as `AgentRunResult.conversation`. Passing the
  original object again to resume a deferred call raises `UserError: Tool call results were
  provided, but the message history is empty.`
- **Deferred tool calls:** a tool that raises `CallDeferred(metadata=…)` ends the run with a
  `DeferredToolRequests` output (with `output_type=[str, DeferredToolRequests]`); the metadata is
  available per `tool_call_id`, and `result.conversation.deferred_tool_requests` holds the pending
  call. Running again with `conversation=result.conversation` and
  `deferred_tool_results=DeferredToolResults(calls={tool_call_id: value})` feeds `value` back as
  the tool's return, and the model continues from it.
- **`models.ALLOW_MODEL_REQUESTS`** exists in 2.55 (resolves the §4 **[verify]**).
- **Tool schemas through a decorator:** a tool function wrapped with `functools.wraps` keeps its
  docstring as the tool description and its parameters as the tool's JSON schema.
