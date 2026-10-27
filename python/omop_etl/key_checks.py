"""Build-time surrogate-key collision check (FR-C8).

``pcornet_id()`` maps a source id to a 63-bit key. A collision silently merges two source rows, so the number of
distinct source ids must equal the number of distinct keys. :func:`check_key_collisions` verifies that for one
source column and reports (or raises on) any loss.
"""

from __future__ import annotations

import re
import warnings
from typing import Any

import duckdb

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_COLUMN = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*|"[^"]+")$')


class KeyCollisionError(RuntimeError):
    """Raised when distinct source ids map to fewer distinct surrogate keys."""


def check_key_collisions(
    con: duckdb.DuckDBPyConnection,
    source: str,
    id_column: str,
    *,
    key_expr: str = "pcornet_id({col})",
    on_collision: str = "warn",
) -> dict[str, Any]:
    """Compares distinct source ids with distinct surrogate keys.

    Parameters
    ----------
    source : table, view or ``read_csv(...)``/``read_parquet(...)`` relation expression to read ids from.
    id_column : column holding the source id.
    key_expr : key expression with a ``{col}`` placeholder (default ``pcornet_id({col})``).
    on_collision : ``"warn"`` (default), ``"raise"`` or ``"ignore"``.

    Returns ``{"source", "id_column", "distinct_ids", "distinct_keys", "lost", "ok"}``.
    """
    if on_collision not in ("warn", "raise", "ignore"):
        raise ValueError("on_collision must be 'warn', 'raise' or 'ignore'.")
    if not _COLUMN.match(id_column):
        raise ValueError(f"Invalid id_column {id_column!r}.")
    rel = source if (_IDENT.match(source) or "(" in source) else None
    if rel is None:
        raise ValueError(f"Invalid source {source!r}.")
    key = key_expr.format(col=f"CAST({id_column} AS VARCHAR)" if "{col}" in key_expr else id_column)
    ids, keys = con.execute(
        f"SELECT COUNT(DISTINCT {id_column}), COUNT(DISTINCT {key}) FROM {rel} WHERE {id_column} IS NOT NULL"
    ).fetchone()
    ids, keys = int(ids), int(keys)
    res = {"source": source, "id_column": id_column, "distinct_ids": ids, "distinct_keys": keys,
           "lost": ids - keys, "ok": ids == keys}
    if not res["ok"]:
        msg = (f"Surrogate-key collision: {ids} distinct {source}.{id_column} values map to only {keys} keys "
               f"({ids - keys} would be merged into other rows).")
        if on_collision == "raise":
            raise KeyCollisionError(msg)
        if on_collision == "warn":
            warnings.warn(msg, UserWarning, stacklevel=2)
    return res


def check_etl_keys(
    con: duckdb.DuckDBPyConnection,
    source_dir: str,
    *,
    site_id: int | None = None,
    disambiguate_patids: bool = False,
    on_collision: str = "warn",
) -> list[dict[str, Any]]:
    """Runs :func:`check_key_collisions` on the source ids the PCORnet ETL turns into surrogate keys (FR-C12).

    Checks ``ENCOUNTERID`` -> ``visit_occurrence_id`` (``pcornet_id``) and, when ``disambiguate_patids`` is set,
    ``PATID`` -> ``person_id``. (Without it person ids are sequential, hence collision-free.) Each result also carries
    ``cdm_rows``, the rows now in ``visit_occurrence`` / ``person``, so a rebuilt site can confirm raw distinct
    encounters equal ``visit_occurrence`` rows. A source table that is missing is skipped.
    """
    from omop_etl.build_omop_cdm import DEFAULT_ALIASES, _source_relation, find_source_file

    jobs = [("encounter", "ENCOUNTERID", "pcornet_id({col})", "visit_occurrence")]
    if disambiguate_patids and site_id is not None:
        jobs.append(("demographic", "PATID", f"pcornet_id({{col}} || '-{int(site_id)}')", "person"))
    out: list[dict[str, Any]] = []
    for table, column, key_expr, cdm_table in jobs:
        path = find_source_file(source_dir, table)
        if path is None:
            continue
        relation = _source_relation(path)
        actual = {c[0].upper(): c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {relation} LIMIT 0").fetchall()}
        candidates = [column] + list(DEFAULT_ALIASES.get(column, []))
        found = next((actual[c.upper()] for c in candidates if c.upper() in actual), None)
        if found is None:
            continue
        res = check_key_collisions(con, relation, f'"{found}"' if not found.isidentifier() else found,
                                   key_expr=key_expr, on_collision=on_collision)
        res.update(table=table, cdm_table=cdm_table,
                   cdm_rows=int(con.execute(f"SELECT count(*) FROM {cdm_table}").fetchone()[0]))
        out.append(res)
    return out
