from __future__ import annotations

from collections.abc import Iterator

import pytest
import yaml
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from aitaem.agent.agent_roles import load_instructions
from aitaem.agent.agent_roles.documents import (
    Intake,
    check_handoff,
    check_reply,
    check_reply_or_handoff,
    parse_handoff,
    store_handoff,
    store_intake,
    to_yaml,
)
from aitaem.agent.artifacts import ProducedBy
from aitaem.agent.deps import SessionDeps
from aitaem.agent.trace import Status

VALID_HANDOFF = "outcome: complete\nresponse: |\n  CTR is 'high'.\nresult_ids: [a001]\n"


class _Params(BaseModel):
    names: list[str] = []


def _run(deps: SessionDeps, validator, outputs: list[str]) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]
    texts: Iterator[str] = iter(outputs)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(next(texts))])

    agent = Agent(FunctionModel(model), output_type=str, deps_type=SessionDeps, retries=5)
    agent.output_validator(validator)
    result = agent.run_sync("go", deps=deps)
    retries = [
        str(p.content)
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, RetryPromptPart)
    ]
    return result.output, retries


@pytest.fixture
def seeded(deps: SessionDeps) -> SessionDeps:
    deps.store.put("object", {}, type_tag="t", summary="s", produced_by=ProducedBy(tool="t"))
    return deps


def test_handoff_validator_accepts_valid(seeded: SessionDeps) -> None:
    output, retries = _run(seeded, check_handoff, [VALID_HANDOFF])
    assert retries == []
    assert parse_handoff(output).response == "CTR is 'high'.\n"


def test_handoff_validator_accepts_fenced_yaml(seeded: SessionDeps) -> None:
    output, _ = _run(seeded, check_handoff, [f"```yaml\n{VALID_HANDOFF}```"])
    assert parse_handoff(output).outcome == "complete"


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        ("outcome: [unclosed", "YAML"),
        ("outcome: done\nresponse: x\n", "outcome"),
        ("outcome: complete\nresponse: x\nresult_ids: [a999]\n", "unknown result_ids ['a999']"),
        ("outcome: needs-input\nresponse: x\n", "at least one question"),
    ],
)
def test_handoff_validator_retries(seeded: SessionDeps, bad: str, error: str) -> None:
    output, retries = _run(seeded, check_handoff, [bad, VALID_HANDOFF])
    assert output == VALID_HANDOFF
    assert len(retries) == 1
    assert error in retries[0] or error.lower() in retries[0].lower()


def test_reply_validator(seeded: SessionDeps) -> None:
    bad = "response: hi\nstatus: ok\nattachments:\n  - artifact_id: a999\n    reason: r\n"
    good = "response: hi\nstatus: ok\nattachments:\n  - artifact_id: a001\n    reason: r\n"
    output, retries = _run(seeded, check_reply, ["response: hi\nstatus: maybe\n", bad, good])
    assert output == good
    assert len(retries) == 2
    assert "a999" in retries[1]


def test_reply_or_handoff_tells_them_apart(seeded: SessionDeps) -> None:
    reply = "response: Which column holds the date?\nstatus: ok\n"
    assert _run(seeded, check_reply_or_handoff, [reply])[0] == reply
    assert _run(seeded, check_reply_or_handoff, [VALID_HANDOFF])[0] == VALID_HANDOFF
    _, retries = _run(
        seeded, check_reply_or_handoff, ["outcome: complete\nstatus: ok\n", VALID_HANDOFF]
    )
    assert "handoff" in retries[0]


def test_intake_round_trips_through_its_artifact(deps: SessionDeps) -> None:
    deps.store.put("object", {}, type_tag="t", summary="s", produced_by=ProducedBy(tool="t"))
    intake = Intake[_Params](
        instructions="Define CTR", params=_Params(names=["ctr"]), result_ids=["a001"]
    )

    intake_id = store_intake(deps, intake, "start_spec_design", "call_1")

    ref = deps.store.get(intake_id)
    assert (ref.kind, ref.type_tag, ref.parents) == ("file", "intake", ["a001"])
    loaded = Intake[_Params].model_validate(yaml.safe_load(deps.store.read(intake_id)))
    assert loaded == intake


def test_store_handoff_links_intake_and_results(seeded: SessionDeps) -> None:
    handoff = parse_handoff(VALID_HANDOFF)
    hid = store_handoff(seeded, handoff, "a001", "run_analysis", "analysis_coordinator")
    ref = seeded.store.get(hid)
    assert (ref.type_tag, ref.parents, ref.status) == ("handoff", ["a001", "a001"], Status.ok)
    assert parse_handoff(seeded.store.read(hid)) == handoff
    assert to_yaml(handoff).startswith("outcome: complete")


def test_load_instructions_reads_packaged_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pkg = tmp_path / "fake_role"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "instructions.md").write_text("1. Do the thing.\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert load_instructions("fake_role") == "1. Do the thing.\n"
