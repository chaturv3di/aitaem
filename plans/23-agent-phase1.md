# Phase 1 — Foundations: `aitaem.agent` Module

Establishes the complete scaffolding for `aitaem.agent`: package structure, optional
install wiring, all primitive data models, the `Bot` abstract base class, history
serialization, and trace assembly from pydantic-ai. Nothing in this phase runs an
LLM; everything is unit-testable without network access.

---

## Decisions Resolved (Pre-flight)

| Question | Decision |
|---|---|
| OQ-1: Eval library | **pydantic-evals primary** (native OTel alignment, deterministic correctness). deepeval added when RAG flows arrive. |
| OQ-2: Reference eval harness | **Ship `tests/evals/`** — consistent with blueprint philosophy (G2). Implemented in Phase 6. |
| OQ-3: Phasing | **Confirmed**: P0 done → Phase 1 → Phase 2 → {3, 4} → 5 → 6 → 7. |
| LLM providers | **Provider-specific extras** (`[agent-anthropic]` for now; `[agent-openai]`, `[agent-google]` added when those providers are actively used). No bare `[agent]` extra. |
| pydantic-evals placement | **`[agent-evals]`** — separate opt-in extra for CI eval pipelines. Also added to **`[dev]`** so aitaem's own eval harness (`tests/evals/`) runs in CI. Not bundled into provider extras. |

---

## Terminology

| Term | Meaning in aitaem |
|---|---|
| **Run** | One `agent.run()` call inside pydantic-ai. Has a `run_id`. Internally may make multiple LLM API requests (e.g. initial request → tool call → follow-up). `RunUsage.requests` counts all of these. |
| **Turn** | One `bot.chat("…")` call from the user's perspective. In aitaem, one turn = one run = one `RunTrace`. |
| **Conversation** | The full multi-turn session on a Bot instance. Spans many turns, identified by a shared `conversation_id`. |
| **Trace** | aitaem's `RunTrace` — a frozen, serializable snapshot of a single turn: tool calls assembled, usage captured, duration clocked. The unit the eval substrate reads. |

These terms are used consistently throughout this document and the implementation.

---

## Background: pydantic-ai v2.2.0 API

Phase 1 primitives must be shaped to integrate cleanly with pydantic-ai v2.2.0
(latest stable as of plan date). Key facts that influence the design:

### `AgentRunResult` — what's accessible after a run

```python
result.run_id           # str — UUID7, unique per .run() call
result.conversation_id  # str — UUID7, shared across multi-turn runs
result.usage            # RunUsage — aggregate token + request counts
result.timestamp        # datetime — timestamp of last ModelResponse
result.metadata         # dict | None — user-supplied
result.new_messages()   # list[ModelMessage] — messages from THIS run only
result.all_messages()   # list[ModelMessage] — full history incl. prior turns
result._traceparent(required=False)  # W3C traceparent string or None
```

There is no `Span` object on the result. OTel spans are emitted fire-and-forget
to an exporter. The traceparent string (`00-{trace_id}-{span_id}-{flags}`) is the
only in-process trace context available without a custom exporter.

### `RunUsage` fields

```python
run_usage.requests          # int — number of LLM API calls in this run
run_usage.tool_calls        # int — total tool invocations
run_usage.input_tokens      # int
run_usage.output_tokens     # int
run_usage.cache_read_tokens # int
run_usage.cache_write_tokens# int
run_usage.total_tokens      # @property: input + output (not a dataclass field)
```

`RunUsage` is a mutable `@dataclass` with an `.incr()` method designed for
optional cross-turn accumulation. `AgentRunResult.usage` returns a `RunUsage`
already aggregated across all internal LLM requests within that single run.
aitaem's `Usage` model is a **frozen snapshot** of this object taken at turn
completion — it does not accumulate across turns. The `from_run_usage()`
classmethod (SF-4) performs this copy.

### Tool calls in message history

Tool calls appear across `ModelResponse` and `ModelRequest` objects, matched by
`tool_call_id`:

- `ModelResponse.parts` → `ToolCallPart(tool_name, args, tool_call_id)` — what the LLM requested
- `ModelRequest.parts` → `ToolReturnPart(tool_name, content, tool_call_id, outcome)` — tool result sent back

`ToolCallPart.args` is `str | dict | None`. When it's a string, it's a JSON
string to be parsed. `ToolReturnPart.outcome` is `'success' | 'failed' | 'denied'`;
`content` is the LLM-facing summary string.

### Multi-turn history

Multi-turn works by passing `result.all_messages()` as `message_history` to the
next `agent.run()`. Pydantic-ai types `ModelMessage` as JSON-serializable via
`TypeAdapter(list[ModelMessage]).dump_python(messages, mode="json")` and
`.validate_python(data)`.

### `FunctionToolset` — runtime tool management

```python
toolset = FunctionToolset()
toolset.add_tool(tool: Tool)      # add a Tool instance
toolset.add_function(fn)          # add a plain function, auto-wrapped
```

The `toolset` is passed as `toolsets=[toolset]` when constructing the
pydantic-ai `Agent`. Additional per-call tools pass via `agent.run(...,
toolsets=[extra_toolset])`.

---

## Package and File Structure

### New files

