# Plan 11: Remove `aggregation` Field from MetricSpec

## Motivation

The `aggregation` field in `MetricSpec` is redundant:

- The SQL aggregation function (`SUM`, `AVG`, `COUNT`, etc.) is already embedded in `numerator`
  (and `denominator`) as part of the SQL expression — e.g. `numerator: SUM(revenue)`.
- The only purpose `aggregation` serves is to distinguish `ratio` from everything else, and that
  is already implied by the presence of a non-null `denominator`.

**Result**: drop `aggregation` entirely and infer aggregation type purely from `numerator`,
`denominator`, and the SQL function calls they contain.

---

## New Spec Contract

### Required fields (unchanged)
`name`, `source`, `numerator`, `timestamp_col`

### Optional fields (unchanged)
`description`, `denominator`, `entities`

### Removed field
`aggregation`

### Validation rules for the new contract

| Rule | Details |
|---|---|
| `numerator` must contain an aggregation call | The SQL expression must contain at least one aggregate function: `SUM`, `AVG`, `COUNT`, `MIN`, `MAX` (case-insensitive). `COUNT(*)` and `COUNT(DISTINCT …)` are valid. |
| `denominator`, when present, must also contain an aggregation call | Same set of aggregate functions. |
| Ratio is implied by `denominator` being non-null | No explicit tag needed. |
| Plain column references without an aggregate are invalid | e.g. `numerator: revenue` → validation error. |

### Updated examples

> Note: examples below show only required fields to highlight the removal of `aggregation`.
> Optional fields (`description`, `entities`, `denominator`) are unchanged and not shown for brevity.

```yaml
# sum
metric:
  name: total_revenue
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "SUM(revenue)"
  timestamp_col: date

# avg
metric:
  name: avg_revenue
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "AVG(revenue)"
  timestamp_col: date

# count
metric:
  name: campaign_count
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "COUNT(*)"
  timestamp_col: date

# max
metric:
  name: max_revenue
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "MAX(revenue)"
  timestamp_col: date

# min
metric:
  name: min_revenue
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "MIN(revenue)"
  timestamp_col: date

# ratio (implied by denominator)
metric:
  name: ctr
  source: duckdb://ad_campaigns.duckdb/ad_campaigns
  numerator: "SUM(clicks)"
  denominator: "SUM(impressions)"
  timestamp_col: date
```

---

## Implementation Checklist

### Sub-feature 1 — Update `MetricSpec` dataclass

**File**: `aitaem/specs/metric.py`

- [ ] Remove the `aggregation: str` field from the `MetricSpec` dataclass.
- [ ] In `from_yaml()`: remove the line that reads and lowercases `spec_dict["aggregation"]`.
- [ ] In `from_yaml()`: remove `aggregation=aggregation` from the `cls(...)` constructor call.
- [ ] In `validate()`: remove `"aggregation": self.aggregation` from `spec_dict`.

**Test**: use existing `test_specs/test_metric_spec.py` after updating it (see Sub-feature 3).

---

### Sub-feature 2 — Update validation logic

**File**: `aitaem/utils/validation.py`

- [ ] Remove `VALID_AGGREGATIONS` constant.
- [ ] Remove the entire aggregation validation block (lines 94–110).
- [ ] Replace the ratio-detection block (lines 134–150) with:
  - If `denominator` is present, validate its SQL expression (already done for numerator).
  - If `denominator` is absent, no action needed.
- [ ] Add a helper `_contains_aggregate_call(expr: str) -> bool` that uses `sqlglot` to parse the
  expression and checks whether the AST contains any node of type
  `sqlglot.exp.AggFunc` (the base class for `Sum`, `Avg`, `Count`, `Min`, `Max`).
  - Fall back to `True` (skip validation) if `sqlglot` is not installed, consistent with existing
    SQL syntax validation behaviour.
- [ ] In `validate_metric_spec`, after the existing SQL syntax check for `numerator`, call
  `_contains_aggregate_call` and append a `ValidationError` if it returns `False`.
  - Error message: `"'numerator' must contain an aggregate function (SUM, AVG, COUNT, MIN, MAX)"`
