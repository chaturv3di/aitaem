from __future__ import annotations

import json

import pyarrow as pa
import pytest

from aitaem.agent.base import Bot


class _StubBot(Bot):
    def _build_agent(self):
        return None


# ---------------------------------------------------------------------------
# SF-8: dump_history / load_history
# ---------------------------------------------------------------------------


def test_dump_history_empty():
    bot = _StubBot(model="claude-sonnet-4-6")
    bundle = bot.dump_history()
    assert bundle["schema_version"] == "1.0"
    assert json.loads(bundle["messages"]) == []
    assert bundle["artifacts"] == {}


def test_dump_load_roundtrip_with_arrow_artifact():
    bot = _StubBot(model="claude-sonnet-4-6")
    table = pa.table({"metric_value": [1.0, 2.0], "metric_name": ["ctr", "ctr"]})
    rid = bot.store.store_tabular(table, None, metadata={"metric": "ctr"})

    bundle = bot.dump_history()
    assert rid in bundle["artifacts"]
    assert bundle["artifacts"][rid]["arrow_b64"] is not None

    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")
    entry = restored.get_result(rid)
    assert entry.arrow.equals(table)
    assert entry.ibis_ref is None
    assert entry.metadata["metric"] == "ctr"


def test_dump_load_roundtrip_null_arrow():
    bot = _StubBot(model="claude-sonnet-4-6")
    rid = bot.store.store_tabular(None, None)
    bundle = bot.dump_history()
    assert bundle["artifacts"][rid]["arrow_b64"] is None

    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")
    entry = restored.get_result(rid)
    assert entry.arrow is None


def test_load_history_wrong_schema_version():
    bundle = {"schema_version": "99.0", "messages": "[]", "artifacts": {}}
    with pytest.raises(ValueError, match="schema_version"):
        _StubBot.load_history(bundle, model="claude-sonnet-4-6")


def test_bundle_is_json_serializable():
    bot = _StubBot(model="claude-sonnet-4-6")
    table = pa.table({"x": [1, 2]})
    bot.store.store_tabular(table, None)
    bundle = bot.dump_history()
    _ = json.dumps(bundle)
