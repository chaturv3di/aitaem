# Storing compute results and tool outputs

**Date:** 2026-11-01
**Sources:** aitaem source at commit 75b3481 (`aitaem/agent/common_tools.py`, `aitaem/agent/store.py`,
`aitaem/agent/base.py`, `aitaem/query/executor.py`, `aitaem/connectors/connection.py`)
**Used by:** `plans/agent_module/ARCHITECTURE.md` §1, §4, `plans/42-aitaem-agent-v3.md`

Related notes: [42-subagent-mechanics](42-subagent-mechanics.md)

---

## 1. What v2 does

- `compute_metrics` (`common_tools.py:262-275`) runs `MetricCompute.compute(...)`, which returns a
  lazy `ibis.Table`, then immediately calls `ibis_table.to_pyarrow()` on the **whole** result and
  stores both the Arrow table and the Ibis ref (`ResultStore.store_tabular`).
- So v2 already pulls every full compute result into process memory. The two-copy store does not
  protect against large results.
- Derived results (ranking, filtering, distributions in `query_tools.py`) are small and stored as
  Arrow only.
- On `load_history`, restored entries have no Ibis ref (`base.py:157`: "`get_ibis()` will return
  None on restored entries").

## 2. Size risk

Compute results can be tens or hundreds of thousands of rows (e.g. by-entity metrics or many
slice values). Materializing them locally, whether into memory or a local DuckDB file, moves the
whole result out of the warehouse on every compute.

## 3. Options for compute results

| Option | Behaviour | Cost |
|---|---|---|
| (a) Local copy below a row threshold | Count rows first; store locally when small | Extra count query; a threshold to tune; two code paths |
| (b) Backend-side cache (Ibis `.cache()`) | Temp table on the source backend | Support per backend unverified **[verify]** |
| (c) Keep the lazy Ibis ref; re-execute on use | Every stats/preview call runs the metric query on the backend | Repeated warehouse execution |

With (c), stats and previews run on the backend because Ibis compiles them to backend SQL; only
their small outputs (aggregates, top-N, previews) come back to the process. The Plans 36/37
distribution tools already work this way.

An Ibis ref cannot be serialized, so it does not survive saving and reloading a session (as in v2,
§1). The architecture already marks missing artifacts `lost` on load; the agent can recompute.

## 4. Results that span several backends

Core already handles this inside one `compute()` call. When query groups span backends,
`QueryExecutor` (`executor.py:36-97`) materializes each group and loads the combined result into a
DuckDB connection from `ConnectionManager._get_cross_backend_conn` (`connection.py:460`), which is
backed by a temp file in `ConnectionManager`'s `tmp_dir` (or in-memory when `tmp_dir=None`). That
factory is private API.

## 5. Concurrency

`ConnectionManager.requires_compute_lock` (`connection.py:514`) is true whenever DuckDB is
registered or several backends are configured, because Ibis DuckDB is not thread-safe. v2 holds a
process-wide lock around compute in that case (`common_tools.py:262`), so parallel executors
computing against DuckDB sources run one at a time.
