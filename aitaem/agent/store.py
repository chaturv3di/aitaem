from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

from aitaem.utils.exceptions import AitaemError


class WrongEntryKindError(AitaemError):
    """Raised when a ResultStore entry is accessed with the wrong kind getter."""


class _EntryBase(BaseModel):
    """Shared base model for all ResultStore entry kinds."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class TabularEntry(_EntryBase):
    """Tabular result entry holding an Arrow artifact and optional live ibis ref."""

    kind: Literal["tabular"] = "tabular"
    arrow: pa.Table | None = None
    ibis_ref: Any | None = None

    def invalidate_ibis_ref(self) -> None:
        self.ibis_ref = None


class TextEntry(_EntryBase):
    """Text artifact entry (e.g. validated YAML spec, JSON config)."""

    kind: Literal["text"] = "text"
    text: str
    content_type: str


# Discriminated union — use isinstance(entry, TabularEntry) / isinstance(entry, TextEntry)
# to branch. Pydantic model fields using this type should annotate with
# Annotated[Union[TabularEntry, TextEntry], Field(discriminator="kind")].
ResultEntry = TabularEntry | TextEntry


class ResultStore:
    """Session-scoped store for computation results.

    Supports two entry kinds: tabular (Arrow + Ibis ref) and text (string + content_type).
    Use store_tabular() / get_tabular() for computation results; store_text() / get_text()
    for serialized artifacts such as validated YAML specs. The generic get() returns the
    union and is useful when the caller handles either kind.
    """

    def __init__(self) -> None:
        self._entries: dict[str, TabularEntry | TextEntry] = {}

    def store_tabular(
        self,
        arrow: pa.Table | None,
        ibis_ref: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        result_id = str(uuid.uuid4())
        self._entries[result_id] = TabularEntry(
            result_id=result_id,
            arrow=arrow,
            ibis_ref=ibis_ref,
            metadata=metadata or {},
        )
        return result_id

    def store_text(
        self,
        text: str,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        result_id = str(uuid.uuid4())
        self._entries[result_id] = TextEntry(
            result_id=result_id,
            text=text,
            content_type=content_type,
            metadata=metadata or {},
        )
        return result_id

    def get(self, result_id: str) -> TabularEntry | TextEntry:
        try:
            return self._entries[result_id]
        except KeyError:
            raise KeyError(f"No result with id={result_id!r}")

    def get_tabular(self, result_id: str) -> TabularEntry:
        entry = self.get(result_id)
        if not isinstance(entry, TabularEntry):
            raise WrongEntryKindError(
                f"Entry {result_id!r} has kind={entry.kind!r}, expected 'tabular'."
            )
        return entry

    def get_text(self, result_id: str) -> TextEntry:
        entry = self.get(result_id)
        if not isinstance(entry, TextEntry):
            raise WrongEntryKindError(
                f"Entry {result_id!r} has kind={entry.kind!r}, expected 'text'."
            )
        return entry

    def get_ibis(self, result_id: str) -> Any | None:
        return self.get_tabular(result_id).ibis_ref

    def get_arrow(self, result_id: str) -> pa.Table | None:
        return self.get_tabular(result_id).arrow

    def invalidate_all_ibis_refs(self) -> None:
        for entry in self._entries.values():
            if isinstance(entry, TabularEntry):
                entry.invalidate_ibis_ref()

    def __len__(self) -> int:
        return len(self._entries)

    def ids(self) -> list[str]:
        return list(self._entries.keys())
