#!/usr/bin/env python3
"""Verify no aitaem core module imports from aitaem.agent (one-way dependency)."""
import ast
import pathlib
import sys


def main() -> int:
    violations: list[str] = []
    root = pathlib.Path("aitaem")
    for py_file in sorted(root.rglob("*.py")):
        if "agent" in py_file.parts:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("aitaem.agent"):
                        violations.append(
                            f"{py_file}:{node.lineno}: `import {alias.name}`"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("aitaem.agent"):
                    violations.append(
                        f"{py_file}:{node.lineno}: `from {module} import ...`"
                    )
    if violations:
        print("Import-graph violations (aitaem core → aitaem.agent is forbidden):")
        for v in violations:
            print(f"  {v}")
        return 1
    print("Import graph check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
