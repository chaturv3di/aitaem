from __future__ import annotations

import base64
import io
from datetime import datetime
from typing import Any

import pyarrow as pa
import pyarrow.ipc as pa_ipc

_SCHEMA_VERSION = "1.0"


def _arrow_to_b64(table: pa.Table) -> str:
    buf = io.BytesIO()
    with pa_ipc.new_stream(buf, table.schema) as writer:
        writer.write_table(table)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _b64_to_arrow(b64: str) -> pa.Table:
    with pa_ipc.open_stream(io.BytesIO(base64.b64decode(b64))) as reader:
        return reader.read_all()


def dump_store(store: Any) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for result_id in store.ids():
        entry = store.get(result_id)
        artifacts[result_id] = {
            "id": result_id,
            "arrow_b64": _arrow_to_b64(entry.arrow) if entry.arrow is not None else None,
            "created_at": entry.created_at.isoformat(),
            "metadata": entry.metadata,
        }
    return artifacts


def load_store(store: Any, artifacts: dict[str, Any]) -> None:
    from aitaem.agent.store import ResultEntry

    for result_id, data in artifacts.items():
        arrow = _b64_to_arrow(data["arrow_b64"]) if data.get("arrow_b64") else None
        entry = ResultEntry(
            id=result_id,
            arrow=arrow,
            ibis_ref=None,
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata", {}),
        )
        store._entries[result_id] = entry


def make_bundle(messages: list[Any], store: Any) -> dict[str, Any]:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    return {
        "schema_version": _SCHEMA_VERSION,
        # Stored as a JSON string, not a nested dict. ModelMessagesTypeAdapter has
        # ser_json_bytes='base64' / val_json_bytes='base64' config — the dump_json /
        # validate_json round-trip is the only path that correctly handles binary
        # message content (images, audio). validate_python loses that guarantee.
        "messages": ModelMessagesTypeAdapter.dump_json(messages).decode(),
        "artifacts": dump_store(store),
    }


def load_bundle(bundle: dict[str, Any], store: Any) -> list[Any]:
    version = bundle.get("schema_version", "")
    if version != _SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported history bundle schema_version={version!r}. "
            f"Expected {_SCHEMA_VERSION!r}."
        )
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    load_store(store, bundle.get("artifacts", {}))
    return ModelMessagesTypeAdapter.validate_json(bundle.get("messages", "[]"))
