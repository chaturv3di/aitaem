# Section 5 — Evals Substrate

## Purpose

Evals are a first-class concern of the agent module. Two questions to answer:

1. **What does the agent module owe an eval harness?** This is the substrate question — what data the architecture must surface so any eval framework can be wired in.
2. **Which framework should we use?** Trade-off analysis across the three serious candidates, with attention to multi-turn flows, tool-call correctness, deterministic ground truth (we *know* what `MetricCompute` should return for a given spec+params), and forthcoming RAG flows.

The architecture commits to a substrate. The library choice is presented for the user's decision with a recommendation.

---

## 1. What the agent module surfaces as eval substrate

Three things, all already in the architecture:

### Trace (per turn)

The aggregated `RunTrace` returned in every bot response (Section 3) is the primary eval substrate. It must be:

- **Replay-sufficient** — given `(initial spec_cache state, model, history-before-turn, user message, RunTrace)`, an eval harness can reconstruct what the LLM saw and what it did. Tool call arguments are captured as structured dicts (not stringified blobs). Tool return values seen by the LLM are captured verbatim.
- **Self-contained per turn** — no cross-turn references inside a single trace. Multi-turn evaluation aggregates traces externally.
- **Serializable** — JSON-compatible. The `RunTrace` is a Pydantic model; `model_dump()` is the canonical serialization.
- **Eval-friendly by structure**, not by adapter. Specifically: `tools_called: list[ToolCall]` where each entry carries `name`, `args: dict`, `result_id: str | None`, `summary_returned_to_llm: dict`, `success: bool`, `duration_ms: int`. This shape is consumable directly by any eval framework's "tool-use" scorer.

### History

`dump_history()` produces a portable conversation record (Section 3). Combined with traces, this gives an eval harness everything it needs to replay multi-turn flows.

### Result store snapshot (optional)

For evals that need to verify the *actual computed value* (not just the narrative), the eval harness can call `bot.get_result(result_id)` to retrieve the materialized artifact for any result ID referenced in the trace. This is the bridge between "LLM behavior correctness" and "computational ground truth" — two evaluation dimensions that the spec-driven architecture makes uniquely possible.

### Why this substrate is special

In most LLM applications, the model's narrative *is* the answer. Evaluators have to judge text quality or use LLM-as-judge. In the AITAEM agent module, the narrative is downstream of a deterministic compute — `MetricCompute.compute(metric="revenue", time_window=("2024-Q4", ...))` has a single correct answer. This means evals can include:

- **Deterministic correctness checks:** did the agent's `compute_metrics` call produce a result equal to the ground-truth-known DataFrame for this question?
- **Tool-selection correctness:** for question Q, did the agent call `compute_metrics` with the expected spec name and parameters?
- **Refusal correctness:** for an out-of-scope question, did the agent return `Status.refused` instead of substituting an approximate metric? (This is the Plan 018 "Metric Precision Rule" pattern, lifted to an eval.)

These are stronger signals than LLM-as-judge on narrative quality, and the substrate is what makes them cheap to compute.

---

## 2. Library candidates

Three serious options. I'm dismissing several others up-front: **Promptfoo** is yaml-config-driven and weak on multi-turn tool flows; **Ragas** is RAG-only and would need supplementing; **LangSmith/LangFuse** are observability platforms that include eval features but aren't primarily eval frameworks. The three below are eval-first.

### Candidate A — pydantic-evals

Standalone package (`pip install pydantic-evals`), authored by the pydantic-ai team but doesn't depend on pydantic-ai. Code-first: datasets, cases, evaluators are all Python classes.

**Architecture.** `Dataset` contains `Case`s, each with `inputs` + `expected_output` + optional metadata. A `task` function (the thing being evaluated) is run on each case. `Evaluator`s consume an `EvaluatorContext` (carrying inputs, outputs, expected, *and an OpenTelemetry SpanTree* of the task execution) and return scalar scores or assertions. `Dataset.evaluate_sync(task)` runs everything in parallel and returns an `EvaluationReport`.

**Distinguishing feature — span-based evaluation.** Because pydantic-ai instruments tool calls as OpenTelemetry spans, `EvaluatorContext.span_tree` is a structured trace of what the agent did. An evaluator can assert "this run called `compute_metrics` with `metric='revenue'`" by walking the tree, not by parsing logs. This aligns *exactly* with the trace substrate the architecture is committing to.

**Built-in evaluators.** `EqualsExpected`, `Contains`, `IsInstance`, `LLMJudge`. The set is intentionally small; custom evaluators are the norm.

**Multi-turn support.** Not opinionated; you write a task that runs N turns and pass the final state as output. The span_tree spans all turns. Works, but more code than deepeval's `ConversationalTestCase`.

**RAG metrics.** None built in. RAG metrics implementable as custom evaluators; no out-of-the-box faithfulness/contextual-recall/contextual-precision.

