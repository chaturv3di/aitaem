"""
aitaem.specs.slice - SliceSpec dataclass

Pure parsing/validation layer. No Ibis or database dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationResult, validate_slice_spec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SliceValue:
    name: str
    where: str


@dataclass(frozen=True)
class SliceSpec:
    name: str
    values: tuple[SliceValue, ...] = ()  # Leaf spec — direct WHERE-based values
    cross_product: tuple[str, ...] = ()  # Composite spec — names of other SliceSpecs
    description: str = ""

    @property
    def is_composite(self) -> bool:
        """True if this spec references other SliceSpecs via cross_product."""
        return bool(self.cross_product)

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> "SliceSpec":
        """Load and validate a SliceSpec from a YAML file path or YAML string.

        Expects top-level key 'slice:'.

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
            raise SpecValidationError("slice", None, [])

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            from aitaem.utils.validation import ValidationError

            raise SpecValidationError(
                "slice", None, [ValidationError(field="yaml", message=f"Invalid YAML syntax: {e}")]
            )

        if not isinstance(data, dict) or "slice" not in data:
            from aitaem.utils.validation import ValidationError

            got_keys = list(data.keys()) if isinstance(data, dict) else []
            raise SpecValidationError(
                "slice",
                None,
                [
                    ValidationError(
                        field="yaml", message=f"Expected top-level key 'slice', got: {got_keys}"
                    )
                ],
            )

        spec_dict = data["slice"]
        if not isinstance(spec_dict, dict):
            from aitaem.utils.validation import ValidationError

            raise SpecValidationError(
                "slice",
                None,
                [ValidationError(field="slice", message="'slice' value must be a mapping")],
            )

        result = validate_slice_spec(spec_dict)
        name = spec_dict.get("name") if isinstance(spec_dict.get("name"), str) else None

        if not result.valid:
            raise SpecValidationError("slice", name, result.errors)

        raw_cross_product = spec_dict.get("cross_product")
        values: tuple[SliceValue, ...] = ()
        cross_product: tuple[str, ...] = ()
        if raw_cross_product is not None:
            # Composite spec
            cross_product = tuple(raw_cross_product)
        else:
            # Leaf spec
            values = tuple(
                SliceValue(name=v["name"], where=v["where"]) for v in spec_dict["values"]
            )

        unknown_fields = set(spec_dict.keys()) - {"name", "values", "cross_product", "description"}
        if unknown_fields:
            logger.debug("SliceSpec '%s': ignoring unknown fields: %s", name, unknown_fields)

        return cls(
            name=spec_dict["name"],
            values=values,
            cross_product=cross_product,
            description=spec_dict.get("description", ""),
        )

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult (does not raise)."""
        spec_dict: dict = {"name": self.name, "description": self.description}
        if self.is_composite:
            spec_dict["cross_product"] = list(self.cross_product)
        else:
            spec_dict["values"] = [{"name": v.name, "where": v.where} for v in self.values]
        return validate_slice_spec(spec_dict)
