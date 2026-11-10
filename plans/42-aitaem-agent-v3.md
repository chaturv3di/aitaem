# Plan 42 — v3 Agent: `AitaemAgent`, Agent Roles, Tool Groups, Artifact Store

**Scope:** the `aitaem.agent` module described in
[`plans/agent_module/ARCHITECTURE.md`](agent_module/ARCHITECTURE.md) (v1.4-draft), built on the
core support from Plan 41. The v2 bots are removed (v3.0, breaking).

**References:** "§AN" points to a section of the architecture; a bare "§N" points to this plan.
Research ([research/agent_module/](../research/agent_module/)):

| Note | Used for |
|---|---|
| [42-workflow-options](../research/agent_module/42-workflow-options.md) | Workflows, handoff outcomes, YAML output check (§2, SF-9) |
| [42-subagent-mechanics](../research/agent_module/42-subagent-mechanics.md) | Fan-out, concurrency, DuckDB per-thread connections (SF-3, SF-10) |
| [42-pydantic-ai-2.55-mechanics](../research/agent_module/42-pydantic-ai-2.55-mechanics.md) | `Conversation`, deferred tools, testing, Python floor (SF-2, SF-13, SF-15) |
| [42-anthropic-prompt-caching](../research/agent_module/42-anthropic-prompt-caching.md) | Caching settings, tail block (SF-17) |
| [42-history-stubbing](../research/agent_module/42-history-stubbing.md) | Stubbing capability (SF-18) |
| [42-result-storage](../research/agent_module/42-result-storage.md) | Lazy compute results, compute lock (SF-3, SF-7, SF-8) |
| [specs/41-spec-yaml-persistence](../research/specs/41-spec-yaml-persistence.md) | `SpecCache.save()` (SF-7) |

---

## 1. Agent instructions

Every role's `instructions.md` follows these rules. Reviewing each file against them is part of
its sub-feature.

1. **Keep it simple.** The instructions state the role's core directive: the steps it takes in
   the sequence diagrams of §A6.3, listed per role in §4.
2. **Write steps in active voice.** The instructions are a numbered list of steps addressed to the
   agent ("Resolve each metric with `resolve_spec`."). Each step names the tool it uses.
3. **State what to do.** Leave out contrast phrasing such as "Do X, not Y" and "This is X, not Y".
4. **Trust the agent and the user with ambiguity.** Leave corner cases out. An agent that meets an
   unclear case asks the user (main session, spec coordinator) or writes a needs-input handoff
   (other coordinators). Each corner-case rule is more text the model must reconcile with its core
   steps, which adds the ambiguity the rule meant to remove.
5. Add a step only when an eval (SF-20) shows the core steps failing on a case the user could not
   resolve.

Tool docstrings are part of each role's prompt (§A6.2) and follow rules 1–3: one sentence on what
the tool does, then its arguments.

## 2. Inter-agent documents

Two schemas serve every level (main session → coordinator, coordinator → executor), in
`agent_roles/documents.py`. Each has a natural-language field and structured fields.

```python
class Intake(BaseModel, Generic[P]):
    instructions: str            # what to do, in the main session's or coordinator's words
    params: P                    # the launched role's typed parameters
    result_ids: list[str] = []   # earlier artifacts to build on

class Handoff(BaseModel):
    outcome: Literal["complete", "needs-input", "inconclusive"]
    response: str                # answer, findings, or reason
    result_ids: list[str] = []   # artifacts that support the response
    questions: list[str] = []    # for needs-input

class Attachment(BaseModel):
    artifact_id: str
    reason: str

class Reply(BaseModel):          # output of the roles that talk to the user
    response: str
    status: Status
    attachments: list[Attachment] = []
```

Params, one model per launched role. Two models are shared with the tool that does the role's
core work, so the intake and the tool call use one definition; they live with that tool in
`tools/aitaem.py`:

```python
class ConnectionParams(BaseModel):               # connect_source; connections coordinator
    backend_type: Literal["duckdb", "bigquery"]
    config: dict[str, str | int | bool]          # ConnectionManager.add_connection keywords

class MetricComputeParams(BaseModel):            # compute_metrics; analysis coordinator
    metrics: list[str]                           # as the user named them
    slices: list[str] = []
    segment: str | None = None
    time_window: tuple[str, str] | None = None   # description: needed when period_type is not "all_time"
    period_type: PeriodType = "all_time"
    by_entity: str | None = None
```

The others are defined in their role's `agent.py`:

