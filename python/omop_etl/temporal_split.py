"""Held-out temporal split (FR-C2): move post-cutoff rows into a separate DuckDB file.

:func:`split_by_date` writes every row dated on/after ``cutoff`` to ``out_db`` (same CDM schema) and optionally deletes
them from the source, so the prospective data set can be kept out of model development. Row counts and per-table
checksums of both halves are returned, and recorded in a :class:`~omop_etl.run_manifest.RunManifest` when one is given.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb

from omop_etl.paths import sql_path_literal

__all__ = ["split_by_date", "DEFAULT_SPLIT_DATE_COLUMNS"]

# table -> column that decides which side of the cutoff the row falls on
DEFAULT_SPLIT_DATE_COLUMNS: dict[str, str] = {
    "visit_occurrence": "visit_start_date",
    "visit_detail": "visit_detail_start_date",
    "condition_occurrence": "condition_start_date",
    "procedure_occurrence": "procedure_date",
    "drug_exposure": "drug_exposure_start_date",
    "measurement": "measurement_date",
    "observation": "observation_date",
    "device_exposure": "device_exposure_start_date",
    "death": "death_date",
    "condition_era": "condition_era_start_date",
    "drug_era": "drug_era_start_date",
    "observation_period": "observation_period_start_date",
}


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Invalid identifier {name!r}.")
    return f'"{name}"'


def _checksum(con: duckdb.DuckDBPyConnection, qualified: str) -> tuple[int, int]:
    """(row count, order-independent checksum over all columns)."""
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {qualified}").fetchall()]
    row_hash = "hash(" + ", ".join(f'"{c}"' for c in cols) + ")" if cols else "0"
    n, ck = con.execute(f"SELECT count(*), COALESCE(sum({row_hash}::HUGEINT), 0)::HUGEINT FROM {qualified}").fetchone()
    return int(n), int(ck)


def split_by_date(
    con: duckdb.DuckDBPyConnection,
    cutoff: str | _dt.date | _dt.datetime,
    out_db: str | Path,
    tables: Sequence[str] | Mapping[str, str] | None = None,
    *,
    remove_from_source: bool = False,
    manifest: Any = None,
) -> dict[str, Any]:
    """Copies rows dated ``>= cutoff`` into ``out_db``.

    Args:
        con: Writable connection to the CDM (a read-only one is fine unless ``remove_from_source``).
        cutoff: Split date (inclusive start of the held-out period).
        out_db: DuckDB file to create; it receives every table in ``tables`` (post-cutoff rows only) and the
            untouched ``person`` / reference tables that are not dated (full copies) so it is self-contained.
        tables: Dated tables to split, or ``{table: date_column}``. Default: the CDM clinical tables present, using
            :data:`DEFAULT_SPLIT_DATE_COLUMNS`.
        remove_from_source: Delete the moved rows from the source database afterwards.
        manifest: Optional ``RunManifest``; per-table counts are added as steps.

    Returns:
        ``{"cutoff", "tables": {table: {"source_before", "kept", "moved", "kept_checksum", "moved_checksum",
        "source_checksum"}}}``. The invariant ``kept + moved == source_before`` (rows) and checksum sums hold
        (``kept_checksum + moved_checksum == source_checksum``); a violation raises ``RuntimeError``.
    """
    cut = cutoff.date().isoformat() if isinstance(cutoff, _dt.datetime) else (
        cutoff.isoformat() if isinstance(cutoff, _dt.date) else str(cutoff))
    _dt.date.fromisoformat(cut)  # validate
    have = {r[0].lower() for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if tables is None:
        spec = {t: c for t, c in DEFAULT_SPLIT_DATE_COLUMNS.items() if t in have}
    elif isinstance(tables, Mapping):
        spec = dict(tables)
    else:
        spec = {t: DEFAULT_SPLIT_DATE_COLUMNS[t] for t in tables}
    missing = [t for t in spec if t.lower() not in have]
    if missing:
        raise LookupError(f"Tables not found: {missing}")
    out_path = Path(out_db)
    if out_path.exists():
        raise FileExistsError(f"{out_db} already exists; refusing to overwrite a held-out database.")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    alias = "_heldout_split"
    con.execute(f"ATTACH {sql_path_literal(out_path)} AS {alias}")
    report: dict[str, Any] = {"cutoff": cut, "tables": {}}
    try:
        # undated tables are copied in full so the held-out file is self-contained
        for t in ("person", "provider", "care_site", "location", "cdm_source"):
            if t in have and t not in spec:
                con.execute(f"CREATE TABLE {alias}.main.{_ident(t)} AS SELECT * FROM {_ident(t)}")
        for t, col in spec.items():
            q, c = _ident(t), _ident(col)
            before = _checksum(con, q)
            con.execute(f"CREATE TABLE {alias}.main.{_ident(t)} AS SELECT * FROM {q} WHERE {c} >= DATE '{cut}'")
            moved = _checksum(con, f"{alias}.main.{_ident(t)}")
            kept = _checksum(con, f"(SELECT * FROM {q} WHERE {c} < DATE '{cut}' OR {c} IS NULL)")
            if kept[0] + moved[0] != before[0] or kept[1] + moved[1] != before[1]:
                raise RuntimeError(f"Split of {t} does not reconstruct the source (counts/checksums differ).")
            report["tables"][t] = {"source_before": before[0], "kept": kept[0], "moved": moved[0],
                                   "source_checksum": before[1], "kept_checksum": kept[1], "moved_checksum": moved[1]}
            if manifest is not None:
                manifest.add_step(f"split:{t}", moved[0], kept=kept[0], source=before[0])
        if remove_from_source:
            for t, col in spec.items():
                con.execute(f"DELETE FROM {_ident(t)} WHERE {_ident(col)} >= DATE '{cut}'")
    finally:
        con.execute(f"DETACH {alias}")
    return report
