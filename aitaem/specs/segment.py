"""
aitaem.specs.segment - SegmentSpec dataclass

Pure parsing/validation layer. No Ibis or database dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationResult, validate_segment_spec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SegmentValue:
    name: str
    where: str


@dataclass(frozen=True)
class SegmentSpec:
    name: str
    source: str
    values: tuple[SegmentValue, ...]
    description: str = ""

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> "SegmentSpec":
        """Load and validate a SegmentSpec from a YAML file path or YAML string.

        Expects top-level key 'segment:'.

        Raises:
            SpecValidationError: if validation fails or YAML is malformed
            FileNotFoundError: if path provided but file does not exist
        """
        is_path = isinstance(yaml_input, Path)
        path: Path = yaml_input if isinstance(yaml_input, Path) else Path(str(yaml_input))

        if is_path or path.exists():
            if not path.exists():
                raise FileNotFoundError(f"Spec file not found: {path}")
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as e:
                raise FileNotFoundError(f"Cannot read file: {path}") from e
        else:
            raw = str(yaml_input)

        if not raw or not raw.strip():
            raise SpecValidationError("segment", None, [])

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            from aitaem.utils.validation import ValidationError

            raise SpecValidationError(
                "segment",
                None,
                [ValidationError(field="yaml", message=f"Invalid YAML syntax: {e}")],
            )

        if not isinstance(data, dict) or "segment" not in data:
            from aitaem.utils.validation import ValidationError

            got_keys = list(data.keys()) if isinstance(data, dict) else []
            raise SpecValidationError(
                "segment",
                None,
                [
                    ValidationError(
                        field="yaml", message=f"Expected top-level key 'segment', got: {got_keys}"
                    )
                ],
            )

        spec_dict = data["segment"]
        if not isinstance(spec_dict, dict):
            from aitaem.utils.validation import ValidationError

            raise SpecValidationError(
                "segment",
                None,
                [ValidationError(field="segment", message="'segment' value must be a mapping")],
            )

        result = validate_segment_spec(spec_dict)
        name = spec_dict.get("name") if isinstance(spec_dict.get("name"), str) else None

        if not result.valid:
            raise SpecValidationError("segment", name, result.errors)

        values = tuple(SegmentValue(name=v["name"], where=v["where"]) for v in spec_dict["values"])

        unknown_fields = set(spec_dict.keys()) - {"name", "source", "values", "description"}
        if unknown_fields:
            logger.debug("SegmentSpec '%s': ignoring unknown fields: %s", name, unknown_fields)

        return cls(
            name=spec_dict["name"],
            source=spec_dict["source"],
            values=values,
            description=spec_dict.get("description", ""),
        )

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult (does not raise)."""
        spec_dict = {
            "name": self.name,
            "source": self.source,
            "values": [{"name": v.name, "where": v.where} for v in self.values],
            "description": self.description,
        }
        return validate_segment_spec(spec_dict)
