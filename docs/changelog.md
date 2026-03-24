# Changelog

## Unreleased

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

## v0.1.2 — 2025-05-xx

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
