"""Session-scoped artifact store: the only data channel between tools and agents.

Every tool call writes one artifact. Its record (`ArtifactRef`) is appended to
`manifest.jsonl` in the session directory, and its payload is kept by kind:

- ``object``: in memory, plus ``objects/<id>.json``
- ``file``: ``files/<id>.<ext>`` (text)
- ``table``: table ``t_<id>`` in ``session.duckdb``
- ``ibis``: a lazy Ibis expression, in memory only
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import duckdb
import ibis
import pyarrow as pa
from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python

from aitaem.agent.trace import Status

Kind = Literal["object", "file", "table", "ibis"]


class ProducedBy(BaseModel):
    """The tool call that wrote an artifact."""

    tool: str
    call_id: str | None = None
    turn: int = 0
    role: str | None = None


class ArtifactRef(BaseModel):
    """The record of one artifact, as written to the manifest."""

    id: str
    kind: Kind
    type_tag: str
    location: dict[str, str]
    summary: str = Field(max_length=200)
    produced_by: ProducedBy
    parents: list[str] = []
    status: Status = Status.ok
    created_at: datetime
    lost: bool = False


class ArtifactStore:
    """Writes, reads, and indexes the artifacts of one session."""

    def __init__(self, session_dir: str | Path | None = None) -> None:
        if session_dir is None:
            session_dir = tempfile.mkdtemp(prefix="aitaem-session-")
        self.session_dir = Path(session_dir)
        self.manifest_path = self.session_dir / "manifest.jsonl"
        self.duckdb_path = self.session_dir / "session.duckdb"
        self.spec_dir = self.session_dir / "specs"
        for sub in ("objects", "files", "specs"):
            (self.session_dir / sub).mkdir(parents=True, exist_ok=True)
        self.manifest_path.touch()
        self._lock = threading.Lock()
        self._next = 1
        self._refs: dict[str, ArtifactRef] = {}
        self._memory: dict[str, Any] = {}

    # ── writing ─────────────────────────────────────────────────────────────

    def _allocate_id(self) -> str:
        with self._lock:
            artifact_id = f"a{self._next:03d}"
            self._next += 1
            return artifact_id

    def put(
        self,
        kind: Kind,
        payload: Any,
        *,
        type_tag: str,
        summary: str,
        produced_by: ProducedBy,
        parents: list[str] | None = None,
        status: Status = Status.ok,
        ext: str = "yaml",
    ) -> ArtifactRef:
        """Write the payload by kind, append its record to the manifest, and return the record."""
        artifact_id = self._allocate_id()
        location = self._write_payload(artifact_id, kind, payload, ext)
        ref = ArtifactRef(
            id=artifact_id,
            kind=kind,
            type_tag=type_tag,
            location=location,
            summary=summary[:200],
            produced_by=produced_by,
            parents=list(parents or []),
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._refs[artifact_id] = ref
            with self.manifest_path.open("a", encoding="utf-8") as f:
                f.write(ref.model_dump_json() + "\n")
        return ref

    def _write_payload(
        self, artifact_id: str, kind: Kind, payload: Any, ext: str
    ) -> dict[str, str]:
        if kind == "object":
            path = self.session_dir / "objects" / f"{artifact_id}.json"
            path.write_text(json.dumps(to_jsonable_python(payload)), encoding="utf-8")
            self._memory[artifact_id] = payload
            return {"path": str(path)}
        if kind == "file":
            path = self.session_dir / "files" / f"{artifact_id}.{ext}"
            path.write_text(payload, encoding="utf-8")
            return {"path": str(path)}
        if kind == "table":
            table_name = f"t_{artifact_id}"
            arrow = payload if isinstance(payload, pa.Table) else pa.Table.from_pandas(payload)
            con = duckdb.connect(str(self.duckdb_path))
            try:
                con.register("_payload", arrow)
                con.execute(f'CREATE TABLE "{table_name}" AS SELECT * FROM _payload')
                con.unregister("_payload")
            finally:
                con.close()
            return {"duckdb_path": str(self.duckdb_path), "table": table_name}
        self._memory[artifact_id] = payload
        return {"key": artifact_id}

    # ── reading ─────────────────────────────────────────────────────────────

    def get(self, artifact_id: str) -> ArtifactRef:
        """Return the record of an artifact; raise `KeyError` for an unknown ID."""
        try:
            return self._refs[artifact_id]
        except KeyError:
            raise KeyError(f"unknown artifact {artifact_id!r}") from None

    def __contains__(self, artifact_id: object) -> bool:
        return artifact_id in self._refs

    def refs(self, type_tag: str | None = None) -> list[ArtifactRef]:
        """Return the records in ID order, optionally only those with `type_tag`."""
        refs = sorted(self._refs.values(), key=lambda r: int(r.id[1:]))
        return [r for r in refs if type_tag is None or r.type_tag == type_tag]

    def read(self, artifact_id: str) -> Any:
        """Return an artifact's payload.

        Objects come back as written (or as decoded JSON after `load`), files as text, tables as
        an Ibis table on a new connection to ``session.duckdb``, ibis artifacts as the stored
        expression. Raise `LookupError` for a `lost` artifact.
        """
        ref = self.get(artifact_id)
        if ref.lost:
            raise LookupError(f"artifact {artifact_id} is lost; recompute it")
        if ref.kind == "object":
            if artifact_id not in self._memory:
                self._memory[artifact_id] = json.loads(Path(ref.location["path"]).read_text())
            return self._memory[artifact_id]
        if ref.kind == "file":
            return Path(ref.location["path"]).read_text(encoding="utf-8")
        if ref.kind == "table":
            con = ibis.duckdb.connect(ref.location["duckdb_path"])
            return con.table(ref.location["table"])
        return self._memory[artifact_id]

    # ── index and reload ────────────────────────────────────────────────────

    def index_lines(self, expanded: int = 5) -> list[str]:
        """Render the artifact index: one line per artifact, the newest `expanded` with lineage."""
        refs = self.refs()
        lines = []
        for i, r in enumerate(refs):
            line = f'{r.id} {r.kind} {r.type_tag} {r.status.value} "{r.summary}"'
            if r.lost:
                line += " lost"
            if i >= len(refs) - expanded:
                line += f" ← {r.produced_by.tool}"
                if r.parents:
                    line += f"({', '.join(r.parents)})"
            lines.append(line)
        return lines

    def manifest_records(self) -> list[str]:
        """Return the manifest as JSON lines, one per artifact."""
        return [r.model_dump_json() for r in self.refs()]

    @classmethod
    def load(cls, session_dir: str | Path, records: list[str] | None = None) -> ArtifactStore:
        """Rebuild a store from its manifest; mark `lost` every artifact whose payload is gone.

        `records` (from `manifest_records`) replaces the manifest file, for a session directory
        the OS has removed.
        """
        store = cls(session_dir)
        if records is not None:
            store.manifest_path.write_text("".join(r + "\n" for r in records), encoding="utf-8")
        tables: set[str] = set()
        if store.duckdb_path.exists():
            con = duckdb.connect(str(store.duckdb_path), read_only=True)
            try:
                tables = {
                    row[0]
                    for row in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()
                }
            finally:
                con.close()
        last = 0
        for line in store.manifest_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ref = ArtifactRef.model_validate_json(line)
            if ref.kind == "ibis":
                ref.lost = True
            elif ref.kind == "table":
                ref.lost = ref.location["table"] not in tables
            else:
                ref.lost = not Path(ref.location["path"]).exists()
            store._refs[ref.id] = ref
            last = max(last, int(ref.id[1:]))
        store._next = last + 1
        return store