- [ ] In `validate_metric_spec`, do the same for `denominator` when it is present.
  - Error message: `"'denominator' must contain an aggregate function (SUM, AVG, COUNT, MIN, MAX)"`
- [ ] Remove the warning log for "denominator is ignored for non-ratio aggregation" (now obsolete).

**Test**: use existing `test_utils/test_validation.py` after updating it (see Sub-feature 3).

---

### Sub-feature 3 — Update tests

#### `tests/test_specs/test_metric_spec.py`
- [ ] Remove `aggregation` from every YAML string fixture and every `MetricSpec(...)` constructor call.
- [ ] Remove `test_aggregation_normalized_to_lowercase` test (concept no longer applies).
- [ ] Remove `test_unsupported_aggregation_raises` test.
- [ ] Add tests for the new validation rules:
  - `test_numerator_without_aggregate_raises` — plain column ref in numerator → error
  - `test_denominator_without_aggregate_raises` — plain column ref in denominator → error
  - `test_denominator_with_aggregate_is_valid` — `SUM(x)` / `SUM(y)` → valid
  - `test_count_star_is_valid` — `COUNT(*)` in numerator → valid
  - `test_count_distinct_is_valid` — `COUNT(DISTINCT user_id)` in numerator → valid

#### `tests/test_specs/test_spec_loader.py`
- [ ] Remove `aggregation` from every `MetricSpec(...)` constructor call.

#### `tests/test_specs/conftest.py`
- [ ] Remove `aggregation` from YAML fixture strings.

#### `tests/test_specs/fixtures/valid_metric_sum.yaml`
- [ ] Remove `aggregation: sum` line.

#### `tests/test_specs/fixtures/valid_metric_ratio.yaml`
- [ ] Remove `aggregation: ratio` line.

#### `tests/test_specs/fixtures/invalid_metric_no_denominator.yaml`
- [ ] Remove `aggregation: ratio` line.
- [ ] Update the `numerator` to include an aggregate function (e.g. `SUM(some_col)`) so the only
  remaining error is the missing denominator — but wait, without `aggregation: ratio` there is no
  longer a rule that requires a denominator. **This fixture tests the wrong thing after the
  change.** Replace it with a fixture that tests a plain-column numerator instead:
  `invalid_metric_no_aggregate_in_numerator.yaml`.

#### `tests/test_query/test_builder.py`
- [ ] Update `make_metric()` helper: remove `aggregation=agg` parameter and field.
- [ ] Update `make_ratio_metric()` helper: remove `aggregation="ratio"` field.
- [ ] Scan all remaining direct `MetricSpec(...)` calls and remove `aggregation=` argument.

#### `tests/test_query/test_builder_period_granularity.py`
- [ ] Remove `aggregation="sum"` from all `MetricSpec(...)` calls.

#### `tests/test_query/test_executor.py`
- [ ] Remove `aggregation=` from all `MetricSpec(...)` calls.

#### `tests/test_insights_by_entity.py`
- [ ] Remove `aggregation:` lines from all inline YAML metric fixtures.

---

### Sub-feature 4 — Update query builder

**File**: `aitaem/query/builder.py`

- [ ] In `_build_metric_value_expr`: change `if metric.aggregation == "ratio":` to
  `if metric.denominator is not None:`.
- [ ] No other changes needed in this file.

---

### Sub-feature 5 — Update example YAML files

**Directory**: `examples/metrics/`

- [ ] `total_revenue.yaml` — remove `aggregation: sum`
- [ ] `avg_revenue.yaml` — remove `aggregation: avg`
- [ ] `campaign_count.yaml` — remove `aggregation: count`; update `numerator` if it currently has a
  bare column — verify it already uses `COUNT(*)` or similar
- [ ] `max_revenue.yaml` — remove `aggregation: max`
- [ ] `roas.yaml` — remove `aggregation: ratio`
- [ ] `ctr.yaml` — remove `aggregation: ratio`

---

### Sub-feature 6 — Update documentation

#### `docs/user-guide/specs.md`
- [ ] Remove the `aggregation` row from the MetricSpec field table.
- [ ] Update all YAML code examples (every block that has `aggregation:`) to remove that line.
- [ ] Update the prose explanation: replace description of the `aggregation` field with an
  explanation that the aggregation type is inferred from the SQL function in `numerator` /
  `denominator`, and that ratio is implied by a non-null `denominator`.

