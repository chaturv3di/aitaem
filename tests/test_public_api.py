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


class TestIbisConnectorExport:
    def test_ibis_connector_in_all(self):
        assert "IbisConnector" in aitaem.__all__

    def test_ibis_connector_same_object(self):
        from aitaem.connectors.ibis_connector import IbisConnector as _IbisConnector
        assert aitaem.IbisConnector is _IbisConnector

    def test_ibis_connector_is_class(self):
        import inspect
        assert inspect.isclass(aitaem.IbisConnector)


class TestStandardColumnsExport:
    def test_standard_columns_in_all(self):
        assert "STANDARD_COLUMNS" in aitaem.__all__

    def test_standard_columns_same_object(self):
        from aitaem.utils.formatting import STANDARD_COLUMNS as _SC
        assert aitaem.STANDARD_COLUMNS is _SC

    def test_standard_columns_is_list_of_strings(self):
        assert isinstance(aitaem.STANDARD_COLUMNS, list)
        assert all(isinstance(c, str) for c in aitaem.STANDARD_COLUMNS)

    def test_standard_columns_contains_expected_columns(self):
        expected = {
            "period_type", "period_start_date", "period_end_date",
            "entity_id", "metric_name", "slice_type", "slice_value",
            "segment_name", "segment_value", "metric_value",
        }
        assert expected == set(aitaem.STANDARD_COLUMNS)


class TestSpecTypeExports:
    def test_spec_types_in_all(self):
        for name in ["MetricSpec", "SliceSpec", "SliceValue", "SegmentSpec", "SegmentValue"]:
            assert name in aitaem.__all__

    def test_spec_types_are_same_objects(self):
        from aitaem.specs.metric import MetricSpec as _MS
        from aitaem.specs.slice import SliceSpec as _SS, SliceValue as _SV
        from aitaem.specs.segment import SegmentSpec as _SeS, SegmentValue as _SeV
        assert aitaem.MetricSpec is _MS
        assert aitaem.SliceSpec is _SS
        assert aitaem.SliceValue is _SV
        assert aitaem.SegmentSpec is _SeS
        assert aitaem.SegmentValue is _SeV

    def test_spec_types_are_classes(self):
        import inspect
        for cls in [aitaem.MetricSpec, aitaem.SliceSpec, aitaem.SliceValue,
                    aitaem.SegmentSpec, aitaem.SegmentValue]:
            assert inspect.isclass(cls)