```
aitaem/agent/
├── __init__.py          # Public exports (see SF-1)
├── response.py          # BotResponse[PayloadT], Status
├── store.py             # ResultEntry, ResultStore
├── trace.py             # RunTrace, ToolCall, Usage, assemble_trace()
├── base.py              # Bot abstract base class
└── history.py           # dump_history / load_history helpers, HistoryBundle

scripts/
└── check_import_graph.py  # CI script

tests/test_agent/
├── __init__.py
├── test_primitives.py   # Status, Usage, ToolCall, RunTrace, BotResponse, ResultStore
├── test_trace.py        # assemble_trace() with mocked AgentRunResult
└── test_history.py      # dump_history / load_history round-trip
```

### Modified files

```
pyproject.toml           # [agent-anthropic], [agent-evals] extras; pydantic-evals added to [dev]
.github/workflows/ci.yml # import-graph check job, test-agent job
```

---

## Implementation Sub-Features

Implement in this order. Each SF is independently testable before moving to the next.

---

### SF-1: Package structure

**Files:** `aitaem/agent/__init__.py`, `aitaem/agent/response.py`,
`aitaem/agent/store.py`, `aitaem/agent/trace.py`, `aitaem/agent/base.py`,
`aitaem/agent/history.py`, `tests/test_agent/__init__.py`

Create the directory and all module files. At this stage, all files except
`__init__.py` may be empty stubs.

`aitaem/agent/__init__.py` exports — add to this incrementally as SFs complete:

```python
from aitaem.agent.response import BotResponse, Status
from aitaem.agent.store import ResultEntry, ResultStore
from aitaem.agent.trace import RunTrace, ToolCall, Usage
from aitaem.agent.base import Bot

__all__ = [
    "Bot",
    "BotResponse",
    "Status",
    "RunTrace",
    "ToolCall",
    "Usage",
    "ResultEntry",
    "ResultStore",
]
```

**Validation:**
```python
# In tests/test_agent/test_primitives.py
def test_aitaem_agent_importable():
    import aitaem.agent  # noqa: F401

def test_public_exports_present():
    from aitaem.agent import Bot, BotResponse, Status, RunTrace, ToolCall, Usage
    from aitaem.agent import ResultEntry, ResultStore
    assert all(x is not None for x in [Bot, BotResponse, Status, RunTrace,
                                         ToolCall, Usage, ResultEntry, ResultStore])
```

---

### SF-2: `pyproject.toml` extras

**File:** `pyproject.toml`

Add provider-specific agent extras and a separate eval extra. No bare `[agent]`
extra — users always pick a provider. Add `pydantic-evals` to `[dev]` so
aitaem's own eval harness in `tests/evals/` runs in CI without bundling it into
user-facing installs.

```toml
[project.optional-dependencies]
# ... existing bigquery, postgres, docs extras unchanged ...

# Agent runtime — one per supported LLM provider.
# pydantic-ai-slim is the lean base (no bundled providers).
# Users pick the provider they use: pip install aitaem[agent-anthropic]
agent-anthropic = [
    "pydantic-ai-slim[anthropic]>=2.2.0",
]
# Eval framework — opt-in for users who want to run their own eval pipelines.
# pydantic-evals is version-locked to pydantic-ai-slim (same version number).
# pip install aitaem[agent-anthropic,agent-evals]
agent-evals = [
    "pydantic-ai-slim[evals]>=2.2.0",
]

dev = [
    "pytest>=7.4.0",
    "pytest-cov>=4.1.0",
    "pytest-mock>=3.12.0",
    "ruff>=0.3.0",
    "mypy>=1.8.0",
    "build>=1.0.0",
    "pydantic-ai-slim[evals]>=2.2.0",       # for aitaem's own tests/evals/ harness
]

# Update the catch-all:
all = ["aitaem[bigquery,postgres,agent-anthropic,agent-evals,dev,docs]"]
```

**Why `pydantic-ai-slim` not `pydantic-ai`:** The bare `pydantic-ai` package is
a bundle wrapper that pulls in all providers (anthropic + openai + google +
logfire + mcp + cli) automatically. `pydantic-ai-slim` is the actual runtime
with only the requested provider added via its extras.

**Why `pydantic-evals` in `[dev]` not `[agent-evals]` only:** aitaem's own CI
(Phase 6) runs the reference eval harness in `tests/evals/`. The `[dev]` extra
covers aitaem's developers. The `[agent-evals]` extra covers users who want to
run the same pattern against their own agents.

**Version constraint:** pydantic-evals is version-locked to pydantic-ai-slim
(both at the same version, e.g. both `2.2.0`). Always keep these in sync. The
`>=2.2.0` constraint allows patch upgrades; pin them together if a breaking
change requires it.

**Validation:** No automated test. Confirm manually:
```bash
uv pip install -e ".[agent-anthropic,dev]" --dry-run
# Should resolve pydantic-ai-slim[anthropic] without pulling in openai/google SDKs

uv pip install -e ".[agent-anthropic,agent-evals]" --dry-run
# Should add pydantic-evals alongside the anthropic SDK
```

---

### SF-3: Import-graph CI check

**Files:** `scripts/check_import_graph.py`, `.github/workflows/ci.yml`

**`scripts/check_import_graph.py`:**

```python
#!/usr/bin/env python3
"""Verify no aitaem core module imports from aitaem.agent (one-way dependency)."""
import ast
import pathlib
import sys


def main() -> int:
    violations: list[str] = []
    root = pathlib.Path("aitaem")
    for py_file in sorted(root.rglob("*.py")):
        # Skip the agent subpackage itself — it is allowed to import aitaem core
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
```

**`.github/workflows/ci.yml`** — add a new job alongside the existing `test`,
`lint`, and `type-check` jobs:

