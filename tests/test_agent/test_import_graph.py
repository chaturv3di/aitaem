from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_import_graph", ROOT / "scripts/check_import_graph.py"
)
assert _spec is not None and _spec.loader is not None
graph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(graph)


def _tree(tmp_path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    root = tmp_path / "aitaem"
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def test_repository_passes() -> None:
    assert graph.check(ROOT / "aitaem") == []


@pytest.mark.parametrize(
    ("rel", "source", "message"),
    [
        ("query/x.py", "from aitaem.agent import trace\n", "core imports"),
        ("agent/x.py", "from aitaem.specs.loader import SpecCache\n", "top level"),
        ("agent/x.py", "import aitaem.query.builder\n", "top level"),
        ("agent/tools/x.py", "from aitaem.agent.agent_roles import documents\n", "tools import"),
        (
            "agent/agent_roles/analysis_executor/agent.py",
            "from aitaem.agent.agent_roles.analysis_coordinator import agent\n",
            "role analysis_executor imports role analysis_coordinator",
        ),
        (
            "agent/agent_roles/analysis_coordinator/agent.py",
            "from aitaem.agent.agent_roles import spec_executor\n",
            "role analysis_coordinator imports role spec_executor",
        ),
    ],
)
def test_each_rule_flags(tmp_path: pathlib.Path, rel: str, source: str, message: str) -> None:
    violations = graph.check(_tree(tmp_path, {rel: source}))
    assert len(violations) == 1
    assert message in violations[0]


def test_allowed_imports(tmp_path: pathlib.Path) -> None:
    root = _tree(
        tmp_path,
        {
            "agent/x.py": "from aitaem import SpecCache\nfrom aitaem.agent.trace import Status\n",
            "agent/agent_roles/main_session/agent.py": (
                "from aitaem.agent.agent_roles.analysis_coordinator.agent import run_analysis\n"
                "from aitaem.agent.agent_roles.documents import Reply\n"
                "from aitaem.agent.tools.stats import stats_toolset\n"
            ),
            "agent/agent_roles/analysis_coordinator/agent.py": (
                "from aitaem.agent.agent_roles.analysis_executor.agent import run_analysis_executors\n"
            ),
        },
    )
    assert graph.check(root) == []
