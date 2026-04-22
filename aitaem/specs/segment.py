"""
aitaem.specs.segment - SegmentSpec dataclass

Pure parsing/validation layer. No Ibis or database dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationResult, validate_segment_spec
from aitaem.utils.yaml_validation import load_yaml_spec_dict

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
            FileNotFoundError: if a Path is provided but the file does not exist
        """
        spec_dict = load_yaml_spec_dict(yaml_input, "segment")

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