```yaml
import-graph:
    name: import-graph
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Check import graph
        run: python scripts/check_import_graph.py
```

**Validation:**
```bash
python scripts/check_import_graph.py  # should print "Import graph check passed."
```

Also add a test that the script detects a violation when given a fake import:

```python
# tests/test_agent/test_primitives.py
def test_import_graph_check_passes():
    """The script must exit 0 on the current codebase."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "scripts/check_import_graph.py"],
        capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

---

### SF-4: `Status` enum, `Usage`, `ToolCall`, `RunTrace` models

**File:** `aitaem/agent/trace.py`

```python
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field


class Status(str, Enum):
    ok = "ok"
    empty = "empty"
    refused = "refused"
    error = "error"


class Usage(BaseModel):
    """Frozen snapshot of token/request usage for a single turn.

    A thin wrapper around pydantic-ai's mutable RunUsage dataclass, taken at
    turn completion so the eval substrate holds an immutable audit record.
    Use from_run_usage() to construct from an AgentRunResult.
    """

    model_config = ConfigDict(frozen=True)

    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @computed_field  # included in model_dump() / model_dump_json()
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_run_usage(cls, ru: Any) -> "Usage":
        """Snapshot a pydantic-ai RunUsage into a frozen Usage instance."""
        return cls(
            requests=ru.requests,
            tool_calls=ru.tool_calls,
            input_tokens=ru.input_tokens,
            output_tokens=ru.output_tokens,
            cache_read_tokens=ru.cache_read_tokens,
            cache_write_tokens=ru.cache_write_tokens,
        )


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    name: str
    args: dict[str, Any]
    result_id: str | None = None       # populated by tools that store in ResultStore
    llm_summary: str | None = None     # compact snippet from ToolReturnPart.content —
                                       # MUST be a human/LLM-readable summary, never raw
                                       # result data. Full data lives in ResultStore only.
    success: bool = True
    duration_ms: float | None = None   # per-tool timing; None until Phase 2 tools


class RunTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    conversation_id: str
    timestamp: datetime
    tool_calls: list[ToolCall]
    usage: Usage
    traceparent: str | None = None     # W3C traceparent from pydantic-ai
    duration_ms: float = 0.0           # total turn wall-clock time
```

**Move `Status` to `response.py` later if preferred; for now co-locate with
the trace types since `BotResponse` references both.**

**Validation:**

```python
# tests/test_agent/test_primitives.py

def test_status_values():
    assert set(Status) == {Status.ok, Status.empty, Status.refused, Status.error}

def test_status_is_str():
    assert Status.ok == "ok"

def test_usage_total_tokens():
    u = Usage(input_tokens=100, output_tokens=50)
    assert u.total_tokens == 150

def test_usage_defaults_zero():
    u = Usage()
    assert u.requests == 0 and u.total_tokens == 0

def test_usage_total_tokens_in_serialization():
    u = Usage(input_tokens=100, output_tokens=50)
    data = u.model_dump()
    assert data["total_tokens"] == 150  # computed_field included

def test_usage_from_run_usage():
    from unittest.mock import MagicMock
    ru = MagicMock()
    ru.requests = 2
    ru.tool_calls = 1
    ru.input_tokens = 200
    ru.output_tokens = 80
    ru.cache_read_tokens = 10
    ru.cache_write_tokens = 0
    u = Usage.from_run_usage(ru)
    assert u.requests == 2
    assert u.total_tokens == 280
    assert u.model_dump_json()  # serializes cleanly

def test_run_trace_total_tokens_serialized():
    """computed_field total_tokens must appear in JSON output of the full trace."""
    import json
    from datetime import timezone
    trace = RunTrace(
        run_id="r1",
        conversation_id="c1",
        timestamp=datetime.now(timezone.utc),
        tool_calls=[],
        usage=Usage(input_tokens=100, output_tokens=50),
    )
    data = json.loads(trace.model_dump_json())
    assert data["usage"]["total_tokens"] == 150
```

---

### SF-5: `ResultEntry` and `ResultStore`

**File:** `aitaem/agent/store.py`

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field


class ResultEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    arrow: pa.Table | None = None
    ibis_ref: Any | None = None        # ibis.Table (lazy); None if materialized only
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    def invalidate_ibis_ref(self) -> None:
        """Clear the live ibis ref (e.g., after connection close)."""
        self.ibis_ref = None


class ResultStore:
    """Session-scoped store for computation results.

    Holds dual representation: Arrow artifact for persistence/serialization and
    optional live ibis.Table ref for warehouse pushdown. The ibis ref may be
    None if not available or if the connection has been closed.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ResultEntry] = {}

    def store(
        self,
        arrow: pa.Table | None,
        ibis_ref: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        result_id = str(uuid.uuid4())
        self._entries[result_id] = ResultEntry(
            id=result_id,
            arrow=arrow,
            ibis_ref=ibis_ref,
            metadata=metadata or {},
        )
        return result_id

    def get(self, result_id: str) -> ResultEntry:
        try:
            return self._entries[result_id]
        except KeyError:
            raise KeyError(f"No result with id={result_id!r}")

    def get_ibis(self, result_id: str) -> Any | None:
        return self.get(result_id).ibis_ref

    def get_arrow(self, result_id: str) -> pa.Table | None:
        return self.get(result_id).arrow

    def invalidate_all_ibis_refs(self) -> None:
        for entry in self._entries.values():
            entry.invalidate_ibis_ref()

    def __len__(self) -> int:
        return len(self._entries)

    def ids(self) -> list[str]:
        return list(self._entries.keys())
```

