"""
aitaem.utils.validation - Spec validation utilities

Two-tier validation:
1. Structural: required fields, enum values, URI format, non-empty strings, list constraints
2. SQL syntax: numerator, denominator, where clauses via sqlglot
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SPEC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

METRIC_FORMAT_VALUES: frozenset[str] = frozenset({"percentage", "absolute", "ratio", "currency"})
_FORMAT_CURRENCY_RE = re.compile(r"^currency:[A-Z]{3}$")


def _is_valid_metric_format(value: str) -> bool:
    return value in METRIC_FORMAT_VALUES or bool(_FORMAT_CURRENCY_RE.match(value))


def _is_valid_spec_name(name: str) -> bool:
    """Return True if name is a valid SQL identifier (letters, digits, underscores; no leading digit)."""
    return bool(_SPEC_NAME_RE.match(name))


@dataclass
class ValidationError:
    field: str
    message: str
    suggestion: str | None = None


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError]
    referenced_columns: dict[str, list[str]] | None = None
    """Maps each spec field to the unqualified column names it references.

    Populated only when ``valid is True``; ``None`` when the spec is invalid.
    Always check ``result.valid`` before using this field.

    Keys for metric specs: ``"numerator"``, ``"denominator"`` (if set),
    ``"timestamp_col"``, ``"entities"`` (if set).

    Keys for slice leaf specs: ``"values[i].where"`` (one per value).
    Keys for wildcard slice specs: ``"where"``.
    Keys for composite slice specs: empty dict ``{}``.
    """


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


def _extract_columns_from_sql(expr: str, context: str = "select") -> list[str]:
    """Extract unqualified column names from a SQL expression via sqlglot AST.

    Returns an empty list if sqlglot is not installed or the expression cannot be parsed.
    Column names are deduplicated while preserving order of first appearance.
    The table qualifier is stripped — ``t.revenue`` yields ``"revenue"``.
    """
    try:
        import sqlglot
        import sqlglot.expressions as exp
    except ImportError:
        return []

    try:
        if context == "select":
            tree = sqlglot.parse_one(f"SELECT {expr}")
        else:
            tree = sqlglot.parse_one(f"SELECT 1 WHERE {expr}")
    except Exception:
        return []

    seen: dict[str, None] = {}
    for node in tree.walk():
        if isinstance(node, exp.Column):
            seen[node.name] = None
    return list(seen)


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
    elif not _is_valid_spec_name(name.strip()):
        errors.append(
            ValidationError(
                field="name",
                message=f"name '{name}' is not a valid SQL identifier "
                        f"(must match ^[A-Za-z_][A-Za-z0-9_]*$)",
                suggestion=f"rename to '{re.sub(r'[^A-Za-z0-9_]', '_', name.strip())}'",
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

    fmt = spec_dict.get("format")
    if fmt is not None:
        if not isinstance(fmt, str) or not _is_valid_metric_format(fmt):
            errors.append(
                ValidationError(
                    field="format",
                    message=(
                        f"Invalid format '{fmt}'. Must be one of "
                        f"{sorted(METRIC_FORMAT_VALUES)} or 'currency:<CODE>' where CODE is "
                        "a 3-letter uppercase ISO 4217 currency code (e.g. 'currency:USD')."
                    ),
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

    referenced_columns: dict[str, list[str]] | None = None
    if not errors:
        col_map: dict[str, list[str]] = {}
        col_map["numerator"] = _extract_columns_from_sql(numerator, context="select")
        if denominator:
            col_map["denominator"] = _extract_columns_from_sql(denominator, context="select")
        col_map["timestamp_col"] = [timestamp_col.strip()]
        if entities:
            col_map["entities"] = [e.strip() for e in entities if isinstance(e, str) and e.strip()]
        referenced_columns = col_map

    return ValidationResult(valid=len(errors) == 0, errors=errors, referenced_columns=referenced_columns)


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


def _is_valid_column_identifier(value: str) -> bool:
    """Return True if value is a simple or dot-qualified SQL column identifier.

    Accepts: ``industry``, ``user_id``, ``public.orders.country``
    Rejects: SQL expressions containing spaces, operators, quotes, or parentheses.
    """
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", value))


def validate_slice_spec(spec_dict: dict) -> ValidationResult:
    """Validate a slice spec dict. Returns ValidationResult with all errors found.

    A SliceSpec is one of:
    - Leaf spec: has 'values'
    - Composite spec: has 'cross_product'
    - Wildcard spec: has 'where' (bare column name)

    Exactly one of the three must be present.
    """
    errors: list[ValidationError] = []

    name = spec_dict.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        errors.append(
            ValidationError(
                field="name", message="'name' is required and must be a non-empty string"
            )
        )
    elif not _is_valid_spec_name(name.strip()):
        errors.append(
            ValidationError(
                field="name",
                message=f"name '{name}' is not a valid SQL identifier "
                        f"(must match ^[A-Za-z_][A-Za-z0-9_]*$)",
                suggestion=f"rename to '{re.sub(r'[^A-Za-z0-9_]', '_', name.strip())}'",
            )
        )

    values = spec_dict.get("values")
    cross_product = spec_dict.get("cross_product")
    where = spec_dict.get("where")

    present = [
        k
        for k, v in [("values", values), ("cross_product", cross_product), ("where", where)]
        if v is not None
    ]

    if len(present) > 1:
        errors.append(
            ValidationError(
                field=present[0],
                message=(
                    f"SliceSpec must have exactly one of 'values', 'cross_product', or 'where', "
                    f"but got: {present}"
                ),
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
    elif where is not None:
        # Wildcard spec: validate bare column identifier
        if not isinstance(where, str) or not where.strip():
            errors.append(
                ValidationError(
                    field="where",
                    message="'where' must be a non-empty column name string",
                )
            )
        elif not _is_valid_column_identifier(where.strip()):
            errors.append(
                ValidationError(
                    field="where",
                    message=(
                        f"'where' must be a plain column identifier (e.g. 'industry' or "
                        f"'schema.table.col'), not a SQL expression; got: '{where}'"
                    ),
                    suggestion="Use 'where: column_name' for wildcard slices",
                )
            )
    else:
        errors.append(
            ValidationError(
                field="values",
                message="SliceSpec must have exactly one of 'values', 'cross_product', or 'where'",
            )
        )

    referenced_columns: dict[str, list[str]] | None = None
    if not errors:
        col_map: dict[str, list[str]] = {}
        if values is not None and isinstance(values, list):
            for i, value in enumerate(values):
                if isinstance(value, dict):
                    where_expr = value.get("where", "")
                    if where_expr and isinstance(where_expr, str):
                        col_map[f"values[{i}].where"] = _extract_columns_from_sql(
                            where_expr, context="where"
                        )
        elif where is not None and isinstance(where, str) and where.strip():
            col_map["where"] = [where.strip()]
        # composite: cross_product holds slice names, not SQL; col_map stays empty
        referenced_columns = col_map

    return ValidationResult(valid=len(errors) == 0, errors=errors, referenced_columns=referenced_columns)


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
    elif not _is_valid_spec_name(name.strip()):
        errors.append(
            ValidationError(
                field="name",
                message=f"name '{name}' is not a valid SQL identifier "
                        f"(must match ^[A-Za-z_][A-Za-z0-9_]*$)",
                suggestion=f"rename to '{re.sub(r'[^A-Za-z0-9_]', '_', name.strip())}'",
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
