from __future__ import annotations

import json
import warnings

import pyarrow as pa
import pytest
from pydantic_ai.toolsets import FunctionToolset

from aitaem.agent.base import Bot, _register_tool


class _StubBot(Bot):
    def _build_agent(self):
        toolset = FunctionToolset()
        for tool in self._tools:
            _register_tool(toolset, tool)
        self._toolset = toolset
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


# ---------------------------------------------------------------------------
# Plan 28 / SF-7: runtime_added_tool_names tracking + load_history() warning
# ---------------------------------------------------------------------------


def _probe_tool() -> str:
    return "probe"


def test_dump_history_records_runtime_added_tool_names():
    bot = _StubBot(model="claude-sonnet-4-6")
    bot.add_tool(_probe_tool)
    bundle = bot.dump_history()
    assert bundle["runtime_added_tool_names"] == ["_probe_tool"]


def test_dump_history_runtime_added_tool_names_empty_by_default():
    bot = _StubBot(model="claude-sonnet-4-6")
    bundle = bot.dump_history()
    assert bundle["runtime_added_tool_names"] == []


def test_load_history_warns_when_runtime_added_tool_missing():
    bot = _StubBot(model="claude-sonnet-4-6")
    bot.add_tool(_probe_tool)
    bundle = bot.dump_history()

    with pytest.warns(UserWarning, match="_probe_tool"):
        _StubBot.load_history(bundle, model="claude-sonnet-4-6")


def test_load_history_no_warning_when_tool_repassed():
    bot = _StubBot(model="claude-sonnet-4-6")
    bot.add_tool(_probe_tool)
    bundle = bot.dump_history()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        restored = _StubBot.load_history(
            bundle, model="claude-sonnet-4-6", tools=[_probe_tool]
        )
    assert "_probe_tool" in restored._toolset.tools


def test_load_history_backward_compat_missing_field_no_warning():
    bundle = {"schema_version": "1.0", "messages": "[]", "artifacts": {}}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _StubBot.load_history(bundle, model="claude-sonnet-4-6")


def test_make_bundle_default_runtime_added_tool_names_is_empty_list():
    from aitaem.agent.history import make_bundle
    from aitaem.agent.store import ResultStore

    bundle = make_bundle([], ResultStore())
    assert bundle["runtime_added_tool_names"] == []


def test_make_bundle_runtime_added_tool_names_round_trips_through_json():
    from aitaem.agent.history import make_bundle
    from aitaem.agent.store import ResultStore

    bundle = make_bundle([], ResultStore(), ["tool_a", "tool_b"])
    reloaded = json.loads(json.dumps(bundle))
    assert reloaded["runtime_added_tool_names"] == ["tool_a", "tool_b"]
