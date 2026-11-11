# Anthropic prompt caching under pydantic-ai 2.55

**Date:** 2026-10-12
**Sources:** Anthropic docs as of 2026-10-12; pydantic-ai-slim 2.55.0 source
**Used by:** `plans/agent_module/ARCHITECTURE.md` §7.3, §8, `plans/42-aitaem-agent-v3.md`

Related notes: [42-pydantic-ai-2.55-mechanics](42-pydantic-ai-2.55-mechanics.md) ·
[42-history-stubbing](42-history-stubbing.md)

---

## 1. Prefix order

Anthropic's prefix order is **tools → system → messages**; a change at or before a breakpoint
invalidates everything after it. Volatile content therefore belongs at the tail.

## 2. Settings

- Unified setting (new since 2.2): `Caching(retention='1h', messages=True)` capability
  (equivalently `ModelSettings(cache='1h')`).
- For Anthropic this sets instruction and tool-definition breakpoints plus the top-level automatic
  message breakpoint (3 of 4 slots). pydantic-ai also adds an extra breakpoint when a turn adds
  more than 18 blocks, guarding the 20-block lookback.
- `1h` writes cost 2x and pay off after two reads; `'5m'` fits when turns are typically seconds
  apart.
- Any explicit `anthropic_cache*` setting overrides the unified one.
- Minimum cacheable sizes: 512 tokens on current flagship models; 4,096 on Haiku 4.5 and
  Opus 4.5/4.6. Below them, caching silently does nothing.

## 3. What breaks a stable prefix

1. **Tool array:** any change in content or order. `prepare_tools` filtering or `tool_choice`
   filtering per turn changes it.
2. **Instructions:** static `InstructionPart`s are sorted before dynamic ones and sit under the
   cache breakpoint. Anything that varies (timestamps, functions) must not be in them.
3. **Dynamic instructions:** a dynamic instruction sits after the system breakpoint but still
   precedes messages, so any change to it invalidates the whole message cache.
4. **Toggling** `tool_choice`, thinking params, `effort`, or image presence between turns.

## 4. Volatile state: tail block

Content regenerated every request can be appended at the end of the outgoing messages behind a
`CachePoint`, in `wrap_model_request`, without being persisted to history (the harness
`SystemReminders` pattern; request-only edits made in `wrap_model_request` are not written back).

Alternative where the model supports it: an enqueued mid-conversation system message (not on
`claude-sonnet-5`, Foundry). It persists, so it must be superseded or stubbed later.

## 5. Tool search and progressive disclosure

- Deferred capabilities: see [mechanics §2](42-pydantic-ai-2.55-mechanics.md#2-deferred-capabilities-the-core-primitive-for-skills).
- Native tool search (`ToolSearch`, `defer_loading`): on Anthropic it keeps the cache stable
  (deferred tools are excluded from the cache key); the local fallback on other providers grows
  the tool array.

## 6. Observability

- The `Instrumentation` capability emits `pydantic_ai.cache.*` span attributes: hit ratio and
  collapse reason (`ttl_expired`, `compacted`, `unexpected`, `not_enabled`).
- Usage exposes `cache_read_input_tokens` and `cache_creation_input_tokens`.
- `anthropic_cache_diagnostics` is available for development.

## 7. Parallel workers

- Anthropic caches become readable only after the first response begins.
- A child with a different model, system prompt, or tools has its own cache anyway, so sharing the
  parent's prefix is not achievable.
- Short worker runs gain little from a rolling message breakpoint:
  `Caching(retention=…, messages=False)` caches tools and static instructions only.
- If many workers share a large identical prefix (same model, tools, system), sending one first
  and letting it begin before launching the rest lets the others read its cache. pydantic-ai has no
  built-in warm-up; a raw `max_tokens: 0` API call is possible.

## 8. Other providers

The same structure is correct for any prefix-caching provider; the unified `cache` setting is
translated per provider (OpenAI GPT-5.6+, Bedrock, OpenRouter have their own knobs).
Anthropic-only features (`tool_addition`, thinking-binding recovery) degrade gracefully.

---

## Update (2026-11-10): tail block hook with a `ProcessHistory` capability present

**Sources:** pydantic-ai-slim 2.55.0, executed with `FunctionModel` (Python 3.12); `ProcessHistory`
source (`capabilities/process_history.py`).

§4's claim that request-only edits made in `wrap_model_request` are not written back holds only
without a `ProcessHistory` capability on the agent. Calling the wrapper's `handler` runs every
capability's `before_model_request`, and `ProcessHistory.before_model_request` writes the messages
it receives into the run history (`ctx.messages[:] = messages`). Executed: a tail block appended in
`wrap_model_request` was stored in history alongside a no-op `ProcessHistory`, in either capability
order (two blocks stored after two turns).

A tail block appended in `before_model_request`, by replacing the last request in a new message
list (`dataclasses.replace(request_context, messages=...)`), with the capability listed **after**
`ProcessHistory`: the model received exactly one block on every request, the history processor
never saw it, and none was stored. Listed before `ProcessHistory`, it was stored.
