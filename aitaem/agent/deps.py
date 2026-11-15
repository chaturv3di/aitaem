"""Dependencies shared by every agent role and tool of one session."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from aitaem import ConnectionManager, SpecCache
from aitaem.agent.artifacts import ArtifactStore


@dataclass
class SessionDeps:
    """The session's store and aitaem handles, passed to every run as `deps`.

    Executors get `dataclasses.replace(deps, role=...)`, which shares the store, the handles, and
    `compute_lock` by identity.
    """

    store: ArtifactStore
    connection_manager: ConnectionManager
    spec_cache: SpecCache
    spec_dir: str | Path
    compute_lock: threading.Lock = field(default_factory=threading.Lock)
    role: str = "main_session"
    turn: int = 0

    @contextmanager
    def compute_guard(self) -> Iterator[None]:
        """Hold `compute_lock` while executing Ibis, when the connection manager requires it."""
        if self.connection_manager.requires_compute_lock:
            with self.compute_lock:
                yield
        else:
            yield
