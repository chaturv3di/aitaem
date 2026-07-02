from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from aitaem.agent.response import BotResponse
from aitaem.agent.store import ResultEntry, ResultStore

if TYPE_CHECKING:
    from pydantic_ai import Agent


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
        self._message_history: list[Any] = []
        self._agent = self._build_agent()

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
        """Add a tool to this bot's default tool set at runtime."""
        raise NotImplementedError("add_tool() implemented in Phase 5.")

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
        """
        from aitaem.agent.history import make_bundle

        return make_bundle(self._message_history, self._store)

    @classmethod
    def load_history(cls, data: dict[str, Any], **kwargs: Any) -> Bot:
        """Construct a new bot pre-loaded with a serialized history bundle.

        Args:
            data: Bundle returned by dump_history() on a prior bot instance.
            **kwargs: Constructor arguments for the concrete Bot subclass.

        Returns:
            A new bot instance with _message_history and store populated from
            data. The bot's _agent is rebuilt fresh from **kwargs.
        """
        from aitaem.agent.history import load_bundle

        bot = cls(**kwargs)
        bot._message_history = load_bundle(data, bot._store)
        return bot

    @property
    def store(self) -> ResultStore:
        return self._store
