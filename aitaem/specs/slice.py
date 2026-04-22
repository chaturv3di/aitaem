"""
aitaem.specs.slice - SliceSpec dataclass

Pure parsing/validation layer. No Ibis or database dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationResult, validate_slice_spec
from aitaem.utils.yaml_validation import load_yaml_spec_dict

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
    column: str = ""  # Wildcard spec — bare column name
    description: str = ""

    @property
    def is_composite(self) -> bool:
        """True if this spec references other SliceSpecs via cross_product."""
        return bool(self.cross_product)

    @property
    def is_wildcard(self) -> bool:
        """True if this spec auto-discovers values from a column at query time."""
        return bool(self.column)

    @classmethod
    def from_yaml(cls, yaml_input: str | Path) -> "SliceSpec":
        """Load and validate a SliceSpec from a YAML file path or YAML string.

        Expects top-level key 'slice:'.

        Raises:
            SpecValidationError: if validation fails or YAML is malformed
            FileNotFoundError: if a Path is provided but the file does not exist
        """
        spec_dict = load_yaml_spec_dict(yaml_input, "slice")

        result = validate_slice_spec(spec_dict)
        name = spec_dict.get("name") if isinstance(spec_dict.get("name"), str) else None

        if not result.valid:
            raise SpecValidationError("slice", name, result.errors)

        raw_cross_product = spec_dict.get("cross_product")
        raw_where = spec_dict.get("where")
        values: tuple[SliceValue, ...] = ()
        cross_product: tuple[str, ...] = ()
        column: str = ""
        if raw_cross_product is not None:
            # Composite spec
            cross_product = tuple(raw_cross_product)
        elif raw_where is not None:
            # Wildcard spec
            column = str(raw_where)
        else:
            # Leaf spec
            values = tuple(
                SliceValue(name=v["name"], where=v["where"]) for v in spec_dict["values"]
            )

        unknown_fields = set(spec_dict.keys()) - {
            "name",
            "values",
            "cross_product",
            "where",
            "description",
        }
        if unknown_fields:
            logger.debug("SliceSpec '%s': ignoring unknown fields: %s", name, unknown_fields)

        return cls(
            name=spec_dict["name"],
            values=values,
            cross_product=cross_product,
            column=column,
            description=spec_dict.get("description", ""),
        )

    def validate(self) -> ValidationResult:
        """Validate spec fields and return a ValidationResult (does not raise)."""
        spec_dict: dict = {"name": self.name, "description": self.description}
        if self.is_composite:
            spec_dict["cross_product"] = list(self.cross_product)
        elif self.is_wildcard:
            spec_dict["where"] = self.column
        else:
            spec_dict["values"] = [{"name": v.name, "where": v.where} for v in self.values]
        return validate_slice_spec(spec_dict)