**Observability.** Native OTel; native Logfire integration; ships traces to any OTel backend.

**Maturity (as of 2026-06).** v1.10x. Stable, well-documented, growing adoption.

**License.** MIT.

### Candidate B — deepeval

Established LLM eval framework from Confident AI. Pytest-integrated. Wide metric catalogue (~50+).

**Architecture.** `LLMTestCase` for single-turn, `ConversationalTestCase` (a sequence of `Turn`s) for multi-turn. Each `Turn` has `role`, `content`, optional `tools_called`, optional `retrieval_context`. Metrics are classes (e.g. `ToolUseMetric`, `GoalAccuracyMetric`, `FaithfulnessMetric`); they're called on test cases.

**Distinguishing feature — RAG metric catalogue.** The RAG triad (`AnswerRelevancyMetric`, `FaithfulnessMetric`, `ContextualRelevancyMetric`) is referenceless and ready to use. Reference-based: `ContextualPrecisionMetric`, `ContextualRecallMetric`. Multi-turn RAG: `TurnFaithfulnessMetric`, `TurnContextualRelevancyMetric`, etc. This is the most mature RAG eval suite in any OSS framework.

**Multi-turn agent metrics.** `ToolUseMetric` (tool selection + argument correctness, multi-turn), `GoalAccuracyMetric` (goal-reach assessment), `ConversationCompletenessMetric`, `ConversationalGEval` (custom criteria over conversations), `TopicAdherenceMetric`, `MultiTurnMCPUseMetric`. The single largest multi-turn metric library available.

**Tool-call evaluation specifics.** `ToolUseMetric` uses LLM-as-judge to assess whether the agent selected the right tools with the right arguments across a conversation. Reference-based version possible by providing `tools_called` ground truth on test cases. Deterministic exact-match on tool name + args is also straightforward as a custom metric.

**Pydantic-ai integration.** First-class: `deepeval` ships a documented integration page for pydantic-ai. Pytest test runs invoke the agent; deepeval scores from the trace.

**Observability.** Doesn't expose OTel-native trace evaluation; metrics are computed over test cases that the user constructs from agent runs. Less "structural" than pydantic-evals' span_tree approach, more "score the conversation."

**Maturity.** Established (~2023). 50+ metrics. Active maintenance.

**License.** Apache 2.0.

**Concern worth flagging.** LLM-as-judge centricity. Most of deepeval's metrics, including some tool-use ones, internally call an LLM to grade. This is fine but: (a) costs add up at scale; (b) judge-model bias propagates; (c) for deterministic ground truth like AITAEM's compute results, LLM-as-judge is overkill and slower than an exact-match check. The framework supports custom non-LLM metrics, so this is mitigatable, but the bias of the metric catalogue is toward judge-based.

### Candidate C — Inspect AI

UK AI Security Institute. Heavy focus on capability and safety evaluation of frontier models. ~200 pre-built benchmarks, agent sandboxing toolkit, web-based viewer.

**Architecture.** `Task` = `Dataset` × `Solver` × `Scorer`. Solver is the elicitation strategy (chain-of-thought, agent loop, tool use); scorer compares output to target. CLI-first (`inspect eval task.py --model anthropic/claude-...`).

**Distinguishing features.** Sandboxing (Docker/K8s) for evaluating agents that execute code; tool-approval policies for human-in-the-loop; native multi-turn dialog and agent primitives; first-class tool calling; ACP (Agent Client Protocol) support.

**Strengths for our use case.** Tool calling is first-class. Multi-turn is first-class. Reproducibility is excellent.

**Weaknesses for our use case.** Designed for *evaluating arbitrary models* against benchmarks — its assumption is that the model under test is the variable. For evaluating *our agent module* with a fixed pydantic-ai harness, Inspect's solver abstraction adds a layer of indirection. We'd be writing solvers that wrap our bot — workable but awkward. RAG metrics not built in.

**Best fit.** Capability/safety benchmarks (e.g. "does QueryBot resist prompt injection in tool descriptions?"), which is post-MVP territory.

**Maturity.** Mature (2024+). Adopted by Anthropic, DeepMind, METR, Apollo Research.

**License.** MIT.

---

## 3. Side-by-side trade-off matrix

