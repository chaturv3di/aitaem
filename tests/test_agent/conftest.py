from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import RunContext, models
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from aitaem import ConnectionManager, SpecCache
from aitaem.agent.artifacts import ArtifactStore
from aitaem.agent.deps import SessionDeps

# Agent tests run on TestModel/FunctionModel only; a real model request fails the test.
models.ALLOW_MODEL_REQUESTS = False
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

EXAMPLES = Path(__file__).parents[2] / "examples"


@pytest.fixture
def example_specs() -> SpecCache:
    """The example metric, slice, and segment specs over `ad_campaigns`."""
    return SpecCache.from_yaml(
        metric_paths=str(EXAMPLES / "metrics"),
        slice_paths=str(EXAMPLES / "slices"),
        segment_paths=str(EXAMPLES / "segments"),
    )


@pytest.fixture
def make_deps(tmp_path: Path, ad_campaigns_connector: Any):
    """Build `SessionDeps` on a fresh store, over the ad_campaigns DuckDB connector by default."""

    def make(
        connection_manager: ConnectionManager | None = None,
        spec_cache: SpecCache | None = None,
    ) -> SessionDeps:
        if connection_manager is None:
            connection_manager = ConnectionManager()
            connection_manager.add_connection("duckdb", connector=ad_campaigns_connector)
        store = ArtifactStore(tmp_path / "session")
        return SessionDeps(
            store=store,
            connection_manager=connection_manager,
            spec_cache=spec_cache if spec_cache is not None else SpecCache(),
            spec_dir=store.spec_dir,
        )

    return make


@pytest.fixture
def deps(make_deps, example_specs: SpecCache) -> SessionDeps:
    return make_deps(spec_cache=example_specs)


def make_ctx(deps: SessionDeps, tool_call_id: str = "call_1") -> RunContext[SessionDeps]:
    """A `RunContext` for calling a tool function directly."""
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id=tool_call_id)
