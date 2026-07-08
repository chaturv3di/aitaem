# Plan 27 — Phase 3: DefinitionBot

**Prerequisites:** Plan 23 (Phase 1), Plan 24 (Phase 2), and Plan 26 (QueryBot v0.2) are fully
implemented and passing. Phase 3 requires prerequisite core changes (P3.0a–P3.0d) that must
be merged before any tool implementation begins. P3.0b (ResultStore discriminated union) also
requires updating Phase 2 call sites in `query_tools.py`.

**Architecture reference:** `plans/agent_module/ARCHITECTURE.md` Section 3, Section 8.

---

## Decisions Resolved (Pre-flight)

| Question | Decision |
|---|---|
| **SpecCache at construction** | **Required** — even an empty `SpecCache()` is valid. Enables conflict detection, composite-slice cross-references, and compatible-spec suggestions. |
| **Anti-hallucination gate** | **Token-gated** — mirrors QueryBot's pattern. LLM cannot return a spec until `validate_spec` runs all checks and mints a `spec_draft_token`. |
| **`list_tables` gap** | **Add `list_tables()` to `IbisConnector` only; add `backend_types` property to `ConnectionManager`.** See P3.0. |
| **Scope** | **Single-turn bot.** User can pass existing/partial YAML to `record_definition_intent` for editing. Multi-turn iterative refinement is deferred (see Architecture doc Section 7, ND-10). |

---

## Terminology

Inherits all Phase 1 terminology (Run, Turn, Conversation, Trace). New terms:

| Term | Meaning |
|---|---|
| **SpecDraft** | A YAML string the LLM wrote via `draft_spec`, stored server-side in `DefinitionDeps.draft_registry` before validation. |
| **spec_draft_token** | Opaque UUID string minted by `validate_spec` only when all checks pass. Equals the ResultStore entry ID of a `TextEntry` storing the validated YAML. The LLM carries it in `DefinitionOutput`; the bot retrieves it via `store.get_text(token)`. |
| **is_update** | Flag on `DefinitionIntent` set to `True` when the user provides `existing_yaml`. Suppresses name-conflict errors for the same spec name. |

---

## Prerequisite (P3.0): Add `list_tables()` to aitaem core

**Must be merged before Phase 3 tool implementation starts.**

`list_tables()` belongs only on `IbisConnector` — it is connector functionality and should
not be duplicated on `ConnectionManager`. Adding it there would set a precedent for lifting
`get_table()`, `get_schema()`, etc., turning `ConnectionManager` into a facade over connector
methods. Instead, `ConnectionManager` gets a `backend_types` property — clearly its own state —
and the `list_tables` tool aggregates across backends itself.

### P3.0a — `IbisConnector.list_tables() -> list[str]`

Wraps `self.connection.list_tables()`. Raises `AitaemConnectionError` when not connected.
No `pattern` parameter in v1 — backend LIKE-filter support varies silently across backends
and the `list_tables` tool does not expose filtering to the LLM anyway. If table filtering
becomes necessary (e.g. very large schemas), add `pattern: str | None = None` with
Python-side `difflib.get_close_matches(pattern, all_tables)` for consistent cross-backend
fuzzy filtering — `difflib` is already a dependency from QueryBot v0.2's near-miss matching.

### P3.0b — ResultStore discriminated union (`aitaem/agent/store.py`)

Extends `ResultStore` to support a first-class `TextEntry` kind alongside the existing
tabular kind. Touches Phase 1 code but is additive and backward-compatible except for the
`ResultEntry` rename (safe at v0→v1 with no external consumers yet).

**Type hierarchy** (`store.py`):

- `_EntryBase`: shared base model with common fields `result_id: str` and `metadata: dict[str, Any]`.
- `TabularEntry(_EntryBase)`: `kind: Literal["tabular"]`, `arrow: pa.Table | None`,
  `ibis_ref: ibis.Table | None`. Direct rename of the current `ResultEntry` internals.
- `TextEntry(_EntryBase)`: `kind: Literal["text"]`, `text: str`,
  `content_type: str` (e.g. `"application/yaml"`, `"application/json"`).
- `ResultEntry`: `TabularEntry | TextEntry` — discriminated union on `kind`.
  The old `ResultEntry` name is reused for the union (no deprecated alias needed at v0→v1).
- `WrongEntryKindError(AitaemError)`: raised when `get_text()` is called on a tabular ID
  or `get_tabular()` on a text ID. Message includes the actual `kind` found.

**New / renamed `ResultStore` methods**:

