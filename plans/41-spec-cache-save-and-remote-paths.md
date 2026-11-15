# Plan 41 — Core Support for the v3 Agent: Saving Specs, Remote Spec Locations

**Scope:** the core changes `plans/agent_module/ARCHITECTURE.md` needs from `aitaem`:

- `SpecCache.save()` writes the cache as YAML files, one directory per spec type, and each save
  directory mirrors the cache, so a deleted spec stays deleted after reload.
- `SpecCache.from_yaml` and `SpecCache.save` accept local paths and fsspec URLs (`gs://`, `s3://`,
  `abfs://`, …).
- `MetricSpec(denominator="")` means no denominator, as YAML `denominator: ''` already does (U7).

Everything else the architecture needs already exists in core (V1, §2).

**Research:** [research/specs/41-spec-yaml-persistence.md](../research/specs/41-spec-yaml-persistence.md).
"research §N", "UN", "AN" and "VN" point to sections of that note; a bare "§N" points to this plan.

---

## 1. Design

**File format.** One spec per YAML file under a top-level key equal to its type (`metric:`,
`slice:`, `segment:`), as today. Each spec class serializes itself with `to_yaml()`, built from the
dict its `validate()` already builds, so each type's YAML layout lives in one place (research §2).
For specs built by `from_yaml`, `to_yaml()` output reads back to an equal spec for every variant
(A2). `yaml.safe_dump` writes multi-line strings as quoted scalars; these read back equal (A2), so
no custom representer.

**Save directories mirror the cache.** `save()` takes one directory per spec type. It writes every
spec of that type to `<dir>/<name>.yaml` and deletes the directory's other `*.yaml`/`*.yml` files
(V2), so a removed spec stays removed after reload. Each file is written from its spec, so it
holds only the spec's fields (U2).

**Only valid specs are saved.** `add()`/`update()` do not validate spec content (U1), so the cache
can hold specs that `from_yaml` would reject, including names such as `../x` that resolve outside
the target directory (fsspec does not normalize `..`, A3). `save()` validates every spec it is about
to write, and rejects two specs of one type whose names differ only in case (one file on
case-insensitive filesystems, A2). It also refuses to write `<name>.yaml` when the directory holds a
file whose name differs from it only in case (e.g. `CTR.yaml` for spec `ctr`). Any failure raises
before a file is written or deleted; the cache is never changed by `save()`.

**Remote locations.** `SpecCache.from_yaml` and `SpecCache.save` resolve every path with
`fsspec.core.url_to_fs`, so local paths and URLs share one code path. `SpecCache` reads each file's
text through fsspec and passes it to the existing spec class `from_yaml` (U3, A2). Every other YAML
reader stays local (§2).

**Dependencies.** `fsspec>=2022.5.0` joins core `dependencies`; every fsspec call used here behaves
the same from 2022.5.0 to 2026.9.0 for the paths this plan touches (A3, V2). Storage drivers
(`gcsfs`, `s3fs`, `adlfs`) stay out of `pyproject.toml`, backend extras included; fsspec's
`ImportError` names the missing driver (research §3).

**Unchanged for existing callers:** exception types (`FileNotFoundError` for a missing path,
`SpecValidationError` for an invalid or duplicate spec), the sorted union of `*.yaml` and `*.yml`
files read from a directory, strict duplicate detection, and `pathlib.Path` inputs. Error message
text is not part of the contract (U6).

## 2. Out of scope

| Item | Reason |
|---|---|
| Lazy compute results, cross-backend results, `requires_compute_lock`, `add_connection`, `SpecCache.add`/`update`/`remove` | Already in core (V1). |
| Module-level `load_spec_from_file`, `load_spec_from_string`, `load_specs_from_directory` | Unchanged and local-only; the spec classes' and `SpecCache`'s `from_yaml` cover them (U4, A1). |
| Remote paths in `MetricSpec`/`SliceSpec`/`SegmentSpec.from_yaml` | Unchanged. They tell a path from YAML text with a local file check; a remote check would add a filesystem lookup to every YAML string (U3). |
| Remote `ConnectionManager.from_yaml` | Unchanged; the architecture does not need it (U5). |
| `SpecCache.from_string` | Unchanged; takes no paths. |
| Content validation in `add()`/`update()` | Unchanged (U1); `save()` validates. |
| Storage drivers as dependencies | §1 Dependencies. |
| Agent integration | `plans/agent_module/ARCHITECTURE.md`. |

