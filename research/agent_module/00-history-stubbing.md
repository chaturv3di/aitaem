# Multi-turn history: stubbing, compaction, thinking-block binding

**Date:** 2026-10-12
**Sources:** pydantic-ai-slim 2.55.0 and pydantic-ai-harness 0.55.0 source; Anthropic docs as of
2026-10-12; arXiv 2508.21433
**Used by:** `plans/agent_module/ARCHITECTURE.md` §9

Related notes: [00-pydantic-ai-2.55-mechanics](00-pydantic-ai-2.55-mechanics.md) ·
[00-anthropic-prompt-caching](00-anthropic-prompt-caching.md)

---

## 1. Why rewrites are costly

- Rewriting old messages invalidates the prompt cache from the first changed byte.
- On current Anthropic thinking models it can also invalidate thinking-block binding (§5).
- Hence the case for append-only history with rare, batched rewrites, preferably when the cache is
  already cold (a cold cache makes the rewrite nearly free).

## 2. pydantic-ai hooks

- `ProcessHistory` capability replaces the removed `Agent(history_processors=…)` argument; its
  edits **persist** into run history.
- Edit parts with `dataclasses.replace`; mutating in place triggers `MessageHistoryMutatedWarning`
  and corrupts the OTel serialization cache.
- Trigger inputs: `ctx.context_window_used` (2.38+; needs a known `context_window`) and
  `prompt_cache_outlook(messages, …)`, which can report `'cold'`.
- Dropping `load_capability` returns, `LoadCapability*` parts, or `ToolAvailabilityDeltaPart`s
  un-loads capabilities ([mechanics §2](00-pydantic-ai-2.55-mechanics.md#2-deferred-capabilities-the-core-primitive-for-skills)).
  The `load_capability` and `search_tools` tools need explicit exclusion from any stubbing.

## 3. Orphaned tool pairs

pydantic-ai 2.55 repairs history at run start and again on the wire (`repair_messages`, public
since 2.53): it drops orphaned returns and synthesizes "interrupted" returns for dangling calls.
This removes the need for a custom defense against provider 400s, but the repair can silently lose
content, so keeping call/return pairs intact still matters. Upstream issue #2050 is still open; its
symptom is handled by the repair pipeline.

## 4. Build vs adopt: harness `ClearToolResults`

`ClearToolResults(max_fraction, keep_pairs, min_clear_tokens, exclude_tools)` is the same
mechanism as artifact stubbing, but writes one static placeholder string (cannot name the artifact
each result was stored as) and carries the harness's exact slim pin. A custom capability that
emits per-artifact stubs is estimated at ~100 lines and can borrow its structure.

## 5. Thinking-block binding

On the 5.x Claude models with extended thinking, the API validates that everything before each
returned thinking block is byte-identical; clearing or shortening an earlier tool result breaks
the binding. Accounts created on/after 2026-08-31 get a 400 by default; pydantic-ai 2.55 recovers
by retrying once with `drop_block` (`AnthropicStaleThinkingBlockWarning`), which costs one failed
request per stubbing batch and loses that reasoning.

Options:

- **(a)** Each stubbing batch also drops/summarizes thinking in the rewritten range (keep-tail
  style). Recommended if thinking is on.
- **(b)** Server-side `clear_tool_uses_20250919` (beta `context-management-2025-06-27`), which does
  not break binding. Reachable via `anthropic_context_management` in 2.55, but pydantic-ai adds
  only the compaction beta header, does not surface `applied_edits`, and the server placeholder
  cannot name the artifact. **[verify]**
- **(c)** Default the agent to thinking off.
- **(d)** Accept one retry per batch.

## 6. Compaction

- Provider-native: `AnthropicCompaction` (threshold flavor, `CompactionPart`) is Anthropic-only
  and beta; the newer on-demand flavor (`compact-2026-09-04`) is not wired in 2.55.
- Harness: `TieredCompaction`, `SummarizingCompaction`.
- Any compaction resets loaded-capability state (state is rebuilt from parts since the last
  `CompactionPart`); the model must reload.

## 7. Evidence on masking vs summarization

Observation masking matched or beat LLM summarization at about half the cost in a coding-agent
study (arXiv 2508.21433). Transfer to analytics conversations is untested; answer fidelity after
stubbing needs its own eval.
