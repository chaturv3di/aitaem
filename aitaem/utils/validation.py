"""
aitaem.utils.validation - Spec validation utilities

Two-tier validation:
1. Structural: required fields, enum values, URI format, non-empty strings, list constraints
2. SQL syntax: numerator, denominator, where clauses via sqlglot
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ValidationError:
    field: str
    message: str
    suggestion: str | None = None


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError]


def _validate_sql_expression(
    expr: str, field: str, context: str = "select"
) -> ValidationError | None:
    """Validate SQL expression using sqlglot. Returns ValidationError if invalid, else None."""
    try:
        import sqlglot
    except ImportError:
        logger.warning("sqlglot is not installed; skipping SQL validation for field '%s'", field)
        return None

    try:
        if context == "select":
            sqlglot.parse_one(f"SELECT {expr}")
        else:
            sqlglot.parse_one(f"SELECT 1 WHERE {expr}")
    except Exception as e:
        return ValidationError(field=field, message=f"Invalid SQL syntax: {e}")
    return None


def _contains_aggregate_call(expr: str) -> bool:
    """Return True if the SQL expression contains at least one aggregate function call.

    Recognised aggregates: SUM, AVG, COUNT, MIN, MAX (case-insensitive).
    Falls back to True (skip validation) if sqlglot is not installed.
    """
    try:
        import sqlglot
        import sqlglot.expressions as exp
    except ImportError:
        logger.warning("sqlglot is not installed; skipping aggregate-call validation")
        return True

    try:
        tree = sqlglot.parse_one(f"SELECT {expr}")
    except Exception:
        # Syntax errors are caught separately by _validate_sql_expression
        return True

    return any(isinstance(node, exp.AggFunc) for node in tree.walk())


def _validate_uri(value: str, field: str) -> ValidationError | None:
    """Validate that value is a URI with a scheme (scheme://...)."""
    if "://" not in value:
        return ValidationError(
            field=field,
            message="Invalid source URI: must include scheme (e.g., 'duckdb://...')",
            suggestion="Use format 'duckdb://path/table' or similar",
        )
    scheme = value.split("://")[0]
    if not scheme:
        return ValidationError(
            field=field,
            message="Invalid source URI: scheme must be non-empty",
            suggestion="Use format 'duckdb://path/table' or similar",
        )
    return None


def validate_metric_spec(spec_dict: dict) -> ValidationResult:
    """Validate a metric spec dict. Returns ValidationResult with all errors found."""
    errors: list[ValidationError] = []

    name = spec_dict.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        errors.append(
            ValidationError(
                field="name", message="'name' is required and must be a non-empty string"
            )
        )

    source = spec_dict.get("source")
    if not source or not isinstance(source, str) or not source.strip():
        errors.append(
            ValidationError(
                field="source", message="'source' is required and must be a non-empty string"
            )
        )
    else:
        uri_error = _validate_uri(source, "source")
        if uri_error:
            errors.append(uri_error)

    numerator = spec_dict.get("numerator")
    if not numerator or not isinstance(numerator, str) or not numerator.strip():
        errors.append(
            ValidationError(
                field="numerator", message="'numerator' must be a non-empty SQL expression"
            )
        )
    else:
        sql_error = _validate_sql_expression(numerator, "numerator", context="select")
        if sql_error:
            errors.append(sql_error)
        elif not _contains_aggregate_call(numerator):
            errors.append(
                ValidationError(
                    field="numerator",
                    message="'numerator' must contain an aggregate function (SUM, AVG, COUNT, MIN, MAX)",
                    suggestion="Wrap the column in an aggregate, e.g. 'SUM(revenue)' or 'COUNT(*)'",
                )
            )

    timestamp_col = spec_dict.get("timestamp_col")
    if not timestamp_col or not isinstance(timestamp_col, str) or not timestamp_col.strip():
        errors.append(
            ValidationError(
                field="timestamp_col",
                message="'timestamp_col' is required and must be a non-empty string",
                suggestion="Add the date/timestamp column name used for time filtering, e.g. 'timestamp_col: created_at'",
            )
        )

    denominator = spec_dict.get("denominator")
    if denominator:
        if not isinstance(denominator, str) or not denominator.strip():
            errors.append(
                ValidationError(
                    field="denominator", message="'denominator' must be a non-empty SQL expression"
                )
            )
        else:
            sql_error = _validate_sql_expression(denominator, "denominator", context="select")
            if sql_error:
                errors.append(sql_error)
            elif not _contains_aggregate_call(denominator):
                errors.append(
                    ValidationError(
                        field="denominator",
                        message="'denominator' must contain an aggregate function (SUM, AVG, COUNT, MIN, MAX)",
                        suggestion="Wrap the column in an aggregate, e.g. 'SUM(impressions)'",
                    )
                )

    entities = spec_dict.get("entities")
    if entities is not None:
        if not isinstance(entities, list) or len(entities) == 0:
            errors.append(
                ValidationError(
                    field="entities",
                    message="'entities' must be a non-empty list of column name strings",
                )
            )
        else:
            for i, entry in enumerate(entities):
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(
                        ValidationError(
                            field=f"entities[{i}]",
                            message=f"'entities' entry at index {i} must be a non-empty string",
                        )
                    )

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def _validate_values_list(values: list, item_type: str, errors: list[ValidationError]) -> None:
    """Validate a list of value dicts (for slice/segment). Appends errors in-place."""
    seen_names: set[str] = set()
    for i, value in enumerate(values):
        if not isinstance(value, dict):
            errors.append(
                ValidationError(
                    field=f"values[{i}]",
                    message=f"{item_type} value at index {i} must be a dict",
                )
            )
            continue

        item_name = value.get("name")
        if not item_name or not isinstance(item_name, str) or not item_name.strip():
            errors.append(
                ValidationError(
                    field=f"values[{i}].name",
                    message=f"{item_type} value at index {i} is missing required field 'name'",
                )
            )
        else:
            if item_name in seen_names:
                errors.append(
                    ValidationError(
                        field="values",
                        message=f"Duplicate {item_type.lower()} value name: '{item_name}'",
                    )
                )
            seen_names.add(item_name)

        where = value.get("where")
        if not where or not isinstance(where, str) or not where.strip():
            name_str = item_name if item_name else f"index {i}"
            errors.append(
                ValidationError(
                    field=f"values[{i}].where",
                    message=f"{item_type} value '{name_str}' is missing required field 'where'",
                )
            )
        else:
            sql_error = _validate_sql_expression(where, f"values[{i}].where", context="where")
            if sql_error:
                errors.append(sql_error)


def validate_slice_spec(spec_dict: dict) -> ValidationResult:
    """Validate a slice spec dict. Returns ValidationResult with all errors found.

    A SliceSpec is either a leaf spec (has 'values') or a composite spec (has
    'cross_product'). Exactly one must be present.
    """
    errors: list[ValidationError] = []

    name = spec_dict.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        errors.append(
            ValidationError(
                field="name", message="'name' is required and must be a non-empty string"
            )
        )

    values = spec_dict.get("values")
    cross_product = spec_dict.get("cross_product")

    if values is not None and cross_product is not None:
        errors.append(
            ValidationError(
                field="values",
                message="SliceSpec must have exactly one of 'values' or 'cross_product', not both",
            )
        )
    elif values is not None:
        # Leaf spec: validate values list
        if not isinstance(values, list) or len(values) == 0:
            errors.append(
                ValidationError(
                    field="values", message="'values' must contain at least one slice value"
                )
            )
        else:
            _validate_values_list(values, "Slice", errors)
    elif cross_product is not None:
        # Composite spec: validate cross_product list
        if not isinstance(cross_product, list) or len(cross_product) < 2:
            errors.append(
                ValidationError(
                    field="cross_product",
                    message="'cross_product' must be a list of at least 2 slice spec names",
                )
            )
        else:
            seen: set[str] = set()
            for item in cross_product:
                if not isinstance(item, str) or not item.strip():
                    errors.append(
                        ValidationError(
                            field="cross_product",
                            message="Each entry in 'cross_product' must be a non-empty string",
                        )
                    )
                elif item in seen:
                    errors.append(
                        ValidationError(
                            field="cross_product",
                            message=f"Duplicate name in 'cross_product': '{item}'",
                        )
                    )
                else:
                    seen.add(item)
    else:
        errors.append(
            ValidationError(
                field="values",
                message="SliceSpec must have exactly one of 'values' or 'cross_product'",
            )
        )

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def validate_segment_spec(spec_dict: dict) -> ValidationResult:
    """Validate a segment spec dict. Returns ValidationResult with all errors found."""
    errors: list[ValidationError] = []

    name = spec_dict.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        errors.append(
            ValidationError(
                field="name", message="'name' is required and must be a non-empty string"
            )
        )

    source = spec_dict.get("source")
    if not source or not isinstance(source, str) or not source.strip():
        errors.append(
            ValidationError(
                field="source", message="'source' is required and must be a non-empty string"
            )
        )
    else:
        uri_error = _validate_uri(source, "source")
        if uri_error:
            errors.append(uri_error)

    values = spec_dict.get("values")
    if values is None or not isinstance(values, list) or len(values) == 0:
        errors.append(
            ValidationError(
                field="values", message="'values' must contain at least one segment value"
            )
        )
    else:
        _validate_values_list(values, "Segment", errors)

    return ValidationResult(valid=len(errors) == 0, errors=errors)
