"""Tests for P3.0b ResultStore discriminated union (TabularEntry / TextEntry)."""

from __future__ import annotations

import json

import pyarrow as pa
import pytest

from aitaem.agent.store import (
    ResultStore,
    TabularEntry,
    TextEntry,
    WrongEntryKindError,
)
from aitaem.agent.base import Bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubBot(Bot):
    def _build_agent(self):
        return None


# ---------------------------------------------------------------------------
# TabularEntry via store_tabular()
# ---------------------------------------------------------------------------


def test_store_tabular_returns_retrievable_id():
    store = ResultStore()
    table = pa.table({"x": [1, 2]})
    rid = store.store_tabular(table, None)
    assert rid != ""
    entry = store.get_tabular(rid)
    assert isinstance(entry, TabularEntry)
    assert entry.arrow.equals(table)


def test_store_tabular_two_calls_different_ids():
    store = ResultStore()
    r1 = store.store_tabular(None, None)
    r2 = store.store_tabular(None, None)
    assert r1 != r2


def test_get_tabular_returns_tabular_entry():
    store = ResultStore()
    rid = store.store_tabular(pa.table({"v": [1]}), None)
    entry = store.get_tabular(rid)
    assert entry.kind == "tabular"
    assert isinstance(entry, TabularEntry)


def test_isinstance_tabular_entry():
    store = ResultStore()
    rid = store.store_tabular(None, None)
    entry = store.get(rid)
    assert isinstance(entry, TabularEntry)
    assert not isinstance(entry, TextEntry)


# ---------------------------------------------------------------------------
# TextEntry via store_text()
# ---------------------------------------------------------------------------


def test_store_text_returns_retrievable_id():
    store = ResultStore()
    yaml = "name: revenue\nsource: duckdb://..."
    rid = store.store_text(yaml, "application/yaml")
    assert rid != ""
    entry = store.get_text(rid)
    assert isinstance(entry, TextEntry)
    assert entry.text == yaml
    assert entry.content_type == "application/yaml"


def test_store_text_two_calls_different_ids():
    store = ResultStore()
    r1 = store.store_text("a", "text/plain")
    r2 = store.store_text("b", "text/plain")
    assert r1 != r2


def test_get_text_returns_text_entry():
    store = ResultStore()
    rid = store.store_text("hello", "text/plain")
    entry = store.get_text(rid)
    assert entry.kind == "text"
    assert isinstance(entry, TextEntry)


def test_isinstance_text_entry():
    store = ResultStore()
    rid = store.store_text("yaml", "application/yaml")
    entry = store.get(rid)
    assert isinstance(entry, TextEntry)
    assert not isinstance(entry, TabularEntry)


# ---------------------------------------------------------------------------
# WrongEntryKindError
# ---------------------------------------------------------------------------


def test_get_text_on_tabular_raises_wrong_kind():
    store = ResultStore()
    rid = store.store_tabular(None, None)
    with pytest.raises(WrongEntryKindError, match="tabular"):
        store.get_text(rid)


def test_get_tabular_on_text_raises_wrong_kind():
    store = ResultStore()
    rid = store.store_text("yaml", "application/yaml")
    with pytest.raises(WrongEntryKindError, match="text"):
        store.get_tabular(rid)


def test_get_arrow_on_text_raises_wrong_kind():
    store = ResultStore()
    rid = store.store_text("x", "text/plain")
    with pytest.raises(WrongEntryKindError):
        store.get_arrow(rid)


def test_get_ibis_on_text_raises_wrong_kind():
    store = ResultStore()
    rid = store.store_text("x", "text/plain")
    with pytest.raises(WrongEntryKindError):
        store.get_ibis(rid)


# ---------------------------------------------------------------------------
# invalidate_all_ibis_refs only touches TabularEntry
# ---------------------------------------------------------------------------


def test_invalidate_all_ibis_refs_only_touches_tabular():
    store = ResultStore()
    mock_ref = object()
    tab_rid = store.store_tabular(None, mock_ref)
    txt_rid = store.store_text("yaml", "application/yaml")

    store.invalidate_all_ibis_refs()

    assert store.get_tabular(tab_rid).ibis_ref is None
    # TextEntry is untouched
    assert store.get_text(txt_rid).text == "yaml"


# ---------------------------------------------------------------------------
# metadata preserved
# ---------------------------------------------------------------------------


def test_store_text_preserves_metadata():
    store = ResultStore()
    rid = store.store_text("yaml", "application/yaml", metadata={"spec_type": "metric", "spec_name": "revenue"})
    entry = store.get_text(rid)
    assert entry.metadata["spec_type"] == "metric"
    assert entry.metadata["spec_name"] == "revenue"


# ---------------------------------------------------------------------------
# dump_history / load_history round-trip for both kinds
# ---------------------------------------------------------------------------


def test_roundtrip_tabular_entry_via_dump_load():
    bot = _StubBot(model="claude-sonnet-4-6")
    table = pa.table({"metric_value": [1.0, 2.0], "metric_name": ["ctr", "ctr"]})
    rid = bot.store.store_tabular(table, None, metadata={"metric": "ctr"})

    bundle = bot.dump_history()
    assert bundle["artifacts"][rid]["kind"] == "tabular"
    assert bundle["artifacts"][rid]["arrow_b64"] is not None

    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")
    entry = restored.get_result(rid)
    assert isinstance(entry, TabularEntry)
    assert entry.arrow.equals(table)
    assert entry.ibis_ref is None
    assert entry.metadata["metric"] == "ctr"


def test_roundtrip_text_entry_via_dump_load():
    bot = _StubBot(model="claude-sonnet-4-6")
    yaml = "name: revenue\nsource: duckdb://db/sales"
    rid = bot.store.store_text(yaml, "application/yaml", metadata={"spec_type": "metric"})

    bundle = bot.dump_history()
    assert bundle["artifacts"][rid]["kind"] == "text"
    assert bundle["artifacts"][rid]["text"] == yaml
    assert bundle["artifacts"][rid]["content_type"] == "application/yaml"

    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")
    entry = restored.get_result(rid)
    assert isinstance(entry, TextEntry)
    assert entry.text == yaml
    assert entry.content_type == "application/yaml"
    assert entry.metadata["spec_type"] == "metric"


def test_roundtrip_mixed_store():
    """Both TabularEntry and TextEntry survive a dump/load cycle in the same store."""
    bot = _StubBot(model="claude-sonnet-4-6")
    table = pa.table({"x": [1]})
    tab_rid = bot.store.store_tabular(table, None)
    txt_rid = bot.store.store_text("yaml", "application/yaml")

    bundle = bot.dump_history()
    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")

    tab = restored.get_result(tab_rid)
    txt = restored.get_result(txt_rid)
    assert isinstance(tab, TabularEntry)
    assert isinstance(txt, TextEntry)
    assert tab.arrow.equals(table)
    assert txt.text == "yaml"


def test_dump_history_is_json_serializable_with_text_entry():
    bot = _StubBot(model="claude-sonnet-4-6")
    bot.store.store_text("some yaml", "application/yaml")
    bundle = bot.dump_history()
    _ = json.dumps(bundle)
