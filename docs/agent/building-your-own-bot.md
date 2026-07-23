# Agent: Building Your Own Bot

`QueryBot` and `DefinitionBot` are opinionated assemblies built on a primitives layer
that's public in its own right. Reach for the primitives directly when you need a bot
whose tools, prompt, or output shape don't fit either convenience bot — the primitives
are designed from the start to support this, not bolted on as an escape hatch.

## Installation

```bash
pip install "aitaem[agent-core]"
```

`agent-core` installs pydantic-ai without a provider SDK — enough to build and test bots
against `TestModel`/`FunctionModel`, as in the example below, or against a provider SDK
you install separately (`pip install anthropic`, `pip install openai`, etc.).

## The `Bot` subclassing contract

Every bot subclasses `Bot` and implements `_build_agent()`, which must:

1. Build a `pydantic_ai.toolsets.FunctionToolset`.
2. Register every tool in `self._tools` onto it, via `aitaem.agent.base._register_tool()`
   (handles both plain callables and pre-built `pydantic_ai.Tool` instances).
3. Assign the toolset to `self._toolset` — `Bot.__init__` raises `TypeError` immediately
   if this isn't set, naming the offending subclass.
4. Return a configured `pydantic_ai.Agent`.

```python
from datetime import datetime, timezone

from pydantic_ai import Agent
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.models.test import TestModel

from aitaem.agent import Bot, BotResponse, Status
from aitaem.agent.base import _register_tool
from aitaem.agent.trace import assemble_trace


def word_count(text: str) -> int:
    """Count words in text."""
    return len(text.split())


class EchoBot(Bot):
    def _build_agent(self) -> Agent:
        toolset = FunctionToolset()
        toolset.add_function(word_count)
        for tool in self._tools:
            _register_tool(toolset, tool)
        self._toolset = toolset  # required — see contract above

        return Agent(model=self._model, toolsets=[toolset], instructions="Answer briefly.")

    async def ask(self, message: str, *, extra_tools=None) -> BotResponse:
        run_start = datetime.now(timezone.utc)
        result = await self._agent.run(message)
        return BotResponse(
            status=Status.ok,
            narrative=str(result.output),
            trace=assemble_trace(result, run_start),
        )


bot = EchoBot(model=TestModel())
response = await bot.ask("How many words are in this sentence?")
print(response.narrative)
print([tc.name for tc in response.trace.tool_calls])
```

Swap `TestModel()` for a real model string (e.g. `"anthropic:claude-haiku-4-5-20251001"`)
once you've installed the matching provider SDK.

## `BotResponse`, `Status`, `RunTrace`, `ResultStore`

Every bot method returns a `BotResponse[PayloadT]` — a frozen Pydantic model with
`status: Status` (`ok` / `empty` / `refused` / `error`), a `narrative: str`, a
`trace: RunTrace`, and an optional typed `payload`. `aitaem.agent.trace.assemble_trace()`
builds a `RunTrace` (tool calls, token usage, duration) from a completed
`pydantic_ai.AgentRunResult` — the example above uses it directly; `QueryBot` and
`DefinitionBot` call it internally.

Any tool that produces a result too large for the LLM-facing summary (a metric table, a
draft spec) should write it to `self._store` (a `ResultStore`) and return a compact
summary plus the `result_id`. Callers dereference the full result via
`bot.get_result(result_id)`, which returns a `TabularEntry` (Arrow table + optional Ibis
ref) or `TextEntry` (string + content type) — narrow with `isinstance()` or use
`ResultStore.get_tabular()`/`get_text()` for a typed getter that raises
`WrongEntryKindError` on a kind mismatch.

## Tool composition

Three surfaces add tools to a bot without subclassing further:

```python
# Construction-time — folded into the bot's default toolset
bot = QueryBot(model=..., spec_cache=..., connection_manager=..., tools=[my_tool])

# Runtime, persistent — takes effect on the next chat()/ask() call
bot.add_tool(another_tool)

# Per-call, ephemeral — additive to construction-time tools, not a replacement
response = await bot.ask("...", extra_tools=[one_off_tool])
```

Tool-name collisions (construction-time duplicates, or a runtime/per-call tool sharing a
name with a default tool) raise `pydantic_ai.exceptions.UserError` at the point of
registration — no silent auto-namespacing.

## History serialization

```python
bundle = bot.dump_history()          # JSON-serializable dict
# ... persist bundle, e.g. json.dumps(bundle) to disk/DB ...
restored = QueryBot.load_history(bundle, model=..., spec_cache=..., connection_manager=...)
```

`dump_history()` preserves Arrow artifacts but not live Ibis refs — `get_result()` on a
restored bot works, but `get_ibis()` returns `None` for restored entries. Tools added at
runtime via `add_tool()` are **not** restored automatically; `load_history()` warns if
the bundle references runtime-added tools that are missing after reload. Pass them again
via `tools=[...]` or call `add_tool()` post-reload to silence the warning.

## Context-window management

For long-running sessions where history may exceed the model's context limit, pass a
history processor via `capabilities` when constructing the `Agent` in `_build_agent()`:

```python
from pydantic_ai.capabilities import ProcessHistory, ReinjectSystemPrompt

def _build_agent(self) -> Agent:
    return Agent(
        model=self._model,
        capabilities=[
            ReinjectSystemPrompt(replace_existing=True),
            ProcessHistory(trim_old_messages),
        ],
    )
```

The processor receives the full message list before each model request (including
mid-tool-call-loop steps) and returns a modified list. Only trim complete tool-call pairs
(`ToolCallPart` + its matching `ToolReturnPart`) as a unit — dropping a `ToolReturnPart`
without its `ToolCallPart` violates provider API constraints
([pydantic-ai issue #2050](https://github.com/pydantic/pydantic-ai/issues/2050)). No
built-in trimmer ships with `aitaem`; this is a reference pattern to implement against,
not a shipped utility.

## Next steps

- [Evaluating Your Agent](evaluating-your-agent.md) — write `pydantic_evals` evaluations
  against a bot you've built
- [Stability & Limitations](stability.md) — what's semver-stable if you're building on
  the primitives layer
- [Agent API Reference](../api/agent.md) — full primitives class and type documentation