## 3. Sub-features

Order: SF-1 → SF-2 → SF-3 → SF-4. SF-1 and SF-2 are independent; SF-3 needs both.

### SF-1 — `to_yaml()` on each spec class; blank denominator

**Files:** `aitaem/specs/metric.py`, `aitaem/specs/slice.py`, `aitaem/specs/segment.py`

- Move the dict-building part of `validate()` into a private `_to_dict() -> dict`; `validate()`
  returns `validate_*_spec(self._to_dict())` as before.
- `to_yaml() -> str` dumps `{"<type>": self._to_dict()}` with `yaml.safe_dump(..., sort_keys=False)`.
- `to_yaml()` does not validate; `save()` does.
- `MetricSpec.__post_init__` replaces `denominator=""` with `None` (`object.__setattr__`, since the
  dataclass is frozen), matching `from_yaml`'s handling of `''` (U7, V3).

**Tests** (`tests/test_specs/test_metric_spec.py`, `test_slice_spec.py`, `test_segment_spec.py`):

- For specs built with `from_yaml`, `from_yaml(spec.to_yaml()) == spec` for:
  - metric with and without `denominator`, `entities`, `format`
  - slice: leaf, composite, wildcard
  - segment: values, single-column wildcard, multi-column wildcard, with `join_keys`
- Each spec type includes at least one YAML-sensitive string (`'yes'`, `'123'`, `'null'`, a quote,
  `': '`, a multi-line description), as in A2.
- `MetricSpec(..., denominator="") == MetricSpec(...)`, and its `denominator` is `None`.
- Existing `validate()` tests pass unchanged.

### SF-2 — Load from local paths and URLs

**Files:** `pyproject.toml`, `aitaem/specs/loader.py`

- Add `fsspec>=2022.5.0` to `[project] dependencies`; install with `uv pip install fsspec`.
- `_normalize_paths` returns `list[str]` (`str(p)` per input): `Path("gs://b/x")` collapses `//`
  (A2).
- Rewrite `SpecCache._load_paths_strict` on `fsspec.core.url_to_fs`:
  - missing path → `FileNotFoundError` naming the path as given
  - directory → sorted union of its `*.yaml` and `*.yml` files; file → that file
  - each file's text is read through the filesystem (UTF-8) and passed to `spec_type.from_yaml`
  - one duplicate-name check for file and directory inputs, replacing today's two copies
- `SpecCache.from_yaml` annotations become `str | Path | list[str | Path] | None`, matching what it
  already accepts.

**Tests** (`tests/test_specs/test_spec_loader.py`):

- Existing `SpecCache` tests pass unchanged.
- Load a file and a directory (`.yaml` and `.yml`) from `memory://`.
- Missing `memory://` path → `FileNotFoundError`.
- Duplicate name across two `memory://` files → `SpecValidationError`.
- A fixture in `tests/test_specs/conftest.py` empties the shared in-memory filesystem after each
  test (A3).

### SF-3 — `SpecCache.save()`

**File:** `aitaem/specs/loader.py`

- Signature: `save(metric_dir: str | Path | None = None, slice_dir: str | Path | None = None, segment_dir: str | Path | None = None) -> None`.
  The `_dir` suffix marks that `save()` takes directories only. `None` skips that spec type. Each
  type's specs come from `_bucket_for_type`.
- Checks, for every requested type, before any write or delete:
  - `validate()` on every spec; the first invalid spec raises `SpecValidationError` with the
    lowercase type (as content-validation failures do today, A4), its name, and its errors
  - two names equal ignoring case raise `SpecValidationError` with the lowercase type, the second
    name, and a `name` field error naming both
  - a file in the directory whose name equals `<name>.yaml` ignoring case, but not exactly, raises
    `FileExistsError` naming the file and the spec