- `store_tabular(arrow, ibis_ref, metadata) -> str` — replaces the current `store()`.
- `store_text(text, content_type, metadata) -> str` — new.
- `get(result_id) -> ResultEntry` — generic retriever returning the union; unchanged name.
- `get_tabular(result_id) -> TabularEntry` — raises `WrongEntryKindError` if kind is not `"tabular"`.
- `get_text(result_id) -> TextEntry` — raises `WrongEntryKindError` if kind is not `"text"`.
- `get_arrow()` and `get_ibis()` — pre-existing Phase 1 methods; updated in P3.0b to
  delegate to `get_tabular()` internally and raise `WrongEntryKindError` on a text entry.
- `invalidate_all_ibis_refs()` — unchanged; only applies to `TabularEntry` instances.

**Call-site updates** (Phase 2 tools, `aitaem/agent/query_tools.py`):

- `compute_metrics`: `ctx.deps.store.store(...)` → `ctx.deps.store.store_tabular(...)`
- All analysis tools (`rank_by_value`, `filter_by_threshold`, `distribution_summary`,
  `period_over_period`, `contribution_share`): `store.store(...)` → `store.store_tabular(...)`,
  `store.get(...)` → `store.get_tabular(...)`.

**History serialization** (`dump_history()` / `load_history()`):

Pydantic's discriminated union with `kind: Literal[...]` serializes the tag automatically.
Update the serializer to round-trip `ResultEntry` via the union type; both kinds must
survive a dump/load cycle with correct `kind` and all fields intact.

**Tests** (`tests/test_agent/test_store.py`, additions):

- `store_tabular()` returns a retrievable ID; `get_tabular(id)` returns a `TabularEntry`
- `store_text("yaml...", "application/yaml")` returns a retrievable ID; `get_text(id)` returns a `TextEntry`
- `get_text(tabular_id)` raises `WrongEntryKindError` with the actual kind in the message
- `get_tabular(text_id)` raises `WrongEntryKindError`
- `isinstance(entry, TextEntry)` is `True` for text entries; `isinstance(entry, TabularEntry)` for tabular
- `dump_history()` / `load_history()` round-trip preserves `kind`, `text`, and `content_type` for `TextEntry`
- `dump_history()` / `load_history()` round-trip preserves `kind`, `arrow`, for `TabularEntry`
- `invalidate_all_ibis_refs()` only touches `TabularEntry` instances

### P3.0c — `ConnectionManager.backend_types: list[str]` (property)

Returns `list(self._connections.keys())`. Exposes registered backend names so callers can
loop without reaching into `_connections` directly. No `list_tables` wrapper on
`ConnectionManager` — aggregation is the tool's responsibility.

### P3.0d — Tests (`tests/test_connectors/test_list_tables.py`)

- `list_tables()` delegates correctly to `self.connection.list_tables()`
- `list_tables()` raises `AitaemConnectionError` when `is_connected` is False
- `backend_types` returns all registered key names
- `backend_types` returns `[]` when no connections are registered

---

## File Structure

### New files

```
aitaem/agent/
├── definition_types.py     # DefinitionDeps, DefinitionOutput, DefinitionPayload, tool result models
├── definition_tools.py     # record_definition_intent, list_tables, describe_table, draft_spec, validate_spec
└── definition_bot.py       # DefinitionBot, DefinitionResponse, prompt builders, _assemble_payload

tests/test_agent/
├── test_definition_types.py
├── test_definition_tools.py
└── test_definition_bot.py
```

### Modified files

```
aitaem/connectors/ibis_connector.py    # P3.0a: add list_tables()
aitaem/connectors/connection.py        # P3.0c: add backend_types property
aitaem/agent/__init__.py               # add Phase 3 exports
plans/agent_module/ARCHITECTURE.md     # add ND-10 to Section 7 table
plans/agent_module/07-non-decisions.md # add ND-10 body
plans/agent_module/08-implementation-order.md  # expand Phase 3 section
```

---

## Implementation Sub-Features

Implement in this order. Each SF is independently testable.

---

### SF-1: Type models (`aitaem/agent/definition_types.py`)

All Pydantic/dataclass types that define the LLM–bot–tool contract.

#### `DefinitionIntent` (dataclass)

Fields: `spec_type: Literal["metric","slice","segment"]`, `description: str`,
`existing_yaml: str | None = None`, `is_update: bool = False`,
`original_name: str | None = None`.

