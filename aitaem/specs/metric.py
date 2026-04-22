"""
aitaem.specs.metric - MetricSpec dataclass

Pure parsing/validation layer. No Ibis or database dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from pathlib import Path

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationResult, validate_metric_spec
from aitaem.utils.yaml_validation import load_yaml_spec_dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricSpec:
    name: str
    source: str
    numerator: str
    timestamp_col: str
    description: str = ""
    denominator: str | None = None
    entities: list[str] | None = None

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> "MetricSpec":
        """Load and validate a MetricSpec from a YAML file path or YAML string.

        If yaml_input is a Path or a string pointing to an existing file, it is
        read as a file. Otherwise, it is treated as a YAML string.

        Raises:
            SpecValidationError: if validation fails or YAML is malformed
            FileNotFoundError: if a Path is provided but the file does not exist
        """
        spec_dict = load_yaml_spec_dict(yaml_input, "metric")

        result = validate_metric_spec(spec_dict)
        name = spec_dict.get("name") if isinstance(spec_dict.get("name"), str) else None

        if not result.valid:
            raise SpecValidationError("metric", name, result.errors)

        denominator = spec_dict.get("denominator") or None
        entities_raw = spec_dict.get("entities")
        entities = list(entities_raw) if entities_raw else None

        unknown_fields = set(spec_dict.keys()) - {f.name for f in fields(cls)}
        if unknown_fields:
            logger.debug("MetricSpec '%s': ignoring unknown fields: %s", name, unknown_fields)

        return cls(
            name=spec_dict["name"],
            source=spec_dict["source"],
            numerator=spec_dict["numerator"],
            timestamp_col=spec_dict["timestamp_col"],
            description=spec_dict.get("description", ""),
            denominator=denominator,
            entities=entities,
        )

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult (does not raise)."""
        spec_dict: dict[str, object] = {
            "name": self.name,
            "source": self.source,
            "numerator": self.numerator,
            "timestamp_col": self.timestamp_col,
            "description": self.description,
        }
        if self.denominator is not None:
            spec_dict["denominator"] = self.denominator
        if self.entities is not None:
            spec_dict["entities"] = self.entities
        return validate_metric_spec(spec_dict)
