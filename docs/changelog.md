# Changelog

## Unreleased

## v0.3.0 — 2026-06-03

### Added

- **`SegmentSpec.entity_id`** — required field identifying the primary key column on the DIM
  table. Used as the right-hand side of the generated JOIN ON condition
  (`_dim.<entity_id>`).

- **`SegmentSpec.join_keys`** — optional whitelist of fact-table FK columns that may be used
  as join keys for this segment. When non-empty, the join key supplied at `compute()` time
  must appear in this list; otherwise a `QueryBuildError` is raised.

- **`segments` dict form in `MetricCompute.compute()`** — `segments` now accepts
  `dict[str, str] | str | None`. The dict form maps exactly one segment name to an explicit
  fact-table FK column, enabling the same segment spec to be joined via different columns
  (e.g., `buyer_id` vs `seller_id` on a transactions table).

- **DIM-table JOIN in generated SQL** — when a segment has `entity_id` set, aitaem generates
  a proper JOIN from the fact table to the DIM table rather than applying segment predicates
  inline against the fact table. Unqualified column references in `values[].where` expressions
  are automatically qualified with `_dim.` via sqlglot AST rewriting.

- **`referenced_columns` for segment specs** — `ValidationResult.referenced_columns` now
  includes `"entity_id"`, `"join_keys"` (when non-empty), and `"values[i].where"` keys for
  segment specs.

### Changed (Breaking)

- **`SegmentSpec.entity_id` is now required.** Existing segment specs without this field will
  fail validation with a `SpecValidationError`. Add `entity_id: <dim_pk_column>` to every
  segment spec YAML file.

- **`SegmentSpec.source` is now used.** Previously parsed but ignored, `source` is now the
  URI of the DIM table that will be joined at query time. Ensure it points to the correct DIM
  table, not the fact table.

- **`segments` in `compute()` no longer accepts `list[str]`.** The parameter type changed from
  `str | list[str] | None` to `dict[str, str] | str | None`. Multi-segment calls are no longer
  supported in a single `compute()` call; call `compute()` once per segment instead.

## v0.2.2 — 2026-06-01

### Added

