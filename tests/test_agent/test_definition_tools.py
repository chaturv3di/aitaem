"""Tests for SF-2 through SF-6: definition tools."""

from __future__ import annotations

from unittest.mock import MagicMock

from aitaem.agent.definition_types import (
    DefinitionDeps,
    DefinitionIntent,
)
from aitaem.agent.definition_tools import (
    describe_table,
    draft_spec,
    list_tables,
    record_definition_intent,
    validate_spec,
)
from aitaem.agent.store import ResultStore, TextEntry
from aitaem.utils.exceptions import AitaemConnectionError, ConnectionNotFoundError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_METRIC_YAML = """\
metric:
  name: revenue
  source: duckdb://analytics.db/transactions
  numerator: "SUM(amount)"
  timestamp_col: transaction_date
"""

_VALID_SLICE_YAML = """\
slice:
  name: by_country
  values:
    - name: US
      where: "country = 'US'"
    - name: EU
      where: "country IN ('DE', 'FR')"
"""

_VALID_COMPOSITE_SLICE_YAML = """\
slice:
  name: by_country_and_device
  cross_product:
    - by_country
    - by_device
"""

_VALID_SEGMENT_YAML = """\
segment:
  name: customer_tier
  source: duckdb://analytics.db/customers
  entity_id: customer_id
  values:
    - name: premium
      where: "tier = 'premium'"
"""

_INVALID_METRIC_YAML = """\
metric:
  name: broken
  source: duckdb://db/t
  numerator: "amount"
  timestamp_col: ts
"""


def _make_store():
    return ResultStore()


def _make_spec_cache(**overrides):
    sc = MagicMock()
    sc.metrics = overrides.get("metrics", {})
    sc.slices = overrides.get("slices", {})
    sc.segments = overrides.get("segments", {})
    return sc


def _make_deps(
    store=None,
    spec_cache=None,
    connection_manager=None,
    intent=None,
):
    deps = DefinitionDeps(
        connection_manager=connection_manager or MagicMock(),
        spec_cache=spec_cache or _make_spec_cache(),
        store=store or _make_store(),
    )
    if intent is not None:
        deps.definition_intent = intent
    return deps


def _make_ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


# ---------------------------------------------------------------------------
# SF-2: record_definition_intent
# ---------------------------------------------------------------------------


def test_record_intent_stores_intent_on_deps():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    record_definition_intent(ctx, spec_type="metric", description="Total revenue")

    assert deps.definition_intent is not None
    assert deps.definition_intent.spec_type == "metric"
    assert deps.definition_intent.description == "Total revenue"


def test_record_intent_is_update_false_without_existing_yaml():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = record_definition_intent(ctx, spec_type="metric", description="x")

    assert result.has_existing_yaml is False
    assert deps.definition_intent.is_update is False
    assert deps.definition_intent.original_name is None


def test_record_intent_is_update_true_with_valid_existing_yaml():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = record_definition_intent(
        ctx, spec_type="metric", description="Update", existing_yaml=_VALID_METRIC_YAML
    )

    assert result.has_existing_yaml is True
    assert result.existing_yaml_parse_warning is None
    assert deps.definition_intent.is_update is True
    assert deps.definition_intent.original_name == "revenue"


def test_record_intent_malformed_existing_yaml_sets_warning():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = record_definition_intent(
        ctx, spec_type="metric", description="x", existing_yaml="not: valid: yaml: content: !!"
    )

    assert result.has_existing_yaml is True
    assert result.existing_yaml_parse_warning is not None
    assert deps.definition_intent.is_update is False
    assert deps.definition_intent.original_name is None


def test_record_intent_second_call_overwrites_first():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    record_definition_intent(ctx, spec_type="metric", description="First")
    record_definition_intent(ctx, spec_type="slice", description="Second")

    assert deps.definition_intent.spec_type == "slice"
    assert deps.definition_intent.description == "Second"


def test_record_intent_returns_correct_spec_type():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = record_definition_intent(ctx, spec_type="segment", description="Segments")

    assert result.spec_type == "segment"


# ---------------------------------------------------------------------------
# SF-3: list_tables
# ---------------------------------------------------------------------------


def test_list_tables_single_backend_success():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb"]
    mock_connector = MagicMock()
    mock_connector.list_tables.return_value = ["events", "users"]
    mock_cm.get_connection.return_value = mock_connector

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = list_tables(ctx)

    assert "duckdb" in result.tables
    assert result.tables["duckdb"] == ["events", "users"]
    assert result.errors == {}


def test_list_tables_all_backends_succeed():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb", "bigquery"]

    def get_conn(bt):
        conn = MagicMock()
        conn.list_tables.return_value = [f"{bt}_table"]
        return conn

    mock_cm.get_connection.side_effect = get_conn

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = list_tables(ctx)

    assert set(result.tables.keys()) == {"duckdb", "bigquery"}
    assert result.errors == {}


