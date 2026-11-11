# Agent: Stability & Limitations

## Stability guarantees

`aitaem[agent]`'s public surface carries the same semver promise as core `aitaem`, with one
deliberate exception:

| Surface | Stability |
|---|---|
| `AitaemAgent`, `Response`, `ResponseAttachment`, `Status` | Semver-stable |
| `RunTrace`, `ToolCall`, `Usage` field shapes | Semver-stable — the [eval substrate](evaluating-your-agent.md) depends on them |
| `ArtifactRef`, `ArtifactStore`, `SessionDeps`, `ToolResult` | Semver-stable |
| The four toolsets: tool names, parameters, and `ToolResult` facts | Semver-stable |
| Role instructions (`instructions.md`) and tool descriptions | Public, but **not** semver-stable — tuning is expected across patch and minor releases |

Don't pin application behavior to the wording of the instructions; a patch release may change it.

!!! note "An instruction change is also a cache reset"
    Instructions and tool descriptions form the cached prompt prefix, so changing them costs one
    cache write the next time each role runs.

## Removed in v3.0

The v2 bots and their public names were removed from `aitaem.agent`: `Bot`, `BotResponse`,
`ResultEntry`, `ResultStore`, `TabularEntry`, `TextEntry`, `WrongEntryKindError`, `QueryBot`,
`QueryResponse`, `QueryPayload`, `MetricIntent`, `ResolvedSpec`, `ExactMatch`, `NearMiss`,
`SpecMatchResult`, `RecordIntentResult`, `ResolveIntentResult`, `SpecResolver`, `DefinitionBot`,
`DefinitionResponse`, `DefinitionPayload`, `DefinitionIntent`, `SpecDraft`, `ColumnInfo`,
`ListTablesResult`, `DescribeTableResult`, `DraftSpecResult`, `ValidateSpecResult`, and
`ValidationIssue`. `ToolCall.result_id` was renamed `ToolCall.artifact_id`. `AitaemAgent`
replaces both bots.

## Requirements

The agent extras (`agent-core`, `agent-anthropic`, `agent`, `agent-evals`) need Python ≥ 3.11 and
`pydantic-ai-slim` ≥ 2.55. Core `aitaem` still supports Python 3.10.

## Limitations

- The agent never stores credentials or holds connections other than through the
  `ConnectionManager`. `connect_source` connects DuckDB and BigQuery; Postgres needs a password,
  so configure it in the `ConnectionManager` you pass to `AitaemAgent`.
- The session directory lives under the system temp directory, and the OS may remove it. Pass a
  `spec_dir` to keep saved specs, and export any artifact you want to keep.
- Compute results do not survive `dump()`/`load()`; they are marked `lost` and recomputed.
- Concurrent `chat()` calls on the same `AitaemAgent` are unsupported: one call at a time per
  instance, one instance per active session.
- Multi-tenancy, access control, and isolation between users are your application's
  responsibility.