```python
class SpecParams(BaseModel):
    spec_type: Literal["metric", "slice", "segment"] | None = None
    names: list[str] = []                        # specs to create, change, or delete
    sources: list[str] = []                      # source URIs to research

class TableResearchParams(BaseModel):            # spec executors
    sources: list[str]

class SpecCombo(BaseModel):
    metric: str                                  # the measure needed, in the coordinator's words
    slices: list[str] = []                       # breakdowns the metric must support
    segment: str | None = None                   # entity grouping the metric must support

class SpecSearchParams(BaseModel):               # analysis executors
    combos: list[SpecCombo]
```

How documents flow:

- Launch tools and fan-out tools take intakes as typed arguments; pydantic-ai validates them and
  sends any validation error back to the calling model as a retry prompt.
- Each launch stores the intake as a YAML `file` artifact (`type_tag="intake"`) and starts the
  launched role with the intake's YAML as its user prompt.
- Coordinators and executors write their handoff as YAML text output. One output validator
  parses it with `yaml.safe_load`, validates it against `Handoff`, checks that every
  `result_ids` entry is in the store and that a needs-input handoff has a question, and raises
  `ModelRetry` with the errors otherwise
  ([workflow-options, Update 2026-11-10](../research/agent_module/42-workflow-options.md#update-2026-11-10-output-validator-on-yaml-text-output-confirmed)).
- The main session writes `Reply` the same way. The spec coordinator writes either a `Reply` or a
  `Handoff`; a document with an `outcome` key is a handoff.
- The launching tool stores each handoff as a YAML `file` artifact (`type_tag="handoff"`, parents:
  the intake and the handoff's `result_ids`) and returns its fields in the `ToolResult` facts.

## 3. Public API

```python
class AitaemAgent:
    def __init__(
        self,
        connection_manager: ConnectionManager | None = None,
        spec_cache: SpecCache | None = None,
        *,
        model: str | Model = "anthropic:claude-sonnet-5-5",
        executor_model: str | Model = "anthropic:claude-haiku-5-5",
        spec_dir: str | Path | None = None,
    ) -> None: ...
    async def chat(self, message: str) -> Response: ...
    def trace(self, trace_id: str) -> RunTrace: ...
    def dump(self) -> dict[str, Any]: ...
    @classmethod
    def load(cls, data: dict[str, Any], connection_manager: ConnectionManager | None = None,
             spec_cache: SpecCache | None = None, *, model: str | Model = ...,
             executor_model: str | Model = ..., spec_dir: str | Path | None = None) -> AitaemAgent: ...
    def cleanup(self) -> None: ...

class Response(BaseModel):
    response: str
    status: Status
    attachments: list[ResponseAttachment]   # artifact_id, type_tag, reason
    trace_id: str
```

- `model` runs the main session and the coordinators; `executor_model` runs the executors.
- Without a `connection_manager`, the agent creates `ConnectionManager(tmp_dir=<session dir>)`;
  without a `spec_cache`, an empty `SpecCache()`.
- Specs are saved under `<spec_dir>/metrics`, `<spec_dir>/slices`, and `<spec_dir>/segments`
  (local path or fsspec URL); `spec_dir` defaults to `<session dir>/specs` (§A4.1).
- `aitaem.agent` exports `AitaemAgent`, `Response`, `ResponseAttachment`, `Status`, `RunTrace`,
  `ToolCall`, `Usage`, `ArtifactRef`, `ArtifactStore`, `SessionDeps`, `ToolResult`, and the four
  toolsets (`stats_toolset`, `data_toolset`, `aitaem_toolset`, `artifact_toolset`), so callers can
  compose tool groups (§A2, G2).

## 4. Agent roles

| Role (`agent_roles/…`) | Model | Output | Tools |
|---|---|---|---|
| `main_session` | `model` | `Reply` | `run_connections`, `run_analysis`, `start_spec_design`; `list_artifacts`, `describe_artifact`, `read_artifact`; stats group |
| `connections_coordinator` | `model` | `Handoff` | `connect_source`, `list_tables`, `describe_table` |
| `analysis_coordinator` | `model` | `Handoff` | `resolve_spec`, `compute_metrics`; stats group; `describe_artifact`, `read_artifact`; `run_analysis_executors` |
| `analysis_executor` | `executor_model` | `Handoff` | `list_specs`, `resolve_spec`, `check_compatibility` |
| `spec_coordinator` | `model` | `Reply` or `Handoff` | `list_specs`, `resolve_spec`, `validate_spec`, `commit_spec`, `delete_spec`; `list_tables`, `describe_table`, `sample_rows`, `date_range`, `column_distribution`; `run_spec_executors` |
| `spec_executor` | `executor_model` | `Handoff` | `describe_table`, `sample_rows`, `date_range` |

Core directives, the skeleton of each `instructions.md` (§1):

**Main session**
1. Answer questions about existing results with the artifact and stats tools.
2. Launch `run_connections` to connect a source, `start_spec_design` to create, change, or delete
   specs, and `run_analysis` to compute metrics. Fill the intake from the user's words and pass the
   IDs of earlier results the task builds on.
3. Read the handoff. On `complete`, check that it answers the user's request; when it falls short,
   launch the coordinator again with what is missing and the handoff's result IDs. On
   `needs-input`, ask the user its questions, then launch the coordinator again with the answers in
   the intake and the handoff's result IDs. On `inconclusive`, report its reason.
4. When a request needs several coordinators, launch them one at a time and read each handoff
   before writing the next intake.
5. Attach the artifacts that support your answer.

**Connections coordinator**
1. Connect the source in the intake with `connect_source`.
2. List its tables with `list_tables`.
3. Write a `complete` handoff with the tables found, or a `needs-input` handoff asking for the
   configuration values you need.

**Analysis coordinator**
1. Plan the steps that answer the intake: the metrics to compute and the stats to run on each
   result.
2. Resolve the specs the intake names with `resolve_spec`. For each other metric, write the slices
   and segment it must support as a combo, and find the specs with `run_analysis_executors`. Choose
   among the specs the handoffs offer; when none fits, write a `needs-input` handoff that offers
   the closest ones.
3. Run the steps with `compute_metrics` and the stats tools, passing each result's ID to the next
   step.
4. Read the values the answer needs with `read_artifact`, keeping to the columns and rows you need.
5. Write a `complete` handoff with the answer, or an `inconclusive` handoff with the reason the
   results support no answer. List the supporting result IDs.

**Analysis executor**
1. Find the candidate specs for each combo in the intake with `list_specs` and `resolve_spec`.
2. Check that each candidate metric supports its combo's slices and segment with
   `check_compatibility`.
3. Write the handoff: for each combo, the compatible spec names and the reason each fits.

**Spec coordinator**
1. Research the tables in the intake with `describe_table`, `sample_rows`, and `date_range`. For
   several tables, write one intake per table and run them with `run_spec_executors`.
2. Draft each spec with the user. Check each draft with `validate_spec` and show it to the user.
3. When the user agrees to a spec, save it with `commit_spec`.
4. When the specs are done, write the handoff listing the saved specs.
5. When the user leaves the task, list the specs you saved, delete the ones the user wants removed
   with `delete_spec`, then write the handoff.

**Spec executor**
1. Describe each table in the intake with `describe_table`.
2. Sample its rows with `sample_rows` and find the bounds of its date columns with `date_range`.
3. Write the handoff: for each table, the columns that can serve as entities, timestamps,
   measures, and slice dimensions.

## 5. Tool groups

Every tool is wrapped by `artifact_tool` (SF-4) and returns a `ToolResult`. Tools carried over from
v2 keep their names and query logic; their results change to `ToolResult`, and v2's tokens, draft
registry, and `ResultStore` are replaced by artifacts.

| Group | Tool | Artifact (kind, `type_tag`) | Implementation |
|---|---|---|---|
| stats | `rank_by_value(result_id, top_n=10, ascending=False, group_by=None)` | table, `ranked`; with `group_by`, input's kind | v2 `query_tools`; `group_by` added (top N within each group, an Ibis window) |
| stats | `distribution_summary(result_id, group_by=None)` | table, `distribution` | v2 `common_tools` |
| stats | `filter_by_threshold(result_id, threshold, op=">", column="metric_value")` | input's kind, `filtered` | v2 `query_tools` |
| stats | `filter_by_value(result_id, column, values)` | input's kind, `filtered` | New; rows whose `column` is in `values` |
| stats | `filter_by_result(result_id, other_id, column="entity_id")` | input's kind, `filtered` | New; rows whose `column` value appears in `other_id`'s `column` (Ibis semi-join) |
| stats | `period_over_period(result_id)` | input's kind, `period_over_period` | Rewritten as an Ibis window expression (lag over `period_start_date` within each group) |
| stats | `contribution_share(result_id)` | input's kind, `contribution_share` | Rewritten as Ibis window expressions (share and cumulative share within each `metric_name`) |
| data | `list_tables(backend_type=None)` | object, `table_list` | v2 `definition_tools` |
| data | `describe_table(source)` | object, `table_schema` | v2 `definition_tools` |
| data | `sample_rows(source, n=10)` | table, `sample` | New; `n` capped at 20 |
| data | `column_distribution(metric_name, column=None, filter=None)` | table, `distribution` | v2 `common_tools`; stays metric-only (Plan 37) |
| data | `date_range(source, date_column, filter=None)` | object, `date_range` | v2 `definition_tools`; bounds-only, temporal columns only (Plan 37) |
| aitaem | `connect_source(params: ConnectionParams)` | object, `connection` | New; `ConnectionManager.add_connection` |
| aitaem | `list_specs(spec_type=None)` | object, `spec_list` | New; names and descriptions from `SpecCache` |
| aitaem | `resolve_spec(spec_type, name)` | object, `spec_match` | v2 `SpecResolver.resolve_metric_name`, extended to slices and segments; the match carries the spec's YAML |
| aitaem | `check_compatibility(metric, slices=[], segments=[])` | object, `compatibility` | New; `MetricCompute.scan()` on a `SpecCache` holding the named specs |
| aitaem | `validate_spec(spec_type, yaml_text)` | object, `spec_draft` | v2 `validate_spec` checks |
| aitaem | `commit_spec(draft_id)` | object, `spec_commit` | v2 `commit_spec` add-or-update rule, plus `SpecCache.save()` |
| aitaem | `delete_spec(spec_type, name)` | object, `spec_delete` | v2 `delete_spec`, plus `SpecCache.save()` |
| aitaem | `compute_metrics(params: MetricComputeParams)` | ibis, `metric_result` | `MetricCompute.compute`, stored lazily |
| artifact | `list_artifacts(type_tag=None)` | object, `artifact_list` | New |
| artifact | `describe_artifact(artifact_id)` | object, `artifact_detail` | New; the full `ArtifactRef`, plus columns for table and ibis artifacts |
| artifact | `read_artifact(artifact_id, columns=None, n=10)` | table, `read` | New; the rows go in the result's facts; `n=None` reads every row; on an ibis artifact, runs the selection on its backend |

Rules:

- Stats with bounded output (aggregates, top-N) produce `table` artifacts in `session.duckdb`.
  Row-preserving stats keep their input's kind: an `ibis` input yields a lazy `ibis` result on the
  same backend; a `table` input yields a new table in `session.duckdb`.
- `filter_by_result` needs both inputs on one backend; otherwise it returns `error`.
- `check_compatibility` uses core's check: a slice fits when its columns exist in the metric's
  source table, a segment when one of its join keys does.
- `resolve_spec` returns `ok` with the exact match, or `refused` with up to three close names
  (`difflib`, as v2), which applies the Metric Precision Rule (§A5).
- `validate_spec` runs v2's checks (structure through `*Spec.from_yaml`, name conflicts, composite
  slice references, column existence) and stores the draft and its issues as the artifact;
  `status` is `ok` when every check passes. `commit_spec` re-runs those checks against the current
  cache, adds or updates the spec, and saves the cache to `spec_dir`; an invalid draft yields an
  `error` artifact and leaves the cache unchanged.
- `connect_source` accepts DuckDB and BigQuery. For Postgres it returns `refused`, telling the user
  to configure Postgres in the `ConnectionManager` passed to `AitaemAgent`: its config holds a
  password, and the agent handles no credentials (§A10).
- `compute_metrics`, `check_compatibility`, the data tools, and stats and reads on `ibis` artifacts take
  `SessionDeps.compute_lock` whenever `connection_manager.requires_compute_lock` is true at call
  time; `connect_source` can change that value mid-session (§A5).

## 6. Out of scope

| Item | Reason |
|---|---|
| Postgres through `connect_source` | Needs a password (§5). |
| `drop_artifact` | No workflow step uses it; the OS removes the session directory (§A4.1). |
| Live-model evals in CI | They need an API key; they run locally (SF-20). |
| Version bump and release | Release process in `CLAUDE.md`. |

## 7. Sub-features

Order: SF-1 → SF-21, in phases. Within Phase 3, SF-5 to SF-8 are independent.

### Phase 1 — Clean slate

#### SF-1 — Remove the v2 bots

**Files:** `aitaem/agent/`, `tests/test_agent/`, `tests/evals/`, `examples/`, `docs/`, `mkdocs.yml`

- Delete `base.py`, `query_bot.py`, `definition_bot.py`, `query_tools.py`, `definition_tools.py`,
  `query_types.py`, `definition_types.py`, `common_tools.py`, `resolver.py`, `store.py`,
  `history.py`, `response.py`, and their tests. Keep `trace.py` and `test_trace.py`.
- Delete the v2 evals and examples 01–04 (`.py`, `.ipynb`) and their `examples/README.md` entries.
- `aitaem/agent/__init__.py` exports `Status`, `RunTrace`, `ToolCall`, `Usage`.
- Delete `docs/agent/building-your-own-bot.md`; reduce `docs/api/agent.md` to the remaining
  exports; update the `mkdocs.yml` nav.
- `docs/changelog.md`, Unreleased → Removed: every name removed from `aitaem.agent.__all__`.
- The v2 code that §5 carries over is read from the commit before SF-1 (`git show <sha>:<path>`),
  with its unit tests.

**Tests:** the full suite passes; `scripts/check_import_graph.py`; `mkdocs build --strict`.

#### SF-2 — pydantic-ai 2.55, Python floor, contract tests

**Files:** `pyproject.toml`, `.github/workflows/ci.yml`, `tests/conftest.py`,
`tests/test_agent/conftest.py`, `tests/test_agent/test_pydantic_ai_contract.py`

- Every pydantic-ai entry in the five agent extras becomes `pydantic-ai-slim[…]>=2.55,<3`. The
  `dev` extra's entry gets the marker `python_version >= "3.11"`, so core development installs on
  3.10 ([mechanics, Update 2026-11-10](../research/agent_module/42-pydantic-ai-2.55-mechanics.md#update-2026-11-10-python-floor-and-mechanics-confirmed-by-execution)).
  Install with `uv pip install -e ".[agent,agent-evals,dev]"`.
- `tests/conftest.py` skips collecting `tests/test_agent/` and `tests/evals/` when `pydantic_ai`
  is not importable, so the 3.10 `test` job runs core tests only.
- CI: the `test-agent` matrix runs Python 3.11 and 3.12; 3.11 pins `pydantic-ai-slim==2.55.0` so
  CI exercises the floor (§A12).
- `tests/test_agent/conftest.py` sets `models.ALLOW_MODEL_REQUESTS = False`.
- Contract tests pin the mechanics this plan relies on, as executed in the research Updates:
  - YAML text output with an output validator retries on `ModelRetry` and accepts the corrected output
  - a tool raising `CallDeferred` ends the run with `DeferredToolRequests`; a run with
    `conversation=result.conversation` and `DeferredToolResults` resumes it
  - a `functools.wraps` decorator keeps a tool's description and parameter schema
  - threads with their own `duckdb.connect(path)` write separate tables to one file concurrently

**Tests:** the contract tests; the full suite on 3.12.

### Phase 2 — Artifact store

#### SF-3 — `ArtifactRef`, `ArtifactStore`

**File:** `aitaem/agent/artifacts.py`

- `ArtifactRef` has the fields of §A4.2 plus `lost: bool = False`.
- `ArtifactStore(session_dir)` creates the layout of §A4.1 under
  `tempfile.mkdtemp(prefix="aitaem-session-")` when no directory is given.
- IDs `a001`, `a002`, … and manifest appends happen under one `threading.Lock`; tools run in
  threads.
- Writers per kind: object (in memory plus `objects/<id>.json`), file (`files/<id>.<ext>`), table
  (Arrow → `t_<id>` in `session.duckdb`, through a connection opened for the call), ibis (in-memory
  reference).
- Readers return the payload; a table reader opens its own Ibis DuckDB connection on
  `session.duckdb`
  ([subagent-mechanics, Update 2026-11-10](../research/agent_module/42-subagent-mechanics.md#update-2026-11-10-duckdb-connections-per-thread-confirmed)).
- `index_lines()` renders the artifact index of §A9.3.
- `ArtifactStore.load(session_dir)` rebuilds the store from `manifest.jsonl` and marks `lost` every
  artifact whose payload is missing, every ibis artifact included (§A9.6).

**Tests** (`tests/test_agent/test_artifacts.py`): round trip per kind; unique, ordered IDs from 8
threads; one manifest line per artifact; concurrent table writes; `load` marks missing files and
ibis artifacts `lost`; index rendering.

#### SF-4 — `SessionDeps`, `ToolResult`, `artifact_tool`

**Files:** `aitaem/agent/deps.py`, `aitaem/agent/tools/__init__.py`

- `SessionDeps` (dataclass): `store`, `connection_manager`, `spec_cache`, `spec_dir`,
  `compute_lock` (one `threading.Lock`, shared by identity with child deps), `role`, `turn`.
  Executors get `dataclasses.replace(deps, role=…)`.
- `ToolResult`: `artifact_id`, `status`, `summary`, `facts: dict[str, Any]` (R2).
- `artifact_tool` wraps a tool whose body returns the payload, kind, `type_tag`, summary, parents,
  status, and facts. It writes the artifact with `produced_by` from the `RunContext` and the deps,
  and returns the `ToolResult`. When the body raises, it writes an `error` artifact with the
  message and returns an `error` `ToolResult` (R1).
- **[verify]** first: `functools.wraps` keeps the schema of a tool whose first parameter is a
  `RunContext` (the contract test covers a plain tool).

**Tests:** success writes the artifact and result; an exception writes an `error` artifact; the
lock is taken exactly when `requires_compute_lock` is true; the wrapped tool's schema matches the
unwrapped one.

### Phase 3 — Tool groups

Each sub-feature carries over the v2 unit tests of its tools, adapted to `ToolResult` and
artifacts, and runs on DuckDB fixtures.

#### SF-5 — artifact group

**File:** `aitaem/agent/tools/artifacts.py`. Tools per §5.
**Tests:** each tool on object, file, table, and ibis artifacts; `read_artifact` selects columns
and reads every row with `n=None`.

#### SF-6 — data group

**File:** `aitaem/agent/tools/data.py`. Tools per §5.
**Tests:** carried-over tests; `sample_rows` caps `n`; `column_distribution` takes catalog
metrics only; `date_range` rejects non-temporal columns.

#### SF-7 — aitaem group

**File:** `aitaem/agent/tools/aitaem.py`. Tools per §5.
**[verify]** first: `SpecCache.add()` accepts a composite slice without the slices it references,
and `scan()` on that cache gives the same verdicts as on the full cache.
**Tests:** `connect_source` for DuckDB, an existing backend (`error`), and Postgres (`refused`);
`resolve_spec` exact and near matches for each spec type; `check_compatibility` verdicts match
`MetricCompute.scan()` on the full cache; `validate_spec` on each check;
`commit_spec` adds, updates, saves to a `tmp_path` and a `memory://` `spec_dir`, and leaves the
cache unchanged on an invalid draft; `delete_spec` removes the spec and its file;
`compute_metrics` stores a lazy result whose `to_pyarrow()` equals a direct `MetricCompute` call.

#### SF-8 — stats group

**File:** `aitaem/agent/tools/stats.py`. Tools per §5.
**Tests:** carried-over tests; `period_over_period` and `contribution_share` match the v2 pandas
results on the same fixture; row-preserving tools keep their input's kind; bounded tools write
tables; `rank_by_value` with `group_by` keeps the top N in each group; `filter_by_value` and
`filter_by_result` on ibis and table inputs; `filter_by_result` across backends returns `error`.

### Phase 4 — Agent roles

Role tests use `FunctionModel` scripts that follow the sequence diagrams (§A6.3), with
`agent.override(model=…)` for executors (§A11).

#### SF-9 — Documents, role scaffolding, import graph

**Files:** `aitaem/agent/agent_roles/__init__.py`, `aitaem/agent/agent_roles/documents.py`,
`scripts/check_import_graph.py`

- The schemas and output validators of §2; `load_instructions(package)` reads a role's
  `instructions.md` through `importlib.resources`.
- `check_import_graph.py` also fails when `aitaem.agent` imports core through anything other than
  `aitaem`'s top level (§A10), when `tools/` imports `agent_roles/`, or when a role imports a role
  it does not launch (main session → coordinators → their executors; §A3).
- **[verify]** first: hatchling includes `instructions.md` in the wheel (`python -m build --wheel`,
  then list the archive).

**Tests:** the validators accept valid documents and retry on invalid YAML, a schema error, an
unknown result ID, and a needs-input handoff without questions; the spec coordinator's validator
tells `Reply` from `Handoff`; intakes round-trip through their YAML artifact; the import graph
script flags each rule on a fixture tree.

#### SF-10 — Executors and fan-out tools

**Files:** `aitaem/agent/agent_roles/analysis_executor/`, `aitaem/agent/agent_roles/spec_executor/`

- Each executor's `agent.py` defines the agent (§4), `name=`, `Caching(retention="1h",
  messages=False)` (§A7.3), and its fan-out tool (`run_analysis_executors`,
  `run_spec_executors`) taking `list[Intake[P]]`.
- The fan-out tool is `async`, runs the executors under an `asyncio.Semaphore(4)` with
  `gather(…, return_exceptions=True)`, passes child deps and `usage=ctx.usage`, stores each
  handoff (or an `error` artifact for a failed executor), and returns each executor's handoff ID,
  outcome, and response (§A7.2).
- **[verify]** first: how a tool reads its run's `CancellationToken` to pass it to executors.

**Tests:** at most four executors run at once; one failure leaves its siblings' handoffs intact;
usage accumulates on the parent run; handoff parents link the intake and results.

#### SF-11 — Connections coordinator

**Files:** `aitaem/agent/agent_roles/connections_coordinator/` (`run_connections` launch tool).
**Tests:** `complete` after connecting DuckDB; `needs-input` for missing config; the handoff
artifact and the launch tool's facts.

#### SF-12 — Analysis coordinator

**Files:** `aitaem/agent/agent_roles/analysis_coordinator/` (`run_analysis` launch tool).
**Tests:** named specs resolved without a fan-out; a fan-out of combos whose handoffs supply the
spec names for `compute_metrics`; no compatible spec → `needs-input` with the closest ones, before
any compute; a chain of compute and stats calls, each taking the previous result's ID → `complete`;
a relaunch with earlier `result_ids` reuses them.

#### SF-13 — Spec coordinator

**Files:** `aitaem/agent/agent_roles/spec_coordinator/` (`start_spec_design` launch tool)

- `start_spec_design(intake)` stores the intake and raises `CallDeferred` with the intake ID, so the
  main session's run ends and the facade routes to the spec coordinator (SF-15)
  ([mechanics, Update 2026-11-10](../research/agent_module/42-pydantic-ai-2.55-mechanics.md#update-2026-11-10-python-floor-and-mechanics-confirmed-by-execution)).
- `Caching(retention="1h", messages=True)`.

**Tests:** research → draft → validate → commit over several turns, specs saved to `spec_dir`;
user leaves → saved specs listed, chosen ones deleted, then a handoff.

#### SF-14 — Main session

**Files:** `aitaem/agent/agent_roles/main_session/`

- `output_type=[str, DeferredToolRequests]` with the `Reply` validator; thinking off (§A9.5);
  `Caching(retention="1h", messages=True)`.
- **[verify]** first: whether the output validator also receives `DeferredToolRequests` outputs;
  if so, it returns them unchanged.

**Tests:** recall answered with stats tools and no launch; each launch tool called with a typed
intake; needs-input handoff → questions to the user; a `complete` handoff that misses part of the
request → a relaunch; two coordinators launched in sequence.

### Phase 5 — Facade

#### SF-15 — `AitaemAgent`, `Response`

**File:** `aitaem/agent/session.py`

- State: the main session's `Conversation`, the active route (main or spec), the spec
  coordinator's `Conversation` and the pending deferred call's ID during spec design, and the
  traces by ID.
- `chat()` on the main route runs the main session and keeps `result.conversation`. When the run
  ends with a `start_spec_design` request, `chat()` runs the spec coordinator on the intake: a
  `Reply` switches the route to spec and is returned; a `Handoff` resumes the main session.
- `chat()` on the spec route runs the spec coordinator with the message. A `Handoff` is stored,
  switches the route to main, and resumes the main session with
  `DeferredToolResults(calls={call_id: <handoff ToolResult>})`; the main session's reply is
  returned.
- `Response` comes from the final `Reply`, with each attachment's `type_tag` read from the store.
  An exception yields `status=error` with the message and a trace with `error` set.
- `cleanup()` removes the session directory.

**Tests:** both workflows of §A6.3 end to end with `FunctionModel`s, including a needs-input round
trip and a spec flow that saves to `tmp_path`; defaults for a missing `ConnectionManager` and
`SpecCache`; an exception becomes an `error` response.

#### SF-16 — `dump()` / `load()`

**File:** `aitaem/agent/session.py`

- `dump()` returns both conversations (`ConversationTypeAdapter`), the route, the pending call ID,
  and the session directory path. `load()` restores them and the store (SF-3), marking missing
  artifacts `lost` (§A9.6).

**Tests:** dump and load mid-analysis and mid-spec-flow, then continue; ibis artifacts are `lost`
after load; a removed session directory loads with every artifact `lost`.

### Phase 6 — Context management

#### SF-17 — Tail block and caching

**File:** `aitaem/agent/history.py`

- A capability whose `wrap_model_request` appends, behind a `CachePoint`, a request-only block
  with the session state and the artifact index (§A8, §A9.2–9.3), for the main session and the spec
  coordinator.
- Session state, as compact JSON capped at 2,000 characters: the latest intake's instructions
  (goal), connected backends, spec names per type, the active route, open questions from the latest
  needs-input handoff, and `error` artifacts newer than the last `ok` artifact of the same tool.

**Tests:** the block reaches the model on every request and stays out of the stored history; the
static prefix (tools, instructions) is byte-identical across turns.

#### SF-18 — History stubbing

**File:** `aitaem/agent/history.py`

- A `ProcessHistory` capability on the main session (§A9.4). Trigger: `ctx.context_window_used`
  ≥ 0.65, or a `'cold'` `prompt_cache_outlook` with ≥ 0.35; at least 2,000 tokens cleared.
- Action: older tool returns become `[artifact <id> · <kind> · <summary> · read_artifact or
  describe_artifact]` via `dataclasses.replace`; call/return pairs, the last four pairs, and the
  current run stay. Escalation: drop the oldest turns at a user-prompt boundary until under 0.65.

**Tests:** below threshold the history is unchanged; above it, old returns become stubs naming
their artifacts, with every pair intact; escalation keeps a user-prompt boundary.

### Phase 7 — Observability, evals, docs

#### SF-19 — Traces and spans

**File:** `aitaem/agent/trace.py`

- `ToolCall.result_id` becomes `artifact_id`. `RunTrace` per `chat()` covers every run of the turn,
  including coordinator and executor runs, with per-run usage and cache token counts (§A11).
- Every agent has `name=` and the `Instrumentation` capability; cache collapse reasons appear on
  the spans.

**Tests:** with `InMemorySpanExporter`, executor spans nest under the fan-out tool span; each tool
call in a trace has an `artifact_id` present in the manifest.

#### SF-20 — Evals

**Files:** `tests/evals/`

- Retarget the v2 cases to `AitaemAgent`: analysis (status, Metric Precision refusals, result
  attachments), spec design (validate → commit), threshold governance (`column_distribution`
  metric-only, `date_range` bounds-only).
- New dimensions (§A11): fan-out correctness and artifact completeness with `FunctionModel`
  (CI); history-stub fidelity and cache hit rate on turn ≥ 2 with a live model, skipped without
  `ANTHROPIC_API_KEY`.

**Tests:** `python -m pytest tests/evals/`.

#### SF-21 — Docs and examples

- `docs/agent/getting-started.md`, `evaluating-your-agent.md`, `stability.md` (semver-stable
  surface, the removed names, Python ≥ 3.11 for the agent extras): rewritten.
- `docs/agent/tool-groups.md` (new): composing the toolsets with `SessionDeps`.
- `docs/api/agent.md`, `docs/api/index.md`, `mkdocs.yml` nav, `docs/user-guide/connectors.md`
  (bot mentions), and the `agent-core` comment in `pyproject.toml`.
- `docs/changelog.md`, Unreleased → Added (`AitaemAgent` and the exports of §3) and Changed
  (pydantic-ai ≥ 2.55; agent extras need Python ≥ 3.11).
- `examples/01_aitaem_agent_example.py` and its `examples/README.md` entry.

**Tests:** `mkdocs build --strict`; the example runs against a DuckDB fixture with a
`FunctionModel`.

## 8. Known limitations

- The Ibis window rewrites (SF-8), `sample_rows`, and lazy reads are tested on DuckDB only; no
  BigQuery or Postgres instance is available.
- History-stub fidelity and cache hit rate are measured only in local live-model runs (SF-20).

## 9. Files changed

| File | Change |
|---|---|
| `aitaem/agent/` v2 modules, `tests/test_agent/` v2 tests, `tests/evals/`, `examples/` 01–04 | SF-1: removed |
| `pyproject.toml`, `.github/workflows/ci.yml`, `tests/conftest.py`, `tests/test_agent/conftest.py` | SF-2 |
| `aitaem/agent/artifacts.py`, `deps.py` | SF-3, SF-4 |
| `aitaem/agent/tools/__init__.py`, `artifacts.py`, `data.py`, `aitaem.py`, `stats.py` | SF-4 to SF-8 |
| `aitaem/agent/agent_roles/documents.py` and one package per role | SF-9 to SF-14 |
| `scripts/check_import_graph.py` | SF-9 |
| `aitaem/agent/session.py`, `aitaem/agent/__init__.py` | SF-15, SF-16 |
| `aitaem/agent/history.py` | SF-17, SF-18 |
| `aitaem/agent/trace.py` | SF-19 |
| `tests/test_agent/*`, `tests/evals/*` | Tests per sub-feature |
| `docs/agent/*`, `docs/api/agent.md`, `docs/api/index.md`, `docs/user-guide/connectors.md`, `docs/changelog.md`, `mkdocs.yml`, `examples/` | SF-1, SF-21 |
