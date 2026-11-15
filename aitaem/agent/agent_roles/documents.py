"""Documents agents exchange: intakes, handoffs, and replies, written as YAML and checked here."""

from __future__ import annotations

import re
from typing import Any, Generic, Literal, TypeVar

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelRetry, RunContext

from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status

P = TypeVar("P", bound=BaseModel)


class Intake(BaseModel, Generic[P]):
    """What a launched role should do."""

    instructions: str
    """What to do, in the launcher's words."""
    params: P
    """The launched role's typed parameters."""
    result_ids: list[str] = []
    """Earlier artifacts to build on."""


class Handoff(BaseModel):
    """What a coordinator or executor reports back."""

    outcome: Literal["complete", "needs-input", "inconclusive"]
    response: str
    """The answer, the findings, or the reason."""
    result_ids: list[str] = []
    """Artifacts that support the response."""
    questions: list[str] = []
    """Questions for the user, for needs-input."""


class Attachment(BaseModel):
    """An artifact that supports a reply."""

    artifact_id: str
    reason: str


class Reply(BaseModel):
    """The output of a role that talks to the user."""

    response: str
    status: Status
    attachments: list[Attachment] = []


# ── YAML ────────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^\s*```(?:ya?ml)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def to_yaml(doc: BaseModel) -> str:
    """Render a document as YAML."""
    return yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


def load_yaml(text: str) -> Any:
    """Parse a YAML document, accepting a fenced code block around it."""
    match = _FENCE.match(text)
    return yaml.safe_load(match.group(1) if match else text)


def parse_handoff(text: str) -> Handoff:
    return Handoff.model_validate(load_yaml(text))


def parse_reply(text: str) -> Reply:
    return Reply.model_validate(load_yaml(text))


def is_handoff(text: str) -> bool:
    """True when a document has an `outcome` key: a handoff, rather than a reply."""
    data = load_yaml(text)
    return isinstance(data, dict) and "outcome" in data


def store_intake(deps: SessionDeps, intake: Intake[Any], tool: str, call_id: str | None) -> str:
    """Store an intake as a YAML file artifact and return its ID."""
    ref = deps.store.put(
        "file",
        to_yaml(intake),
        type_tag="intake",
        summary=intake.instructions[:200],
        produced_by=ProducedBy(tool=tool, call_id=call_id, turn=deps.turn, role=deps.role),
        parents=list(intake.result_ids),
    )
    return ref.id


def store_handoff(
    deps: SessionDeps, handoff: Handoff, intake_id: str, tool: str, role: str | None
) -> str:
    """Store a handoff as a YAML file artifact, with the intake and its results as parents."""
    ref = deps.store.put(
        "file",
        to_yaml(handoff),
        type_tag="handoff",
        summary=f"{handoff.outcome}: {handoff.response}"[:200],
        produced_by=ProducedBy(tool=tool, turn=deps.turn, role=role),
        parents=[intake_id, *handoff.result_ids],
    )
    return ref.id


# ── output validators ───────────────────────────────────────────────────────


def _retry(kind: str, errors: str) -> ModelRetry:
    return ModelRetry(f"Write the {kind} as a YAML document. Fix these errors: {errors}")


def _unknown_ids(deps: SessionDeps, ids: list[str]) -> list[str]:
    return [i for i in ids if i not in deps.store]


def check_handoff(ctx: RunContext[SessionDeps], text: str) -> str:
    """Output validator: the text is a YAML `Handoff` whose result IDs exist."""
    try:
        handoff = parse_handoff(text)
    except (yaml.YAMLError, ValidationError) as e:
        raise _retry("handoff", str(e)) from e
    errors = []
    if unknown := _unknown_ids(ctx.deps, handoff.result_ids):
        errors.append(f"unknown result_ids {unknown}")
    if handoff.outcome == "needs-input" and not handoff.questions:
        errors.append("a needs-input handoff lists at least one question")
    if errors:
        raise _retry("handoff", "; ".join(errors))
    return text


def check_reply(ctx: RunContext[SessionDeps], text: str) -> str:
    """Output validator: the text is a YAML `Reply` whose attachments exist."""
    try:
        reply = parse_reply(text)
    except (yaml.YAMLError, ValidationError) as e:
        raise _retry("reply", str(e)) from e
    if unknown := _unknown_ids(ctx.deps, [a.artifact_id for a in reply.attachments]):
        raise _retry("reply", f"unknown attachment artifact_ids {unknown}")
    return text


def check_reply_or_handoff(ctx: RunContext[SessionDeps], text: str) -> str:
    """Output validator: a YAML `Handoff` when the text has an `outcome` key, else a `Reply`."""
    try:
        handoff = is_handoff(text)
    except yaml.YAMLError as e:
        raise _retry("reply or handoff", str(e)) from e
    return check_handoff(ctx, text) if handoff else check_reply(ctx, text)
