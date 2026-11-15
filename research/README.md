# research/

Findings, evidence, and options considered. **Decisions do not live here**; they live in
`plans/` (architecture docs under `plans/<area>/`, implementation plans as `plans/NN-*.md`),
which link back to the research notes that justify them.

## Conventions

- One folder per area (`agent_module/`, …); one note per topic, named `NN-<topic>.md`.
- **The `NN-` prefix tags the plan the note supports:** it matches the number of the
  `plans/NN-*.md` implementation plan that relies on it. Use `00-` while that plan does not exist
  yet (e.g. research feeding an architecture doc), and rename the note to the plan's number once
  the plan is created. Several notes can share a prefix.
- Every note starts with a header: **Date**, **Sources** (library versions, URLs, papers), and
  **Used by** (the plan/architecture docs that rely on it).
- Mark each claim's provenance:
  - **[verify]**: read in source or docs but not confirmed by execution. Must be checked
    (e.g. with a `FunctionModel` test) before an implementation relies on it.
  - Unmarked: confirmed by reading source and/or execution, as stated in the note's header.
- Notes may compare options and state a recommendation, but the decision itself is recorded in
  `plans/`.
- Research goes stale by design. Do not silently rewrite a note to match a later decision or a
  newer library version: add a dated **Update** section, or write a new note and link it from the
  old one.
