from aitaem.agent.response import BotResponse, Status
from aitaem.agent.store import (
    ResultEntry,
    ResultStore,
    TabularEntry,
    TextEntry,
    WrongEntryKindError,
)
from aitaem.agent.trace import RunTrace, ToolCall, Usage
from aitaem.agent.base import Bot
from aitaem.agent.query_bot import QueryBot, QueryResponse
from aitaem.agent.query_types import (
    QueryPayload,
    MetricIntent,
    ResolvedSpec,
    ExactMatch,
    NearMiss,
    SpecMatchResult,
    RecordIntentResult,
    ResolveIntentResult,
)
from aitaem.agent.resolver import SpecResolver
from aitaem.agent.definition_bot import DefinitionBot, DefinitionResponse
from aitaem.agent.definition_types import (
    DefinitionPayload,
    DefinitionIntent,
    SpecDraft,
    ColumnInfo,
    ListTablesResult,
    DescribeTableResult,
    DraftSpecResult,
    ValidateSpecResult,
    ValidationIssue,
)

__all__ = [
    # Phase 1 primitives
    "Bot",
    "BotResponse",
    "Status",
    "RunTrace",
    "ToolCall",
    "Usage",
    "ResultEntry",
    "ResultStore",
    # P3.0b — ResultStore discriminated union
    "TabularEntry",
    "TextEntry",
    "WrongEntryKindError",
    # Phase 2 — QueryBot
    "QueryBot",
    "QueryResponse",
    "QueryPayload",
    # v0.2 — intent-gated resolution
    "MetricIntent",
    "ResolvedSpec",
    "ExactMatch",
    "NearMiss",
    "SpecMatchResult",
    "RecordIntentResult",
    "ResolveIntentResult",
    "SpecResolver",
    # Phase 3 — DefinitionBot
    "DefinitionBot",
    "DefinitionResponse",
    "DefinitionPayload",
    "DefinitionIntent",
    "SpecDraft",
    "ColumnInfo",
    "ListTablesResult",
    "DescribeTableResult",
    "DraftSpecResult",
    "ValidateSpecResult",
    "ValidationIssue",
]
