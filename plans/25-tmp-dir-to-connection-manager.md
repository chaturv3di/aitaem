# Plan 25 — Move `tmp_dir` from `MetricCompute` to `ConnectionManager`

Relocates the cross-backend temporary DuckDB management (currently on
`MetricCompute`) to `ConnectionManager`, where it belongs conceptually: CM is
the single authority over all backend connections in a session, including the
ephemeral DuckDB used to reconcile cross-backend result sets.

---

## Motivation

Plan 22 introduced a `tmp_dir` parameter on `MetricCompute.__init__` to control
where the temporary DuckDB file is written when a `compute()` call spans
multiple source backends. The parameter ended up in the wrong class:

- `MetricCompute` is responsible for *computing metrics from specs*. It should
  not care how backends are connected or where intermediate storage lives.
- `ConnectionManager` already owns all backend connections. A temporary DuckDB
  used for cross-backend materialisation is just another managed connection —
  it should be created, tracked, and torn down by `ConnectionManager`.

Moving `tmp_dir` into `ConnectionManager`:
- Keeps `MetricCompute.__init__` clean (2 parameters: `spec_cache` +
  `connection_manager`).
- Makes `close_all()` the single teardown point for *all* connections,
  including the cross-backend DuckDB.
- Lets callers who use `from_yaml()` still pass `tmp_dir` without touching
  `MetricCompute`.
- Removes the `__del__` method from `MetricCompute` entirely.

---

## Scope

| Item | Description |
|------|-------------|
| CM-1 | `ConnectionManager.__init__` gains `tmp_dir: str \| None = "/tmp"` |
| CM-2 | `ConnectionManager.from_yaml()` gains `tmp_dir: str \| None = "/tmp"` keyword arg |
| CM-3 | `ConnectionManager._get_cross_backend_conn()` — new private method; lazy-init |
| CM-4 | `ConnectionManager.close_all()` extended to tear down cross-backend DuckDB |
| CM-5 | `ConnectionManager.__del__` — new safety-net for temp file deletion |
| MC-1 | `MetricCompute.__init__` `tmp_dir` parameter removed |
| MC-2 | `MetricCompute._get_cross_backend_conn()` removed; `__del__` removed |
| MC-3 | `MetricCompute.compute()` passes `self.connection_manager._get_cross_backend_conn` to executor |
| TEST | New `ConnectionManager` tests; existing `MetricCompute` fixtures unchanged |
| DOCS | `docs/api/connectors.md`, `docs/api/insights.md`, `docs/api/index.md`, `docs/user-guide/connectors.md`, `docs/user-guide/computing-metrics.md`, `docs/getting-started.md`, `docs/changelog.md` |

Out of scope: Any change to `QueryExecutor`; any change to `IbisConnector`;
any change to how specs or queries are built.

---

## Background & Critical Observations

### Current cross-backend flow (post plan 22)

```
MetricCompute.__init__(spec_cache, conn_mgr, tmp_dir="/tmp")
  stores self._tmp_dir, self._cross_backend_conn, self._cross_backend_db_path

MetricCompute._get_cross_backend_conn() → ibis.BaseBackend
  lazily creates temp DuckDB file (or :memory:) on first call

MetricCompute.compute(...)
  executor.execute(groups, cross_backend_conn_factory=self._get_cross_backend_conn)

MetricCompute.__del__()
  sets self._cross_backend_conn = None
  os.unlink(self._cross_backend_db_path)  # temp file cleanup
```

### Target flow (after this plan)

```
ConnectionManager.__init__(tmp_dir="/tmp")
  stores self._tmp_dir, self._cross_backend_conn, self._cross_backend_db_path

ConnectionManager._get_cross_backend_conn() → ibis.BaseBackend
  lazily creates temp DuckDB file (or :memory:) on first call

ConnectionManager.close_all()
  closes all IbisConnector connections  (existing behaviour)
  closes + deletes the cross-backend DuckDB  (new)

ConnectionManager.__del__()
  safety-net: deletes temp file if close_all() was not called

MetricCompute.__init__(spec_cache, conn_mgr)   ← no tmp_dir
  no cross-backend state

MetricCompute.compute(...)
  executor.execute(groups,
      cross_backend_conn_factory=self.connection_manager._get_cross_backend_conn)
```

### `from_yaml()` signature

`ConnectionManager.from_yaml(yaml_path)` currently constructs a bare
`ConnectionManager()`. After this plan it constructs `cls(tmp_dir=tmp_dir)`:

