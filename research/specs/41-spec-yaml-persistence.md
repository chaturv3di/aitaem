# Saving specs as YAML, locally and in object storage

**Date:** 2026-11-01
**Sources:** aitaem source at commit 75b3481 (`aitaem/specs/*.py`, `aitaem/utils/yaml_validation.py`,
`aitaem/agent/definition_tools.py`); fsspec **2026.9.0** (installed in a scratch venv and
exercised; see §3)
**Used by:** `plans/41-spec-cache-save-and-remote-paths.md`, `plans/agent_module/ARCHITECTURE.md` §1

---

## 1. Current state

- **Nothing in aitaem writes specs.** Users author YAML files; `SpecCache` only loads them
  (`from_yaml`, `from_string`) and mutates its in-memory state (`add`, `update`, `remove`).
- v2 `DefinitionBot.commit_spec` (`definition_tools.py:521`) only calls `SpecCache.add`/`update`,
  so specs created by the bot are lost when the process ends.
- **Loading is local-only.** `SpecCache._normalize_paths` (`loader.py:293`) converts every path to
  `pathlib.Path`; `_load_paths_strict` uses `Path.exists`/`is_dir`/`glob`. A `gs://…` string
  would be treated as a relative local path.
- **No spec class can serialize itself.** `MetricSpec`, `SliceSpec`, `SegmentSpec` are frozen
  dataclasses with `from_yaml` and no `to_yaml`.
- `SpecCache.from_yaml` takes one path argument per spec type (`metric_paths`, `slice_paths`,
  `segment_paths`); each entry may be a file or a directory, and a directory is read as its sorted
  `*.yaml` + `*.yml` files. Duplicate names within a type raise `SpecValidationError`.

## 2. Turning a spec back into YAML

Each spec class already builds the YAML-shaped dict in its `validate()` method
(`metric.py:69`, `slice.py:97`, `segment.py:93`), because validation runs on the dict form. That
dict uses the **YAML keys**, which differ from the dataclass fields in places:

| Spec | Dataclass field | YAML key |
|---|---|---|
| `SliceSpec` (wildcard) | `column: str` | `where: <column>` |
| `SegmentSpec` (wildcard) | `column: tuple[str, ...]` | `where: <column>` (one) or `where: [<columns>]` (several) |
| `SliceSpec` / `SegmentSpec` (values) | `values: tuple[*Value]` | `values: [{name, where}]` |

Optional fields are emitted only when set (`denominator`, `entities`, `format`, `join_keys`);
`description` is always emitted (default `""`, which `from_yaml` reads back as `""`).

The file format wraps the dict under a top-level key equal to the spec type (`metric:`, `slice:`,
`segment:`); `load_yaml_spec_dict` rejects anything else.

Consequence: serialization can reuse the dict that `validate()` already builds, so the YAML
layout is defined in one place per spec type.

## 3. fsspec (verified by execution, fsspec 2026.9.0)

- **No required dependencies** (`importlib.metadata.requires('fsspec')` lists only extras).
- `fsspec.core.url_to_fs(path)` returns `(filesystem, stripped_path)` for both local paths
  (`LocalFileSystem`) and URLs (`memory://`, `gs://`, `s3://`, `abfs://`, `file://`).
- The filesystem API covers what the loader needs: `exists`, `isdir`, `glob`, `makedirs(exist_ok=True)`,
  `open(path, 'w')`.
- **Missing storage drivers fail with an error naming the package:**
  - `gs://` → `ImportError: Please install gcsfs to access Google Storage`
  - `s3://` → `ImportError: Install s3fs to access S3`
  - `abfs://` → `ImportError: Install adlfs to access Azure Datalake Gen2 and Azure Blob Storage`
- `memory://` is built in, so remote-style paths can be tested without any cloud account or driver.
- **Relative local paths become absolute** (`url_to_fs('metrics/')` returns the absolute path), so
  error messages built from the stripped path would differ from today's, which echo the path the
  user passed. Existing test `test_spec_loader.py:91` matches on message text.
