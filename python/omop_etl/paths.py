"""Cross-platform path sanitization for DuckDB file operations.

DuckDB SQL string literals are parsed by DuckDB itself, so raw Windows paths (``C:\\data\\x.csv``),
paths containing single quotes, or ``pathlib`` objects interpolated into f-strings are a recurring source
of parser errors. Everything that puts a filesystem path into SQL should go through :func:`sanitize_sql_path`
(plain string) or :func:`sql_path_literal` (quoted, escaped literal).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Union

_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+://")

__all__ = [
    "sanitize_sql_path",
    "sql_path_literal",
    "attach_database",
    "read_csv_sql",
    "read_parquet_sql",
    "copy_to",
]


def sanitize_sql_path(p: Union[str, Path]) -> str:
    """Resolve ``p`` to an absolute path with forward slashes, safe to place inside a SQL literal.

    Remote URIs (``s3://``, ``https://``, ``gs://`` ...) are only slash-normalized. Glob patterns survive
    because the path is never required to exist.
    """
    s = str(p)
    if _URI_RE.match(s):
        return s.replace("\\", "/")
    return str(Path(s).resolve()).replace("\\", "/")


def sql_path_literal(p: Union[str, Path]) -> str:
    """:func:`sanitize_sql_path` as a quoted SQL string literal with embedded single quotes escaped."""
    return "'" + sanitize_sql_path(p).replace("'", "''") + "'"


def _options_sql(options: Mapping[str, Any] | None) -> str:
    if not options:
        return ""
    parts = []
    for key, value in options.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif isinstance(value, (list, tuple)):
            rendered = "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in value) + "]"
        else:
            rendered = "'" + str(value).replace("'", "''") + "'"
        parts.append(f"{key} = {rendered}")
    return ", " + ", ".join(parts)


def read_csv_sql(path: Union[str, Path], **options: Any) -> str:
    """``read_csv_auto('<sanitized path>', opt = ...)`` table-function expression."""
    return f"read_csv_auto({sql_path_literal(path)}{_options_sql(options)})"


def read_parquet_sql(path: Union[str, Path], **options: Any) -> str:
    """``read_parquet('<sanitized path>', opt = ...)`` table-function expression."""
    return f"read_parquet({sql_path_literal(path)}{_options_sql(options)})"


def attach_database(con, path: Union[str, Path], alias: str, *, read_only: bool = False,
                    db_type: str | None = None) -> None:
    """``ATTACH`` a database file with a sanitized path."""
    opts = []
    if db_type:
        opts.append(f"TYPE {db_type}")
    if read_only:
        opts.append("READ_ONLY")
    suffix = f" ({', '.join(opts)})" if opts else ""
    con.execute(f"ATTACH {sql_path_literal(path)} AS {alias}{suffix}")


def copy_to(con, source: str, path: Union[str, Path], *, format: str = "PARQUET", **options: Any) -> None:
    """``COPY <table or (query)> TO '<sanitized path>' (FORMAT ...)``.

    ``source`` is a table name or a parenthesized query; ``options`` become COPY options.
    """
    opts = f"FORMAT {format}"
    for key, value in options.items():
        if isinstance(value, bool):
            opts += f", {key} {'TRUE' if value else 'FALSE'}"
        elif isinstance(value, (int, float)):
            opts += f", {key} {value}"
        else:
            opts += f", {key} '{str(value)}'"
    con.execute(f"COPY {source} TO {sql_path_literal(path)} ({opts})")
