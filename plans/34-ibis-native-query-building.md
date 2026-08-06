# Plan 34 — Ibis-Native Query Building & BigQuery URI Parsing

**Scope:** Two pre-existing dialect-portability bugs found while testing Plan 33 against live BigQuery — not caused by Plan 33. Both stem from `aitaem` hand-rolling SQL/identifier text where Ibis (backed by sqlglot) already handles it correctly per-backend. Fix: stop hand-rolling, delegate to Ibis.

---

## 0. The gaps

### Gap A — `QueryBuilder` emits DuckDB-only SQL, breaks on BigQuery

`QueryBuilder` (`aitaem/query/builder.py`) builds every query as a hand-assembled SQL string, passed to `connector.connection.sql(q)` (`query/executor.py:109`) with no dialect translation — valid only if the string is portable across all backends. It isn't:

- `_build_periods_cte()` (`builder.py:547-559`, used whenever `period_type != "all_time"`) emits SQL:1999 `VALUES`-with-column-list CTE syntax. DuckDB accepts it; BigQuery rejects it (reproduced against sqlglot's BigQuery parser; matches the live BigQuery error exactly).
- The same family hardcodes DuckDB type names (`CAST(... AS VARCHAR/DOUBLE)`); `_qualify_where_with_dim_alias` (`builder.py:445`) forces `dialect="duckdb"` output.
- `test_builder.py`'s only BigQuery-tagged test covers URI parsing, not query SQL — the period-granularity path is DuckDB-validated only.

Fires for any non-`all_time` `period_type` against any non-DuckDB backend — confirmed via a live QueryBot trace (a `weekly` query against a BigQuery-sourced metric failed with this exact error).

User-authored SQL (`MetricSpec.numerator`/`denominator`, `SliceValue`/`SegmentValue.where`) stays as raw text. Today's handling of it is inconsistent: `numerator`/`denominator` and slice-`where` reach the backend fully unparsed, while segment-`where` goes through the hardcoded-`duckdb` `_qualify_where_with_dim_alias`. The fix routes all fragment kinds through one mechanism (see "Fragment splicing" in §1), fixing that inconsistency as a byproduct. `QueryBuilder`'s self-authored scaffolding SQL remains the primary target.

### Gap B — DefinitionBot's BigQuery URI prompt row breaks the format's own pattern

`ConnectionManager._parse_bigquery_uri()` (`connection.py:364-395`) normalizes every `/` to `.` then splits positionally, so `project.dataset.table`, `project/dataset.table`, `project.dataset/table`, and `project/dataset/table` all parse to the identical, correct triple. This is not a parsing bug: GCP identifiers can never contain `.` or `/` internally, so no shape is ambiguous with another.

The friction is in the DefinitionBot prompt (`definition_bot.py:197-201`): DuckDB and Postgres each follow one uniform rule (`<container>/<table>`, single slash), but the BigQuery row breaks it — `<project>/<dataset>.<tbl>`, slash *then* dot. An LLM carries "one slash before the table" over from the sibling rows and has to switch conventions for BigQuery; a live DefinitionBot session took four attempts to land a working URI.

**Fix:**
- **Prompt**: BigQuery row matches its siblings — `bigquery://<project>/<dataset>/<table>`, all-slash.
- **Normalizer** (`validate_spec()`): translates the all-slash LLM form to the core-canonical `project/dataset.table` on the raw YAML draft text (`draft.yaml_string`), once, after Check 1 and before Check 2. Every later step reads the mutated string — Check 2's parse, Check 5's column check, and the `store_text()` that persists it; `commit_spec()` re-parses that same `ResultStore` entry, inheriting the normalization with no second pass. Purpose: uniform stored spec text for the RAG corpus, and explicit LLM→core translation rather than reliance on `_parse_bigquery_uri`'s permissiveness. Fails open on anything it can't confidently normalize.
- **Core** (`_parse_bigquery_uri`): behavior unchanged; docstring and error-message text updated to lead with `project/dataset.table`. No breaking change.

Out of scope, flagged only: `list_tables()`/`describe_table()` don't surface project/dataset, so on a fresh catalog the LLM has no reliable source for them beyond guessing.

---

## 1. Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Gap A fix strategy | Rewrite `QueryBuilder`'s scaffolding SQL (periods CTE, DIM join, null-filtering, group-by/aggregate, outer projection/casts, cross-slice/segment union) as real Ibis expressions (`.filter()`, `.join()`, `.mutate()`, `.group_by().aggregate()`, `.union()`); leave user-authored fragments as raw text | Ibis's per-backend compiler emits correct syntax for the scaffolding, eliminating the bug class. Converting arbitrary user SQL into a typed expression tree isn't generally possible and isn't where the bug is. |
| Periods CTE replacement | Generate the periods table server-side: `ibis.range(0, count).unnest()` → integer offset; `anchor + offset * ibis.interval(<unit>=1)` = `period_start`, `+ interval` again = `period_end`. `anchor`/`count` derived from the unchanged `_generate_period_boundaries()` (its loop steps one fixed calendar unit per iteration, so anchor+count reproduce its output exactly). `<unit>`: `hours`/`days`(×7 weekly)/`months`/`years` per `period_type`. | Verified across DuckDB/BigQuery/Postgres, including calendar-variant units (months/years). Avoids `ibis.memtable()` (per-execution BigQuery upload via `load_table_from_dataframe`) and `ibis.literal([...]).unnest()` (inlines every row as literal SQL text — doesn't scale for large period counts). `_generate_period_boundaries()` and its tests are untouched. |
| Period-boundary semantics | Half-open `[period_start, period_end)`: predicate is `timestamp >= period_start AND timestamp < period_end` (`>=`, strict `<`), unchanged from today and identical for the `all_time` time-window filter (`_build_time_filter_sql`) and the per-period join (`builder.py:358-359`). | Matches `_generate_period_boundaries()`'s contract (`builder.py:479`). The Ibis rewrite must reproduce this exactly — not `BETWEEN` (double-counts boundary rows) or `>` (drops start rows). |
| Fragment splicing | Wrap each fragment in `SELECT *, <fragment> AS <alias> FROM <table>` and embed via `Table.sql(...)`, no explicit `dialect=` (same-dialect parse+reprint via sqlglot, not cross-dialect transpile). Applies uniformly to `numerator`/`denominator`, slice-`where`, and segment-`where`. | Ibis has no scalar/column-level raw-SQL API, only whole-table `Table.sql()`. Single dependency surface (Ibis→sqlglot) vs. a second hand-rolled string-splicing layer that would also escape SF-1/SF-2's dialect-compilation checks. Byproduct: segment-`where`'s hardcoded-`duckdb` bug is fixed, since it now re-emits in the target dialect. Cost: `numerator`/`denominator`/slice-`where` gain a first-ever sqlglot parse-and-reprint dependency; `Table.sql()` normalizes rather than passes through, so the regression surface is a backend-specific construct being reprinted into something the backend rejects or reinterprets — pinned by SF-7's round-trip fidelity test, not just a compile check. |
| `build_queries()` signature | Gains a required `connection_manager` (or per-source connector) parameter, threaded from `Insights.compute()`, used to obtain a bound `ibis.Table` per source. | Both fragment-splicing (`Table.sql()` raises `IbisError` on unbound tables) and the DIM join need a live table handle to resolve schema — even though the result stays lazy/unexecuted. The one caller, `compute()` (`insights.py:216`), already holds `self.connection_manager` and reuses it immediately for `QueryExecutor`. Spec loading/validation/caching never call `QueryBuilder`, so that layer stays connection-free. |
| Expression construction | Build all filters/joins/mutations directly against the bound `ibis.Table`, not via `ibis._` deferred expressions. | The bound table is available at the top of `_build_metric_segment_query`, before any predicate is built, so deferral adds indirection with no benefit — `t.timestamp_col >= period_start` is as testable and more direct. |
| `QueryGroup` shape | `sql_queries: list[str]` → `expressions: list[ibis.Table]` | Matches what `QueryBuilder` now produces; `QueryExecutor._union_queries` drops the `connector.connection.sql(q)` step. |
| Gap A blast radius | Contained to `aitaem/query/` and its tests | `QueryGroup`/`QueryBuilder`/`QueryExecutor` aren't exported from `aitaem/__init__.py`, aren't in `docs/api/*.md`, and nothing outside `aitaem/query/` imports `QueryGroup.sql_queries`. |
| Gap B fix strategy | Core `_parse_bigquery_uri` unchanged (no shape rejected); DefinitionBot prompt switches to the all-slash sibling pattern (`bigquery://<project>/<dataset>/<table>`); a normalizer in `validate_spec()` translates the all-slash LLM form to the core-canonical `project/dataset.table`. | The friction is prompt inconsistency, not a parsing defect, so the fix targets the prompt. The normalizer gives explicit LLM→core translation and uniform stored-spec text. |
| Where the normalizer runs | Once in `validate_spec()`, mutating `draft.yaml_string` in place (after Check 1, before Check 2) via `yaml.safe_load`/`safe_dump`. Not in `commit_spec`; not via `dataclasses.replace()`. | `store_text()` persists the mutated string; `commit_spec` re-parses that same `ResultStore` entry, inheriting the normalization with no second pass and no drift. |
| Normalization failure mode | Fail-open: on YAML parse failure, unexpected shape, missing `source` key, or unrecognized URI shape, skip the rewrite. | Matches Check 5's warn-not-block posture — a miss degrades to today's behavior, not a new hard failure. |

---

## 2. Scope

**In scope:**
- SF-1 — `QueryBuilder`: convert the `all_time` query-building path to real Ibis expressions.
- SF-2 — `QueryBuilder`: convert the non-`all_time` (period-granularity) path to real Ibis expressions, including a server-side `ibis.range()`+interval-arithmetic periods join. This is the fix for the reported bug.
- SF-3 — `QueryExecutor`: simplify `_union_queries` now that inputs are already `ibis.Table` expressions.
- SF-4 — `ConnectionManager._parse_bigquery_uri()`: docstring/error-message wording only, leading with `project/dataset.table`; parsing behavior unchanged.
- SF-5 — `aitaem/agent/definition_tools.py`: `validate_spec()` gains a one-time raw-YAML-text normalizer that translates an LLM-authored `bigquery://project/dataset/table` source URI into the core-canonical `project/dataset.table` form, fail-open on anything unexpected.
- SF-6 — `aitaem/agent/definition_bot.py`: simplify Layer A's BigQuery URI prompt row to the single all-slash form, matching its DuckDB/Postgres siblings.
- SF-7 — Tests: rewrite `test_builder.py`'s string-content assertions into expression/schema assertions; add BigQuery-dialect compilation checks for both query paths; add `_parse_bigquery_uri` regression coverage confirming unchanged behavior; add normalizer unit tests and a `validate_spec` integration test.
- SF-8 — Docs: changelog entry for all fixes.

**Out of scope:**
- Making `numerator`/`denominator`/`where` fragments anything other than raw backend-native SQL text.
- `list_tables()`/`describe_table()` surfacing project/dataset info to the LLM — flagged for awareness, not fixed here.
- Any change to `MetricSpec`/`SliceSpec`/`SegmentSpec` schema.
- Postgres/DuckDB URI parsing — confirmed not affected (single unambiguous separator each).
- Live BigQuery execution testing — validation is via sqlglot/Ibis dialect-compilation checks and existing DuckDB-backed integration tests, not a live BigQuery connection (none available in this environment).
- Rewriting the LLM's source URI anywhere other than `validate_spec` — no pass in `commit_spec`, no rewrite of already-committed specs.

---

## 3. Sub-features

### SF-1 — `QueryBuilder` all_time path → Ibis expressions

**File:** `aitaem/query/builder.py`
- `build_queries()` gains the required `connection_manager`/connector parameter (see "`build_queries()` signature" in §1) and obtains a bound `ibis.Table` per source.
- Replace the hand-assembled `WITH _labeled AS (...) SELECT ... FROM _labeled [WHERE] [GROUP BY]` string (`_build_metric_segment_query`'s `all_time` branch, `builder.py:280-337`) with `.filter()` (time window, half-open `>= start AND < end` — see "Period-boundary semantics" in §1; DIM join predicate), `.join()` (DIM table), `.mutate()` (slice/segment CASE-WHEN-equivalent columns, outer projection literals via `ibis.literal(...)`), and `.group_by().aggregate()` where applicable — all built directly against the bound table.
- Slice/segment CASE-WHEN expressions (`_build_slice_case_when_expr`, `_build_segment_case_when_expr`) and the metric value expression (`_build_metric_value_expr`) embed their user-authored fragments via `Table.sql()`, each wrapped in a `SELECT *, <fragment> AS <alias> FROM <table>` query scoped to the specific table (see "Fragment splicing" in §1). This replaces `_qualify_where_with_dim_alias`'s hand-rolled hardcoded-`duckdb` rewrite for segment `where`.

### SF-2 — `QueryBuilder` non-all_time (period granularity) path → Ibis expressions

**File:** `aitaem/query/builder.py`
- Replace `_build_periods_cte()`'s `VALUES` CTE with a server-generated spine: call the unchanged `_generate_period_boundaries()` and take only `anchor = boundaries[0][0]` and `count = len(boundaries)`. Map `period_type` to an Ibis interval unit — `hourly`→`hours=1`, `daily`→`days=1`, `weekly`→`days=7`, `monthly`→`months=1`, `yearly`→`years=1`.
- Build the periods table: `spine = ibis.range(0, count).unnest().name("_offset").as_table()`, `period_start = anchor + spine._offset * ibis.interval(**{unit: 1})`, `period_end = period_start + ibis.interval(**{unit: 1})`. No pandas object, no `ibis.memtable`, no literal-per-row embedding — the SQL engine generates the series natively.
- Replace the periods `JOIN` (`builder.py:356-360`) with `.join()` against this spine table, predicate `timestamp >= period_start AND timestamp < period_end` (half-open, unchanged).
- Same treatment as SF-1 for the rest of the non-`all_time` branch (`builder.py:339-399`): DIM join, outer projection/casts, null-filtering, `GROUP BY`.
- Direct fix for the reported bug — verify with a BigQuery-dialect compilation check (`ibis.to_sql(expr, dialect="bigquery")`) asserting no `VALUES`-with-column-list construct appears and the query compiles; also verify DuckDB/Postgres compilation and correct row output (live DuckDB execution) for at least one fixed-length unit (daily/weekly) and one calendar-variant unit (monthly).

### SF-3 — `QueryExecutor` simplification

**File:** `aitaem/query/executor.py`
- `_union_queries()` (`executor.py:100-113`) drops the `connector.connection.sql(q)` step — inputs are already `ibis.Table` expressions from `QueryGroup.expressions`. Union logic (`result.union(t)`) is unchanged.
- `QueryGroup.sql_queries: list[str]` (`builder.py:30`) → `QueryGroup.expressions: list[ibis.Table]`.

### SF-4 — BigQuery source-URI documentation consistency (core, no behavior change)

**File:** `aitaem/connectors/connection.py`
- `_parse_bigquery_uri()`'s replace-then-split logic (`connection.py:377-395`) is unchanged — it already accepts all four shapes and parses them to the same correct triple.
- Update its docstring and the `InvalidURIError` message (lines 384-389) to lead with `project/dataset.table`, consistently with `parse_source_uri`'s docstring (`connection.py:263-296`, currently leads with the dotted form at line 274). Wording-only change; no shape is rejected.

### SF-5 — LLM-facing BigQuery URI normalizer in `validate_spec`

**File:** `aitaem/agent/definition_tools.py`
- Add `_normalize_bigquery_source_uri(uri: str) -> str`: for a `bigquery://` URI shaped `project/dataset/table` (all-slash, the prompt's canonical form), rewrite the last `/` to `.`, producing `project/dataset.table`. No-op for any other scheme or shape — including forms `_parse_bigquery_uri` already accepts; the goal is to normalize only the shape the prompt now asks the LLM to produce.
- Add `_normalize_source_in_yaml(yaml_text: str) -> str`: `yaml.safe_load` the draft text; if parsing fails, the shape isn't as expected, or there's no `spec.source` key, return `yaml_text` unchanged. Otherwise run `source` through `_normalize_bigquery_source_uri` and `yaml.safe_dump` back to text.
- Wire `_normalize_source_in_yaml` into `validate_spec()` right after Check 1 and before Check 2, rewriting `draft.yaml_string` in place. No changes to `commit_spec()`.

### SF-6 — DefinitionBot prompt copy simplification

**File:** `aitaem/agent/definition_bot.py`
- Update the Source URI Format table (`definition_bot.py:200`, BigQuery row) from `bigquery://<project>/<dataset>.<tbl>` to `bigquery://<project>/<dataset>/<table>`, with a matching example (e.g. `bigquery://myproject/ds/sales`).
- DuckDB/Postgres rows are unaffected.

### SF-7 — Tests

**Files:** `tests/test_query/test_builder.py`, `tests/test_query/test_executor.py`, `tests/test_connectors/test_connection_manager.py`, `tests/test_agent/test_definition_tools.py`
- `test_builder.py`: rewrite raw-SQL-substring assertions into expression/schema assertions, or `ibis.to_sql(expr, dialect=...)` checks (at minimum `duckdb` and `bigquery`).
- New: non-`all_time` query building compiles cleanly under `dialect="bigquery"` — the direct regression test for the reported bug.
- New: period-boundary edge case — a row with a timestamp exactly equal to a `period_end` value is excluded from that period (and included in the next), and one exactly equal to `period_start` is included; covers both the `all_time` filter and the per-period join.
- New: fragment-splicing parse coverage — a `numerator`/`denominator`/`where` fragment using non-trivial syntax compiles via `Table.sql()` under each supported dialect (`duckdb`, `bigquery`, `postgres`); also confirm a segment-`where` fragment now re-emits in the target dialect instead of hardcoded `duckdb`.
- New: fragment-splicing **round-trip fidelity** — the real regression surface, since these fragments gain a first-ever sqlglot parse-and-reprint (see "Fragment splicing" in §1). For at least one backend-specific construct per dialect that must survive intact (e.g. BigQuery `SAFE_CAST(...)`/`SAFE_DIVIDE(...)`/`COUNTIF(...)`, DuckDB `list_aggregate(...)`), assert the reprinted SQL preserves that construct's semantics — the backend-specific function token is not transpiled away or dropped. This is a tripwire: pins current behavior so a future sqlglot upgrade that mangles a backend-specific fragment fails loudly rather than silently. (Benign syntactic normalization — `!=`→`<>`, `::`→`CAST`, quote style — is expected and not asserted against; the check targets the function/construct, not byte-identity of the whole fragment.)
- New: `_parse_bigquery_uri` — regression coverage confirming all four shapes still parse to the identical, correct triple (locks in SF-4's "no behavior change" claim).
- New: `_normalize_bigquery_source_uri`/`_normalize_source_in_yaml` unit tests — `project/dataset/table` → `project/dataset.table`; no-op for non-bigquery schemes, malformed YAML, a missing `source` key, and an already-canonical URI.
- New: `validate_spec` integration test — a draft with `source: bigquery://project/dataset/table` passes validation, and the minted draft's stored text contains the normalized `bigquery://project/dataset.table` form.
- `test_executor.py`: update for `QueryGroup.expressions` in place of `.sql_queries`.

### SF-8 — Docs

- `docs/changelog.md` → `## Unreleased` → `### Fixed`: non-`all_time` (period-granularity) queries against non-DuckDB backends (confirmed: BigQuery) no longer fail with a dialect syntax error; DefinitionBot's BigQuery URI prompt row now matches its DuckDB/Postgres siblings' pattern, with `validate_spec()` translating the LLM's form to the core-canonical `project/dataset.table` before storage. No breaking change — `_parse_bigquery_uri` accepts every shape it did before.

---

## 4. Files changed summary

| File | Change |
|---|---|
| `aitaem/query/builder.py` | SF-1: all_time path → Ibis expressions; SF-2: non-all_time path → Ibis expressions incl. `ibis.range()`-based periods spine; `build_queries()` gains `connection_manager` param; `QueryGroup.expressions` replaces `.sql_queries` |
| `aitaem/query/executor.py` | SF-3: `_union_queries` simplified — no more `connector.connection.sql(q)` |
| `aitaem/connectors/connection.py` | SF-4: docstring/error-message wording only — leads with `project/dataset.table`; parsing behavior unchanged |
| `aitaem/agent/definition_tools.py` | SF-5: `_normalize_bigquery_source_uri`/`_normalize_source_in_yaml` helpers, wired into `validate_spec()` before Check 2 |
| `aitaem/agent/definition_bot.py` | SF-6: Source URI Format table — BigQuery row simplified to all-slash form |
| `tests/test_query/test_builder.py` | SF-7: string-assertions → expression/schema assertions; BigQuery-dialect compilation checks |
| `tests/test_query/test_executor.py` | SF-7: updated for `QueryGroup.expressions` |
| `tests/test_connectors/test_connection_manager.py` | SF-7: `_parse_bigquery_uri` regression coverage — all four shapes still parse identically |
| `tests/test_agent/test_definition_tools.py` | SF-7: normalizer unit tests; `validate_spec` normalization integration test |
| `docs/changelog.md` | SF-8: Unreleased entry (no breaking-change note — parsing behavior unchanged) |