```python
@classmethod
def from_yaml(cls, yaml_path: str, tmp_dir: str | None = "/tmp") -> ConnectionManager:
    manager = cls(tmp_dir=tmp_dir)
    ...
```

`tmp_dir` is *not* parsed from the YAML file. It is an operational parameter
(where to put temp files on this machine), not a connection-configuration
parameter (how to talk to a backend). Keeping it out of YAML avoids polluting
the connection schema and keeps `validate_backend_config()` unchanged.

### Breaking changes

| Change | Severity | Migration |
|--------|----------|-----------|
| `MetricCompute.__init__` `tmp_dir` parameter removed | Breaking for callers who pass it explicitly | Remove `tmp_dir` arg; pass it to `ConnectionManager()` instead |
| `MetricCompute._get_cross_backend_conn` removed | Private — not breaking for library consumers |  |
| `ConnectionManager.__init__` gains optional `tmp_dir` | Non-breaking — default matches old `MetricCompute` default |  |
| `ConnectionManager.from_yaml` gains optional `tmp_dir` kwarg | Non-breaking |  |

`tmp_dir` was introduced in plan 22 (v0.4.0), which was not yet released.
Both the removal from `MetricCompute` and the addition to `ConnectionManager`
can therefore be shipped together in the same version without a separate
deprecation window.

### `close_all()` and `__del__` details

`close_all()` disconnects the cross-backend backend before deleting the file,
to avoid leaving a lock on the DuckDB file on some platforms:

```python
def close_all(self) -> None:
    for connector in self._connections.values():
        connector.close()
    # cross-backend teardown
    if self._cross_backend_conn is not None:
        try:
            self._cross_backend_conn.disconnect()
        except Exception:
            pass
        self._cross_backend_conn = None
    if self._cross_backend_db_path is not None:
        try:
            os.unlink(self._cross_backend_db_path)
        except OSError:
            pass
        self._cross_backend_db_path = None
```

`__del__` is a safety net only — it handles the case where the caller never
calls `close_all()`. It does *not* attempt to disconnect the backend (the
Python GC order is non-deterministic at shutdown):

```python
def __del__(self) -> None:
    if self._cross_backend_db_path is not None:
        try:
            os.unlink(self._cross_backend_db_path)
        except OSError:
            pass
```

---

## Implementation Sub-Features

Implement in this order. Each SF is independently testable before proceeding.

---

### SF-1: Add cross-backend management to `ConnectionManager`

**Files changed:** `aitaem/connectors/connection.py`

**Changes:**

1. Add imports at the top:
   ```python
   import os
   import tempfile
   import ibis
   ```
   (`os` may already be imported; check before adding.)

2. Update `__init__`:
   ```python
   def __init__(self, tmp_dir: str | None = "/tmp") -> None:
       self._connections: dict[str, IbisConnector] = {}
       self._tmp_dir = tmp_dir
       self._cross_backend_conn: ibis.BaseBackend | None = None
       self._cross_backend_db_path: str | None = None
   ```

3. Update `from_yaml`:
   ```python
   @classmethod
   def from_yaml(cls, yaml_path: str, tmp_dir: str | None = "/tmp") -> ConnectionManager:
       ...
       manager = cls(tmp_dir=tmp_dir)   # was: manager = cls()
       ...
   ```
   No other change inside `from_yaml`.

4. Add `_get_cross_backend_conn()`:
   ```python
   def _get_cross_backend_conn(self) -> ibis.BaseBackend:
       """Return the persistent cross-backend DuckDB connection, creating it lazily.

       Used internally when a compute() call spans multiple source backends.
       The connection is backed by a temporary DuckDB file in tmp_dir (or an
       in-memory database when tmp_dir is None). It is torn down by close_all().
       """
       if self._cross_backend_conn is None:
           if self._tmp_dir is not None:
               fd, path = tempfile.mkstemp(suffix=".duckdb", dir=self._tmp_dir)
               os.close(fd)
               self._cross_backend_db_path = path
               self._cross_backend_conn = ibis.duckdb.connect(path)
           else:
               self._cross_backend_conn = ibis.duckdb.connect(":memory:")
       return self._cross_backend_conn
   ```