- Then, per requested type:
  1. resolve the directory with `fsspec.core.url_to_fs` and `makedirs(exist_ok=True)` (local writes
     do not create it, A3)
  2. write each spec's `to_yaml()` to `<dir>/<name>.yaml`
  3. delete every `*.yaml`/`*.yml` file in the directory whose path was not written in step 2 (V2)
- Docstring: each directory mirrors the cache after `save()`; remote URLs need the matching fsspec
  driver (`gcsfs` for `gs://`, `s3fs` for `s3://`, `adlfs` for `abfs://`).

**Tests** (`tests/test_specs/test_spec_loader.py`):

- Save to `tmp_path` directories that do not exist yet; `SpecCache.from_yaml` on them returns equal
  specs.
- The same round trip on `memory://` directories.
- A `None` argument writes and deletes nothing for that type.
- Saving after `update()` overwrites the spec's file.
- Saving after `remove()` deletes the removed spec's file; `from_yaml` on the directory no longer
  returns it.
- A `.yml` file and a differently named `.yaml` file already in the directory are deleted; a
  `notes.txt` stays.
- A `CTR.yaml` in the directory while saving spec `ctr` → `FileExistsError`; nothing is written or
  deleted, and the cache still holds `ctr`.
- A programmatically added invalid spec → `SpecValidationError`, and no file is written or deleted
  for any type.
- A spec named `../x` → `SpecValidationError`, and nothing is written inside or outside the target.
- Metric specs `Ctr` and `ctr` in one cache → `SpecValidationError`, and nothing is written or
  deleted.

**Minimum fsspec version:** after the tests pass, install `fsspec==2022.5.0`, run
`tests/test_specs/`, then reinstall the latest.

### SF-4 — Docs

- `docs/changelog.md`, `## Unreleased` → Added:
  - `to_yaml()` on `MetricSpec`, `SliceSpec`, `SegmentSpec`
  - `SpecCache.save()`
  - fsspec URLs in `SpecCache.from_yaml` and `SpecCache.save`
  - new core dependency `fsspec`
- `docs/changelog.md`, `## Unreleased` → Fixed: `MetricSpec(denominator="")` is treated as no
  denominator (it produced invalid SQL).
- `docs/user-guide/specs.md`: remote paths in "Loading specs"; a short "Saving specs" section:
  each directory mirrors the cache after `save()`, and remote URLs need a driver.
- `aitaem/specs/README.md`: `save()` and `to_yaml()` in the API reference; remove "Remote YAML
  loading (HTTP/S3)" from its planned enhancements (the connectors README keeps its own item).
- `docs/api/index.md:23`: `SpecCache` description becomes "Load, cache, and save YAML specs".
- `docs/api/specs.md`: unchanged; mkdocstrings renders the new methods from their docstrings (U8).

## 4. Known limitation

**[verify]** Object stores have no real directories; `isdir` on a prefix, `makedirs`, and `rm`
behave per driver (research §3, A5, V2). Tests use `memory://` only; no driver or bucket is
available to test `gcsfs`/`s3fs`/`adlfs`.

## 5. Files changed

| File | Change |
|---|---|
| `aitaem/specs/metric.py`, `slice.py`, `segment.py` | SF-1: `_to_dict()`, `to_yaml()`; `MetricSpec.__post_init__` |
| `pyproject.toml` | SF-2: `fsspec` dependency |
| `aitaem/specs/loader.py` | SF-2: `_normalize_paths` returns strings, `_load_paths_strict` on fsspec, `from_yaml` annotations; SF-3: `save()` |
| `tests/test_specs/test_metric_spec.py`, `test_slice_spec.py`, `test_segment_spec.py` | SF-1 tests |
| `tests/test_specs/conftest.py` | SF-2: `memory://` cleanup fixture |
| `tests/test_specs/test_spec_loader.py` | SF-2, SF-3 tests |
| `docs/changelog.md`, `docs/user-guide/specs.md`, `docs/api/index.md`, `aitaem/specs/README.md` | SF-4 |
