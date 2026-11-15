# Agent

!!! info "Optional install"
    The agent module is not part of the core `aitaem` install. See
    [Getting Started](../agent/getting-started.md) for install instructions and a quick start.

## Session

### AitaemAgent

::: aitaem.agent.session.AitaemAgent

### Response

::: aitaem.agent.session.Response

### ResponseAttachment

::: aitaem.agent.session.ResponseAttachment

### Status

::: aitaem.agent.trace.Status

## Traces

### RunTrace

::: aitaem.agent.trace.RunTrace

### AgentRun

::: aitaem.agent.trace.AgentRun

### ToolCall

::: aitaem.agent.trace.ToolCall

### Usage

::: aitaem.agent.trace.Usage

## Artifacts and tools

See [Tool Groups](../agent/tool-groups.md) for composing the toolsets in your own agents.

### ArtifactStore

::: aitaem.agent.artifacts.ArtifactStore

### ArtifactRef

::: aitaem.agent.artifacts.ArtifactRef

### SessionDeps

::: aitaem.agent.deps.SessionDeps

### ToolResult

::: aitaem.agent.tools.ToolResult

### Toolsets

| Toolset | Module |
|---|---|
| `stats_toolset` | `aitaem.agent.tools.stats` |
| `data_toolset` | `aitaem.agent.tools.data` |
| `aitaem_toolset` | `aitaem.agent.tools.aitaem` |
| `artifact_toolset` | `aitaem.agent.tools.artifacts` |