5. Update `close_all()` — extend existing method, preserving the existing
   `self._connections.clear()` call (omitting it would leave stale
   `IbisConnector` references alive and would cause the duplicate-guard in
   `add_connection` to fire incorrectly after a close):
   ```python
   def close_all(self) -> None:
       for connector in self._connections.values():
           connector.close()
       self._connections.clear()          # ← preserve existing behaviour
       if self._cross_backend_conn is not None:
           try:
               self._cross_backend_conn.disconnect()
           except Exception:
               pass
           self._cross_backend_conn = None
       if self._cross_backend_db_path is not None:
           try:
               os.unlink(self._cross_backend_db_path)
           except OSError:
               pass
           self._cross_backend_db_path = None
   ```

6. Add `__del__` safety net:
   ```python
   def __del__(self) -> None:
       if self._cross_backend_db_path is not None:
           try:
               os.unlink(self._cross_backend_db_path)
           except OSError:
               pass
   ```

**Edge cases and invariants:**

- **No collision with user DuckDB connections**: `_cross_backend_conn` is a
  standalone attribute, never stored in `self._connections`. Therefore
  `get_connection("duckdb")` always returns the user's analytics connector;
  `_get_cross_backend_conn()` always returns the temp backend. The two code
  paths are fully independent.

- **`add_connection` duplicate guard is unaffected**: The guard at
  `if backend_type in self._connections` checks only `_connections`. A user
  can call `_get_cross_backend_conn()` before or after `add_connection("duckdb",
  ...)` with no conflict either way. Two user DuckDB connections still raise
  `ConfigurationError` as before.

- **`add_connection(backend_type, connector=...)` path**: Even when
  `connector` is passed, the duplicate guard fires first (line 168 runs before
  the `if connector is not None` branch). No bypass is possible.

- **`close_all()` idempotency**: After the first call, `_connections` is
  empty, `_cross_backend_conn` is `None`, and `_cross_backend_db_path` is
  `None`. A second call iterates zero connectors and skips both `if ... is not
  None` guards. Safe to call multiple times.

- **`in-memory` path when `tmp_dir=None`**: `ibis.duckdb.connect(":memory:")`
  returns a backend with no file. `_cross_backend_db_path` stays `None`; no
  `os.unlink` is ever attempted in `close_all()` or `__del__`.

- **`from_yaml` YAML key collision**: `from_yaml` iterates every top-level
  YAML key and calls `add_connection(key, ...)`. A stray `tmp_dir: /custom`
  entry in the YAML would fail with `UnsupportedBackendError`. Since `tmp_dir`
  is documented as a Python parameter (not a YAML key), this is acceptable;
  call it out explicitly in the docstring.

- **Tables from `compute()` become invalid after `close_all()`**: Any
  `ibis.Table` returned from `compute()` and backed by the cross-backend
  DuckDB is invalidated once `close_all()` disconnects and deletes that
  backend. Document this in `_get_cross_backend_conn()`'s docstring and in
  `close_all()`'s docstring.

**Validation:**
New tests in `tests/test_connectors/test_connection_manager.py` — see SF-3.

---

### SF-2: Remove `tmp_dir` from `MetricCompute`

**Files changed:** `aitaem/insights.py`

**Changes:**

1. Remove imports no longer needed:
   ```python
   # Remove:
   import os
   import tempfile
   ```

2. Update `__init__` docstring and signature:
   ```python
   def __init__(
       self,
       spec_cache: SpecCache,
       connection_manager: ConnectionManager,
   ) -> None:
       """
       Args:
           spec_cache: Loaded and validated metric, slice, and segment specs.
           connection_manager: Backend connections for query execution.
               When a compute() call spans multiple source backends,
               connection_manager._get_cross_backend_conn() provides the
               temporary DuckDB used for intermediate materialisation.
               Control where that file is written via the ConnectionManager
               tmp_dir parameter.
       """
       self.spec_cache = spec_cache
       self.connection_manager = connection_manager
   ```
   Remove: `self._tmp_dir`, `self._cross_backend_conn`, `self._cross_backend_db_path`.

3. Remove `_get_cross_backend_conn()` method entirely.

4. Remove `__del__()` method entirely.

5. Update the `executor.execute()` call inside `compute()`:
   ```python
   # Before:
   result = executor.execute(
       query_groups,
       cross_backend_conn_factory=self._get_cross_backend_conn,
   )
   # After:
   result = executor.execute(
       query_groups,
       cross_backend_conn_factory=self.connection_manager._get_cross_backend_conn,
   )
   ```

6. Update `compute()` docstring "Args" section — remove any mention of `tmp_dir`;
   update the cross-backend note to say the temporary DuckDB is managed by
   `ConnectionManager`.

