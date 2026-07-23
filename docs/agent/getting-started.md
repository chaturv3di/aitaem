# Agent: Getting Started

`aitaem.agent` is an optional install that ships two LLM-facing convenience bots on top
of the deterministic compute layer: `QueryBot` (answer questions against your metric
catalog) and `DefinitionBot` (draft and validate new specs from natural language).

## Installation

```bash
pip install "aitaem[agent-anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...
```

`agent-anthropic` is the tested, concrete install path — every example in this repo runs
against an Anthropic model. If you plan to use a different or multiple model providers,
install the provider-neutral superset instead:

```bash
pip install "aitaem[agent]"
```

Both bots accept any [pydantic-ai](https://ai.pydantic.dev/) model string (e.g.
`"openai:gpt-4o"`), so `agent` — which also pulls in the OpenAI SDK — is the extra to
reach for once you're not exclusively on Anthropic.

## QueryBot quick start

```python
from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import QueryBot

spec_cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
)

conn_mgr = ConnectionManager()
conn_mgr.add_connection("duckdb", path="examples/data/ad_campaigns.duckdb")

bot = QueryBot(
    model="anthropic:claude-haiku-4-5-20251001",
    spec_cache=spec_cache,
    connection_manager=conn_mgr,
)

response = await bot.chat("What was total revenue and ROAS across all campaigns?")
print(response.narrative)
```

## DefinitionBot quick start

```python
from aitaem.connectors import ConnectionManager
from aitaem.specs import SpecCache
from aitaem.agent import DefinitionBot

spec_cache = SpecCache.from_yaml(
    metric_paths="examples/metrics/",
    slice_paths="examples/slices/",
)

conn_mgr = ConnectionManager()
conn_mgr.add_connection("duckdb", path="examples/data/ad_campaigns.duckdb")

bot = DefinitionBot(
    model="anthropic:claude-haiku-4-5-20251001",
    spec_cache=spec_cache,
    connection_manager=conn_mgr,
)

response = await bot.ask(
    "Define a metric called avg_cpc for average cost per click — "
    "total ad spend divided by total clicks."
)
print(response.narrative)
print(response.payload.yaml_string)
```

## Full examples

The [`examples/`](https://github.com/chaturv3di/aitaem/tree/main/examples) directory has
runnable scripts and notebooks, numbered in a suggested reading order:

| # | Example | What it covers |
|---|---------|-----------------|
| 01 | `definition_bot_example` | The four-step spec-definition workflow, direct YAML parsing and LLM-assisted drafting |
| 02 | `query_bot_example` | A multi-turn `QueryBot` conversation |
| 03 | `intent_resolution_example` | A deep dive into `QueryBot`'s intent → resolve → compute gate |
| 04 | `evaluating_agents_example` | Writing `pydantic_evals` evaluations against a live model |

## `SetupBot`

A third convenience bot — a connection-configuration wizard — is planned but not yet
shipped. It's a v1.x deliverable, not part of this release.

## `tenant_id`

Both bots accept an optional `tenant_id: str | None` constructor parameter. It is an
OpenAI prompt-cache routing key — **not** a security or isolation boundary. When set, an
OpenAI-backed bot passes it through as `openai_prompt_cache_key` so requests from the
same tenant route to the same server-side cache pool.

When omitted, it falls back to a hash of the visible metric/slice/segment names in your
`SpecCache`. Two `spec_cache`s with the same visible catalog (e.g. two users under the
same RBAC permission set) land in the same cache routing lane automatically; different
visible catalogs land in different lanes — with no configuration needed.

!!! warning "This is not a tenancy or isolation feature"
    `tenant_id` only affects cache routing efficiency. The agent module never stores
    credentials, persists conversation history beyond what you explicitly serialize via
    `dump_history()`, manages multi-tenancy or RBAC, or holds connections other than the
    `ConnectionManager` you provide. It does not create isolation between tenants —
    separate tenants need separate `ConnectionManager`/`SpecCache`/bot instances,
    enforced by your application, regardless of what `tenant_id` you pass.

    Concurrent `chat()`/`ask()` calls on the *same* bot instance are also unsupported —
    one call at a time per instance. Concurrency lives at the caller level: N users need
    N bot instances, not N concurrent calls on one shared instance.

See [Stability & Limitations](stability.md) for the full reference.

## Next steps

- [Building Your Own Bot](building-your-own-bot.md) — use the primitives layer directly
- [Evaluating Your Agent](evaluating-your-agent.md) — write `pydantic_evals` evaluations
- [Agent API Reference](../api/agent.md) — full class and type documentation
