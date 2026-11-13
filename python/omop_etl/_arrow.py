"""DuckDB result -> pyarrow Table across DuckDB versions (``.arrow()`` was removed in 2.0; ``fetch_arrow_table`` before 1.4)."""
from __future__ import annotations

from typing import Any


def to_arrow_table(result: Any):
    """Return a ``pyarrow.Table`` from a DuckDB result (or relation) on any supported DuckDB version."""
    for name in ("to_arrow_table", "fetch_arrow_table"):
        fn = getattr(result, name, None)
        if fn is not None:
            return fn()
    reader = result.arrow()
    return reader.read_all() if hasattr(reader, "read_all") else reader
