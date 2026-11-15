"""
aitaem.specs.loader - Spec loading and caching utilities

Provides functions to load specs from files, strings, or directories,
and a SpecCache for eagerly-loaded, session-scoped caching.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Union

from fsspec.core import url_to_fs

from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import SpecNotFoundError, SpecValidationError
from aitaem.utils.validation import ValidationError

logger = logging.getLogger(__name__)

SpecType = Union[type[MetricSpec], type[SliceSpec], type[SegmentSpec]]
AnySpec = Union[MetricSpec, SliceSpec, SegmentSpec]
PathArg = Union[str, Path, list[Union[str, Path]], None]


def load_spec_from_file(path: str | Path, spec_type: SpecType) -> AnySpec:
    """Load a single spec from a YAML file.

    Raises:
        FileNotFoundError: if file does not exist
        SpecValidationError: if spec is invalid
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Spec file not found: {path}")
    return spec_type.from_yaml(path)


def load_spec_from_string(yaml_string: str, spec_type: SpecType) -> AnySpec:
    """Load a single spec from a YAML string.

    Raises:
        SpecValidationError: if spec is invalid
    """
    return spec_type.from_yaml(yaml_string)


def load_specs_from_directory(
    directory: str | Path,
    spec_type: SpecType,
) -> dict[str, AnySpec]:
    """Load all YAML files (*.yaml, *.yml) from a directory.

    Returns a dict mapping spec name → spec object.
    Skips files with parse/validation errors, logging warnings.

    Raises:
        ValueError: if directory does not exist or path is not a directory
    """
    directory = Path(directory)
    if not directory.exists():
        raise ValueError(f"Directory does not exist: {directory}")
    if not directory.is_dir():
        raise ValueError(f"Expected a directory, got a file: {directory}")

    specs: dict[str, AnySpec] = {}
    yaml_files = list(directory.glob("*.yaml")) + list(directory.glob("*.yml"))

    for file_path in sorted(yaml_files):
        try:
            spec = spec_type.from_yaml(file_path)
            if spec.name in specs:
                logger.warning(
                    "Duplicate spec name '%s' found in '%s'. Overwriting previous definition.",
                    spec.name,
                    file_path,
                )
            specs[spec.name] = spec
        except (SpecValidationError, FileNotFoundError, Exception) as e:
            logger.warning("Skipping '%s': %s", file_path, e)

    return specs