**Validation:**

```python
# tests/test_agent/test_primitives.py

import pyarrow as pa
import pytest

def test_result_store_store_returns_unique_ids():
    store = ResultStore()
    id1 = store.store(None, None)
    id2 = store.store(None, None)
    assert id1 != id2

def test_result_store_get_retrieves_entry():
    store = ResultStore()
    table = pa.table({"x": [1, 2, 3]})
    rid = store.store(table, None, metadata={"source": "test"})
    entry = store.get(rid)
    assert entry.id == rid
    assert entry.arrow.equals(table)
    assert entry.metadata["source"] == "test"

def test_result_store_get_missing_raises():
    store = ResultStore()
    with pytest.raises(KeyError):
        store.get("does-not-exist")

def test_result_store_get_ibis_none_when_not_set():
    store = ResultStore()
    rid = store.store(None, None)
    assert store.get_ibis(rid) is None

def test_result_store_invalidate_ibis_refs():
    store = ResultStore()
    mock_ref = object()
    rid = store.store(None, mock_ref)
    assert store.get_ibis(rid) is mock_ref
    store.invalidate_all_ibis_refs()
    assert store.get_ibis(rid) is None

def test_result_store_len_and_ids():
    store = ResultStore()
    assert len(store) == 0
    r1 = store.store(None, None)
    r2 = store.store(None, None)
    assert len(store) == 2
    assert set(store.ids()) == {r1, r2}
```

---

### SF-6: `BotResponse[PayloadT]`

**File:** `aitaem/agent/response.py`

```python
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from aitaem.agent._trace import RunTrace, Status

PayloadT = TypeVar("PayloadT")


class BotResponse(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(frozen=True)

    status: Status
    narrative: str
    trace: RunTrace
    reason: str | None = None      # set when status=refused or status=error
    payload: PayloadT | None = None
```

**Validation:**

```python
# tests/test_agent/test_primitives.py

from datetime import datetime, timezone

def _minimal_trace() -> RunTrace:
    from aitaem.agent._trace import Usage
    return RunTrace(
        run_id="r", conversation_id="c",
        timestamp=datetime.now(timezone.utc),
        tool_calls=[], usage=Usage(),
    )

def test_bot_response_frozen():
    from pydantic import ValidationError
    trace = _minimal_trace()
    resp = BotResponse(status=Status.ok, narrative="Done.", trace=trace)
    with pytest.raises(ValidationError):
        resp.status = Status.error  # frozen model must reject mutation

def test_bot_response_full_json_serialization():
    """Full nested serialization including RunTrace and computed total_tokens."""
    import json
    trace = _minimal_trace()
    resp = BotResponse(
        status=Status.refused,
        narrative="Cannot answer.",
        trace=trace,
        reason="No matching metric.",
    )
    data = json.loads(resp.model_dump_json())
    assert data["status"] == "refused"
    assert data["trace"]["usage"]["total_tokens"] == 0  # computed_field present
```

---

### SF-7: `Bot` abstract base class

**File:** `aitaem/agent/base.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from aitaem.agent._response import BotResponse
from aitaem.agent._store import ResultEntry, ResultStore

if TYPE_CHECKING:
    from pydantic_ai import Agent


class Bot(ABC):
    """Abstract base class for all AITAEM agent bots.

    Subclasses implement _build_agent() to return a configured pydantic-ai
    Agent. Bots are constructed per user/session; conversation state (message
    history and result store) lives on the bot instance.

    The standard pattern for subclass construction:
        class MyBot(Bot):
            def __init__(self, my_resource, **kwargs):
                self._my_resource = my_resource  # set BEFORE super().__init__()
                super().__init__(**kwargs)        # triggers _build_agent()

            def _build_agent(self):
                # self._my_resource is already available here
                return pydantic_ai.Agent(...)

    Context-window management: for long-running sessions where history may
    exceed the model's context limit, pass a history processor via the
    capabilities argument when constructing the Agent in _build_agent():

        from pydantic_ai.capabilities import ProcessHistory, ReinjectSystemPrompt

        def _build_agent(self):
            return Agent(
                model=self._model,
                capabilities=[
                    ReinjectSystemPrompt(replace_existing=True),
                    ProcessHistory(trim_old_messages),
                ],
            )

    The processor callable receives the full message list before each model
    request (including mid-tool-call-loop steps) and returns a modified list.
    IMPORTANT: only trim complete tool-call pairs (ToolCallPart + matching
    ToolReturnPart) as a unit. Dropping a ToolReturnPart without its
    ToolCallPart violates provider API constraints. See pydantic-ai issue #2050.
    No built-in trimmer is provided by pydantic-ai; implement one in Phase 2+.
    """

    def __init__(
        self,
        *,
        model: str,
        tools: list[Any] | None = None,
    ) -> None:
        self._model = model
        self._store = ResultStore()
        self._message_history: list[Any] = []  # list[pydantic_ai.messages.ModelMessage]
        self._agent = self._build_agent()

    @abstractmethod
    def _build_agent(self) -> Agent:
        """Return a configured pydantic-ai Agent for this bot."""
        # self._my_resource is already available here

    async def chat(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> BotResponse:
        """Send a message and accumulate history (multi-turn entry point)."""
        raise NotImplementedError(
            "chat() must be implemented by the convenience bot subclass."
        )

    async def ask(
        self,
        message: str,
        *,
        extra_tools: list[Any] | None = None,
    ) -> BotResponse:
        """Send a single-turn message without accumulating history."""
        raise NotImplementedError(
            "ask() must be implemented by the convenience bot subclass."
        )

    def add_tool(self, tool: Any) -> None:
        """Add a tool to this bot's default tool set at runtime."""
        raise NotImplementedError("add_tool() implemented in Phase 5.")

    def add_bot(self, bot: "Bot") -> None:
        """Register another bot as a tool (sugar for add_tool(other.as_tool()))."""
        self.add_tool(bot.as_tool())

    def as_tool(self) -> Any:
        """Return a pydantic-ai Tool that wraps this bot's ask() method."""
        raise NotImplementedError("as_tool() implemented in Phase 5.")

    def get_result(self, result_id: str) -> ResultEntry:
        """Retrieve a stored computation result by ID."""
        return self._store.get(result_id)

    def dump_history(self) -> dict[str, Any]:
        """Serialize conversation history and result artifacts to a JSON-safe dict."""
        raise NotImplementedError("dump_history() implemented in SF-8.")

    @classmethod
    def load_history(cls, data: dict[str, Any], **kwargs: Any) -> "Bot":
        """Reconstruct a bot with pre-loaded conversation history and artifacts."""
        raise NotImplementedError("load_history() implemented in SF-8.")

    @property
    def store(self) -> ResultStore:
        return self._store
```