**Edge cases:**
- `QueryExecutor.execute()` signature is unchanged — it still accepts
  `cross_backend_conn_factory: Callable[[], ibis.BaseBackend] | None`. Passing
  `self.connection_manager._get_cross_backend_conn` (a bound method) satisfies
  this type.
- The factory is only invoked if cross-backend materialisation is needed. In the
  common single-backend case, `_get_cross_backend_conn()` is never called and no
  temp file is created.

**Validation:**
Run existing `tests/test_insights*.py` — all should pass without modification
(no existing test passes `tmp_dir` to `MetricCompute`).

---

### SF-3: Update and add tests

**Files changed:**
- `tests/test_connectors/test_connection_manager.py`
- `tests/test_insights.py` *(minor: confirm no `tmp_dir` arg)*

**New tests in `test_connection_manager.py`:**

```python
import ibis

class TestCrossBackendConn:
    def test_cross_backend_conn_returns_ibis_backend(self):
        cm = ConnectionManager()
        conn = cm._get_cross_backend_conn()
        assert isinstance(conn, ibis.BaseBackend)
        cm.close_all()

    def test_cross_backend_conn_is_idempotent(self):
        cm = ConnectionManager()
        conn1 = cm._get_cross_backend_conn()
        conn2 = cm._get_cross_backend_conn()
        assert conn1 is conn2
        cm.close_all()

    def test_cross_backend_conn_tmp_dir_none_uses_memory(self):
        cm = ConnectionManager(tmp_dir=None)
        conn = cm._get_cross_backend_conn()
        assert isinstance(conn, ibis.BaseBackend)
        assert cm._cross_backend_db_path is None
        cm.close_all()

    def test_cross_backend_conn_tmp_dir_creates_file(self, tmp_path):
        cm = ConnectionManager(tmp_dir=str(tmp_path))
        cm._get_cross_backend_conn()
        assert cm._cross_backend_db_path is not None
        assert os.path.exists(cm._cross_backend_db_path)
        cm.close_all()

    def test_close_all_deletes_temp_file(self, tmp_path):
        cm = ConnectionManager(tmp_dir=str(tmp_path))
        cm._get_cross_backend_conn()
        db_path = cm._cross_backend_db_path
        assert os.path.exists(db_path)
        cm.close_all()
        assert not os.path.exists(db_path)

    def test_close_all_without_cross_backend_does_not_raise(self):
        cm = ConnectionManager()
        cm.close_all()  # no cross-backend conn created — should not raise

    def test_from_yaml_passes_tmp_dir(self, tmp_path):
        yaml_content = "duckdb:\n  path: ':memory:'\n"
        yaml_file = tmp_path / "connections.yaml"
        yaml_file.write_text(yaml_content)
        cm = ConnectionManager.from_yaml(str(yaml_file), tmp_dir=str(tmp_path))
        assert cm._tmp_dir == str(tmp_path)
        cm.close_all()

    def test_from_yaml_default_tmp_dir(self, tmp_path):
        yaml_content = "duckdb:\n  path: ':memory:'\n"
        yaml_file = tmp_path / "connections.yaml"
        yaml_file.write_text(yaml_content)
        cm = ConnectionManager.from_yaml(str(yaml_file))
        assert cm._tmp_dir == "/tmp"
        cm.close_all()
```

**Confirm `MetricCompute` fixture instantiation** — verify that all `mc` /
`mc_format` / `entity_mc` fixtures in `test_insights*.py` still use only
2 positional arguments and do not need to change. (They already do — no
`tmp_dir` was passed in any existing fixture.)

**Validation:**
```
python -m pytest tests/test_connectors/test_connection_manager.py -v
python -m pytest tests/test_insights*.py -v
```

---

### SF-4: Update documentation

**Files changed:**

#### `docs/api/connectors.md`

In the `ConnectionManager` section:

- Update constructor signature to show `tmp_dir` parameter:
  ```
  ConnectionManager(tmp_dir: str | None = "/tmp")
  ```
- Add parameter description for `tmp_dir`.
- Add description for new `_get_cross_backend_conn()` method.
- Note that `close_all()` now also tears down the cross-backend DuckDB.
- Update `from_yaml()` signature to show optional `tmp_dir` kwarg.

#### `docs/api/insights.md`

- Update `MetricCompute.__init__` signature — remove `tmp_dir` parameter.
- Remove `tmp_dir` from the parameter table / docstring.
- In the cross-backend note, replace "controlled by the `tmp_dir` parameter on
  `MetricCompute`" with "controlled by the `tmp_dir` parameter on
  `ConnectionManager`".