- **[verify]** Object stores have no real directories: `isdir` on a prefix and `makedirs` behave
  per driver (gcsfs/s3fs). Not exercised here, since no driver or bucket was available.

## 4. Spec names as file names

Spec names must match `^[A-Za-z_][A-Za-z0-9_]*$` (`validation.py:17`), so `<name>.yaml` is always a
valid file or object name. On case-insensitive filesystems (macOS default, Windows), two specs whose
names differ only in case would map to the same file.

## 5. Options considered for where specs are saved

| Option | For | Against |
|---|---|---|
| YAML files via fsspec (local or object storage) | Same format users already author; diffable in git; `from_yaml` reads it back; object storage available alongside most OLAPs | Needs `to_yaml` per spec class and fsspec path handling in the loader |
| A table written through an Ibis backend connection | One write API for local DuckDB and remote OLAPs | New storage format next to YAML; Ibis write support per backend unverified; specs no longer human-editable files |

## 6. Dependency placement

aitaem's core dependencies cover local use (`ibis-framework[duckdb]`, `pyyaml`, `pandas`, `pyarrow`,
`sqlglot`); remote backends are extras (`aitaem[bigquery]`, `aitaem[postgres]`), each holding only
what is needed to query that backend (`pyproject.toml:23-40`). fsspec itself has no dependencies;
the service-specific parts are the storage drivers (`gcsfs`, `s3fs`, `adlfs`), which fsspec asks
for by name when missing.

---

## Update (2026-11-01): review of existing core behaviour against saving and remote paths

**Sources:** aitaem source at commit 75b3481 (`aitaem/specs/*.py`, `aitaem/utils/yaml_validation.py`,
`aitaem/connectors/connection.py`, `aitaem/specs/README.md`, `aitaem/connectors/README.md`,
`docs/user-guide/specs.md`, `docs/api/`, `tests/test_specs/test_spec_loader.py`). Every behaviour
marked *(executed)* was confirmed by running it against the installed package.

### U1. `SpecCache` holds specs that were never validated

- `SpecCache.add()` and `update()` check only for duplicate names and composite-slice references
  (`loader.py:169-210`); neither calls the spec's `validate()`. Spec dataclasses have no
  `__post_init__` check.
- `add(MetricSpec(name='1 bad', entities=[], …))` succeeds, and that spec's `validate()` returns
  `False` *(executed)*. `add()` of a spec named `../x` also succeeds *(executed)*.
- `aitaem/specs/README.md` documents `add()` as "Validated immediately on addition", which does not
  match the code.
- **Correction to §4:** the name pattern `^[A-Za-z_][A-Za-z0-9_]*$` is enforced only when a spec
  goes through `validate()` (as `from_yaml` does). A spec added programmatically can have any name,
  so `<name>.yaml` is a safe file name only for validated specs; `../x` would resolve outside the
  target directory.
- Consequence: a cache can contain specs whose YAML `from_yaml` would reject.

### U2. File names are not tied to spec names

- A spec file holds exactly one spec (`load_yaml_spec_dict` requires a single top-level key), but
  nothing ties the file name to the spec name, and both `.yaml` and `.yml` are read.
- Loading a directory raises `SpecValidationError` when two files hold the same spec name
  (`loader.py:318-331`). A directory with `ctr.yml` and `ctr.yaml` both holding spec `ctr` fails
  with "Duplicate spec name 'ctr': already loaded from a previous file" *(executed)*.
- Examples and docs follow the `<name>.yaml` convention (`docs/user-guide/specs.md:402`,
  `examples/`), but it is not required.
- Loading discards YAML comments (`yaml.safe_load`) and ignores unknown keys (logged at debug level,
  e.g. `metric.py:54-56`). Writing a spec back to the file it came from loses both, and loses the
  author's key order and string style.

### U3. Spec classes' `from_yaml` decides between path and YAML text locally