Set by `record_definition_intent`. `is_update` is `True` when `existing_yaml` is provided.
`original_name` is parsed from `existing_yaml` once at intent-recording time (not at
validate time) so `validate_spec` step 3 is a simple string comparison. If `existing_yaml`
is malformed, `record_definition_intent` surfaces the parse error immediately and leaves
`original_name=None`, which `validate_spec` treats as `is_update=False`.

#### `SpecDraft` (dataclass)

Fields: `draft_id: str`, `spec_type: Literal[...]`, `yaml_string: str`.

Server-side storage for a not-yet-validated YAML. The LLM never reads these directly;
it receives a `draft_id` and uses that to call `validate_spec`.

#### `DefinitionDeps` (dataclass)

Fields: `connection_manager`, `spec_cache`, `store: ResultStore`,
`draft_registry: dict[str, SpecDraft] = field(default_factory=dict)`,
`definition_intent: DefinitionIntent | None = None`.

Reconstructed fresh on every `agent.run()` call — both `ask()` and each individual `chat()`
turn. `draft_registry` is therefore ephemeral: drafts exist only within the `run()` that
created them and are gone once it completes.

`store` is a reference to `DefinitionBot._store` (the `ResultStore` inherited from `Bot`,
held for the bot's lifetime). Validated specs written by `validate_spec` as `TextEntry`
instances survive across turns because the store itself is held on the bot. This mirrors
the QueryBot `deps.store` pattern exactly.

#### `DefinitionOutput` (Pydantic, `output_type`)

Fields: `status: Status`, `narrative: str`, `spec_draft_token: str | None = None`,
`reason: str | None = None`. Frozen.

Terminal LLM response. `spec_draft_token` must be copied verbatim from `validate_spec` —
the LLM cannot generate it independently. `reason` is populated on `refused`/`error`.

#### `DefinitionPayload` (Pydantic)

Fields: `spec_type`, `spec_name`, `yaml_string`, `spec_draft_token`,
`validation_warnings: list[str]`, `referenced_columns: dict[str,list[str]] | None`,
`metric_spec`, `slice_spec`, `segment_spec`.

Assembled by the bot after `agent.run()`. Exactly one of `metric_spec`/`slice_spec`/
`segment_spec` is set based on `spec_type`; others are `None`. Requires
`arbitrary_types_allowed=True` since aitaem spec objects are frozen dataclasses, not
Pydantic models.

#### Tool result models

| Model | Fields | Purpose |
|---|---|---|
| `RecordDefinitionIntentResult` | `spec_type`, `has_existing_yaml: bool`, `existing_yaml_parse_warning: str \| None` | Confirms intent was recorded; warning set if `existing_yaml` could not be parsed |
| `ColumnInfo` | `name: str`, `dtype: str` | One column in a table schema |
| `ListTablesResult` | `tables: dict[str, list[str]]`, `errors: dict[str, str]` | Successes and per-backend failures; both may be non-empty on partial success |
| `DescribeTableResult` | `table_name`, `backend_type: str`, `columns: list[ColumnInfo]`, `error: str \| None` | Schema for one table; `error` set on unknown backend or table-not-found |
| `DraftSpecResult` | `draft_id: str`, `spec_type`, `yaml_preview: str` | Draft stored; `yaml_preview` is first 800 chars for LLM context |
| `ValidationIssue` | `field: str`, `message: str`, `suggestion: str \| None` | One validation failure |
| `ValidateSpecResult` | `spec_draft_token: str \| None`, `errors: list[ValidationIssue]`, `column_errors: list[ValidationIssue]`, `warnings: list[str]`, `referenced_columns`, `error: str \| None` | Gate result; `spec_draft_token` set only when all checks pass |

`errors` carries structural/SQL/name-conflict/composite-ref failures. `column_errors` carries
live-schema mismatches. `error` (singular) is a tool-level failure (e.g. `draft_id` not found).

#### Tests (`test_definition_types.py`)

- `DefinitionIntent` defaults (`is_update=False`, `existing_yaml=None`)
- `DefinitionDeps` initialises with empty `draft_registry` and `None` intent
- `DefinitionOutput` is frozen (mutation raises)
- `DefinitionPayload` accepts arbitrary types for spec fields
- `ValidateSpecResult` with errors has `spec_draft_token=None`
- `ValidateSpecResult` on success has `spec_draft_token` set and empty error lists

---

### SF-2: `record_definition_intent` (`aitaem/agent/definition_tools.py`)

```
record_definition_intent(
    ctx: RunContext[DefinitionDeps],
    spec_type: Literal["metric", "slice", "segment"],
    description: str,
    existing_yaml: str | None = None,
) -> RecordDefinitionIntentResult
```

Stores a `DefinitionIntent` into `ctx.deps.definition_intent`. If called more than once
per run, the second call overwrites the first (last-write wins). When `existing_yaml` is
provided, parses it immediately via `*Spec.from_yaml()` to extract `spec.name`, sets
`original_name` and `is_update=True`. If `existing_yaml` is malformed, sets
`original_name=None` and `is_update=False` and includes a warning in the result so the LLM
knows the provided YAML could not be parsed.

**Key nuance:** One spec per turn. System prompt instructs the LLM to handle only the
first spec if the user asks for multiple, and suggest calling `ask()` again for additional specs.

#### Tests

- Intent is stored onto `ctx.deps`
- `is_update` is `False` when `existing_yaml` is omitted, `True` when provided
- `original_name` is set to the spec name parsed from `existing_yaml`
- Malformed `existing_yaml` sets `is_update=False`, `original_name=None`, warning in result
- A second call overwrites the first intent
- Return value has correct `spec_type` and `has_existing_yaml`

---

### SF-3: `list_tables` (`aitaem/agent/definition_tools.py`)

```
list_tables(
    ctx: RunContext[DefinitionDeps],
    backend_type: str | None = None,
) -> ListTablesResult
```

Aggregates table names from the connection manager. When `backend_type` is given, calls
`cm.get_connection(backend_type).list_tables()` — on failure, returns a single entry in
`errors`. When `None`, iterates `cm.backend_types` and calls `list_tables()` on each
connector independently; successes accumulate in `tables`, failures accumulate in `errors`,
keyed by `backend_type`. Both fields may be non-empty on partial success — the LLM can act
on available backends while noting which failed.

#### Tests

- Returns `{backend_type: [table_names]}` in `tables` for a single successful backend
- All backends succeed: `tables` has all entries, `errors` is empty
- One backend fails: that backend appears in `errors` with its exception message; others appear in `tables`
- All backends fail: `tables` is empty, `errors` has an entry per backend
- Single-backend request failure: `tables` is empty, `errors` has one entry for that backend

---

### SF-4: `describe_table` (`aitaem/agent/definition_tools.py`)

```
describe_table(
    ctx: RunContext[DefinitionDeps],
    table_name: str,
    backend_type: str,
) -> DescribeTableResult
```

Retrieves the ibis table schema via `connector.get_table(table_name).schema()` and returns
column names and dtype strings. `backend_type` is required — the LLM always has it
available from a prior `list_tables` call, and making it required keeps traces stable
regardless of how many backends the customer has registered. Auto-selection based on backend
count is intentionally absent: adding a second backend must never break prior prompt
patterns. Any exception from the connector is returned as `error` (not propagated).

#### Tests

- Returns correct column names and dtypes for a mocked table schema
- Table-not-found returns `error` and empty `columns`
- Unknown `backend_type` returns `error` (from `get_connection` raising)

---

### SF-5: `draft_spec` (`aitaem/agent/definition_tools.py`)

```
draft_spec(
    ctx: RunContext[DefinitionDeps],
    spec_type: Literal["metric", "slice", "segment"],
    yaml_string: str,
) -> DraftSpecResult
```

Stores the LLM-written YAML string into `ctx.deps.draft_registry` under a freshly minted
`draft_id` (UUID-based, prefixed `dd_`). Performs **no validation** — all validation is
deferred to `validate_spec`. Returns `draft_id` and first 800 chars as `yaml_preview`.
Each call creates a new entry; repeated calls for corrections produce distinct `draft_id`s.

#### Tests

- Draft is stored in `ctx.deps.draft_registry` under the returned `draft_id`
- Two calls produce different `draft_id`s
- `yaml_preview` is truncated to 800 chars for long YAML
- Even structurally invalid YAML is stored without error (no validation here)

---

### SF-6: `validate_spec` (`aitaem/agent/definition_tools.py`)

```
validate_spec(
    ctx: RunContext[DefinitionDeps],
    draft_id: str,
) -> ValidateSpecResult
```

The anti-hallucination gate. Runs five checks in order; returns immediately on first failure:

1. **Draft lookup** — if `draft_id` not in `draft_registry`, return `error` (tool-level).
2. **Structural + SQL** — call `MetricSpec.from_yaml()` / `SliceSpec.from_yaml()` /
   `SegmentSpec.from_yaml()`. Catches `SpecValidationError` and maps each item to
   `ValidationIssue(field, message)`. Returns `errors` list.
3. **Name conflict / name lock** — two sub-checks:
   - If `is_update=False`: check `spec.name` is not already in `spec_cache.metrics` /
     `.slices` / `.segments`. Return conflict error if it is.
   - If `is_update=True`: compare `spec.name` against `ctx.deps.definition_intent.original_name`
     (already extracted at intent-recording time — no re-parse here). If names differ, return
     a name-lock error. If names match, skip the conflict check.
4. **Composite cross-reference** (slice only) — if `spec.is_composite`, verify every name in
   `spec.cross_product` exists in `spec_cache.slices`. Missing names listed in `errors`.
5. **Column existence** (best-effort) — use `ValidationResult.referenced_columns` from
   `spec.validate()` to enumerate columns referenced in SQL expressions. For each, call
   `connector.get_table(table_name)` and check against `ibis_table.columns`. Failures go
   into `column_errors`. If the connection is unavailable (any exception), append a `warning`
   and continue — this check must never be a hard blocker.

On full pass: call `ctx.deps.store.store_text(yaml_string, content_type="application/yaml",
metadata={spec_type, spec_name, referenced_columns (JSON), warnings (JSON)})`. The returned
ResultStore ID becomes `spec_draft_token`. The entry is a `TextEntry` — retrievable via
`store.get_text(token)` and identifiable by `isinstance(entry, TextEntry)`.

**Key nuances:**
- `errors` and `column_errors` are separated so the LLM knows whether to fix the YAML
  structure/SQL or check column names via `describe_table`.
- Column check failure is always a `warning`, never an `error` — live schema may be
  unavailable in some environments.
- The `is_update` flag comes from `ctx.deps.definition_intent`; if intent was never
  recorded, treat as `is_update=False`.

#### Tests

- Unknown `draft_id` returns `error` field set, `spec_draft_token=None`
- Structurally invalid YAML returns non-empty `errors`, `spec_draft_token=None`
- Valid YAML with no conflicts mints `spec_draft_token` and stores a `TextEntry` in `ResultStore`
- `store.get_text(spec_draft_token)` returns entry with correct `text` (the YAML) and metadata
- Duplicate metric name returns name-conflict error
- Duplicate name is NOT an error when `is_update=True` and draft name matches `existing_yaml` name
- Draft with a different name than `existing_yaml` returns a name-lock error even when `is_update=True`
- Composite slice with missing cross-ref name returns cross-ref error
- Composite slice with all refs present produces no cross-ref errors
- Column not in live schema populates `column_errors`, `spec_draft_token=None`
- Connection failure during column check adds `warning`, does not block token mint

---

### SF-7: System prompt builders (`aitaem/agent/definition_bot.py`)

#### `_build_layer_a_definition() -> str`

Layer A: stable workflow and rules, identical for all tenants. Contents:

- **4-step workflow** with tool names, argument descriptions, and ordering rules
  (`record_definition_intent` → `list_tables`/`describe_table` → `draft_spec` → `validate_spec`
  → repeat draft/validate loop on errors → `DefinitionOutput`)
- **YAML format reference** for all three spec types and all three slice subtypes (leaf,
  wildcard, composite), showing required/optional fields and example values
- **Source URI format** by backend type (DuckDB, BigQuery, PostgreSQL) with examples;
  instructs LLM to infer URI from existing catalog entries (Layer B) when unsure
- **Spec Precision Rule** — never set `spec_draft_token` without a valid `validate_spec`
  result; never invent column names; refuse with `status=refused` when data doesn't exist
- **Final response instructions** — how to populate `DefinitionOutput` fields

#### `_build_layer_b_definition(spec_cache) -> str`

Layer B: per-tenant existing catalog, session-stable. Contents:

All spec names are **always** included — this avoids a name-conflict round-trip regardless
of catalog size. Slice subtype (leaf/wildcard/composite) is always shown alongside the name
since it is compact and needed for composite `cross_product` references.

Additional details (source URI, description) are shown only when
`len(metrics) + len(slices) + len(segments) ≤ _LARGE_CATALOG_THRESHOLD`. Above the
threshold, names and slice subtypes are listed but source URIs and descriptions are omitted
to keep the prompt compact.

`_LARGE_CATALOG_THRESHOLD = 32` (same constant as QueryBot; controls detail inclusion, not
name inclusion).

#### Tests

- Layer A contains tool names for all 4 steps
- Layer A contains YAML format for metric, slice (leaf/wildcard/composite), and segment
- Layer A contains Source URI examples
- Layer A contains Spec Precision Rule
- Layer B always lists all metric, slice, and segment names regardless of catalog size
- Layer B always marks slice subtype: `(composite)`, `(wildcard)`, or `(leaf)`
- Layer B shows source URI and description for metrics/segments when total ≤ threshold
- Layer B omits source URI and description when total > threshold, but still lists all names
- Layer B shows `(none)` for empty catalogs

---

### SF-8: `DefinitionBot` class (`aitaem/agent/definition_bot.py`)

#### `DefinitionResponse`

Typed alias: `DefinitionResponse = BotResponse[DefinitionPayload]`. No new logic.

#### `_definition_permission_fingerprint(spec_cache) -> str`

Returns an 8-char MD5 hex of sorted `metrics`, `slices`, and `segments` key sets joined.
Same purpose as QueryBot's `_permission_fingerprint`: stable cache-routing lane per tenant
catalog composition.

#### `_provider_cache_config_definition(model_str, tenant_id) -> dict`

Returns provider-appropriate model settings for prompt caching (Anthropic 5m cache control,
OpenAI prompt cache key). Same pattern as QueryBot's `_provider_cache_config`.

#### `DefinitionBot(Bot)`

```python
def __init__(
    self,
    *,
    model: str | Any,
    connection_manager: ConnectionManager,
    spec_cache: SpecCache,
    tenant_id: str | None = None,
    tools: list[Any] | None = None,
) -> None
```

Uses `self._store` (the `ResultStore` inherited from `Bot`) for cross-turn persistence of
validated specs as `TextEntry` instances. Pattern mirrors `QueryBot` exactly.

**Key nuance:** Bot-specific resources (`_connection_manager`, `_spec_cache`, `_tenant_id`)
must be set **before** `super().__init__()`, because `Bot.__init__` calls `_build_agent()`
which needs these attributes.

#### `_build_agent() -> Agent`

Constructs the pydantic-ai `Agent` with:
- `deps_type=DefinitionDeps`, `output_type=DefinitionOutput`
- A `FunctionToolset` containing all five tools from SF-2 through SF-6
- Static instructions = Layer A + Layer B (session-stable; prompt-cached)
- `ReinjectSystemPrompt(replace_existing=True)` capability
- A dynamic `@agent.instructions` function (Layer C) that injects today's date

#### Tests

- `DefinitionBot` instantiates without error
- `bot.store` is a `ResultStore` instance
- `DefinitionResponse` is a subtype of `BotResponse`
- Agent has all five tool names registered

---

### SF-9: `chat()`, `ask()`, and `_assemble_payload()` (`aitaem/agent/definition_bot.py`)

#### `async def ask(message: str, ...) -> DefinitionResponse`

Calls `agent.run(message, deps=DefinitionDeps(...))` — no `message_history`. Does **not**
accumulate history on `self._message_history`. On exception, calls `_error_response()`.

#### `async def chat(message: str, ...) -> DefinitionResponse`

Calls `agent.run(message, message_history=self._message_history, deps=DefinitionDeps(...))`.
Accumulates history into `self._message_history` on success. On exception, calls
`_error_response()`.

**Key nuance:** `ask()` intentionally never mutates `_message_history`. This is the primary
entry point for DefinitionBot and makes each call fully independent.

**Draft lifetime across turns:** `DefinitionDeps` (and its `draft_registry`) is reconstructed
fresh on every `agent.run()` call. A `draft_id` from turn N is not accessible in turn N+1.
Cross-turn revision via `chat()` works by the LLM re-drafting from scratch — it recovers the
prior YAML from message history (the `DefinitionOutput.narrative` or the `yaml_preview` in
the prior `DraftSpecResult` tool return). The `spec_draft_token` (`TextEntry` in `self._store`) does survive across turns since
`ResultStore` is held on the bot. Explicitly: **revisions across turns re-draft from scratch; there is no cross-turn
draft resumption.** For specs whose YAML exceeds 800 chars, cross-turn recovery via `chat()`
may be lossy since `yaml_preview` is truncated at that limit; full multi-turn refinement is
deferred (ND-10).

#### `@staticmethod _assemble_payload(output: DefinitionOutput, store: ResultStore) -> DefinitionPayload`

When `output.status != ok` or `spec_draft_token` is `None`, returns an empty
`DefinitionPayload` (all fields `None`). Otherwise calls `store.get_text(spec_draft_token)`
to retrieve the `TextEntry`, reads `entry.text` as `yaml_string` and `entry.metadata` for
`spec_type`, `spec_name`, `referenced_columns`, and `warnings`, then re-parses
`entry.text` via `*Spec.from_yaml()` to populate `metric_spec`/`slice_spec`/`segment_spec`.
Re-parse exceptions are silently swallowed (the YAML was already validated). Same static
pattern as `QueryBot._assemble_payload`.

#### `@staticmethod _error_response(exc, run_start, conversation_id) -> DefinitionResponse`

Builds a `DefinitionResponse` with `status=error`, a generic `narrative`, and an empty
`DefinitionPayload`. Synthesises a `RunTrace` with `error` populated from the exception.

#### Tests

- `ask()` returns a `DefinitionResponse`
- `ask()` does not modify `_message_history`
- `chat()` appends to `_message_history` after each call
- `_assemble_payload` with `status=refused` returns all-`None` payload
- `_assemble_payload` reads `yaml_string` from `store.get_text(token).text`
- `_assemble_payload` sets `metric_spec` for `spec_type="metric"`

---

### SF-10: FunctionModel integration tests (`tests/test_agent/test_definition_bot.py`)

End-to-end tests using `pydantic_ai.models.function.FunctionModel` — no real LLM.
The `FunctionModel` callback simulates the 4-step flow: it inspects `ToolReturnPart`
messages to decide which tool to call next, mirroring what the LLM would do.

Test cases to cover:

- Full flow (record_definition_intent → list_tables → describe_table → draft_spec →
  validate_spec → DefinitionOutput) returns `status=ok`
- `response.payload.yaml_string` is populated with the drafted YAML
- `response.payload.metric_spec` is a `MetricSpec` instance with correct `name`
- `bot.get_result(spec_draft_token)` returns a `TextEntry`; `isinstance(entry, TextEntry)` is `True`
- `ask()` does not accumulate history (second `ask()` call has no prior tool calls visible)
- `chat()` accumulates history (second `chat()` call sees first turn's messages)
- `response.trace.tool_calls` contains all five tool names from the flow
- **Correction loop**: FunctionModel simulates `validate_spec` returning errors on the first
  draft (e.g. missing required field), then the LLM calling `draft_spec` again with corrected
  YAML, then `validate_spec` succeeding — final `status=ok` and `spec_draft_token` set.
  Validates that the draft→validate→fix cycle works end-to-end under realistic tool sequencing.
- **`is_update` rename conflict**: FunctionModel provides `existing_yaml` with `name: revenue`
  in `record_definition_intent` (so `is_update=True`), then drafts a spec with `name: orders`
  where `orders` already exists in the spec cache. `validate_spec` must return a **name-lock
  error** (name changed during update), not a conflict error. FunctionModel then corrects the
  draft to `name: revenue` and re-validates — final `status=ok`. Validates both that the
  name-lock check fires with the right message and that the correction path resolves it.

---

### SF-11: Update `aitaem/agent/__init__.py`

Add to `__all__` (alongside existing Phase 1 + 2 exports):

```
# Phase 3 — DefinitionBot
DefinitionBot, DefinitionResponse, DefinitionPayload,
DefinitionIntent, SpecDraft,
ColumnInfo, ListTablesResult, DescribeTableResult,
DraftSpecResult, ValidateSpecResult, ValidationIssue,

# P3.0b — ResultStore discriminated union (also benefits Phase 2 evals)
TabularEntry, TextEntry, WrongEntryKindError,
```

`ResultEntry` (the union type alias) is already exported from Phase 1; its type now widens
to `TabularEntry | TextEntry` — not a new export, but the meaning changes.

#### Tests

- All listed names are importable from `aitaem.agent`
- No existing Phase 1/2 exports are broken

---

### SF-12: Architecture doc updates

**`plans/agent_module/07-non-decisions.md`** — Add ND-10 body: multi-turn iterative
refinement for DefinitionBot is deferred; `chat()` exists but single-turn `ask()` is the
primary model; same deferral applies to QueryBot; user's verbatim confirmation quoted.

**`plans/agent_module/ARCHITECTURE.md`** — Add ND-10 row to the Section 7 table.

**`plans/agent_module/08-implementation-order.md`** — Replace sparse Phase 3 stubs with
P3.0 through P3.4 breakdown matching this plan.

---

## Files Changed Summary

| File | Change |
|---|---|
| `aitaem/connectors/ibis_connector.py` | P3.0a: add `list_tables()` |
| `aitaem/connectors/connection.py` | P3.0c: add `backend_types` property |
| `aitaem/agent/store.py` | P3.0b: discriminated union (`TabularEntry`, `TextEntry`, `WrongEntryKindError`), `store_tabular()`, `store_text()`, `get_tabular()`, `get_text()` |
| `aitaem/agent/query_tools.py` | P3.0b: update call sites to `store_tabular()` / `get_tabular()` |
| `aitaem/agent/definition_types.py` | **New** — all type/data models |
| `aitaem/agent/definition_tools.py` | **New** — 5 tools |
| `aitaem/agent/definition_bot.py` | **New** — DefinitionBot class, prompt builders, `_assemble_payload` |
| `aitaem/agent/__init__.py` | Add Phase 3 exports + `TabularEntry`, `TextEntry`, `WrongEntryKindError` |
| `tests/test_connectors/test_list_tables.py` | **New** — P3.0a/c tests |
| `tests/test_agent/test_store.py` | Add P3.0b tests (discriminated union, round-trip, `WrongEntryKindError`) |
| `tests/test_agent/test_definition_types.py` | **New** — SF-1 tests |
| `tests/test_agent/test_definition_tools.py` | **New** — SF-2 through SF-6 tests |
| `tests/test_agent/test_definition_bot.py` | **New** — SF-7 through SF-11 tests |
| `plans/agent_module/ARCHITECTURE.md` | Add ND-10 to Section 7 table |
| `plans/agent_module/07-non-decisions.md` | Add ND-10 body |
| `plans/agent_module/08-implementation-order.md` | Expand Phase 3 section |

---

## Testing Strategy

Run after each SF:

1. **After P3.0:** `python -m pytest tests/test_connectors/test_list_tables.py -v`
2. **After SF-1:** `python -m pytest tests/test_agent/test_definition_types.py -v`
3. **After SF-2 to SF-6:** `python -m pytest tests/test_agent/test_definition_tools.py -v`
4. **After SF-7 to SF-11:** `python -m pytest tests/test_agent/test_definition_bot.py -v`

**Full suite before commit:**
```bash
python -m pytest tests/test_agent/ --cov=aitaem/agent --cov-report=term-missing
python -m pytest tests/ --ignore=tests/test_agent/    # core suite must stay green
python tools/check_import_graph.py
ruff check aitaem/agent/
```

---

## Success Criteria

- [ ] `from aitaem.agent import DefinitionBot, DefinitionResponse, DefinitionPayload` works
- [ ] `IbisConnector.list_tables()` returns table names from the underlying ibis backend
- [ ] `ConnectionManager.backend_types` returns registered backend names
- [ ] Full 4-step tool flow works end-to-end in a FunctionModel test with `status=ok`
- [ ] `validate_spec` with unknown `draft_id` returns `error` (not raises)
- [ ] `validate_spec` with structurally invalid YAML returns field-level `errors`
- [ ] `validate_spec` with name conflict returns error; conflict is suppressed when `is_update=True`
- [ ] `validate_spec` with missing composite cross-ref returns cross-ref error
- [ ] `validate_spec` with columns absent from live schema returns `column_errors`
- [ ] `validate_spec` with connection failure during column check returns `warning`, not error
- [ ] `ask()` does not accumulate history; `chat()` does
- [ ] `DefinitionPayload.metric_spec` / `slice_spec` / `segment_spec` is populated from validated YAML
- [ ] `bot.get_result(spec_draft_token)` returns a `TextEntry`; `isinstance(entry, TextEntry)` is `True`
- [ ] `store.get_text(tabular_id)` raises `WrongEntryKindError`; `store.get_tabular(text_id)` raises `WrongEntryKindError`
- [ ] `TabularEntry` and `TextEntry` round-trip correctly through `dump_history()` / `load_history()`
- [ ] All Phase 1 and Phase 2 tests remain green
- [ ] `python -m pytest tests/test_agent/ --cov=aitaem/agent` passes with ≥ 85% coverage
- [ ] `python tools/check_import_graph.py` exits 0

---

## Known Deviations from Architecture Doc

| Architecture says | Implementation does | Rationale |
|---|---|---|
| DefinitionBot has `list_tables`, `describe_table`, `validate_spec` tools | Also has `record_definition_intent` and `draft_spec` | Required for token-gated anti-hallucination pattern (mirrors QueryBot v0.2) |
| `validate_spec` uses `referenced_columns` for cross-table-reference checking | Uses it for live column existence checking | Superset of what arch doc describes; same mechanism, stronger guarantee |
