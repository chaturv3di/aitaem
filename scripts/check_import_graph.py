#!/usr/bin/env python3
"""Check the import rules between aitaem core and the agent module.

1. aitaem core never imports aitaem.agent (one-way dependency).
2. aitaem.agent imports core only through the `aitaem` top level.
3. aitaem.agent.tools never imports aitaem.agent.agent_roles.
4. A role imports only the roles it launches: main session → coordinators → their executors.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROLES = "aitaem.agent.agent_roles"

# The roles each role may import (the roles it launches).
LAUNCHES: dict[str, set[str]] = {
    "main_session": {"connections_coordinator", "analysis_coordinator", "spec_coordinator"},
    "connections_coordinator": set(),
    "analysis_coordinator": {"analysis_executor"},
    "analysis_executor": set(),
    "spec_coordinator": {"spec_executor"},
    "spec_executor": set(),
}


def _imports(tree: ast.AST) -> list[tuple[int, str, list[str]]]:
    """(line, module, imported names) for every import statement."""
    found: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module, [a.name for a in node.names]))
    return found


def check(root: pathlib.Path) -> list[str]:
    """Return one message per violation in the `aitaem` package at `root`."""
    violations: list[str] = []
    for py_file in sorted(root.rglob("*.py")):
        parts = py_file.relative_to(root).parts
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        in_agent = parts[:1] == ("agent",)
        role = parts[2] if parts[:2] == ("agent", "agent_roles") and len(parts) > 3 else None
        for line, module, names in _imports(tree):
            where = f"{py_file}:{line}"
            if not in_agent:
                if module.startswith("aitaem.agent"):
                    violations.append(f"{where}: core imports `{module}`")
                continue
            if module.startswith("aitaem.") and not module.startswith("aitaem.agent"):
                violations.append(f"{where}: `{module}`; import core from the `aitaem` top level")
            if parts[:2] == ("agent", "tools") and module.startswith(ROLES):
                violations.append(f"{where}: tools import `{module}`")
            if role is not None:
                targets = (
                    [module.split(".")[3]]
                    if module.startswith(ROLES + ".")
                    else names
                    if module == ROLES
                    else []
                )
                for target in targets:
                    if target in LAUNCHES and target != role and target not in LAUNCHES[role]:
                        violations.append(f"{where}: role {role} imports role {target}")
    return violations


def main() -> int:
    violations = check(pathlib.Path("aitaem"))
    if violations:
        print("Import-graph violations:")
        for v in violations:
            print(f"  {v}")
        return 1
    print("Import graph check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