**Validation:**

```python
# tests/test_agent/test_primitives.py

import pytest

def test_bot_is_abstract():
    """Bot cannot be instantiated directly."""
    with pytest.raises(TypeError):
        Bot(model="claude-sonnet-4-6")

def test_bot_subclass_must_implement_build_agent():
    """Subclass with _build_agent returning None is instantiable."""
    class ConcreteBot(Bot):
        def _build_agent(self):
            return None  # stub

    bot = ConcreteBot(model="claude-sonnet-4-6")
    assert bot.store is not None
    assert isinstance(bot.store, ResultStore)

def test_bot_get_result_delegates_to_store():
    import pyarrow as pa

    class ConcreteBot(Bot):
        def _build_agent(self):
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    table = pa.table({"v": [1]})
    rid = bot.store.store(table, None)
    entry = bot.get_result(rid)
    assert entry.arrow.equals(table)

def test_bot_chat_raises_not_implemented():
    import asyncio

    class ConcreteBot(Bot):
        def _build_agent(self):
            return None

    bot = ConcreteBot(model="claude-sonnet-4-6")
    with pytest.raises(NotImplementedError):
        asyncio.get_event_loop().run_until_complete(bot.chat("hello"))
```

---

### SF-8: History I/O — `dump_history()` / `load_history()`

**File:** `aitaem/agent/history.py`

Implements the serialization helpers. The format version is pinned so that
deserialization can detect and reject incompatible bundles in future.

```python
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pyarrow.ipc as pa_ipc

_SCHEMA_VERSION = "1.0"


def _arrow_to_b64(table: pa.Table) -> str:
    buf = io.BytesIO()
    with pa_ipc.new_stream(buf, table.schema) as writer:
        writer.write_table(table)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _b64_to_arrow(b64: str) -> pa.Table:
    with pa_ipc.open_stream(io.BytesIO(base64.b64decode(b64))) as reader:
        return reader.read_all()


def dump_store(store: Any) -> dict[str, Any]:
    """Serialize a ResultStore to a JSON-safe dict."""
    artifacts: dict[str, Any] = {}
    for result_id in store.ids():
        entry = store.get(result_id)
        artifacts[result_id] = {
            "id": result_id,
            "arrow_b64": _arrow_to_b64(entry.arrow) if entry.arrow is not None else None,
            "created_at": entry.created_at.isoformat(),
            "metadata": entry.metadata,
        }
    return artifacts


def load_store(store: Any, artifacts: dict[str, Any]) -> None:
    """Hydrate a ResultStore from a serialized artifact dict (arrow only; ibis refs lost)."""
    from aitaem.agent._store import ResultEntry

    for result_id, data in artifacts.items():
        arrow = _b64_to_arrow(data["arrow_b64"]) if data.get("arrow_b64") else None
        entry = ResultEntry(
            id=result_id,
            arrow=arrow,
            ibis_ref=None,
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata", {}),
        )
        store._entries[result_id] = entry


def make_bundle(messages: list[Any], store: Any) -> dict[str, Any]:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    return {
        "schema_version": _SCHEMA_VERSION,
        # Stored as a JSON string, not a nested dict. ModelMessagesTypeAdapter has
        # ser_json_bytes='base64' / val_json_bytes='base64' config — the dump_json /
        # validate_json round-trip is the only path that correctly handles binary
        # message content (images, audio). validate_python loses that guarantee.
        "messages": ModelMessagesTypeAdapter.dump_json(messages).decode(),
        "artifacts": dump_store(store),
    }


def load_bundle(bundle: dict[str, Any], store: Any) -> list[Any]:
    version = bundle.get("schema_version", "")
    if version != _SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported history bundle schema_version={version!r}. "
            f"Expected {_SCHEMA_VERSION!r}."
        )
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    load_store(store, bundle.get("artifacts", {}))
    return ModelMessagesTypeAdapter.validate_json(bundle.get("messages", "[]"))
```

**Integrate into `bot/base.py`** — replace the stubs in `dump_history` and
`load_history` with concrete implementations that call these helpers:

