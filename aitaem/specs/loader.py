"""
aitaem.specs.loader - Spec loading and caching utilities

Provides functions to load specs from files, strings, or directories,
and a SpecCache for lazy, session-scoped caching.

Note: SpecCache is not thread-safe in Phase 1.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import SpecNotFoundError, SpecValidationError

logger = logging.getLogger(__name__)

SpecType = Union[type[MetricSpec], type[SliceSpec], type[SegmentSpec]]
AnySpec = Union[MetricSpec, SliceSpec, SegmentSpec]


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
    """Lazy, session-scoped cache for specs loaded from configured directories.

    Specs are loaded on first access (by name) and cached for the duration
    of the session. Supports metrics, slices, and segments.

    Note: Not thread-safe in Phase 1.
    """

    def __init__(
        self,
        metric_paths: list[str | Path] | None = None,
        slice_paths: list[str | Path] | None = None,
        segment_paths: list[str | Path] | None = None,
    ) -> None:
        """Initialize with paths to search. Loading is deferred until first access."""
        self._metric_paths: list[Path] = [Path(p) for p in (metric_paths or [])]
        self._slice_paths: list[Path] = [Path(p) for p in (slice_paths or [])]
        self._segment_paths: list[Path] = [Path(p) for p in (segment_paths or [])]

        self._metrics: dict[str, MetricSpec] | None = None
        self._slices: dict[str, SliceSpec] | None = None
        self._segments: dict[str, SegmentSpec] | None = None

    def _load_all(self, paths: list[Path], spec_type: SpecType) -> dict[str, AnySpec]:
        """Load all specs from the configured paths."""
        result: dict[str, AnySpec] = {}
        for path in paths:
            if path.is_dir():
                loaded = load_specs_from_directory(path, spec_type)
                for name, spec in loaded.items():
                    if name in result:
                        logger.warning(
                            "Duplicate spec name '%s' found across paths. Overwriting.", name
                        )
                    result[name] = spec
            elif path.suffix in (".yaml", ".yml"):
                try:
                    spec = load_spec_from_file(path, spec_type)
                    if spec.name in result:
                        logger.warning(
                            "Duplicate spec name '%s' found across paths. Overwriting.", spec.name
                        )
                    result[spec.name] = spec
                except (FileNotFoundError, SpecValidationError, Exception) as e:
                    logger.warning("Skipping '%s': %s", path, e)
        return result

    def get_metric(self, name: str) -> MetricSpec:
        """Return MetricSpec for the given name. Loads and caches on first call.

        Raises:
            SpecNotFoundError: if name not found in any configured path
        """
        if self._metrics is None:
            self._metrics = self._load_all(self._metric_paths, MetricSpec)  # type: ignore[arg-type]
        if name not in self._metrics:
            raise SpecNotFoundError("metric", name, [str(p) for p in self._metric_paths])
        return self._metrics[name]

    def get_slice(self, name: str) -> SliceSpec:
        """Return SliceSpec by name. Same lazy-load semantics.

        Raises:
            SpecNotFoundError: if name not found in any configured path
        """
        if self._slices is None:
            self._slices = self._load_all(self._slice_paths, SliceSpec)  # type: ignore[arg-type]
        if name not in self._slices:
            raise SpecNotFoundError("slice", name, [str(p) for p in self._slice_paths])
        return self._slices[name]

    def get_segment(self, name: str) -> SegmentSpec:
        """Return SegmentSpec by name. Same lazy-load semantics.

        Raises:
            SpecNotFoundError: if name not found in any configured path
        """
        if self._segments is None:
            self._segments = self._load_all(self._segment_paths, SegmentSpec)  # type: ignore[arg-type]
        if name not in self._segments:
            raise SpecNotFoundError("segment", name, [str(p) for p in self._segment_paths])
        return self._segments[name]

    def clear(self) -> None:
        """Clear all cached specs (forces re-scan on next access)."""
        self._metrics = None
        self._slices = None
        self._segments = None
