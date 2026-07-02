from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field


class ResultEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    arrow: pa.Table | None = None
    ibis_ref: Any | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    def invalidate_ibis_ref(self) -> None:
        self.ibis_ref = None


class ResultStore:
    """Session-scoped store for computation results.

    Holds dual representation: Arrow artifact for persistence/serialization
    and optional live ibis.Table ref for warehouse pushdown. The ibis ref may
    be None if not available or if the connection has been closed.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ResultEntry] = {}

    def store(
        self,
        arrow: pa.Table | None,
        ibis_ref: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        result_id = str(uuid.uuid4())
        self._entries[result_id] = ResultEntry(
            id=result_id,
            arrow=arrow,
            ibis_ref=ibis_ref,
            metadata=metadata or {},
        )
        return result_id

    def get(self, result_id: str) -> ResultEntry:
        try:
            return self._entries[result_id]
        except KeyError:
            raise KeyError(f"No result with id={result_id!r}")

    def get_ibis(self, result_id: str) -> Any | None:
        return self.get(result_id).ibis_ref

    def get_arrow(self, result_id: str) -> pa.Table | None:
        return self.get(result_id).arrow

    def invalidate_all_ibis_refs(self) -> None:
        for entry in self._entries.values():
            entry.invalidate_ibis_ref()

    def __len__(self) -> int:
        return len(self._entries)

    def ids(self) -> list[str]:
        return list(self._entries.keys())
