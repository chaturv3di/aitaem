"""Smoke tests for aitaem's top-level public API.

Verifies that all symbols documented in __all__ are importable from `aitaem`
and that re-exported objects are identical to their source definitions.
"""

import aitaem
import aitaem.utils.exceptions as _exc
from aitaem.query.builder import PeriodType as _PeriodType
from aitaem.query.builder import VALID_PERIOD_TYPES as _VALID_PERIOD_TYPES


class TestExceptionExports:
    def test_all_exceptions_in_all(self):
        expected = {
            "AitaemError",
            "AitaemConnectionError",
            "ConnectionNotFoundError",
            "TableNotFoundError",
            "ConfigurationError",
            "InvalidURIError",
            "UnsupportedBackendError",
            "QueryBuildError",
            "QueryExecutionError",
            "SpecValidationError",
            "SpecNotFoundError",
        }
        assert expected.issubset(set(aitaem.__all__))

    def test_exception_objects_are_same_as_source(self):
        assert aitaem.AitaemError is _exc.AitaemError
        assert aitaem.AitaemConnectionError is _exc.AitaemConnectionError
        assert aitaem.ConnectionNotFoundError is _exc.ConnectionNotFoundError
        assert aitaem.TableNotFoundError is _exc.TableNotFoundError
        assert aitaem.ConfigurationError is _exc.ConfigurationError
        assert aitaem.InvalidURIError is _exc.InvalidURIError
        assert aitaem.UnsupportedBackendError is _exc.UnsupportedBackendError
        assert aitaem.QueryBuildError is _exc.QueryBuildError
        assert aitaem.QueryExecutionError is _exc.QueryExecutionError
        assert aitaem.SpecValidationError is _exc.SpecValidationError
        assert aitaem.SpecNotFoundError is _exc.SpecNotFoundError

    def test_aitaem_connection_error_does_not_shadow_builtin(self):
        import builtins
        assert aitaem.AitaemConnectionError is not builtins.ConnectionError

    def test_exceptions_are_aitaem_error_subclasses(self):
        for name in [
            "AitaemConnectionError", "ConnectionNotFoundError", "TableNotFoundError",
            "ConfigurationError", "InvalidURIError", "UnsupportedBackendError",
            "QueryBuildError", "QueryExecutionError", "SpecValidationError", "SpecNotFoundError",
        ]:
            cls = getattr(aitaem, name)
            assert issubclass(cls, aitaem.AitaemError), f"{name} is not a subclass of AitaemError"


class TestPeriodTypeExports:
    def test_valid_period_types_in_all(self):
        assert "VALID_PERIOD_TYPES" in aitaem.__all__
        assert "PeriodType" in aitaem.__all__

    def test_valid_period_types_is_same_object(self):
        assert aitaem.VALID_PERIOD_TYPES is _VALID_PERIOD_TYPES

    def test_period_type_is_same_object(self):
        assert aitaem.PeriodType is _PeriodType

    def test_valid_period_types_contains_expected_values(self):
        assert aitaem.VALID_PERIOD_TYPES == frozenset(
            {"all_time", "daily", "weekly", "monthly", "yearly"}
        )

    def test_valid_period_types_derived_from_literal(self):
        from typing import get_args
        assert aitaem.VALID_PERIOD_TYPES == frozenset(get_args(aitaem.PeriodType))