class SpecCache:
    """Eagerly-loaded cache for metric, slice, and segment specs.

    Use from_yaml() or from_string() as the primary entry points.
    The constructor creates an empty cache; specs can be added via add().
    """

    def __init__(self) -> None:
        """Empty cache. Use from_yaml() or from_string() to load specs."""
        self._metrics: dict[str, MetricSpec] = {}
        self._slices: dict[str, SliceSpec] = {}
        self._segments: dict[str, SegmentSpec] = {}
        self.version: int = 0
        """Monotonic counter, incremented on every successful add()/update()/remove()/clear()."""

    @classmethod
    def from_yaml(
        cls,
        metric_paths: PathArg = None,
        slice_paths: PathArg = None,
        segment_paths: PathArg = None,
    ) -> "SpecCache":
        """Load and validate all specs from YAML files or directories.

        Loading is eager — all specs are loaded and validated before returning.
        Each path is a local path or an fsspec URL (``gs://``, ``s3://``, ``abfs://``, ...);
        remote URLs need the matching fsspec driver (``gcsfs``, ``s3fs``, ``adlfs``).
        A directory is read as its ``*.yaml`` and ``*.yml`` files.

        Raises:
            FileNotFoundError: if a path does not exist
            SpecValidationError: if any spec is invalid
        """
        cache = cls()
        cache._metrics = cls._load_paths_strict(metric_paths, MetricSpec)  # type: ignore[assignment]
        cache._slices = cls._load_paths_strict(slice_paths, SliceSpec)  # type: ignore[assignment]
        cache._segments = cls._load_paths_strict(segment_paths, SegmentSpec)  # type: ignore[assignment]
        cache._validate()
        return cache

    @classmethod
    def from_string(
        cls,
        metric_yaml: str | list[str] | None = None,
        slice_yaml: str | list[str] | None = None,
        segment_yaml: str | list[str] | None = None,
    ) -> "SpecCache":
        """Load specs from YAML strings. Validates eagerly.

        Each argument can be a single YAML string or a list of YAML strings.

        Raises:
            SpecValidationError: if any spec is invalid
        """
        cache = cls()
        for yaml_str in cls._normalize_strings(metric_yaml):
            spec = load_spec_from_string(yaml_str, MetricSpec)
            if spec.name in cache._metrics:
                raise SpecValidationError(
                    "MetricSpec",
                    spec.name,
                    [ValidationError(field="name", message=f"Duplicate spec name '{spec.name}'")],
                )
            cache._metrics[spec.name] = spec  # type: ignore[assignment]
        for yaml_str in cls._normalize_strings(slice_yaml):
            spec = load_spec_from_string(yaml_str, SliceSpec)
            if spec.name in cache._slices:
                raise SpecValidationError(
                    "SliceSpec",
                    spec.name,
                    [ValidationError(field="name", message=f"Duplicate spec name '{spec.name}'")],
                )
            cache._slices[spec.name] = spec  # type: ignore[assignment]
        for yaml_str in cls._normalize_strings(segment_yaml):
            spec = load_spec_from_string(yaml_str, SegmentSpec)
            if spec.name in cache._segments:
                raise SpecValidationError(
                    "SegmentSpec",
                    spec.name,
                    [ValidationError(field="name", message=f"Duplicate spec name '{spec.name}'")],
                )
            cache._segments[spec.name] = spec  # type: ignore[assignment]
        cache._validate()
        return cache

    def add(self, spec: MetricSpec | SliceSpec | SegmentSpec) -> None:
        """Add a spec programmatically.

        Raises:
            SpecValidationError: if a spec with the same name is already present,
                or if adding it breaks cross-reference consistency (rolled back
                — the cache is left exactly as it was before the call).
        """
        bucket, _spec_type = self._bucket_for(spec)
        if spec.name in bucket:
            raise SpecValidationError(
                type(spec).__name__,
                spec.name,
                [ValidationError(field="name", message=f"Duplicate spec name '{spec.name}'")],
            )
        bucket[spec.name] = spec
        try:
            self._validate()
        except Exception:
            del bucket[spec.name]
            raise
        self.version += 1

    def update(self, spec: MetricSpec | SliceSpec | SegmentSpec) -> None:
        """Update an existing spec by name (overwrite).

        Raises:
            SpecNotFoundError: if no spec with this name exists in the target bucket.
            SpecValidationError: if the update breaks cross-reference consistency
                (rolled back — the cache is left exactly as it was before the call).
        """
        bucket, spec_type = self._bucket_for(spec)
        if spec.name not in bucket:
            raise SpecNotFoundError(spec_type, spec.name, [])
        previous = bucket[spec.name]
        bucket[spec.name] = spec
        try:
            self._validate()
        except Exception:
            bucket[spec.name] = previous
            raise
        self.version += 1

    def remove(self, spec_type: str, name: str) -> None:
        """Remove a spec by type and name.

        Args:
            spec_type: "metric", "slice", or "segment".
            name: Name of the spec to remove.

        Raises:
            SpecNotFoundError: if no such spec exists.
            SpecValidationError: if removal breaks cross-reference consistency
                (e.g. an existing composite slice still references it; rolled
                back — the cache is left exactly as it was before the call).
        """
        bucket = self._bucket_for_type(spec_type)
        if name not in bucket:
            raise SpecNotFoundError(spec_type, name, [])
        previous = bucket.pop(name)
        try:
            self._validate()
        except Exception:
            bucket[name] = previous
            raise
        self.version += 1

    def save(
        self,
        metric_dir: str | Path | None = None,
        slice_dir: str | Path | None = None,
        segment_dir: str | Path | None = None,
    ) -> None:
        """Write the cached specs as YAML files, one directory per spec type.

        Each spec is written to ``<dir>/<name>.yaml``, and every other ``*.yaml``/``*.yml``
        file in the directory is deleted, so each directory mirrors the cache after
        ``save()``. Missing directories are created. ``None`` skips that spec type.

        Each directory is a local path or an fsspec URL; remote URLs need the matching
        fsspec driver (``gcsfs`` for ``gs://``, ``s3fs`` for ``s3://``, ``adlfs`` for
        ``abfs://``).

        All checks run before any file is written or deleted, and the cache is never
        changed.

        Raises:
            SpecValidationError: if a spec is invalid, or two specs of one type have
                names that differ only in case.
            FileExistsError: if a directory holds a file whose name differs from
                ``<name>.yaml`` only in case (e.g. ``CTR.yaml`` for spec ``ctr``).
        """
        targets = []
        for spec_type, directory in (
            ("metric", metric_dir),
            ("slice", slice_dir),
            ("segment", segment_dir),
        ):
            if directory is None:
                continue
            specs = self._bucket_for_type(spec_type)
            fs, root = url_to_fs(str(directory))
            files = self._list_files(fs, root) if fs.isdir(root) else []
            self._check_savable(spec_type, specs, files)
            targets.append((fs, root, specs, files))

        for fs, root, specs, files in targets:
            fs.makedirs(root, exist_ok=True)
            written = set()
            for name, spec in specs.items():
                file_name = f"{name}.yaml"
                with fs.open(posixpath.join(root, file_name), "w", encoding="utf-8") as f:
                    f.write(spec.to_yaml())
                written.add(file_name)
            for path in files:
                file_name = posixpath.basename(path)
                if self._is_yaml_file(file_name) and file_name not in written:
                    fs.rm(path)

    @property
    def metrics(self) -> Mapping[str, MetricSpec]:
        """Read-only view of all loaded metric specs, keyed by name."""
        return MappingProxyType(self._metrics)

    @property
    def slices(self) -> Mapping[str, SliceSpec]:
        """Read-only view of all loaded slice specs, keyed by name."""
        return MappingProxyType(self._slices)

    @property
    def segments(self) -> Mapping[str, SegmentSpec]:
        """Read-only view of all loaded segment specs, keyed by name."""
        return MappingProxyType(self._segments)

    def get_metric(self, name: str) -> MetricSpec:
        """Return MetricSpec for the given name.

        Raises:
            SpecNotFoundError: if name not found
        """
        if name not in self._metrics:
            raise SpecNotFoundError("metric", name, [])
        return self._metrics[name]

    def get_slice(self, name: str) -> SliceSpec:
        """Return SliceSpec for the given name.

        Raises:
            SpecNotFoundError: if name not found
        """
        if name not in self._slices:
            raise SpecNotFoundError("slice", name, [])
        return self._slices[name]

    def get_segment(self, name: str) -> SegmentSpec:
        """Return SegmentSpec for the given name.

        Raises:
            SpecNotFoundError: if name not found
        """
        if name not in self._segments:
            raise SpecNotFoundError("segment", name, [])
        return self._segments[name]

    def clear(self) -> None:
        """Clear all cached specs."""
        self._metrics = {}
        self._slices = {}
        self._segments = {}
        self.version += 1

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_paths(paths: PathArg) -> list[str]:
        if paths is None:
            return []
        if isinstance(paths, (str, Path)):
            return [str(paths)]
        return [str(p) for p in paths]

    @staticmethod
    def _normalize_strings(strings: str | list[str] | None) -> list[str]:
        if strings is None:
            return []
        if isinstance(strings, str):
            return [strings]
        return list(strings)

    @classmethod
    def _load_paths_strict(cls, paths: PathArg, spec_type: SpecType) -> dict[str, AnySpec]:
        """Load specs from files/directories (local paths or fsspec URLs), raising on any error."""
        result: dict[str, AnySpec] = {}
        for path in cls._normalize_paths(paths):
            fs, root = url_to_fs(path)
            if fs.isdir(root):
                yaml_files = [
                    f for f in cls._list_files(fs, root) if cls._is_yaml_file(posixpath.basename(f))
                ]
            elif fs.isfile(root):
                yaml_files = [root]
            else:
                raise FileNotFoundError(f"Spec path not found: {path}")
            for yaml_file in yaml_files:
                with fs.open(yaml_file, "r", encoding="utf-8") as f:
                    spec = spec_type.from_yaml(f.read())
                if spec.name in result:
                    raise SpecValidationError(
                        spec_type.__name__,
                        spec.name,
                        [
                            ValidationError(
                                field="name",
                                message=f"Duplicate spec name '{spec.name}': already loaded from a previous file",
                            )
                        ],
                    )
                result[spec.name] = spec
        return result

    @staticmethod
    def _list_files(fs, root: str) -> list[str]:
        """Return the sorted paths of the files directly inside directory root."""
        return sorted(e["name"] for e in fs.ls(root, detail=True) if e["type"] == "file")

    @staticmethod
    def _is_yaml_file(file_name: str) -> bool:
        return file_name.endswith((".yaml", ".yml"))

    @staticmethod
    def _check_savable(spec_type: str, specs: dict[str, AnySpec], files: list[str]) -> None:
        """Raise if specs cannot be saved as <name>.yaml files next to the given existing files.

        Raises:
            SpecValidationError: if a spec is invalid, or two names differ only in case.
            FileExistsError: if a file name differs from <name>.yaml only in case.
        """
        seen: dict[str, str] = {}
        for name, spec in specs.items():
            validation = spec.validate()
            if not validation.valid:
                raise SpecValidationError(spec_type, name, validation.errors)
            if name.lower() in seen:
                first = seen[name.lower()]
                raise SpecValidationError(
                    spec_type,
                    name,
                    [
                        ValidationError(
                            field="name",
                            message=f"Spec names '{first}' and '{name}' differ only in case "
                            "and would be saved to the same file on case-insensitive filesystems",
                        )
                    ],
                )
            seen[name.lower()] = name

        for name in specs:
            file_name = f"{name}.yaml"
            for path in files:
                existing = posixpath.basename(path)
                if existing.lower() != file_name.lower() or existing == file_name:
                    continue
                raise FileExistsError(
                    f"Cannot save {spec_type} '{name}' as '{file_name}': '{path}' differs "
                    "from it only in case; rename the spec or move the file"
                )

    def _bucket_for(
        self, spec: MetricSpec | SliceSpec | SegmentSpec
    ) -> tuple[dict[str, AnySpec], str]:
        """Return (cache dict, lowercase spec_type) for a spec instance."""
        if isinstance(spec, MetricSpec):
            return self._metrics, "metric"  # type: ignore[return-value]
        elif isinstance(spec, SliceSpec):
            return self._slices, "slice"  # type: ignore[return-value]
        elif isinstance(spec, SegmentSpec):
            return self._segments, "segment"  # type: ignore[return-value]
        raise TypeError(f"Unsupported spec type: {type(spec).__name__}")

    def _bucket_for_type(self, spec_type: str) -> dict[str, AnySpec]:
        """Return the cache dict for a lowercase spec_type ('metric'/'slice'/'segment')."""
        if spec_type == "metric":
            return self._metrics  # type: ignore[return-value]
        elif spec_type == "slice":
            return self._slices  # type: ignore[return-value]
        elif spec_type == "segment":
            return self._segments  # type: ignore[return-value]
        raise ValueError(f"Unknown spec_type: {spec_type!r}")

    def _validate(self) -> None:
        """Validate that composite specs' cross-product references resolve.

        Raises:
            SpecValidationError: if a referenced name is missing or is itself composite.
        """
        for spec_name, spec in self._slices.items():
            if not spec.is_composite:
                continue
            for ref_name in spec.cross_product:
                if ref_name not in self._slices:
                    raise SpecValidationError(
                        "slice",
                        spec_name,
                        [
                            ValidationError(
                                field="cross_product",
                                message=f"Referenced slice '{ref_name}' not found in loaded slices",
                            )
                        ],
                    )
                ref_spec = self._slices[ref_name]
                if ref_spec.is_composite:
                    raise SpecValidationError(
                        "slice",
                        spec_name,
                        [
                            ValidationError(
                                field="cross_product",
                                message=f"Nested composite slices not supported (Phase 1): "
                                f"'{ref_name}' is also composite",
                            )
                        ],
                    )
