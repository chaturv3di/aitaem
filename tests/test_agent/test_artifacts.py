from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import ibis
import pyarrow as pa
import pytest

from aitaem.agent.artifacts import ArtifactStore, ProducedBy
from aitaem.agent.trace import Status

BY = ProducedBy(tool="test", call_id="c1", turn=1)


def _put(store: ArtifactStore, kind: str, payload: object, **kw: object):
    return store.put(kind, payload, type_tag="t", summary="s", produced_by=BY, **kw)  # type: ignore[arg-type]


def test_session_layout_under_temp_dir() -> None:
    store = ArtifactStore()
    assert store.session_dir.name.startswith("aitaem-session-")
    for sub in ("objects", "files", "specs"):
        assert (store.session_dir / sub).is_dir()
    assert store.manifest_path.exists()
    shutil.rmtree(store.session_dir)


def test_round_trip_per_kind(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    obj = _put(store, "object", {"a": [1, 2]})
    file = _put(store, "file", "outcome: complete\n")
    table = _put(store, "table", pa.table({"x": [1, 2, 3]}))
    expr = ibis.memtable({"y": [4, 5]})
    lazy = _put(store, "ibis", expr)

    assert store.read(obj.id) == {"a": [1, 2]}
    assert json.loads(Path(obj.location["path"]).read_text()) == {"a": [1, 2]}
    assert store.read(file.id) == "outcome: complete\n"
    assert file.location["path"].endswith(f"files/{file.id}.yaml")
    assert store.read(table.id).to_pyarrow()["x"].to_pylist() == [1, 2, 3]
    assert table.location["table"] == f"t_{table.id}"
    assert store.read(lazy.id) is expr


def test_unknown_id_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="a999"):
        ArtifactStore(tmp_path).get("a999")


def test_ids_unique_and_ordered_from_threads(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ids: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        for _ in range(5):
            ref = _put(store, "object", {"i": i})
            with lock:
                ids.append(ref.id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(ids) == [f"a{n:03d}" for n in range(1, 41)]
    lines = store.manifest_path.read_text().splitlines()
    assert sorted(json.loads(line)["id"] for line in lines) == sorted(ids)
    assert [r.id for r in store.refs()] == sorted(ids)


def test_concurrent_table_writes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            _put(store, "table", pa.table({"x": list(range(i + 1))}))
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    sizes = sorted(store.read(r.id).count().execute() for r in store.refs())
    assert sizes == list(range(1, 9))


def test_load_marks_missing_payloads_and_ibis_lost(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    kept = _put(store, "object", {"k": 1})
    gone = _put(store, "file", "x")
    table = _put(store, "table", pa.table({"x": [1]}))
    lazy = _put(store, "ibis", ibis.memtable({"y": [1]}))
    Path(gone.location["path"]).unlink()

    loaded = ArtifactStore.load(tmp_path)

    assert not loaded.get(kept.id).lost
    assert loaded.read(kept.id) == {"k": 1}
    assert loaded.get(gone.id).lost
    assert not loaded.get(table.id).lost
    assert loaded.get(lazy.id).lost
    with pytest.raises(LookupError, match="lost"):
        loaded.read(lazy.id)
    assert _put(loaded, "object", {}).id == "a005"


def test_load_removed_directory_from_records(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "s")
    _put(store, "object", {"k": 1})
    _put(store, "table", pa.table({"x": [1]}))
    records = store.manifest_records()
    shutil.rmtree(tmp_path / "s")

    loaded = ArtifactStore.load(tmp_path / "s", records)

    assert [r.lost for r in loaded.refs()] == [True, True]


def test_index_lines(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = _put(store, "object", {})
    store.put(
        "ibis",
        ibis.memtable({"y": [1]}),
        type_tag="metric_result",
        summary="DAU by country",
        produced_by=ProducedBy(tool="compute_metrics"),
        parents=[first.id],
        status=Status.ok,
    )

    lines = store.index_lines(expanded=1)

    assert lines[0] == 'a001 object t ok "s"'
    assert lines[1] == 'a002 ibis metric_result ok "DAU by country" ← compute_metrics(a001)'