- **`ValidationResult.referenced_columns`** — populated on successful spec validation; a
  `dict[str, list[str]]` mapping each spec field to the unqualified column names it references.
  `None` when the spec is invalid. Intended for downstream consumers who hold a warehouse
  connection and want to verify that every referenced column is present in the source table
  before computing metrics. See [Column introspection](user-guide/specs.md#column-introspection)
  for usage.

## v0.2.1 — 2026-05-28

### Added

- **`MetricSpec.format`** — optional metadata field for metric value interpretation.
  Allowed values: `percentage`, `absolute`, `ratio`, `currency`, and `currency:<CODE>` where
  `<CODE>` is a 3-letter uppercase ISO 4217 currency code (e.g. `currency:USD`). Plain
  `"currency"` is valid for monetary metrics with mixed or unspecified currency. Validated at
  spec load time; invalid values raise `SpecValidationError`.

- **`metric_format` output column** — every `compute()` result now includes a `metric_format`
  column (inserted after `metric_name`) carrying the spec's `format` value, or `None` when
  `format` is not set. The output schema now has 11 columns.

- **`hourly` period type** — `period_type="hourly"` produces one output row per clock hour.
  `time_window` now accepts full ISO datetime strings (e.g. `"2024-01-15T08:00:00"`) when
  using hourly granularity; plain date strings fall back to midnight. Sub-hour precision in the
  start value is silently truncated to the nearest full hour.

- **`METRIC_FORMAT_VALUES`** — new constant exported from `aitaem`, a `frozenset` of the simple
  format values: `{"percentage", "absolute", "ratio", "currency"}`.

### Changed (Breaking)

- **`STANDARD_COLUMNS`** now has **11 entries**. The `metric_format` column is inserted at
  index 5 (after `metric_name`). Code that relies on column position or count (e.g.
  `df.iloc[:, 9]`) must be updated.

## v0.2.0 — 2026-05-27

### Changed (Breaking)
- `MetricSpec`, `SliceSpec`, `SegmentSpec`: the `name` field is now validated as a
  SQL identifier at load time. Names must match `^[A-Za-z_][A-Za-z0-9_]*$` — letters,
  digits, and underscores only, starting with a letter or underscore. Specs whose names
  contain spaces, hyphens, dots, or other characters will raise `SpecValidationError`
  at load time rather than failing silently or raising `QueryExecutionError` at compute time.

  **Migration:** rename any affected specs.
  For example: `"English speaking countries"` → `"english_speaking_countries"`,
  `"revenue-2024"` → `"revenue_2024"`. The validation error message includes a
  suggested replacement name.

- `SpecCache.from_yaml()`, `SpecCache.from_string()`, `SpecCache.add()`: now raise
  `SpecValidationError` when a spec with a duplicate name is loaded. Previously
  `from_yaml()` logged a warning and overwrote the earlier spec; `from_string()` and
  `add()` silently kept the first. Uniqueness is enforced per spec type (metrics, slices,
  and segments have independent namespaces).

  **Migration:** ensure all spec files have unique names per type. If you were relying on
  the overwrite behaviour to update a spec at runtime, use `cache.clear()` followed by a
  fresh load instead.

- `ConnectionError` renamed to `AitaemConnectionError` throughout the library to avoid
  shadowing Python's built-in `ConnectionError`.

  **Migration:** replace any `except ConnectionError` or `from aitaem... import ConnectionError`
  with `AitaemConnectionError`, which is now importable directly from `aitaem`.

### Added
- `STANDARD_COLUMNS: list[str]` is now importable directly from `aitaem`. Contains the
  ordered list of column names that `MetricCompute.compute()` always returns:
  `period_type`, `period_start_date`, `period_end_date`, `entity_id`, `metric_name`,
  `slice_type`, `slice_value`, `segment_name`, `segment_value`, `metric_value`.
- Spec types (`MetricSpec`, `SliceSpec`, `SliceValue`, `SegmentSpec`, `SegmentValue`) are
  now importable directly from `aitaem` (previously only from `aitaem.specs`).
- `IbisConnector` is now importable directly from `aitaem` (previously only from
  `aitaem.connectors` or `aitaem.connectors.ibis_connector`).
- All exception classes are now importable directly from `aitaem` (previously required
  internal import paths such as `aitaem.utils.exceptions`).
- `PeriodType` — a `Literal` type alias for valid `period_type` values; importable from
  `aitaem`. Use in Pydantic models or type annotations.
- `VALID_PERIOD_TYPES` — a `frozenset[str]` of valid `period_type` values; importable from
  `aitaem`. Derived from `PeriodType` so both are always in sync.
- `MetricCompute.compute()`: `period_type` parameter is now annotated as `PeriodType`
  (previously bare `str`), enabling IDE completions and static analysis warnings.
- `SpecCache.metrics`, `SpecCache.slices`, `SpecCache.segments` — read-only `Mapping`
  properties for iterating over all loaded specs without individual `get_*` lookups.

## v0.1.5 — 2026-04-22

### Added
- `SliceSpec`: new wildcard variant — set `where: <column_name>` at the spec level
  (instead of listing `values`) to auto-populate slice values from the column's distinct
  values at query time. Supports simple and dot-qualified column names.

### Fixed
- `MetricSpec.from_yaml()`, `SliceSpec.from_yaml()`, `SegmentSpec.from_yaml()`: no longer
  raise an unhandled `OSError` when a YAML string longer than the OS `PATH_MAX` value is
  passed. The path-existence check now wraps `path.is_file()` in `try/except OSError` and
  falls back to treating the input as YAML content.

## v0.1.4 — 2026-03-23

### Changed
- **`MetricSpec`**: removed `aggregation` field. Aggregation type is now inferred from the SQL
  function embedded in `numerator` (and `denominator`). Ratio is implied when `denominator` is
  present. Validation enforces that both `numerator` and `denominator` (when present) contain a
  recognised aggregate function call (`SUM`, `AVG`, `COUNT`, `MIN`, `MAX`).

### Migration guide
- Remove `aggregation:` from all metric YAML specs.
- Ensure `numerator` (and `denominator` when present) contain an explicit aggregate function call
  such as `SUM(col)`, `AVG(col)`, `COUNT(*)`, `MIN(col)`, or `MAX(col)`.

### Added
- `MetricSpec`: new optional `entities` field — declares which entity columns the metric supports for disaggregation (e.g. `entities: [user_id, device_id]`). Must be a non-empty list if provided.
- `MetricCompute.compute()`: new `by_entity` parameter — groups results by an entity column declared in each metric's `entities` list; raises `QueryBuildError` if any metric does not support the requested entity column.
- Standard output schema gains an `entity_id` column (position 4, between `period_end_date` and `metric_name`); `None` when `by_entity` is not set.
- Added PostgreSQL backend support via `ibis-framework[postgres]` (`pip install "aitaem[postgres]"`)
- New `aitaem.connectors.backend_specs` module with `DuckDBConfig`, `BigQueryConfig`, and `PostgresConfig` dataclasses — centralizes backend field validation for all connectors
- PostgreSQL source URI format: `postgres://schema/table` (e.g. `postgres://public/orders`)

## v0.1.3 — 2026-03-17

- New `aitaem.helpers` module for user-facing convenience functions
- New `load_csvs_to_duckdb(csv_path, db_path, overwrite=True)` helper — loads a single CSV or all top-level CSVs in a folder into a DuckDB file and returns a connected `IbisConnector`
- `MetricSpec`: unknown-fields check now uses `dataclasses.fields()` instead of a hard-coded set
- README: updated CSV loading example to use `load_csvs_to_duckdb`

## v0.1.2 — 2026-03-14

- Updated installation instructions to use PyPI
- Added CI, PyPI version, and Python version badges to README

## v0.1.1

- Bug fixes and internal improvements

## v0.1.0 — Initial release

- `MetricSpec`, `SliceSpec`, `SegmentSpec` with YAML parsing and validation
- `SpecCache` with eager loading from files, directories, or strings
- `ConnectionManager` with DuckDB and BigQuery support
- `MetricCompute` — primary user interface for computing metrics
- Cross-product (composite) slice support
- Standard 9-column output DataFrame
- Example ad campaigns dataset with sample YAML specs

---

For full release diffs, see [GitHub Releases](https://github.com/chaturv3di/aitaem/releases).
