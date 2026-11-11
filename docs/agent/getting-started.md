# Agent: Getting Started

`aitaem.agent` is an optional install that puts one multi-turn agent session, `AitaemAgent`, on
top of the deterministic compute layer. You talk to it with `chat()`; it connects sources, designs
metric, slice, and segment specs with you, and answers analytical questions by computing catalog
metrics and running statistics on the results.

## Installation

The agent extras need Python ≥ 3.11 (they depend on `pydantic-ai-slim` ≥ 2.55); core `aitaem`
still runs on Python 3.10.

```bash
pip install "aitaem[agent-anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...
```

`agent-anthropic` is the tested install path. For other or several model providers, install the
provider-neutral superset, `pip install "aitaem[agent]"`; `AitaemAgent` accepts any
[pydantic-ai](https://ai.pydantic.dev/) model string.

## Quick start

```python
from aitaem import ConnectionManager, SpecCache
from aitaem.agent import AitaemAgent

specs = SpecCache.from_yaml(metric_paths="examples/metrics/", slice_paths="examples/slices/")
connections = ConnectionManager.from_yaml("examples/connections.yaml")

agent = AitaemAgent(connections, specs)
response = await agent.chat("Which region has the highest click-through rate?")
print(response.status, response.response)
for attachment in response.attachments:
    print(attachment.artifact_id, attachment.type_tag, attachment.reason)
```

`AitaemAgent(connection_manager=None, spec_cache=None, *, model, executor_model, spec_dir=None)`:

- `model` (default `"anthropic:claude-sonnet-5-5"`) runs the main session and the coordinators;
  `executor_model` (default `"anthropic:claude-haiku-5-5"`) runs the executors.
- Without a `ConnectionManager`, the agent starts with none connected and can connect DuckDB or
  BigQuery sources itself. Without a `SpecCache`, it starts with an empty catalog.
- Specs the agent saves go to `<spec_dir>/metrics`, `<spec_dir>/slices`, and
  `<spec_dir>/segments`. `spec_dir` is a local path or an fsspec URL; it defaults to the session
  directory, which the OS removes eventually, so pass one to keep the specs.

## How a session works

The **main session** talks to you. It answers questions about results it already holds with the
artifact and stats tools, and it launches a **coordinator** for each task:

| Task | Coordinator | How it runs |
|---|---|---|
| Connect a DuckDB or BigQuery source | connections | Runs on its own and hands back what it connected, or the settings it needs |
| Create, change, or delete specs | spec | Talks with you directly until the specs are done, saving each spec you agree to |
| Answer an analytical question | analysis | Finds the specs through parallel executors, computes, runs statistics, and hands back the answer |

When a coordinator needs more from you, the main session asks and launches it again with your
answers, reusing the results it already computed.

## Responses, artifacts, and traces

Every tool call writes an **artifact** to a session directory under the system temp directory.
Compute results stay lazy on their source backend; statistics and reads run there, and only their
outputs come back.

- `Response`: `response` (text), `status` (`ok`, `empty`, `refused`, `error`), `attachments`
  (artifact ID, type tag, reason), and `trace_id`. `refused` means no catalog spec answers the
  question precisely; the agent does not approximate one.
- `agent.trace(trace_id)` returns the turn's `RunTrace`: every run (main session, coordinators,
  executors) with its tool calls and its usage, cache reads and writes included.
- `agent.store` is the `ArtifactStore`: `refs()`, `get(id)`, `read(id)`, and `index_lines()`.

## Saving and restoring a session

```python
data = agent.dump()        # JSON-ready: conversations, route, and the artifact manifest
agent = AitaemAgent.load(data, connections, specs)
...
agent.cleanup()            # removes the session directory now
```

Compute results are lazy expressions and do not survive `load()`: they, and any artifact whose file
is gone, are marked `lost`, and the agent recomputes them when needed.

## Full example

[`examples/01_aitaem_agent_example.py`](https://github.com/chaturv3di/aitaem/blob/main/examples/01_aitaem_agent_example.py)
runs a two-turn session over the ad campaigns dataset and prints each turn's runs and the
artifact index.

## Next steps

- [Tool Groups](tool-groups.md) — use the stats, data, aitaem, and artifact tools in your own
  pydantic-ai agents
- [Evaluating Your Agent](evaluating-your-agent.md) — write `pydantic_evals` evaluations
- [Stability & Limitations](stability.md)
- [Agent API Reference](../api/agent.md)