- `MetricSpec.from_yaml(str)` (and the slice/segment equivalents) reads the string as a file only if
  it is an existing local file; otherwise it parses the string as YAML (`yaml_validation.py:86-114`).
- `MetricSpec.from_yaml('gs://bucket/metrics/ctr.yaml')` raises `SpecValidationError: Expected
  top-level key 'metric', got: []` *(executed)*.
- Making that check remote-aware would mean a filesystem lookup (possibly a network call) on every
  YAML string passed in.
- Passing file **text** (not a path) to `from_yaml` is unaffected: text with a newline is never an
  existing file name.

### U4. Module-level loader functions are covered by `from_yaml`

- `load_spec_from_string(s, T)` is a direct call to `T.from_yaml(s)` (`loader.py:41-47`).
- `load_spec_from_file(path, T)` is `T.from_yaml(Path(path))` plus an existence check
  (`loader.py:28-38`); `from_yaml` already raises `FileNotFoundError` for a missing `Path`.
- `load_specs_from_directory(dir, T)` is a lenient form of `SpecCache.from_yaml(...)`: it skips
  invalid files with a warning and lets a later duplicate overwrite an earlier one
  (`loader.py:50-84`). Plan 15 left it unchanged as a lower-level utility.
- None of them is called anywhere in `aitaem`; they are exported from `aitaem.specs` (not the
  top-level `aitaem`), listed in `aitaem/specs/README.md:419-431`, and tested in
  `test_spec_loader.py`.

### U5. Connections YAML is also local-only, and both READMEs plan remote loading

- `ConnectionManager.from_yaml` reads its config with `Path.exists` and `open()`
  (`connection.py:76-85`).
- "Remote YAML loading (HTTP/S3)" is listed under planned enhancements in both
  `aitaem/specs/README.md:573` and `aitaem/connectors/README.md:547`.

### U6. Error-message evidence in §3 was misattributed

- The message-matching test cited in §3 (`test_spec_loader.py:91`, `match="Directory does not
  exist"`) tests `load_specs_from_directory`, not `SpecCache`.
- No `SpecCache` test checks message text; `test_from_yaml_raises_on_missing_path`
  (`test_spec_loader.py:219`) checks only the exception type. Today's `SpecCache` message prints
  `Path(p)`, which already differs from the input in some cases (a trailing `/` is dropped).

### U7. Round trip of `denominator=""`

- `MetricSpec(denominator="")` passes `validate()` *(executed)*. The dict from `validate()` keeps
  it (`metric.py:78`), so YAML built from that dict has `denominator: ''`, and `from_yaml` turns
  `''` into `None` (`metric.py:50`).
- The query builder checks `denominator is not None` (`builder.py:478`), so the in-memory `""`
  produces `NULLIF(, 0)`, while the reloaded spec computes a plain numerator. Round trip is exact
  for specs as `from_yaml` produces them, not for this programmatic case.

### U8. Where the spec API is documented

- `docs/api/specs.md` renders `SpecCache`, `MetricSpec`, `SliceSpec`, `SliceValue`, `SegmentSpec`,
  `SegmentValue` with mkdocstrings, so new methods appear from their docstrings.
- `docs/api/index.md:23` describes `SpecCache` as "Load and cache YAML specs".
- `aitaem/specs/README.md:391-417` is a hand-written `SpecCache` API reference.
- `docs/user-guide/specs.md:386-415` ("Loading specs") documents the paths `from_yaml` accepts.

---

## Update (2026-11-01): audit of every claim in this note

Every claim in §1–§6 and U1–U8 was re-checked against the source at commit 75b3481 (line references
re-read) or by execution. *(executed)* means run against the installed aitaem package, or for
fsspec, in a scratch venv with only fsspec installed (Python 3.12).

### A1. Corrections

- **U4 is wrong about callers.** `SpecCache.from_string` calls `load_spec_from_string`
  (`loader.py:140`, `:149`, `:158`). `load_spec_from_file` and `load_specs_from_directory` have no
  callers in `aitaem`.
