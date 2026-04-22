# Changelog

## Unreleased

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
