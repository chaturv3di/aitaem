"""Pins aitaem.agent.__all__ against docs/api/agent.md's per-symbol directives.

docs/api/agent.md is hand-maintained (one mkdocstrings directive per symbol, not an
auto-discovered module-level directive), so a new export added to __all__ has no
rendered entry until someone adds one. This test turns that drift into a CI failure
instead of a silent docs gap.
"""

from __future__ import annotations

from aitaem import agent

# Keep in sync with docs/api/agent.md's three subsections (Primitives / QueryBot /
# DefinitionBot) — this is the enforcement mechanism for that sync, not a duplicate.
DOCUMENTED_SYMBOLS = {
    # Primitives
    "Bot", "BotResponse", "Status", "RunTrace", "ToolCall", "Usage",
    "ResultStore", "ResultEntry", "TabularEntry", "TextEntry", "WrongEntryKindError",
    # QueryBot
    "QueryBot", "QueryResponse", "QueryPayload", "MetricIntent", "ResolvedSpec",
    "ExactMatch", "NearMiss", "SpecMatchResult", "RecordIntentResult",
    "ResolveIntentResult", "SpecResolver",
    # DefinitionBot
    "DefinitionBot", "DefinitionResponse", "DefinitionPayload", "DefinitionIntent",
    "SpecDraft", "ColumnInfo", "ListTablesResult", "DescribeTableResult",
    "DraftSpecResult", "ValidateSpecResult", "ValidationIssue",
}


def test_all_matches_documented_symbols():
    assert set(agent.__all__) == DOCUMENTED_SYMBOLS