- **§2 is wrong that `load_yaml_spec_dict` "rejects anything else".** It requires the spec-type key
  and ignores other top-level keys: `metric: {name: a}` plus `other: 1` returns `{'name': 'a'}`
  *(executed)*.
- **U3's line range** for `_read_input` is `yaml_validation.py:83-114`.

### A2. Confirmed by execution

- §1: `SpecCache.from_yaml(metric_paths='gs://bucket/metrics/')` raises `FileNotFoundError: Spec
  path not found: gs:/bucket/metrics` (`pathlib` collapses `//`).
- §2: YAML built by `yaml.safe_dump({type: dict}, sort_keys=False)` from the dict `validate()`
  builds reads back with `from_yaml` to an equal spec for every variant: metric with and without
  `denominator`/`entities`/`format`; slice leaf, wildcard, composite; segment values,
  single-column wildcard, multi-column wildcard, `join_keys`. Values included YAML-sensitive
  strings (`'yes'`, `'123'`, `'null'`, `'on'`, quotes, `': '` inside a value, a multi-line
  description).
- §2, new: `safe_dump` writes a multi-line string as a single-quoted scalar spread over several
  lines, with an empty line between each line of text, not in the `|` block style people write by
  hand. It reads back equal.
- §4: on this machine's default APFS volume, writing `Ctr.yaml` then `ctr.yaml` leaves one file,
  `Ctr.yaml`, holding the second content. `MetricSpec` names `Ctr` and `ctr` both pass `validate()`
  and can both be in one `SpecCache`.
- U3: multi-line YAML text passed to `from_yaml` is never taken as a path (`Path(text).is_file()`
  is `False`).

### A3. fsspec behaviour relevant to the plan *(executed)*

- The calls the plan needs pass on fsspec **2022.5.0, 2022.11.0, 2023.1.0, 2023.6.0, 2024.2.0, and
  2026.9.0**: `url_to_fs` on relative, `Path`-derived, and `file://` local paths (all resolved to
  the same absolute path); `glob` for `*.yaml` and `*.yml`; `isdir`; `exists` on a missing path;
  `makedirs(exist_ok=True)` twice; UTF-8 text write and read with `open(..., encoding='utf-8')`;
  `memory://` equivalents; and `ImportError` naming `gcsfs`/`s3fs`/`adlfs` for
  `gs://`/`s3://`/`abfs://`. Older versions were not tried.
- **Local writes do not create parent directories**: `open(<missing dir>/x.yaml, 'w')` raises
  `FileNotFoundError`. `memory://` writes do create them.
- **`..` is not normalized**: a local write to `<dir>/../w.yaml` lands in the parent of `<dir>`.
- `memory://` is one shared filesystem instance per process; clearing `fs.store` and resetting
  `fs.pseudo_dirs` to `['']` empties it.
- In `memory://`, a directory exists once a file is written under it or after `makedirs`; `isdir`
  is `False` and `isfile` `True` for a file.
- fsspec is not installed in the aitaem development environment today. Installed packages declare
  it only in extras: `pandas` (`fss`, `all`: `fsspec>=2024.10.0`), `duckdb` (`all`), and
  `ibis-framework` (`athena`: `fsspec[s3]`) (read from installed package metadata).

### A4. Confirmed by reading

- `SpecValidationError(spec_type, name, errors)` (`exceptions.py:101`). Content-validation
  failures pass the lowercase type (`metric.py:48`, `slice.py:60`, `segment.py:57`); duplicate-name
  failures pass the class name (`loader.py:142-160`, `:322`, `:336`).
- All other line references in §1–§6 and U1–U8 point at the code they describe.

### A5. Still unverified

- **[verify]** §3's object-store directory semantics (`isdir` on a prefix, `makedirs`) for `gcsfs`,
  `s3fs`, `adlfs`: no driver or bucket available.

---

