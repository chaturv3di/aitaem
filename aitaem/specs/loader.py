"""
aitaem.specs.loader - Spec loading and caching utilities

Provides functions to load specs from files, strings, or directories,
and a SpecCache for eagerly-loaded, session-scoped caching.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

from aitaem.specs.metric import MetricSpec
from aitaem.specs.segment import SegmentSpec
from aitaem.specs.slice import SliceSpec
from aitaem.utils.exceptions import SpecNotFoundError, SpecValidationError
from aitaem.utils.validation import ValidationError

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
    """Eagerly-loaded cache for metric, slice, and segment specs.

    Use from_yaml() or from_string() as the primary entry points.
    The constructor creates an empty cache; specs can be added via add().
    """

    def __init__(self) -> None:
        """Empty cache. Use from_yaml() or from_string() to load specs."""
        self._metrics: dict[str, MetricSpec] = {}
        self._slices: dict[str, SliceSpec] = {}
        self._segments: dict[str, SegmentSpec] = {}

    @classmethod
    def from_yaml(
        cls,
        metric_paths: str | list[str] | None = None,
        slice_paths: str | list[str] | None = None,
        segment_paths: str | list[str] | None = None,
    ) -> "SpecCache":
        """Load and validate all specs from YAML files or directories.

        Loading is eager — all specs are loaded and validated before returning.

        Raises:
            FileNotFoundError: if a path does not exist
            SpecValidationError: if any spec is invalid
        """
        cache = cls()
        cache._metrics = cls._load_paths_strict(metric_paths, MetricSpec)  # type: ignore[arg-type, assignment]
        cache._slices = cls._load_paths_strict(slice_paths, SliceSpec)  # type: ignore[arg-type, assignment]
        cache._segments = cls._load_paths_strict(segment_paths, SegmentSpec)  # type: ignore[arg-type, assignment]
        cache._validate_slice_cross_references()
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
            cache._metrics.setdefault(spec.name, spec)  # type: ignore[arg-type]
        for yaml_str in cls._normalize_strings(slice_yaml):
            spec = load_spec_from_string(yaml_str, SliceSpec)
            cache._slices.setdefault(spec.name, spec)  # type: ignore[arg-type]
        for yaml_str in cls._normalize_strings(segment_yaml):
            spec = load_spec_from_string(yaml_str, SegmentSpec)
            cache._segments.setdefault(spec.name, spec)  # type: ignore[arg-type]
        cache._validate_slice_cross_references()
        return cache

    def add(self, spec: MetricSpec | SliceSpec | SegmentSpec) -> None:
        """Add a spec programmatically. First-write-wins for duplicate names."""
        if isinstance(spec, MetricSpec):
            self._metrics.setdefault(spec.name, spec)
        elif isinstance(spec, SliceSpec):
            self._slices.setdefault(spec.name, spec)
        elif isinstance(spec, SegmentSpec):
            self._segments.setdefault(spec.name, spec)

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

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_paths(paths: str | list[str] | None) -> list[Path]:
        if paths is None:
            return []
        if isinstance(paths, (str, Path)):
            return [Path(paths)]
        return [Path(p) for p in paths]

    @staticmethod
    def _normalize_strings(strings: str | list[str] | None) -> list[str]:
        if strings is None:
            return []
        if isinstance(strings, str):
            return [strings]
        return list(strings)

    @classmethod
    def _load_paths_strict(
        cls, paths: str | list[str] | None, spec_type: SpecType
    ) -> dict[str, AnySpec]:
        """Load specs from paths/directories, raising on any error."""
        result: dict[str, AnySpec] = {}
        for path in cls._normalize_paths(paths):
            if not path.exists():
                raise FileNotFoundError(f"Spec path not found: {path}")
            if path.is_dir():
                yaml_files = sorted(list(path.glob("*.yaml")) + list(path.glob("*.yml")))
                for yaml_file in yaml_files:
                    spec = spec_type.from_yaml(yaml_file)
                    if spec.name in result:
                        logger.warning("Duplicate spec name '%s'. Overwriting.", spec.name)
                    result[spec.name] = spec
            else:
                spec = spec_type.from_yaml(path)
                if spec.name in result:
                    logger.warning("Duplicate spec name '%s'. Overwriting.", spec.name)
                result[spec.name] = spec
        return result

    def _validate_slice_cross_references(self) -> None:
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