#### `docs/changelog.md`
- [ ] Add an entry under `## [Unreleased]`:
  ```
  ### Changed
  - **MetricSpec**: removed `aggregation` field; aggregation type is now inferred from the
    SQL functions in `numerator` and `denominator`. Ratio is implied when `denominator` is present.
  ### Migration
  - Remove `aggregation:` from all metric YAML specs.
  - Ensure `numerator` (and `denominator` when present) contain an explicit aggregate function
    call such as `SUM(col)`, `AVG(col)`, `COUNT(*)`, `MIN(col)`, or `MAX(col)`.
  ```

#### `aitaem/specs/README.md`
- [ ] Remove `aggregation` from the YAML schema block and field description table.
- [ ] Update all YAML examples (multiple locations) to remove `aggregation:`.
- [ ] Update the API signature section (line ~303) to remove `aggregation: str`.
- [ ] Update the example output (line ~456) that prints `ctr.aggregation`.

#### `ARCHITECTURE.md`
- [ ] Remove `aggregation: str` from the class field definition block.
- [ ] Update all YAML examples in the architecture document.
- [ ] Update any prose that mentions the `aggregation` field or the `VALID_AGGREGATIONS` set.

---

## Files Changed (complete list)

| File | Type of change |
|---|---|
| `aitaem/specs/metric.py` | Remove field + from_yaml + validate |
| `aitaem/utils/validation.py` | Remove aggregation checks; add aggregate-call checks |
| `aitaem/query/builder.py` | `metric.aggregation == "ratio"` → `metric.denominator is not None` |
| `examples/metrics/total_revenue.yaml` | Remove `aggregation:` |
| `examples/metrics/avg_revenue.yaml` | Remove `aggregation:` |
| `examples/metrics/campaign_count.yaml` | Remove `aggregation:`; verify numerator |
| `examples/metrics/max_revenue.yaml` | Remove `aggregation:` |
| `examples/metrics/ctr.yaml` | Remove `aggregation:` |
| `examples/metrics/roas.yaml` | Remove `aggregation:` |
| `tests/test_specs/test_metric_spec.py` | Remove aggregation refs; add new aggregate-call tests |
| `tests/test_specs/test_spec_loader.py` | Remove `aggregation=` from constructors |
| `tests/test_specs/conftest.py` | Remove `aggregation:` from YAML fixtures |
| `tests/test_specs/fixtures/valid_metric_sum.yaml` | Remove `aggregation: sum` |
| `tests/test_specs/fixtures/valid_metric_ratio.yaml` | Remove `aggregation: ratio` |
| `tests/test_specs/fixtures/invalid_metric_no_denominator.yaml` | Replace with `invalid_metric_no_aggregate_in_numerator.yaml` |
| `tests/test_query/test_builder.py` | Remove `aggregation=` from all MetricSpec calls |
| `tests/test_query/test_builder_period_granularity.py` | Remove `aggregation=` |
| `tests/test_query/test_executor.py` | Remove `aggregation=` |
| `tests/test_insights_by_entity.py` | Remove `aggregation:` from inline YAML |
| `docs/user-guide/specs.md` | Remove field docs; update examples and prose |
| `docs/changelog.md` | Add unreleased entry |
| `aitaem/specs/README.md` | Remove all aggregation references |
| `ARCHITECTURE.md` | Remove all aggregation references |

---

## Out of Scope

- `plans/02-specs-module.md` and `plans/03-query-module.md` reference `aggregation` in the
  historical plan documents. These are historical artefacts and do **not** need to be updated.
- `tests/test_connectors/test_bigquery_integration.py` has a method named
  `test_execute_aggregation_query` — this name is about SQL aggregation in general, not the
  `MetricSpec.aggregation` field, and does **not** need to change.

---

## Validation Strategy

After each sub-feature:
1. Run `python -m pytest tests/test_specs/ tests/test_query/ tests/test_insights_by_entity.py -x`
2. After all sub-features: run full suite `python -m pytest` with coverage.
3. Run `ruff check . && ruff format .` for linting.