## Update (2026-11-10): core support needed by the v3 agent architecture

**Sources:** aitaem source at commit 75b3481 (`aitaem/insights.py`, `aitaem/query/executor.py`,
`aitaem/connectors/connection.py`, `aitaem/specs/loader.py`, `aitaem/agent/definition_tools.py`);
`plans/agent_module/ARCHITECTURE.md` (v1.3-draft); fsspec **2026.9.0** and **2022.5.0**, each in a
scratch venv with only fsspec installed (Python 3.12). *(executed)* means run against the
installed aitaem package (ibis 12.0.0, DuckDB backend) or, for fsspec, in those venvs.

### V1. What the architecture needs from core, and what exists

| Need (ARCHITECTURE section) | Core today |
|---|---|
| Lazy compute results (§4.4, §10) | `MetricCompute.compute` returns an `ibis.Table` bound to the source backend; rows are fetched only on `to_pandas()` *(executed, DuckDB)*. |
| Results spanning several backends (§4.4) | `QueryExecutor.execute` materializes each group and loads the union into the `ConnectionManager`'s temp DuckDB (`executor.py:84-97`). |
| Compute lock (§5) | `ConnectionManager.requires_compute_lock` (`connection.py:514`) is `True` when DuckDB is registered *(executed)*. |
| Connecting sources (`connect_source`, §5) | `ConnectionManager.add_connection` *(executed)*. |
| Commit and delete in memory (`commit_spec`, `delete_spec`, §5) | `SpecCache.add`/`update`/`remove` *(`remove` executed)*; v2 `commit_spec`/`delete_spec` call them (`definition_tools.py:521-629`). |
| Saving specs as YAML (§1, §10) | Missing (§1 of this note). |
| Caller-supplied local path or fsspec URL as save location (§1) | Missing: loading is local-only (§1, A2). |
| A deleted spec stays deleted after reload (§1 spec design, §6.3 `delete_spec`) | Missing. With a write-only `save()`, the file of a removed spec stays in the save directory and the next `from_yaml` loads it again. |

### V2. Deleting files with fsspec *(executed)*

- `fs.rm(<file>)` removes only that file, on local paths and `memory://`, on fsspec 2022.5.0 and
  2026.9.0.
- Globbing `*.yaml` and `*.yml` in a directory, then removing one match, leaves the other matches
  and non-YAML files (`notes.txt`) in place.
- `fs.rm` on a missing path raises `FileNotFoundError` on local paths (both versions) and on
  `memory://` with 2026.9.0; `memory://` with 2022.5.0 raises nothing.
- **[verify]** `rm` on `gcsfs`/`s3fs`/`adlfs`: no driver or bucket available (as A5).

### V3. Normalizing `denominator=""` *(executed)*

- `MetricSpec.from_yaml` reads YAML `denominator: ''` as `None` (as U7).
- On a frozen dataclass, a `__post_init__` that calls `object.__setattr__(self, "denominator", None)`
  when the value is `""` makes the instance equal to, and hash the same as, one built without a
  denominator.
- `validate_metric_spec` skips the denominator check when the value is falsy (`validation.py:214`),
  so after normalization no blank denominator reaches the query builder's `is not None` check
  (`builder.py:478`).

---

## Update (2026-11-10): directory existence on `memory://` with fsspec 2022.5.0

**Sources:** fsspec **2022.5.0** and **2026.9.0** in the aitaem development environment (Python
3.12). *(executed)*

- **Correction to A3:** on fsspec 2022.5.0, a `memory://` directory that exists only because files
  were written under it reports `exists() == False` and `isdir() == True`. After `makedirs`, both
  are `True`. On 2026.9.0 both are `True` in either case.
- `isdir`/`isfile` agree on both versions for local paths and `memory://`: directory →
  `isdir` only, file → `isfile` only, missing → neither.
- Consequence: telling a missing path from a file or directory with `isdir`/`isfile` works on both
  versions; `exists` does not.
