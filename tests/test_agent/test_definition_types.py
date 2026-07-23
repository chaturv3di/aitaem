"""Tests for SF-1: definition type models."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from aitaem.agent.definition_types import (
    DefinitionDeps,
    DefinitionIntent,
    DefinitionOutput,
    DefinitionPayload,
    DraftSpecResult,
    ListTablesResult,
    SpecDraft,
    ValidateSpecResult,
    ValidationIssue,
)
from aitaem.agent.store import ResultStore
from aitaem.agent.trace import Status


# ---------------------------------------------------------------------------
# DefinitionIntent
# ---------------------------------------------------------------------------


def test_definition_intent_defaults():
    intent = DefinitionIntent(spec_type="metric", description="Total revenue")
    assert not intent.is_update
    assert intent.existing_yaml is None
    assert intent.original_name is None


def test_definition_intent_with_update_fields():
    intent = DefinitionIntent(
        spec_type="metric",
        description="Updated revenue",
        existing_yaml="metric:\n  name: revenue",
        is_update=True,
        original_name="revenue",
    )
    assert intent.is_update
    assert intent.original_name == "revenue"
    assert intent.existing_yaml is not None


# ---------------------------------------------------------------------------
# SpecDraft
# ---------------------------------------------------------------------------


def test_spec_draft_stores_fields():
    draft = SpecDraft(draft_id="dd_abc123", spec_type="metric", yaml_string="metric:\n  name: x")
    assert draft.draft_id == "dd_abc123"
    assert draft.spec_type == "metric"
    assert draft.yaml_string == "metric:\n  name: x"


# ---------------------------------------------------------------------------
# DefinitionDeps
# ---------------------------------------------------------------------------


def test_definition_deps_default_empty_registry_and_none_intent():
    store = ResultStore()
    deps = DefinitionDeps(
        connection_manager=MagicMock(),
        spec_cache=MagicMock(),
        store=store,
    )
    assert deps.draft_registry == {}
    assert deps.definition_intent is None


def test_definition_deps_store_reference():
    store = ResultStore()
    deps = DefinitionDeps(
        connection_manager=MagicMock(),
        spec_cache=MagicMock(),
        store=store,
    )
    assert deps.store is store


# ---------------------------------------------------------------------------
# DefinitionOutput
# ---------------------------------------------------------------------------


def test_definition_output_is_frozen():
    output = DefinitionOutput(status=Status.ok, narrative="Done")
    with pytest.raises(ValidationError):
        output.narrative = "Changed"  # type: ignore[misc]


def test_definition_output_optional_fields_default_none():
    output = DefinitionOutput(status=Status.ok, narrative="Done")
    assert output.spec_draft_token is None
    assert output.reason is None


def test_definition_output_with_token():
    output = DefinitionOutput(
        status=Status.ok,
        narrative="Defined.",
        spec_draft_token="abc-123",
    )
    assert output.spec_draft_token == "abc-123"


# ---------------------------------------------------------------------------
# DefinitionPayload
# ---------------------------------------------------------------------------


def test_definition_payload_accepts_arbitrary_types():
    from unittest.mock import MagicMock

    mock_spec = MagicMock()
    payload = DefinitionPayload(
        spec_type="metric",
        spec_name="revenue",
        yaml_string="metric:\n  name: revenue",
        metric_spec=mock_spec,
    )
    assert payload.metric_spec is mock_spec
    assert payload.slice_spec is None
    assert payload.segment_spec is None


def test_definition_payload_empty_defaults():
    payload = DefinitionPayload()
    assert payload.spec_type is None
    assert payload.spec_name is None
    assert payload.yaml_string is None
    assert payload.validation_warnings == []
    assert payload.referenced_columns is None


# ---------------------------------------------------------------------------
# ValidateSpecResult
# ---------------------------------------------------------------------------


def test_validate_spec_result_with_errors_has_no_token():
    result = ValidateSpecResult(
        errors=[ValidationIssue(field="name", message="Name conflict")]
    )
    assert result.spec_draft_token is None
    assert len(result.errors) == 1


def test_validate_spec_result_on_success():
    result = ValidateSpecResult(
        result_id="tok-abc",
        errors=[],
        column_errors=[],
        warnings=[],
    )
    assert result.spec_draft_token == "tok-abc"
    assert result.errors == []
    assert result.column_errors == []


def test_validate_spec_result_token_derived_from_result_id():
    assert ValidateSpecResult(result_id="abc").spec_draft_token == "abc"


def test_validate_spec_result_none_result_id_yields_no_token():
    assert ValidateSpecResult(result_id=None).spec_draft_token is None


def test_validate_spec_result_empty_result_id_yields_no_token():
    assert ValidateSpecResult(result_id="").spec_draft_token is None


def test_validate_spec_result_serialization_exposes_token_only():
    dumped = ValidateSpecResult(result_id="abc").model_dump()
    assert dumped["spec_draft_token"] == "abc"
    assert "result_id" not in dumped


def test_validate_spec_result_rejects_legacy_constructor_kwarg():
    with pytest.raises(ValidationError) as exc_info:
        ValidateSpecResult(spec_draft_token="x")  # type: ignore[call-arg]
    assert "spec_draft_token" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ListTablesResult
# ---------------------------------------------------------------------------


def test_list_tables_result_both_fields_populated():
    result = ListTablesResult(
        tables={"duckdb": ["events", "users"]},
        errors={"bigquery": "Connection refused"},
    )
    assert "duckdb" in result.tables
    assert "bigquery" in result.errors


def test_list_tables_result_defaults():
    result = ListTablesResult()
    assert result.tables == {}
    assert result.errors == {}


# ---------------------------------------------------------------------------
# DraftSpecResult
# ---------------------------------------------------------------------------


def test_draft_spec_result_fields():
    result = DraftSpecResult(
        draft_id="dd_abc",
        spec_type="metric",
        yaml_preview="metric:\n  name: revenue...",
    )
    assert result.draft_id == "dd_abc"
    assert result.spec_type == "metric"
