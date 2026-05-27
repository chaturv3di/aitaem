# Changelog

## Unreleased

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
