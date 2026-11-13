"""Small, dependency-free helpers for building SQL safely.

DuckDB's Python client executes multi-statement strings, so any identifier or
string literal interpolated into SQL must be validated or escaped. These helpers
are the single place that is done.

* :func:`sql_str` - escape a value as a single-quoted SQL string literal.
* :func:`quote_ident` - validate a (dotted) identifier and double-quote it.
* :func:`sql_int` - coerce to ``int`` (raises on anything else) for numeric literals.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["sql_str", "quote_ident", "sql_int", "fetch_arrow", "IDENT_RE"]

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sql_str(value: Any) -> str:
    """Return ``value`` as a SQL string literal, doubling embedded single quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def quote_ident(name: Any, what: str = "identifier", max_parts: int = 3) -> str:
    """Validate ``name`` as dot-separated plain identifiers and return it double-quoted.

    Raises ``ValueError`` for anything that is not letters/digits/underscores (so quotes,
    semicolons and whitespace can never reach the SQL text).
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be a non-empty string, got {name!r}.")
    parts = name.split(".")
    if len(parts) > max_parts or not all(IDENT_RE.match(p) for p in parts):
        raise ValueError(
            f"Invalid {what} {name!r}: use up to {max_parts} dot-separated names made of "
            "letters, digits and underscores."
        )
    return ".".join(f'"{p}"' for p in parts)


def sql_int(value: Any, what: str = "value") -> int:
    """Coerce ``value`` to ``int`` for interpolation; reject bools and non-integral values."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be an integer, got {value!r}.")
    try:
        out = int(value)
        if isinstance(value, float) and out != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be an integer, got {value!r}.") from None
    return out


def fetch_arrow(result: Any) -> Any:
    """Materialise a DuckDB connection/relation result as a ``pyarrow.Table`` on any DuckDB version.

    DuckDB renamed and then removed the Arrow accessors: ``fetch_arrow_table()`` (<1.4),
    ``to_arrow_table()`` (>=1.4) and ``.arrow()`` (returns a table or a reader, removed in 2.0).
    """
    for name in ("to_arrow_table", "fetch_arrow_table"):
        fn = getattr(result, name, None)
        if fn is not None:
            return fn()
    out = result.arrow()
    return out.read_all() if hasattr(out, "read_all") else out
