# Agent: Tool Groups

The tools `AitaemAgent` gives its roles are grouped into four pydantic-ai `FunctionToolset`s, which
you can give to your own agents. Each tool call writes an artifact and returns a small
`ToolResult`: the artifact ID, a status, a summary, and a few facts — never a full result table.

| Toolset | Tools | Touches |
|---|---|---|
| `stats_toolset` | `rank_by_value`, `distribution_summary`, `filter_by_threshold`, `filter_by_value`, `filter_by_result`, `period_over_period`, `contribution_share` | Compute results and tables only |
| `data_toolset` | `list_tables`, `describe_table`, `sample_rows`, `column_distribution`, `date_range` | Source backends through the `ConnectionManager`, read-only |
| `aitaem_toolset` | `connect_source`, `list_specs`, `find_specs`, `read_spec`, `check_compatibility`, `validate_spec`, `commit_spec`, `delete_spec`, `compute_metrics` | The `SpecCache`, the `ConnectionManager`, and `MetricCompute` |
| `artifact_toolset` | `list_artifacts`, `describe_artifact`, `read_artifact` | The artifact store |

## Composing toolsets

Every tool takes `SessionDeps` as its run dependencies:

```python
from pydantic_ai import Agent

from aitaem import ConnectionManager, SpecCache
from aitaem.agent import ArtifactStore, SessionDeps, aitaem_toolset, artifact_toolset, stats_toolset

store = ArtifactStore()                      # a new session directory under the temp dir
deps = SessionDeps(
    store=store,
    connection_manager=ConnectionManager.from_yaml("examples/connections.yaml"),
    spec_cache=SpecCache.from_yaml(metric_paths="examples/metrics/"),
    spec_dir=store.spec_dir,
)

agent = Agent(
    "anthropic:claude-sonnet-5-5",
    deps_type=SessionDeps,
    toolsets=[aitaem_toolset, stats_toolset, artifact_toolset],
    instructions="Compute the metrics the user asks for, then read the values you need.",
)
result = await agent.run("What is total revenue by campaign type?", deps=deps)

for ref in store.refs():
    print(ref.id, ref.kind, ref.type_tag, ref.summary)
```

## What the tools guarantee

- **Lazy compute results.** `compute_metrics` stores a lazy Ibis expression on the source
  backend. Row-preserving statistics (filters, `period_over_period`, `contribution_share`, and
  `rank_by_value` with `group_by`) stay lazy on that backend; bounded ones (`distribution_summary`,
  `rank_by_value` without `group_by`) write small tables to the session's DuckDB file.
  `read_artifact` returns only the columns and rows it asks for.
- **One query at a time on DuckDB.** Ibis DuckDB connections are not thread-safe, so tools that
  execute a query hold `SessionDeps.compute_lock` whenever the `ConnectionManager` has DuckDB
  registered or several backends configured.
- **Percentile statistics come from catalog metrics.** `column_distribution` takes a metric name
  and summarizes a column of its source table; `date_range` takes any table and returns bounds
  only.
- **Specs are saved through `SpecCache.save()`.** `commit_spec` and `delete_spec` write the whole
  catalog to `<spec_dir>/metrics`, `<spec_dir>/slices`, and `<spec_dir>/segments`.
- **No credentials.** `connect_source` connects DuckDB and BigQuery; configure Postgres in the
  `ConnectionManager` you pass in.