```python
# In aitaem/agent/base.py — replace stub methods:

from aitaem.agent._history import make_bundle, load_bundle

def dump_history(self) -> dict[str, Any]:
    """Serialize conversation history and result artifacts to a JSON-safe dict.

    The returned dict is JSON-serializable (suitable for json.dumps). Ibis refs
    are not serialized; only Arrow artifacts are preserved. Reload with
    load_history() to restore history with Arrow artifacts available via
    get_result(), but get_ibis() will return None on restored entries.
    """
    return make_bundle(self._message_history, self._store)

@classmethod
def load_history(cls, data: dict[str, Any], **kwargs: Any) -> "Bot":
    """Construct a new bot pre-loaded with a serialized history bundle.

    Args:
        data: Bundle returned by dump_history() on a prior bot instance.
        **kwargs: Constructor arguments for the concrete Bot subclass.

    Returns:
        A new bot instance with _message_history and store populated from data.
        The bot's _agent is rebuilt fresh from **kwargs.
    """
    bot = cls(**kwargs)
    bot._message_history = load_bundle(data, bot._store)
    return bot
```

**Validation:**

```python
# tests/test_agent/test_history.py

import json
import pyarrow as pa
import pytest
from datetime import timezone, datetime


class _StubBot(Bot):
    """Minimal concrete Bot for testing history I/O without an LLM."""
    def _build_agent(self):
        return None


def test_dump_history_empty():
    import json
    bot = _StubBot(model="claude-sonnet-4-6")
    bundle = bot.dump_history()
    assert bundle["schema_version"] == "1.0"
    assert json.loads(bundle["messages"]) == []   # stored as JSON string, not a list
    assert bundle["artifacts"] == {}


def test_dump_load_roundtrip_with_arrow_artifact():
    bot = _StubBot(model="claude-sonnet-4-6")
    table = pa.table({"metric_value": [1.0, 2.0], "metric_name": ["ctr", "ctr"]})
    rid = bot.store.store(table, None, metadata={"metric": "ctr"})

    bundle = bot.dump_history()
    assert rid in bundle["artifacts"]
    assert bundle["artifacts"][rid]["arrow_b64"] is not None

    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")
    entry = restored.get_result(rid)
    assert entry.arrow.equals(table)
    assert entry.ibis_ref is None  # ibis refs not preserved
    assert entry.metadata["metric"] == "ctr"


def test_dump_load_roundtrip_null_arrow():
    bot = _StubBot(model="claude-sonnet-4-6")
    rid = bot.store.store(None, None)
    bundle = bot.dump_history()
    assert bundle["artifacts"][rid]["arrow_b64"] is None

    restored = _StubBot.load_history(bundle, model="claude-sonnet-4-6")
    entry = restored.get_result(rid)
    assert entry.arrow is None


def test_load_history_wrong_schema_version():
    bundle = {"schema_version": "99.0", "messages": [], "artifacts": {}}
    with pytest.raises(ValueError, match="schema_version"):
        _StubBot.load_history(bundle, model="claude-sonnet-4-6")


def test_bundle_is_json_serializable():
    bot = _StubBot(model="claude-sonnet-4-6")
    table = pa.table({"x": [1, 2]})
    bot.store.store(table, None)
    bundle = bot.dump_history()
    # Must not raise
    _ = json.dumps(bundle)
```

**Note:** Message serialization round-trip requires pydantic-ai `ModelMessage`
objects to be present in the history. The full round-trip test (with actual
pydantic-ai messages) is an integration test, gated with `@pytest.mark.integration`.
Unit tests above cover the artifact serialization path independently.

**`ReinjectSystemPrompt` — design note for Phase 2+:**

When `dump_history()` serializes `all_messages()`, the bundle includes the
original `SystemPromptPart`. When the conversation is reloaded via `load_history()`
and the agent runs again, pydantic-ai detects the existing system prompt in
history and does not inject a duplicate — no conflict.

However, if the agent's system prompt has been updated since the conversation
was saved, the stored (old) system prompt takes precedence. To ensure the
agent's current configured prompt always wins, subclasses implementing
`_build_agent()` in Phase 2+ should pass the capability:

```python
from pydantic_ai.capabilities import ReinjectSystemPrompt

# In _build_agent():
return Agent(
    model=self._model,
    capabilities=[ReinjectSystemPrompt(replace_existing=True)],
    ...
)
```

`replace_existing=True` strips all `SystemPromptPart` entries from history before
each run and re-injects the agent's current system prompt. This is the correct
default for production bots where the system prompt may evolve between releases.
Phase 1 does not add this (no agent runs happen here); it is a Phase 2 concern.

---

### SF-9: Trace assembly from pydantic-ai

**File:** `aitaem/agent/trace.py` — add `assemble_trace()` function