def test_list_tables_one_backend_fails():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb", "bigquery"]

    def get_conn(bt):
        if bt == "bigquery":
            raise AitaemConnectionError("BQ auth failed")
        conn = MagicMock()
        conn.list_tables.return_value = ["events"]
        return conn

    mock_cm.get_connection.side_effect = get_conn

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = list_tables(ctx)

    assert "duckdb" in result.tables
    assert "bigquery" in result.errors
    assert "BQ auth failed" in result.errors["bigquery"]


def test_list_tables_all_backends_fail():
    mock_cm = MagicMock()
    mock_cm.backend_types = ["duckdb", "bigquery"]
    mock_cm.get_connection.side_effect = ConnectionNotFoundError("not found")

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = list_tables(ctx)

    assert result.tables == {}
    assert "duckdb" in result.errors
    assert "bigquery" in result.errors


def test_list_tables_single_backend_specified_fails():
    mock_cm = MagicMock()
    mock_cm.get_connection.side_effect = ConnectionNotFoundError("not found")

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = list_tables(ctx, backend_type="duckdb")

    assert result.tables == {}
    assert "duckdb" in result.errors


# ---------------------------------------------------------------------------
# SF-4: describe_table
# ---------------------------------------------------------------------------


def _make_ibis_table_mock(columns):
    """columns: list of (name, dtype_str)."""
    ibis_table = MagicMock()
    schema = MagicMock()
    schema.names = [c[0] for c in columns]
    schema.types = [c[1] for c in columns]
    ibis_table.schema.return_value = schema
    ibis_table.columns = [c[0] for c in columns]
    return ibis_table


def test_describe_table_returns_column_info():
    mock_connector = MagicMock()
    mock_connector.get_table.return_value = _make_ibis_table_mock(
        [("user_id", "int64"), ("event_ts", "timestamp"), ("amount", "float64")]
    )
    mock_cm = MagicMock()
    mock_cm.get_connection.return_value = mock_connector

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = describe_table(ctx, table_name="events", backend_type="duckdb")

    assert result.error is None
    assert result.table_name == "events"
    assert result.backend_type == "duckdb"
    col_names = [c.name for c in result.columns]
    assert "user_id" in col_names
    assert "event_ts" in col_names
    assert "amount" in col_names


def test_describe_table_table_not_found():
    from aitaem.utils.exceptions import TableNotFoundError

    mock_connector = MagicMock()
    mock_connector.get_table.side_effect = TableNotFoundError("Table 'foo' not found")
    mock_cm = MagicMock()
    mock_cm.get_connection.return_value = mock_connector

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = describe_table(ctx, table_name="foo", backend_type="duckdb")

    assert result.error is not None
    assert result.columns == []


def test_describe_table_unknown_backend():
    mock_cm = MagicMock()
    mock_cm.get_connection.side_effect = ConnectionNotFoundError("No backend 'xyz'")

    deps = _make_deps(connection_manager=mock_cm)
    ctx = _make_ctx(deps)

    result = describe_table(ctx, table_name="t", backend_type="xyz")

    assert result.error is not None
    assert "xyz" in result.error
    assert result.columns == []


# ---------------------------------------------------------------------------
# SF-5: draft_spec
# ---------------------------------------------------------------------------


def test_draft_spec_stores_in_registry():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = draft_spec(ctx, spec_type="metric", yaml_string=_VALID_METRIC_YAML)

    assert result.draft_id in deps.draft_registry
    stored = deps.draft_registry[result.draft_id]
    assert stored.yaml_string == _VALID_METRIC_YAML
    assert stored.spec_type == "metric"


def test_draft_spec_two_calls_different_ids():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    r1 = draft_spec(ctx, spec_type="metric", yaml_string="a")
    r2 = draft_spec(ctx, spec_type="metric", yaml_string="b")

    assert r1.draft_id != r2.draft_id
    assert len(deps.draft_registry) == 2


def test_draft_spec_yaml_preview_truncated():
    long_yaml = "metric:\n  name: x\n" + "  description: " + "a" * 900
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = draft_spec(ctx, spec_type="metric", yaml_string=long_yaml)

    assert len(result.yaml_preview) == 800
    assert result.yaml_preview == long_yaml[:800]


def test_draft_spec_invalid_yaml_stored_without_error():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = draft_spec(ctx, spec_type="metric", yaml_string="this: is: not: valid: yaml!!")

    # draft_spec performs no validation — even invalid YAML is accepted
    assert result.draft_id in deps.draft_registry


# ---------------------------------------------------------------------------
# SF-6: validate_spec
# ---------------------------------------------------------------------------


def _store_draft(deps, spec_type, yaml_string):
    """Helper: store a draft and return its draft_id."""
    ctx = MagicMock()
    ctx.deps = deps
    result = draft_spec(ctx, spec_type=spec_type, yaml_string=yaml_string)
    return result.draft_id


def test_validate_spec_unknown_draft_id_returns_error():
    deps = _make_deps()
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id="dd_does_not_exist")

    assert result.error is not None
    assert result.spec_draft_token is None


