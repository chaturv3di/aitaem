from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from aitaem.agent.response import BotResponse
from aitaem.agent.store import ResultEntry, ResultStore

if TYPE_CHECKING:
    from pydantic_ai import Agent


def _register_tool(toolset: Any, tool: Any) -> None:
    """Add one tool (plain callable or pydantic-ai Tool instance) to a FunctionToolset."""
    from pydantic_ai import Tool

    if isinstance(tool, Tool):
        toolset.add_tool(tool)
    else:
        toolset.add_function(tool)


class Bot(ABC):
    """Abstract base class for all aitaem agent bots.

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
                from pydantic_ai.toolsets import FunctionToolset

                toolset = FunctionToolset()
                toolset.add_function(my_default_tool)
                for tool in self._tools:
                    _register_tool(toolset, tool)
                self._toolset = toolset  # REQUIRED — see contract below

                return pydantic_ai.Agent(..., toolsets=[self._toolset])

    Tool composition contract: _build_agent() MUST build a FunctionToolset,
    register self._tools onto it (via the module-level _register_tool()
    helper), and assign it to self._toolset before returning the Agent.
    add_tool() mutates self._toolset in place, so it has nothing to mutate
    otherwise. Bot.__init__ raises TypeError immediately after _build_agent()
    returns if self._toolset is still None, naming the offending subclass.

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
        self._tools: list[Any] = list(tools or [])
        self._store = ResultStore()
        self._message_history: list[Any] = []
        self._runtime_added_tool_names: list[str] = []
        # Contract: subclass _build_agent() MUST assign a FunctionToolset here before
        # returning — enforced by the check below. If you're reading this in a
        # debugger because self._toolset is None, your _build_agent() didn't set it.
        self._toolset: Any = None
        self._agent = self._build_agent()
        if self._toolset is None:
            raise TypeError(
                f"{type(self).__name__}._build_agent() did not set self._toolset. "
                "Concrete Bot subclasses must build a FunctionToolset, register "
                "self._tools onto it, and assign it to self._toolset before "
                "returning the Agent."
            )

    @abstractmethod
    def _build_agent(self) -> Agent:
        """Return a configured pydantic-ai Agent for this bot."""

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
        """Add a tool to this bot's persistent tool set at runtime.

        Takes effect on the next chat()/ask() call. Mutations during an
        in-progress run() are undefined.
        """
        before = set(self._toolset.tools)
        _register_tool(self._toolset, tool)
        self._runtime_added_tool_names.extend(sorted(set(self._toolset.tools) - before))

    def add_bot(self, bot: Bot) -> None:
        """Register another bot as a tool (sugar for add_tool(other.as_tool()))."""
        self.add_tool(bot.as_tool())

    def as_tool(self) -> Any:
        """Return a pydantic-ai Tool that wraps this bot's ask() method."""
        raise NotImplementedError("as_tool() implemented in Phase 5.")

    def get_result(self, result_id: str) -> ResultEntry:
        """Retrieve a stored computation result by ID."""
        return self._store.get(result_id)

    def dump_history(self) -> dict[str, Any]:
        """Serialize conversation history and result artifacts to a JSON-safe dict.

        The returned dict is JSON-serializable (suitable for json.dumps). Ibis refs
        are not serialized; only Arrow artifacts are preserved. Reload with
        load_history() to restore history with Arrow artifacts available via
        get_result(), but get_ibis() will return None on restored entries.

        Tools added at runtime via add_tool() are NOT restored by load_history()
        — their names are recorded in the bundle so load_history() can warn if
        they're missing after reload, but the callables themselves aren't
        portably serializable. Pass them again via tools=[...] or call
        add_tool() on the reloaded bot to restore them.
        """
        from aitaem.agent.history import make_bundle

        return make_bundle(
            self._message_history, self._store, self._runtime_added_tool_names
        )

    @classmethod
    def load_history(cls, data: dict[str, Any], **kwargs: Any) -> Bot:
        """Construct a new bot pre-loaded with a serialized history bundle.

        Args:
            data: Bundle returned by dump_history() on a prior bot instance.
            **kwargs: Constructor arguments for the concrete Bot subclass.

        Returns:
            A new bot instance with _message_history and store populated from
            data. The bot's _agent is rebuilt fresh from **kwargs.

        Warns (UserWarning) if the bundle references tools added via
        add_tool() on the original bot that are not present on the reloaded
        bot — pass them again via tools=[...] in **kwargs, or call add_tool()
        after reload, to silence the warning.
        """
        from aitaem.agent.history import load_bundle

        bot = cls(**kwargs)
        bot._message_history = load_bundle(data, bot._store)
        missing = set(data.get("runtime_added_tool_names", [])) - set(bot._toolset.tools)
        if missing:
            warnings.warn(
                f"load_history() bundle references runtime-added tool(s) not "
                f"present after reload: {sorted(missing)}. Pass them again via "
                f"tools=[...] or call add_tool() to restore them.",
                stacklevel=2,
            )
        return bot

    @property
    def store(self) -> ResultStore:
        return self._store