| Dimension | pydantic-evals | deepeval | Inspect AI |
|---|---|---|---|
| Alignment with pydantic-ai | Native (same authors, shared idioms) | First-class integration; pytest-based | Generic; wraps any agent via solver |
| Tool-call evaluation | Via OTel span_tree; structural | Via `ToolUseMetric` (LLM-judge); via custom metrics (deterministic) | Via solver/scorer; flexible |
| Multi-turn ergonomics | Roll-your-own via custom evaluator | `ConversationalTestCase` + multi-turn metrics; turnkey | Native multi-turn; agent primitives |
| Deterministic correctness checks | Excellent — `EqualsExpected`, structural span checks | Possible via custom metrics | Excellent — custom scorers |
| LLM-as-judge support | `LLMJudge` evaluator | Pervasive across catalogue | `model_graded_qa`, etc. |
| RAG metrics out-of-box | None | RAG triad + 5 ref-based + multi-turn RAG | None |
| Observability integration | Native OTel + Logfire | Reporting + Confident AI cloud (opt-in) | Built-in viewer; OTel via extensions |
| CI/pytest integration | Yes (`evaluate_sync` in pytest) | Yes (pytest-first design) | Yes (CLI + Python API) |
| Custom metric authoring | Python class, type-safe | Python class | Python function with decorators |
| Concurrency control | `max_concurrency` parameter | Built-in | Built-in (`max_tasks`, etc.) |
| Maturity / adoption | Newer; high-quality | Established; broad | Mature; serious-eval community |
| Bias of metric catalogue | Small, structural | Wide, LLM-judge-heavy | Small core, large benchmark library |
| Cost-to-onboard | Low (idiomatic if you know pydantic-ai) | Medium (different idioms) | Medium-high (CLI-first, solver model) |
| Dependency footprint | Light (`pydantic-evals` + optional Logfire) | Medium (LLM SDKs, scoring helpers) | Light core, heavy if using sandboxes |

---

## 4. Recommendation

**Primary: `pydantic-evals` as the foundation. Plan for `deepeval` as a RAG-specific complement when RAG flows land.**

Reasoning:

1. **The trace substrate the architecture already commits to is OTel-shaped.** pydantic-evals' `span_tree`-based evaluation is the most direct way to consume that substrate. We'd be paying *no* adapter cost.
2. **The deterministic correctness signal is the highest-value eval for QueryBot.** "Did the agent call `compute_metrics` with the right spec, and did the result match the known ground truth?" is precisely a structural span check + an artifact comparison. Both are straightforward in pydantic-evals; the LLM-as-judge bias of deepeval's catalogue is unhelpful here.
3. **The cost of writing multi-turn custom evaluators is low.** Multi-turn is "loop N times, then evaluate" — not architecturally hard. Deepeval's `ConversationalTestCase` saves boilerplate but not architecture. The architecture's `dump_history()` + `RunTrace` already give us what we need.
4. **Logfire integration is non-trivial value-add.** If we're going to ship observability hooks anyway (Section 5 of the architecture decisions), having evals appear in the same dashboard is a real win.

**Why deepeval is the right complement, not the right primary:**

- **RAG metrics are the killer feature.** When RAG-augmented bots arrive, hand-implementing faithfulness, contextual recall/precision, contextual relevancy, and their multi-turn variants is real work. Deepeval gets all of this for free. The cost (LLM-as-judge calls) is acceptable because RAG eval is fundamentally judge-based — there's no deterministic ground truth for "is this answer faithful to this context."
- **Frameworks coexist trivially.** Pydantic-evals is OTel-native; deepeval metrics are callable from any Python code. A single CI job can run pydantic-evals-driven structural evals *and* deepeval-driven RAG evals on the same agent traces.

**Inspect AI as a tertiary, future option.** When we want capability/safety benchmarks (prompt injection resistance, tool-misuse resistance), Inspect is the right tool. Not needed at v1. Worth keeping aware of because the broader frontier-model eval community is consolidating there.

---

## 5. What this means for the agent module

The architecture commits to a substrate, not a framework. Concretely:

1. **`RunTrace` is OpenTelemetry-compatible by design.** Each `ToolCall` corresponds to a span; tool args and results are span attributes. This is forward-compatible with pydantic-evals span_tree and any OTel backend.
2. **`dump_history()` produces a JSON-serializable conversation record** in a format consumable by any framework (it's just `list[Message]`).
3. **The result store supports point-in-time retrieval of computed artifacts** via `bot.get_result(result_id)`, accessible to eval harnesses for ground-truth comparison.
4. **No framework-specific code in the agent module itself.** The library doesn't import `pydantic_evals` or `deepeval`. Evals are a downstream consumer concern.

This means switching frameworks later — if pydantic-evals stalls, if deepeval pivots, if something new emerges — costs nothing in the agent module. Only the user-side eval harness changes.

---

## 6. Open question for the user

The recommendation above assumes the user wants the agent module's authors (us) to ship a small reference eval harness as part of the `aitaem` repository — e.g. `tests/evals/test_query_bot.py` demonstrating how to wire `QueryBot` to pydantic-evals with a few example cases.

Two paths:

- **(a) Ship reference evals in the repo.** Makes the substrate self-evidencing — users see how to evaluate their own deployments. Cost: maintaining the eval examples alongside the library.
- **(b) Document the substrate, don't ship evals.** Lighter library; cleaner separation. Cost: harder for new AITAEM users to know how to evaluate.

I lean (a) for the same reason the agent module exists at all (blueprint pattern from Section 2). Flagging for the user's call.