#### `docs/api/index.md`

- In the `MetricCompute` constructor row, update signature to:
  `MetricCompute(cache, conn)` (no `tmp_dir`).
- In the `ConnectionManager` row, update signature to:
  `ConnectionManager(tmp_dir="/tmp")` and add a note about
  `_get_cross_backend_conn()`.

#### `docs/user-guide/connectors.md`

Add a new subsection **"Temporary storage for cross-backend queries"**:

> When a `compute()` call includes metrics from different backends (e.g., one
> from BigQuery and one from DuckDB), aitaem must materialise intermediate
> result sets into a temporary DuckDB database. `ConnectionManager` manages
> this automatically.
>
> By default the temporary file is written to `/tmp`. Pass `tmp_dir` to
> control the location, or set it to `None` to keep everything in memory:
>
> ```python
> # File-based (default) — safe for large cross-backend result sets
> conn = ConnectionManager(tmp_dir="/tmp")
>
> # In-memory — use when result sets are known to be small
> conn = ConnectionManager(tmp_dir=None)
>
> # From YAML — pass tmp_dir as a keyword argument
> conn = ConnectionManager.from_yaml("connections.yaml", tmp_dir="/data/tmp")
> ```
>
> The temporary database is deleted automatically when `close_all()` is
> called, or when the `ConnectionManager` instance is garbage collected.

#### `docs/user-guide/computing-metrics.md`

- Remove or update any sentence that describes the `tmp_dir` parameter on
  `MetricCompute`.
- Add a cross-reference to the new subsection in the connectors guide.

#### `docs/getting-started.md`

- If the quick-start example shows `MetricCompute(cache, conn, tmp_dir=...)`,
  update it to remove `tmp_dir`. If `MetricCompute(cache, conn)` is already
  shown (most likely), no change is needed.

#### `docs/changelog.md`

Add under `## Unreleased`:

```markdown
### Breaking changes
- `MetricCompute.__init__` `tmp_dir` parameter removed. Pass `tmp_dir` to
  `ConnectionManager()` or `ConnectionManager.from_yaml()` instead.

### New features
- `ConnectionManager.__init__` accepts `tmp_dir: str | None = "/tmp"` to
  control where the temporary DuckDB file is written during cross-backend
  compute calls (previously this was a `MetricCompute` concern).
- `ConnectionManager.from_yaml()` accepts `tmp_dir` as a keyword argument.
- `ConnectionManager._get_cross_backend_conn()` — new private method that
  lazily creates and returns the cross-backend DuckDB connection.
- `ConnectionManager.close_all()` now also tears down the cross-backend
  DuckDB connection and deletes its temporary file.
```

---

## Files Changed Summary

| File | Change |
|------|--------|
| `aitaem/connectors/connection.py` | `__init__` gains `tmp_dir`; `from_yaml` gains `tmp_dir` kwarg; new `_get_cross_backend_conn()`; extended `close_all()`; new `__del__` |
| `aitaem/insights.py` | `__init__` loses `tmp_dir`; `_get_cross_backend_conn` and `__del__` removed; `compute()` calls `connection_manager._get_cross_backend_conn` |
| `tests/test_connectors/test_connection_manager.py` | New `TestCrossBackendConn` class (8 new tests) |
| `docs/api/connectors.md` | Constructor signature, new method, `close_all` update, `from_yaml` signature |
| `docs/api/insights.md` | Constructor signature, `tmp_dir` removed from param table |
| `docs/api/index.md` | Signatures updated for both classes |
| `docs/user-guide/connectors.md` | New subsection on temporary storage |
| `docs/user-guide/computing-metrics.md` | Remove `tmp_dir` mention; add cross-reference |
| `docs/getting-started.md` | Remove `tmp_dir` mention if present |
| `docs/changelog.md` | Breaking change + new features under `## Unreleased` |

---

## Testing Strategy

1. Confirm baseline: `python -m pytest` — all tests pass before starting.
2. After SF-1: `python -m pytest tests/test_connectors/test_connection_manager.py -v`
   — new `TestCrossBackendConn` tests pass; no regressions.
3. After SF-2: `python -m pytest tests/test_insights*.py -v`
   — all pass (no fixture change required; `compute()` delegates to CM).
4. After SF-3: `python -m pytest --cov=aitaem --cov-report=term-missing`
   — full suite passes; confirm no coverage regression on
   `aitaem/connectors/connection.py` or `aitaem/insights.py`.
5. Commit after all SFs pass.
