"""
aitaem.utils.yaml_validation - Shared YAML loading and structural validation

Handles the common pre-processing preamble shared by all spec from_yaml() methods:
file-vs-string detection (including PATH_MAX safety), file reading, empty-input
guard, YAML parsing, and top-level key/type assertions.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from aitaem.utils.exceptions import SpecValidationError
from aitaem.utils.validation import ValidationError


def load_yaml_spec_dict(yaml_input: str | Path, spec_type_name: str) -> dict:
    """Resolve yaml_input to a raw spec dict.

    1. Detects whether yaml_input is a file path or a YAML string, handling
       OSError from path.exists() (e.g. ENAMETOOLONG) by treating the input
       as YAML content.
    2. Reads the file or uses the string as-is.
    3. Guards against empty input.
    4. Parses YAML, raising SpecValidationError on YAMLError.
    5. Validates the top-level key equals spec_type_name.
    6. Validates the value under that key is a dict.

    Returns:
        The dict under the top-level key.

    Raises:
        FileNotFoundError: if yaml_input is a Path (or a string that resolves
            to an existing file path) and the file does not exist.
        SpecValidationError: for empty input, bad YAML, missing/wrong
            top-level key, or a non-dict top-level value.
    """
    raw = _read_input(yaml_input, spec_type_name)

    if not raw or not raw.strip():
        raise SpecValidationError(spec_type_name, None, [])

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise SpecValidationError(
            spec_type_name,
            None,
            [ValidationError(field="yaml", message=f"Invalid YAML syntax: {e}")],
        )

    if not isinstance(data, dict) or spec_type_name not in data:
        got_keys = list(data.keys()) if isinstance(data, dict) else []
        raise SpecValidationError(
            spec_type_name,
            None,
            [
                ValidationError(
                    field="yaml",
                    message=f"Expected top-level key '{spec_type_name}', got: {got_keys}",
                )
            ],
        )

    spec_dict = data[spec_type_name]
    if not isinstance(spec_dict, dict):
        raise SpecValidationError(
            spec_type_name,
            None,
            [
                ValidationError(
                    field=spec_type_name,
                    message=f"'{spec_type_name}' value must be a mapping",
                )
            ],
        )

    return spec_dict


def _read_input(yaml_input: str | Path, spec_type_name: str) -> str:
    """Return the raw YAML text from yaml_input.

    If yaml_input is a Path object, always treats it as a file path.
    If yaml_input is a string, checks whether it resolves to an existing file,
    wrapping path.exists() in try/except OSError so that strings longer than
    PATH_MAX (which raise ENAMETOOLONG) are correctly treated as YAML content.
    """
    if isinstance(yaml_input, Path):
        if not yaml_input.exists():
            raise FileNotFoundError(f"Spec file not found: {yaml_input}")
        try:
            return yaml_input.read_text(encoding="utf-8")
        except OSError as e:
            raise FileNotFoundError(f"Cannot read file: {yaml_input}") from e

    # yaml_input is a str — check if it resolves to an existing file
    path = Path(str(yaml_input))
    try:
        is_file = path.is_file()
    except OSError:
        # ENAMETOOLONG or similar — string exceeds PATH_MAX and cannot be a
        # file path; treat it as YAML content directly.
        is_file = False

    if is_file:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise FileNotFoundError(f"Cannot read file: {path}") from e

    return str(yaml_input)