def test_validate_spec_invalid_yaml_returns_errors():
    deps = _make_deps()
    draft_id = _store_draft(deps, "metric", "metric:\n  name: broken\n  source: duckdb://db/t\n  numerator: amount\n  timestamp_col: ts\n")
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    # numerator lacks aggregate → structural error
    assert result.spec_draft_token is None
    assert len(result.errors) > 0


def test_validate_spec_valid_yaml_mints_token():
    deps = _make_deps()
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    assert result.spec_draft_token is not None
    assert result.errors == []
    entry = deps.store.get_text(result.spec_draft_token)
    assert isinstance(entry, TextEntry)
    assert "revenue" in entry.text


def test_validate_spec_token_stored_as_text_entry():
    deps = _make_deps()
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    entry = deps.store.get_text(result.spec_draft_token)
    assert entry.content_type == "application/yaml"
    assert entry.metadata["spec_type"] == "metric"
    assert entry.metadata["spec_name"] == "revenue"


def test_validate_spec_name_conflict_returns_error():
    sc = _make_spec_cache(metrics={"revenue": MagicMock()})
    deps = _make_deps(spec_cache=sc)
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    assert result.spec_draft_token is None
    assert any("already exists" in e.message for e in result.errors)


def test_validate_spec_no_conflict_when_is_update_and_name_matches():
    sc = _make_spec_cache(metrics={"revenue": MagicMock()})
    deps = _make_deps(spec_cache=sc)
    deps.definition_intent = DefinitionIntent(
        spec_type="metric",
        description="Update revenue",
        is_update=True,
        original_name="revenue",
    )
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    # No name-lock error and no conflict error
    assert all("already exists" not in e.message for e in result.errors)
    assert all("cannot be changed" not in e.message for e in result.errors)
    assert result.spec_draft_token is not None


def test_validate_spec_name_lock_fires_when_is_update_and_name_changed():
    sc = _make_spec_cache(metrics={"orders": MagicMock()})
    deps = _make_deps(spec_cache=sc)
    # existing_yaml had name=revenue but draft has name=orders (conflict + rename)
    deps.definition_intent = DefinitionIntent(
        spec_type="metric",
        description="Update",
        is_update=True,
        original_name="revenue",  # locked to "revenue"
    )
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)  # YAML has name=revenue
    # Change YAML to use name=orders
    changed_yaml = _VALID_METRIC_YAML.replace("name: revenue", "name: orders")
    deps.draft_registry[draft_id].yaml_string = changed_yaml
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    assert result.spec_draft_token is None
    assert any("cannot be changed" in e.message for e in result.errors)


def test_validate_spec_composite_slice_missing_cross_ref():
    sc = _make_spec_cache(slices={"by_country": MagicMock()})
    # by_device is missing
    deps = _make_deps(spec_cache=sc)
    draft_id = _store_draft(deps, "slice", _VALID_COMPOSITE_SLICE_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    assert result.spec_draft_token is None
    assert any("cross_product" in e.field for e in result.errors)
    assert any("by_device" in e.message for e in result.errors)


def test_validate_spec_composite_slice_all_refs_present():
    sc = _make_spec_cache(slices={"by_country": MagicMock(), "by_device": MagicMock()})
    deps = _make_deps(spec_cache=sc)
    draft_id = _store_draft(deps, "slice", _VALID_COMPOSITE_SLICE_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    # No cross-ref errors (column check may produce warning but not block)
    assert not any("cross_product" in e.field for e in result.errors)
    assert result.spec_draft_token is not None


def test_validate_spec_column_not_in_schema_populates_column_errors():
    mock_connector = MagicMock()
    mock_ibis = MagicMock()
    # Only 'ts' is present; 'amount' (used in numerator) is absent
    mock_ibis.columns = ["ts", "id"]
    mock_connector.get_table.return_value = mock_ibis
    mock_cm = MagicMock()
    mock_cm.get_connection_for_source.return_value = mock_connector
    mock_cm.parse_source_uri.return_value = ("duckdb", "analytics.db", "transactions")

    deps = _make_deps(connection_manager=mock_cm)
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    assert len(result.column_errors) > 0
    assert result.spec_draft_token is None


def test_validate_spec_connection_failure_during_column_check_adds_warning():
    mock_cm = MagicMock()
    mock_cm.get_connection_for_source.side_effect = Exception("Connection failed")
    mock_cm.parse_source_uri.return_value = ("duckdb", "analytics.db", "transactions")

    deps = _make_deps(connection_manager=mock_cm)
    draft_id = _store_draft(deps, "metric", _VALID_METRIC_YAML)
    ctx = _make_ctx(deps)

    result = validate_spec(ctx, draft_id=draft_id)

    # Token still minted; connection failure is a warning, not a blocker
    assert result.spec_draft_token is not None
    assert len(result.warnings) > 0
    assert result.column_errors == []
