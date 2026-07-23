"""SF-2: structural contract test — every tool that mints a ResultStore entry
must expose the entry's id under the canonical `result_id` field.

Discovers tools by AST-scanning aitaem/agent/*_tools.py rather than testing
known tools one by one, so a future tool that writes to ResultStore under a
differently-named field is caught here, at CI time, instead of surfacing as
a silent None from assemble_trace() (aitaem/agent/trace.py's
_extract_result_id) discovered later, if ever, by a human reading a trace.
"""

from __future__ import annotations

import ast
import importlib
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

_STORE_METHODS = {"store_tabular", "store_text"}
_AGENT_DIR = Path(__file__).resolve().parents[2] / "aitaem" / "agent"


def _tool_functions_that_write_to_store() -> Iterator[tuple[str, Callable[..., Any]]]:
    """AST-scan every aitaem/agent/*_tools.py module for top-level function
    definitions whose body contains a call matching `<expr>.store_tabular(...)`
    or `<expr>.store_text(...)` (matched syntactically, by attribute-call name
    — not by resolving the receiver to a specific ResultStore instance, since
    that would require executing the tool). Yields (qualified_name, function)
    for each match, resolving the function object from the corresponding
    module's namespace via getattr/importlib.

    Glob-based file discovery (aitaem/agent/*_tools.py), not a fixed list of
    tool modules, so a future tools module (e.g. setup_tools.py once SetupBot
    ships) is covered with no change to this function or the test using it.
    """
    for path in sorted(_AGENT_DIR.glob("*_tools.py")):
        module_name = f"aitaem.agent.{path.stem}"
        module = importlib.import_module(module_name)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            calls_store = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in _STORE_METHODS
                for call in ast.walk(node)
            )
            if calls_store:
                fn = getattr(module, node.name)
                yield f"{module_name}.{node.name}", fn


def test_every_store_writing_tool_exposes_result_id():
    """For every (name, fn) yielded by _tool_functions_that_write_to_store(),
    resolve fn's declared return type (typing.get_type_hints(fn)["return"])
    and assert "result_id" appears in ReturnType.model_fields or
    ReturnType.model_computed_fields. Failure message names the offending
    tool and its return type directly, so a future violation fails at CI
    time, at the exact tool that introduced it — not as a silent None
    discovered later, if ever, by a human reading a trace.
    """
    discovered = list(_tool_functions_that_write_to_store())
    assert discovered, "No store-writing tools discovered — check the AST scan itself."

    for name, fn in discovered:
        hints = typing.get_type_hints(fn)
        return_type = hints.get("return")
        assert return_type is not None, f"{name} has no return type annotation."

        fields = getattr(return_type, "model_fields", {})
        computed_fields = getattr(return_type, "model_computed_fields", {})
        assert "result_id" in fields or "result_id" in computed_fields, (
            f"{name} writes to ResultStore but its return type {return_type!r} "
            "does not expose a `result_id` field (per the ToolResult protocol, "
            "03-component-architecture.md §2)."
        )
