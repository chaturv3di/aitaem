# Agent workflow options

**Date:** 2026-10-24
**Sources:** design discussion, 2026-10-24; user observations from testing the v2 `DefinitionBot`
**Used by:** `plans/agent_module/ARCHITECTURE.md` §1, `plans/agent_module/WORKFLOWS.md` (pending), `plans/42-aitaem-agent-v3.md`

Related notes: [42-subagent-mechanics](42-subagent-mechanics.md)

---

Candidate task types in aitaem:

- Setting up backend connection(s)
- Creating new specs, based on research of the tables available in the backend
- Planning and executing a list of analytical tasks to answer user queries (aitaem metric
  computes, statistics on the results, basic analysis)

## Option 1: Main session → coordinator → executors (written handoffs)

- The main session writes instructions and parameters for the coordinator in an intake document.
- Coordinators never interact with the user directly.
- The coordinator spins out executors via similar written instructions.
- Executors return results individually.
- The coordinator compiles results, resolves (or flags) conflicts, and writes a single handoff for
  the main session.
- The main session reports the summary, answers user questions, and can re-trigger the task (a new
  coordinator spawn) with the intake document updated from user feedback.

## Option 2: Main session suspends; the coordinator talks to the user

- Same coordinator + multiple identical executors setup.
- The user speaks directly with the coordinator, which conducts one specific task only.
- When the task is complete, the coordinator writes the handoff document and control returns to
  the main session.

## Option 3 (hybrid): Option 1 with a needs-input handoff

- Same as Option 1, but the coordinator can end with a *needs-input* handoff instead of a final
  answer.
- The main session asks the user, updates the intake document with the answers, and re-spawns the
  coordinator, which reuses the earlier results instead of redoing them.

**A coordinator never stops while executors are running.** If it could ask the user a question
with executors in flight, it would be holding a live conversation over running work, which is
Option 2 in all but name. It can stop only at two checkpoints:

1. **Before dispatch:** while planning, the coordinator checks the intake against the specs
   (resolving metrics, applying the Metric Precision Rule). Most ambiguity surfaces here, before
   any compute is spent.
2. **After synthesis:** every executor has finished and the coordinator has reviewed all results.
   It then concludes that it is done, needs more information, or that the analysis is
   inconclusive.

Between two consecutive fan-out calls no executors are running, so that gap is also a valid
checkpoint.

