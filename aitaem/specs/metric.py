"""
aitaem.specs.metric - MetricSpec dataclass

Pure parsing/validation layer. No Ibis or database dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationResult, validate_metric_spec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricSpec:
    name: str
    source: str
    aggregation: str
    numerator: str
    timestamp_col: str
    description: str = ""
    denominator: str | None = None

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> "MetricSpec":
        """Load and validate a MetricSpec from a YAML file path or YAML string.

        If yaml_input is a valid file path (exists on disk), it is read as a file.
        Otherwise, it is treated as a YAML string.

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
            raise SpecValidationError("metric", None, [])

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            from aitaem.utils.validation import ValidationError

            raise SpecValidationError(
                "metric", None, [ValidationError(field="yaml", message=f"Invalid YAML syntax: {e}")]
            )

        if not isinstance(data, dict) or "metric" not in data:
            from aitaem.utils.validation import ValidationError

            got_keys = list(data.keys()) if isinstance(data, dict) else []
            raise SpecValidationError(
                "metric",
                None,
                [
                    ValidationError(
                        field="yaml", message=f"Expected top-level key 'metric', got: {got_keys}"
                    )
                ],
            )

        spec_dict = data["metric"]
        if not isinstance(spec_dict, dict):
            from aitaem.utils.validation import ValidationError

            raise SpecValidationError(
                "metric",
                None,
                [ValidationError(field="metric", message="'metric' value must be a mapping")],
            )

        result = validate_metric_spec(spec_dict)
        name = spec_dict.get("name") if isinstance(spec_dict.get("name"), str) else None

        if not result.valid:
            raise SpecValidationError("metric", name, result.errors)

        aggregation = spec_dict["aggregation"].lower()
        denominator = spec_dict.get("denominator") or None

        unknown_fields = set(spec_dict.keys()) - {f.name for f in fields(cls)}
        if unknown_fields:
            logger.debug("MetricSpec '%s': ignoring unknown fields: %s", name, unknown_fields)

        return cls(
            name=spec_dict["name"],
            source=spec_dict["source"],
            aggregation=aggregation,
            numerator=spec_dict["numerator"],
            timestamp_col=spec_dict["timestamp_col"],
            description=spec_dict.get("description", ""),
            denominator=denominator,
        )

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult (does not raise)."""
        spec_dict = {
            "name": self.name,
            "source": self.source,
            "aggregation": self.aggregation,
            "numerator": self.numerator,
            "timestamp_col": self.timestamp_col,
            "description": self.description,
        }
        if self.denominator is not None:
            spec_dict["denominator"] = self.denominator
        return validate_metric_spec(spec_dict)
