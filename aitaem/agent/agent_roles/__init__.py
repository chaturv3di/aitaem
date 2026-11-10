"""Agent roles: one package per role, each with its `instructions.md` and `agent.py`."""

from __future__ import annotations

from importlib.resources import files


def load_instructions(package: str) -> str:
    """Return the `instructions.md` packaged with a role."""
    return files(package).joinpath("instructions.md").read_text(encoding="utf-8")
