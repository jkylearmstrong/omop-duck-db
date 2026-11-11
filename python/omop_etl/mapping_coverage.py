"""Concept-mapping coverage report: what share of source codes (and rows) reached a standard concept (ODD-8).

After a PCORnet (or any) ETL, the first question a reviewer asks is how much of the source vocabulary was actually mapped.
:func:`mapping_coverage_report` answers it per CDM domain table - conditions (PCORnet ``diagnosis``), procedures,
drugs (``prescribing`` / ``dispensing`` / ``med_admin``), measurements (``lab_result_cm`` / ``vital``), observations and
visits - from the loaded tables alone, so it works for any source mapped into the OMOP CDM.

A row is *mapped* when its standard concept id is non-zero. A source code (the ``*_source_value``) is *fully mapped* when
all its rows are mapped, *partially mapped* when some are (the same code reached different concepts or none), *unmapped* when
none are. Where the vocabulary is loaded, ``pct_rows_standard`` also checks that the mapped concepts really are standard.

Counts below ``min_cell`` (default 11) are suppressed, and source codes that occur on fewer than ``min_cell`` rows are folded
into one ``(codes with fewer than N rows)`` line, because a rare code can single out a patient.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

import numpy as np
import pandas as pd

from omop_etl.disclosure import DEFAULT_MIN_CELL

__all__ = ["mapping_coverage_report", "COVERAGE_DOMAINS"]

#: domain table -> (standard concept id column, source value column)
COVERAGE_DOMAINS: dict[str, tuple[str, str]] = {
    "condition_occurrence": ("condition_concept_id", "condition_source_value"),
    "procedure_occurrence": ("procedure_concept_id", "procedure_source_value"),
    "drug_exposure": ("drug_concept_id", "drug_source_value"),
    "measurement": ("measurement_concept_id", "measurement_source_value"),
    "observation": ("observation_concept_id", "observation_source_value"),
    "visit_occurrence": ("visit_concept_id", "visit_source_value"),
}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COUNT_COLS = ["n_rows", "n_rows_mapped", "n_rows_unmapped", "n_source_codes", "n_codes_fully_mapped",
               "n_codes_partially_mapped", "n_codes_unmapped"]


def _table_exists(con: Any, table: str) -> bool:
    try:
        con.execute(f'SELECT 1 FROM "{table}" LIMIT 0')
        return True
    except Exception:
        return False


def _vocab_loaded(con: Any) -> bool:
    try:
        return int(con.execute("SELECT COUNT(*) FROM concept").fetchone()[0]) > 0
    except Exception:
        return False


def mapping_coverage_report(con: Any, domains: Mapping[str, tuple[str, str]] | None = None, *,
                            min_cell: int | None = DEFAULT_MIN_CELL, top_unmapped: int = 25) -> dict[str, pd.DataFrame]:
    """Reports mapping coverage per domain table.

    Args:
        con: DuckDB connection holding the loaded CDM tables.
        domains: ``{table: (concept_id_column, source_value_column)}``; default :data:`COVERAGE_DOMAINS`.
        min_cell: Suppression threshold (``None`` keeps exact counts - for use inside the site only).
        top_unmapped: How many of the most frequent unmapped source codes to list per domain.

    Returns:
        ``{"summary": DataFrame, "unmapped": DataFrame}``. ``summary`` has one row per domain: ``n_rows``,
        ``n_rows_mapped``, ``pct_rows_mapped``, ``n_source_codes``, ``n_codes_fully_mapped`` / ``_partially_mapped`` /
        ``_unmapped``, ``pct_codes_fully_mapped``, ``pct_rows_standard`` (NaN without a vocabulary) and ``status``
        (``ok`` / ``table missing`` / ``empty``). ``unmapped`` lists the most frequent source codes with no standard concept.
    """
    spec = dict(domains or COVERAGE_DOMAINS)
    mc = min_cell if (min_cell and min_cell > 1) else None
    vocab = _vocab_loaded(con)
    srows, urows = [], []
    for table, (idcol, srccol) in spec.items():
        for name in (table, idcol, srccol):
            if not _IDENT.match(str(name)):
                raise ValueError(f"not a valid identifier: {name!r}")
        rec: dict[str, Any] = {"domain": table, **{c: np.nan for c in _COUNT_COLS}, "pct_rows_mapped": np.nan,
                               "pct_codes_fully_mapped": np.nan, "pct_rows_standard": np.nan}
        if not _table_exists(con, table):
            rec["status"] = "table missing"
            srows.append(rec)
            continue
        base = (f'SELECT COALESCE(CAST("{srccol}" AS VARCHAR), \'(null)\') AS code, '
                f'CASE WHEN COALESCE("{idcol}", 0) <> 0 THEN 1 ELSE 0 END AS mapped, "{idcol}" AS cid FROM "{table}"')
        per_code = f"SELECT code, COUNT(*) AS n, SUM(mapped) AS m FROM ({base}) GROUP BY code"
        tot = con.execute(
            f"SELECT COALESCE(SUM(n), 0), COALESCE(SUM(m), 0), COUNT(*), "
            f"COALESCE(SUM(CASE WHEN m = n THEN 1 ELSE 0 END), 0), COALESCE(SUM(CASE WHEN m > 0 AND m < n THEN 1 ELSE 0 END), 0), "
            f"COALESCE(SUM(CASE WHEN m = 0 THEN 1 ELSE 0 END), 0) FROM ({per_code})").fetchone()
        n, m, ncodes, full, part, none = (int(x) for x in tot)
        if n == 0:
            rec["status"] = "empty"
            srows.append(rec)
            continue
        rec.update(n_rows=n, n_rows_mapped=m, n_rows_unmapped=n - m, n_source_codes=ncodes, n_codes_fully_mapped=full,
                   n_codes_partially_mapped=part, n_codes_unmapped=none, pct_rows_mapped=100.0 * m / n,
                   pct_codes_fully_mapped=100.0 * full / ncodes if ncodes else np.nan, status="ok")
        if vocab:
            std = con.execute(
                f"SELECT COUNT(*) FROM ({base}) b JOIN concept c ON c.concept_id = b.cid WHERE b.mapped = 1 AND c.standard_concept = 'S'"
            ).fetchone()[0]
            rec["pct_rows_standard"] = 100.0 * int(std) / n
        srows.append(rec)
        # unmapped source codes, most frequent first; rare codes are folded into one line
        floor = mc or 1
        un = con.execute(f"SELECT code, n - m AS n_unmapped FROM ({per_code}) WHERE n - m > 0 ORDER BY n - m DESC, code").df()
        shown = un[un["n_unmapped"] >= floor].head(top_unmapped)
        for r in shown.itertuples(index=False):
            urows.append({"domain": table, "source_value": r.code, "n_rows_unmapped": int(r.n_unmapped)})
        rest = int(un["n_unmapped"].sum()) - int(shown["n_unmapped"].sum())
        if rest > 0 and (mc is None or rest >= mc):
            tail = f"(other unmapped codes; codes with fewer than {mc} rows are pooled here)" if mc else "(other unmapped codes)"
            urows.append({"domain": table, "source_value": tail, "n_rows_unmapped": rest})

    summary = pd.DataFrame(srows)
    if mc:
        small = pd.DataFrame({c: (summary[c] > 0) & (summary[c] < mc) for c in _COUNT_COLS}).any(axis=1)
        # the counts are linked (mapped + unmapped = rows, ...), so one hidden count hides the row's counts and percentages
        summary["suppressed"] = small
        hide_cols = _COUNT_COLS + ["pct_rows_mapped", "pct_codes_fully_mapped", "pct_rows_standard"]
        summary.loc[small, hide_cols] = np.nan
    else:
        summary["suppressed"] = False
    for c in _COUNT_COLS:
        summary[c] = summary[c].astype("float64")
    cols = ["domain", "n_rows", "n_rows_mapped", "n_rows_unmapped", "pct_rows_mapped", "pct_rows_standard", "n_source_codes",
            "n_codes_fully_mapped", "n_codes_partially_mapped", "n_codes_unmapped", "pct_codes_fully_mapped", "status", "suppressed"]
    unmapped = pd.DataFrame(urows, columns=["domain", "source_value", "n_rows_unmapped"])
    return {"summary": summary[cols], "unmapped": unmapped}
