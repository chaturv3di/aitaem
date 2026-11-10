"""artifact group: list, describe, and read the session's artifacts."""

from __future__ import annotations

from pydantic_ai import FunctionToolset, RunContext
from pydantic_core import to_jsonable_python

from aitaem.agent.deps import SessionDeps
from aitaem.agent.tools import ToolOutput, artifact_tool, tabular


@artifact_tool
def list_artifacts(ctx: RunContext[SessionDeps], type_tag: str | None = None) -> ToolOutput:
    """List the session's artifacts, one line each.

    Args:
        type_tag: Only artifacts with this type tag, such as `metric_result` or `handoff`.
    """
    refs = ctx.deps.store.refs(type_tag)
    lines = [f'{r.id} {r.kind} {r.type_tag} {r.status.value} "{r.summary}"' for r in refs]
    return ToolOutput(
        [r.model_dump(mode="json") for r in refs],
        "object",
        "artifact_list",
        f"{len(refs)} artifacts" + (f" tagged {type_tag}" if type_tag else ""),
        facts={"artifacts": lines},
    )


@artifact_tool
def describe_artifact(ctx: RunContext[SessionDeps], artifact_id: str) -> ToolOutput:
    """Describe an artifact: its full record, plus its columns for tables and compute results.

    Args:
        artifact_id: The artifact to describe.
    """
    ref = ctx.deps.store.get(artifact_id)
    detail = ref.model_dump(mode="json")
    if ref.kind in ("table", "ibis") and not ref.lost:
        _, table = tabular(ctx, artifact_id)
        detail["columns"] = {name: str(dtype) for name, dtype in table.schema().items()}
    return ToolOutput(
        detail,
        "object",
        "artifact_detail",
        f"{artifact_id}: {ref.summary}"[:200],
        [artifact_id],
        facts=detail,
    )


@artifact_tool
def read_artifact(
    ctx: RunContext[SessionDeps],
    artifact_id: str,
    columns: list[str] | None = None,
    n: int | None = 10,
) -> ToolOutput:
    """Read rows of a table or compute result, or the content of any other artifact.

    Args:
        artifact_id: The artifact to read.
        columns: The columns to read; all columns when omitted.
        n: The number of rows to read; every row when null.
    """
    ref = ctx.deps.store.get(artifact_id)
    if ref.kind not in ("table", "ibis"):
        content = to_jsonable_python(ctx.deps.store.read(artifact_id))
        return ToolOutput(
            content,
            "object",
            "read",
            f"content of {artifact_id}",
            [artifact_id],
            facts={"content": content},
        )
    _, table = tabular(ctx, artifact_id)
    if columns:
        table = table.select(*columns)
    if n is not None:
        table = table.limit(n)
    if ref.kind == "ibis":
        with ctx.deps.compute_guard():
            arrow = table.to_pyarrow()
    else:
        arrow = table.to_pyarrow()
    rows = to_jsonable_python(arrow.to_pylist())
    return ToolOutput(
        arrow,
        "table",
        "read",
        f"{arrow.num_rows} rows × {arrow.num_columns} columns of {artifact_id}",
        [artifact_id],
        facts={"columns": arrow.column_names, "rows": rows},
    )


artifact_toolset: FunctionToolset[SessionDeps] = FunctionToolset(
    [list_artifacts, describe_artifact, read_artifact]
)