```python
import json
from datetime import datetime, timezone


def assemble_trace(result: Any, run_start: datetime) -> RunTrace:
    """Assemble a RunTrace from a completed pydantic-ai AgentRunResult.

    Extracts tool call/return pairs from new_messages(), maps RunUsage to our
    Usage model, and computes wall-clock duration.

    Args:
        result: A pydantic_ai.AgentRunResult (typed as Any to avoid hard
            import at module level; pydantic-ai is an optional dependency).
        run_start: The datetime.now(timezone.utc) captured immediately before
            agent.run() was called.

    Returns:
        A frozen RunTrace instance.
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    duration_ms = (datetime.now(timezone.utc) - run_start).total_seconds() * 1000

    # First pass: collect ToolCallPart entries by tool_call_id
    pending: dict[str, dict[str, Any]] = {}
    for msg in result.new_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    args = part.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {"_raw": args}
                    elif args is None:
                        args = {}
                    pending[part.tool_call_id] = {
                        "tool_call_id": part.tool_call_id,
                        "name": part.tool_name,
                        "args": args,
                    }

    # Second pass: match ToolReturnPart to fill in result/summary/success
    for msg in result.new_messages():
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    tc = pending.get(part.tool_call_id)
                    if tc is not None:
                        tc["llm_summary"] = part.content
                        tc["success"] = part.outcome == "success"

    tool_calls = [
        ToolCall(
            tool_call_id=tc["tool_call_id"],
            name=tc["name"],
            args=tc["args"],
            llm_summary=tc.get("llm_summary"),
            success=tc.get("success", True),
        )
        for tc in pending.values()
    ]

    return RunTrace(
        run_id=result.run_id,
        conversation_id=result.conversation_id,
        timestamp=result.timestamp,
        tool_calls=tool_calls,
        usage=Usage.from_run_usage(result.usage),
        traceparent=result._traceparent_value,  # private field, avoids method call
        duration_ms=duration_ms,
    )
```

**Validation:**

```python
# tests/test_agent/test_trace.py

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from aitaem.agent._trace import assemble_trace, RunTrace, Usage, ToolCall, Status


def _make_tool_call_part(name: str, args: dict, tc_id: str):
    part = MagicMock()
    part.__class__.__name__ = "ToolCallPart"
    part.tool_name = name
    part.args = args
    part.tool_call_id = tc_id
    return part


def _make_tool_return_part(name: str, content: str, tc_id: str, outcome: str = "success"):
    part = MagicMock()
    part.__class__.__name__ = "ToolReturnPart"
    part.tool_name = name
    part.content = content
    part.tool_call_id = tc_id
    part.outcome = outcome
    return part


def test_assemble_trace_no_tool_calls():
    """Trace assembles correctly when the LLM answers without calling any tool."""
    result = MagicMock()
    result.run_id = "run-1"
    result.conversation_id = "conv-1"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = []
    result.usage.requests = 1
    result.usage.tool_calls = 0
    result.usage.input_tokens = 100
    result.usage.output_tokens = 50
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    start = datetime.now(timezone.utc)
    with patch("aitaem.agent._trace.datetime") as mock_dt:
        mock_dt.now.return_value = start
        trace = assemble_trace(result, start)

    assert trace.run_id == "run-1"
    assert trace.conversation_id == "conv-1"
    assert trace.tool_calls == []
    assert trace.usage.input_tokens == 100
    assert trace.usage.total_tokens == 150  # computed_field
    assert trace.traceparent is None


def test_assemble_trace_with_tool_call():
    """ToolCallPart + ToolReturnPart pair produces a ToolCall entry."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "compute_metrics"
    tc_part.args = {"metrics": ["ctr"]}
    tc_part.tool_call_id = "tc-42"

    tr_part = MagicMock(spec=ToolReturnPart)
    tr_part.tool_name = "compute_metrics"
    tr_part.content = "Computed 1 metric."
    tr_part.tool_call_id = "tc-42"
    tr_part.outcome = "success"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]

    model_request = MagicMock(spec=ModelRequest)
    model_request.parts = [tr_part]

    result = MagicMock()
    result.run_id = "run-2"
    result.conversation_id = "conv-2"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response, model_request]
    result.usage.requests = 1
    result.usage.tool_calls = 1
    result.usage.input_tokens = 200
    result.usage.output_tokens = 80
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = "00-abc-def-01"

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)

    assert len(trace.tool_calls) == 1
    tc = trace.tool_calls[0]
    assert tc.name == "compute_metrics"
    assert tc.args == {"metrics": ["ctr"]}
    assert tc.llm_summary == "Computed 1 metric."
    assert tc.success is True
    assert tc.result_id is None  # not set in Phase 1
    assert trace.traceparent == "00-abc-def-01"


def test_assemble_trace_failed_tool_call():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "compute_metrics"
    tc_part.args = {}
    tc_part.tool_call_id = "tc-99"

    tr_part = MagicMock(spec=ToolReturnPart)
    tr_part.tool_name = "compute_metrics"
    tr_part.content = "Metric not found."
    tr_part.tool_call_id = "tc-99"
    tr_part.outcome = "failed"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]
    model_request = MagicMock(spec=ModelRequest)
    model_request.parts = [tr_part]

    result = MagicMock()
    result.run_id = "r"
    result.conversation_id = "c"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response, model_request]
    result.usage.requests = 1
    result.usage.tool_calls = 1
    result.usage.input_tokens = 50
    result.usage.output_tokens = 20
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)

    assert trace.tool_calls[0].success is False


def test_assemble_trace_string_args_parsed():
    """String JSON args in ToolCallPart are parsed to dict."""
    import json
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    tc_part = MagicMock(spec=ToolCallPart)
    tc_part.tool_name = "rank_by_value"
    tc_part.args = json.dumps({"result_id": "abc", "limit": 10})
    tc_part.tool_call_id = "tc-str"

    model_response = MagicMock(spec=ModelResponse)
    model_response.parts = [tc_part]

    result = MagicMock()
    result.run_id = "r"
    result.conversation_id = "c"
    result.timestamp = datetime.now(timezone.utc)
    result.new_messages.return_value = [model_response]
    result.usage.requests = 1
    result.usage.tool_calls = 0
    result.usage.input_tokens = 0
    result.usage.output_tokens = 0
    result.usage.cache_read_tokens = 0
    result.usage.cache_write_tokens = 0
    result._traceparent_value = None

    start = datetime.now(timezone.utc)
    trace = assemble_trace(result, start)
    assert trace.tool_calls[0].args == {"result_id": "abc", "limit": 10}
```

