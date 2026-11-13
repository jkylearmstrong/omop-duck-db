"""Validation for operator-supplied SQL identifiers that are interpolated into statements (review M-19)."""
from __future__ import annotations

import re
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_ident(name: Any, what: str = "identifier", max_parts: int = 3) -> str:
    """Return ``name`` unchanged if it is 1..``max_parts`` dot-separated plain identifiers, else raise ``ValueError``."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be a non-empty string, got {name!r}.")
    parts = name.split(".")
    if len(parts) > max_parts or not all(_IDENT.match(p) for p in parts):
        raise ValueError(
            f"Invalid {what} {name!r}: use up to {max_parts} dot-separated names of letters, digits and underscores."
        )
    return name
