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
    entity_id: str
    values: tuple[SegmentValue, ...]
    description: str = ""
    join_keys: tuple[str, ...] = ()

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
        raw_join_keys = spec_dict.get("join_keys") or []
        join_keys = tuple(raw_join_keys) if isinstance(raw_join_keys, list) else ()

        unknown_fields = set(spec_dict.keys()) - {
            "name", "source", "entity_id", "values", "description", "join_keys"
        }
        if unknown_fields:
            logger.debug("SegmentSpec '%s': ignoring unknown fields: %s", name, unknown_fields)

        return cls(
            name=spec_dict["name"],
            source=spec_dict["source"],
            entity_id=spec_dict["entity_id"],
            values=values,
            description=spec_dict.get("description", ""),
            join_keys=join_keys,
        )

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult (does not raise)."""
        spec_dict: dict = {
            "name": self.name,
            "source": self.source,
            "entity_id": self.entity_id,
            "values": [{"name": v.name, "where": v.where} for v in self.values],
            "description": self.description,
        }
        if self.join_keys:
            spec_dict["join_keys"] = list(self.join_keys)
        return validate_segment_spec(spec_dict)