**Note on test isolation:** Tests for `assemble_trace` import pydantic-ai types.
They require `pydantic-ai-slim` installed (i.e., any `[agent-*]` extra). CI installs
`aitaem[agent-anthropic,dev]` for the agent tests job (see SF-10).

---

### SF-10: CI test job for agent tests

**File:** `.github/workflows/ci.yml`

Add a dedicated job that installs the `[agent]` extra and runs the agent tests:

```yaml
test-agent:
    name: test-agent (${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install uv
        run: pip install uv
      - name: Install agent dependencies
        run: uv pip install --system -e ".[agent-anthropic,dev]"
      - name: Run agent tests
        run: python -m pytest tests/test_agent/ --cov=aitaem/agent
```

The existing `test` job installs only `.[dev]` (no `pydantic-ai`) so it does
NOT run `tests/test_agent/`. This keeps core tests isolated from the optional
extra, confirming the one-way dependency at the CI level.

---

## Files Changed Summary

| File | Change |
|---|---|
| `aitaem/agent/__init__.py` | New — public exports for primitives layer |
| `aitaem/agent/response.py` | New — `BotResponse[PayloadT]`, `Status` |
| `aitaem/agent/store.py` | New — `ResultEntry`, `ResultStore` |
| `aitaem/agent/trace.py` | New — `RunTrace`, `ToolCall`, `Usage`, `assemble_trace()` |
| `aitaem/agent/base.py` | New — `Bot` abstract base class |
| `aitaem/agent/history.py` | New — `make_bundle()`, `load_bundle()`, serialization helpers |
| `scripts/check_import_graph.py` | New — CI import-graph enforcement script |
| `tests/test_agent/__init__.py` | New — empty |
| `tests/test_agent/test_primitives.py` | New — SF-1 through SF-7 tests |
| `tests/test_agent/test_trace.py` | New — SF-9 tests |
| `tests/test_agent/test_history.py` | New — SF-8 tests |
| `pyproject.toml` | Add `[agent-anthropic]`, `[agent-evals]` extras; update `all` |
| `.github/workflows/ci.yml` | Add `import-graph` and `test-agent` jobs |

No existing files in `aitaem/` (core) are modified. The one-way dependency is
maintained from the start.

---

## Testing Strategy

1. **Before starting:** Run `python -m pytest` to confirm a green baseline on the
   existing suite.

2. **After SF-1 (package structure):** Run
   `python -m pytest tests/test_agent/test_primitives.py::test_aitaem_agent_importable` —
   confirms the package is importable.

3. **After SF-3 (import-graph check):** Run
   `python scripts/check_import_graph.py` — must exit 0.

4. **After SF-4 (trace types):** Run
   `python -m pytest tests/test_agent/test_primitives.py -k "status or usage or tool_call or run_trace"` —
   model construction and property tests pass.

5. **After SF-5 (ResultStore):** Run
   `python -m pytest tests/test_agent/test_primitives.py -k "result_store"` —
   store operations and isolation tests pass.

6. **After SF-6 (BotResponse):** Run
   `python -m pytest tests/test_agent/test_primitives.py -k "bot_response"` —
   generic payload typing confirmed.

7. **After SF-7 (Bot base):** Run
   `python -m pytest tests/test_agent/test_primitives.py -k "bot"` —
   abstract enforcement and delegation confirmed.

8. **After SF-8 (history I/O):** Run
   `python -m pytest tests/test_agent/test_history.py` —
   round-trip serialization with Arrow artifacts confirmed; schema version check passes.

9. **After SF-9 (trace assembly):** Run
   `python -m pytest tests/test_agent/test_trace.py` —
   all four scenarios (no tools, single tool, failed tool, string args) pass.

10. **Full Phase 1 completion:**
    ```bash
    uv pip install -e ".[agent-anthropic,dev]"
    python -m pytest tests/test_agent/ --cov=aitaem/agent --cov-report=term-missing
    python scripts/check_import_graph.py
    python -m pytest tests/ --ignore=tests/test_agent/ --cov=aitaem
    ```
    The final command confirms the existing core suite is unaffected.

11. **Commit** once all tests pass.

---

## Success Criteria

Phase 1 is complete when:

- [ ] `pip install aitaem[agent]` resolves without conflicts (pydantic-ai 2.0.0 + pydantic-evals)
- [ ] `from aitaem.agent import Bot, BotResponse, Status, RunTrace, ToolCall, Usage, ResultEntry, ResultStore` works
- [ ] All models are JSON-serializable and round-trip through `dump_history` / `load_history`
- [ ] `assemble_trace()` correctly extracts tool calls, maps usage, and computes duration from a pydantic-ai result
- [ ] `python scripts/check_import_graph.py` exits 0 on the codebase
- [ ] `tests/test_agent/` passes fully under `python -m pytest`
- [ ] Existing `tests/` (core) suite remains green with no changes to core files
- [ ] CI `import-graph` job and `test-agent` job both defined in `ci.yml`

Phase 2 (QueryBot) is unblocked once these criteria are met.