This is enforced by structure rather than prompt: executors are launched by one fan-out tool that
awaits all of them ([42-subagent-mechanics §1–2](42-subagent-mechanics.md#1-delegation-primitives-read-in-source)),
so the coordinator cannot act until every executor has returned.

Consequences:

- Every result of a run is complete when the coordinator stops, and it is already in the
  session-scoped artifact store shared by the main session and coordinators. The main session can
  preview and run stats on it; a re-spawned coordinator reuses it by ID. There is no half-done
  work to track.
- Cost: an early executor result can reveal an ambiguity that makes sibling executors' work
  wasted. The pre-dispatch check catches most such cases, and `UsageLimits` bounds the waste;
  cancelling executors mid-flight would bring back the complexity this option avoids.

Handoff outcomes:

| Outcome | Carries | Main session then |
|---|---|---|
| **complete** | Answer summary + result artifact IDs | Reports to the user |
| **needs-input** | Questions for the user + result artifact IDs (none if stopped before dispatch) | Asks the user, updates the intake, re-spawns |
| **inconclusive** | Why the results don't support a conclusion + result artifact IDs | Reports; may or may not ask the user anything |

## Feasibility of Option 2

Feasible in aitaem because the library owns the routing: the `AitaemAgent` facade's `chat()` can
send each user message to whichever agent is active. The coordinator's output is either a message
for the user or a final handoff; the handoff switches routing back to the main session. While
suspended, the main session's conversation is simply not called. This is a small facade state
machine, not a pydantic-ai feature.

## Comparison

| | Option 1 | Option 2 | Option 3 (hybrid) |
|---|---|---|---|
| Testability | Strong: a coordinator run is a pure function of intake → handoff | Weaker: depends on the dialogue | Strong, as Option 1 |
| Mid-task clarification | Costly: stop, ask via main session, restart from scratch | Natural | Moderate: one round trip at a checkpoint (before dispatch or after synthesis); all results reused |
| Fidelity to the user's words | Main session paraphrases into the intake; nuance can be lost | Direct; the main session sees only the handoff | As Option 1 |
| Complexity | One conversation to persist | Two conversations plus routing state; the coordinator needs its own history handling; off-topic drift needs an exit path | As Option 1, plus the needs-input handoff type |
| Main session context | Delegated work adds only intake + handoff documents | Same; its prompt cache may expire during a long coordinator dialogue | As Option 1 |

All options imply delegation depth 2 (main → coordinator → executors). Intake and handoff
documents fit naturally as typed artifacts exchanged by ID.

## Task fit

| Task | Characteristics | Best fit |
|---|---|---|
| Connection setup | Few steps, a couple of user decisions, little parallelism | Option 3 |
| Spec creation | Heavy table research (parallel executors) **and** several turns of user dialogue | Option 2 |
| Analytical questions | Plan → parallel computes → compile; user involved mostly at start and end | Option 3 |

Evidence for spec creation needing multi-turn dialogue (user observation from v2 `DefinitionBot`
testing): drafts repeatedly missed `entity` fields and metric formatting hints, and formulas
sometimes needed clarification. This was also the reason `DefinitionBot` was a separate bot in v2.

## Constraint: main-session context

The v2 `QueryBot` carries substantial instructions (how to call tools, what to check, what not to
do) even for small requests. With Haiku as a target model, the main session's context is the
scarcest resource. Those instructions are needed for computing, backend access, and spec changes,
so that work belongs with the coordinators, even when the request is small.

A main session with no analysis capability at all is too thin, though:

- To draft a correct intake for the analysis coordinator, the main session must know which metrics
  are already computed and what their values look like (e.g. to scope a follow-up question).
- Follow-up questions about existing results ("which country was highest?") can be answered from
  computed results without a coordinator run.
- When one request spans several coordinators run in sequence, the main session must understand
  each handoff before writing the next intake.

Read-only recall plus basic stats on table artifacts is expected to need far lighter instructions
than computing (not yet measured). The existing `stats` group touches table artifacts only and has no backend handle,
so the boundary can be enforced by which tool groups the main session receives.

## Handoff document format

Intake and handoff documents are written by one LLM for another (and read by people debugging a
session), unlike tool results, which are produced by code.

- **YAML with free-text fields** suits LLM authors: no quote escaping as in JSON, multi-line text
  reads naturally, and the documents stay readable for a human.
- **Some fields drive code, not just reading:** the facade routes on the handoff's outcome, and a
  re-spawned coordinator reuses its result IDs. Those few fields need machine checking; the rest
  (summary, reasoning, caveats, conflicts) can stay free text.
- **Checking mechanism:** the coordinator's output is a string; an output validator parses it as
  YAML, validates the checked fields against a Pydantic model, and asks the model to retry on
  failure (`ModelRetry`). **[verify]** output validators and `ModelRetry` behaviour in
  pydantic-ai 2.55.
- Tool results stay Pydantic models, since code produces them.

## Outcome

Decisions are recorded in `plans/agent_module/ARCHITECTURE.md` §1.

---

## Update (2026-11-10): output validator on YAML text output confirmed

**Sources:** pydantic-ai-slim 2.55.0, executed with `FunctionModel` (Python 3.12).

An `@agent.output_validator` on `output_type=str` that parses the text with `yaml.safe_load`,
validates it against a Pydantic model, and raises `ModelRetry` on failure sends a retry prompt
(`retry-prompt` part in history) and accepts the model's corrected second output. A YAML block
scalar containing quotes parsed back intact. This resolves the **[verify]** under "Handoff
document format".
