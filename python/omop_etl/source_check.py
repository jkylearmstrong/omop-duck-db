"""Source-file contract for the PCORnet -> OMOP ETL (``check-source``).

Accepted layout of a source directory (CSV or Parquet; per-table choice, CSV preferred when both exist):

* File names are ``<table>.csv`` or ``<table>.parquet``, matched case-insensitively (``ENCOUNTER.CSV`` works).
* Column names are matched case-insensitively, with the header aliases in ``build_omop_cdm.DEFAULT_ALIASES``
  (for example ``SSID`` for ``PATID``). Columns that are not required are null-padded when absent.
* Every column is read as text and cast by the ETL, so dates may be ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM:SS``
  (CSV text or native Parquet date/timestamp columns; Parquet values are rendered to ISO strings).
* Tables other than the required ones are optional; the ETL skips the corresponding OMOP table.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb

from omop_etl.build_omop_cdm import DEFAULT_ALIASES, _source_relation, find_source_file

__all__ = ["SOURCE_CONTRACT", "SourceContractError", "check_source", "format_report"]

# table -> stems, required columns, optional columns, OMOP table fed, whether the ETL cannot run without it
SOURCE_CONTRACT: dict[str, dict[str, Any]] = {
    "demographic": {"stems": ["demographic"], "required": ["PATID", "BIRTH_DATE", "SEX"],
                    "optional": ["RACE", "HISPANIC", "PROVIDERID"], "feeds": "person", "needed": True},
    "encounter": {"stems": ["encounter"], "required": ["ENCOUNTERID", "PATID", "ENC_TYPE", "ADMIT_DATE"],
                  "optional": ["DISCHARGE_DATE", "DISCHARGE_STATUS", "PROVIDERID", "FACILITYID"],
                  "feeds": "visit_occurrence", "needed": True},
    "diagnosis": {"stems": ["diagnosis"], "required": ["PATID", "DX", "DX_TYPE"],
                  "optional": ["DIAGNOSISID", "ENCOUNTERID", "ADMIT_DATE", "DX_DATE", "PDX", "PROVIDERID"],
                  "feeds": "condition_occurrence", "needed": False},
    "procedures": {"stems": ["procedures"], "required": ["PATID", "PX", "PX_TYPE"],
                   "optional": ["PROCEDURESID", "ENCOUNTERID", "PX_DATE", "PROVIDERID"],
                   "feeds": "procedure_occurrence", "needed": False},
    "lab_result_cm": {"stems": ["lab_result_cm"], "required": ["PATID", "LAB_LOINC", "RESULT_NUM"],
                      "optional": ["LAB_RESULT_CM_ID", "ENCOUNTERID", "RESULT_DATE", "RESULT_UNIT", "RAW_LAB_NAME"],
                      "feeds": "measurement", "needed": False},
    "prescribing": {"stems": ["prescribing"], "required": ["PATID"],
                    "optional": ["PRESCRIBINGID", "ENCOUNTERID", "RXNORM_CUI", "RX_START_DATE", "RX_END_DATE"],
                    "feeds": "drug_exposure", "needed": False},
    "death": {"stems": ["death"], "required": ["PATID"], "optional": ["DEATH_DATE"],
              "feeds": "death", "needed": False},
    "provider": {"stems": ["provider"], "required": ["PROVIDERID"], "optional": ["PROVIDER_NPI", "PROVIDER_SEX"],
                 "feeds": "provider", "needed": False},
    "vital": {"stems": ["vital"], "required": ["PATID"], "optional": [], "feeds": "measurement", "needed": False},
    "med_admin": {"stems": ["med_admin"], "required": ["PATID"], "optional": [], "feeds": "drug_exposure",
                  "needed": False},
    "dispensing": {"stems": ["dispensing"], "required": ["PATID"], "optional": [], "feeds": "drug_exposure",
                   "needed": False},
    "lds_address_history": {"stems": ["lds_address_history", "address_history"], "required": ["PATID"],
                            "optional": [], "feeds": "location", "needed": False},
}


class SourceContractError(ValueError):
    """A required source table or column is missing."""


def _columns(path: str) -> list[str]:
    con = duckdb.connect()
    try:
        return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {_source_relation(path)} LIMIT 0").fetchall()]
    finally:
        con.close()


def check_source(source_dir: str | os.PathLike, source_format: str | None = None, *, raise_on_error: bool = False
                 ) -> dict[str, Any]:
    """Reports which expected source tables and columns are present and which ETL steps will be skipped.

    Args:
        source_dir: Directory of ``<table>.csv`` / ``<table>.parquet`` files.
        source_format: ``'csv'``, ``'parquet'`` or None (either; CSV preferred).
        raise_on_error: Raise :class:`SourceContractError` naming each missing required column / table.

    Returns:
        dict with ``ok`` (bool), ``tables`` (per table: ``file``, ``present``, ``missing_required``,
        ``missing_optional``, ``feeds``), ``skipped`` (OMOP tables that will not be built) and ``errors``.
    """
    fmt = None if source_format is None else str(source_format).lower()
    if fmt not in (None, "csv", "parquet"):
        raise ValueError("source_format must be 'csv', 'parquet' or None.")
    src = Path(source_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")
    tables: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    skipped: set[str] = set()
    for name, spec in SOURCE_CONTRACT.items():
        path = None
        for stem in spec["stems"]:
            path = find_source_file(str(src), stem, fmt)
            if path:
                break
        entry: dict[str, Any] = {"file": None if path is None else os.path.basename(path), "present": path is not None,
                                 "feeds": spec["feeds"], "missing_required": [], "missing_optional": []}
        if path is None:
            if spec["needed"]:
                errors.append(f"table '{name}': required file {name}.{fmt or 'csv|parquet'} not found in {source_dir}")
            else:
                skipped.add(spec["feeds"])
        else:
            have = {c.upper() for c in _columns(path)}

            def present(col: str) -> bool:
                return any(c.upper() in have for c in [col, *DEFAULT_ALIASES.get(col.upper(), [])])

            entry["missing_required"] = [c for c in spec["required"] if not present(c)]
            entry["missing_optional"] = [c for c in spec["optional"] if not present(c)]
            for col in entry["missing_required"]:
                errors.append(f"table '{name}' ({entry['file']}): required column '{col}' is missing")
        tables[name] = entry
    # an OMOP table is only skipped when every source feeding it is absent
    fed = {e["feeds"] for e in tables.values() if e["present"]}
    report = {"ok": not errors, "source_dir": str(src), "source_format": fmt or "csv|parquet",
              "tables": tables, "skipped": sorted(skipped - fed), "errors": errors}
    if raise_on_error and errors:
        raise SourceContractError("Source contract violations:\n  " + "\n  ".join(errors))
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = [f"Source check: {report['source_dir']} (format: {report['source_format']})"]
    for name, e in report["tables"].items():
        if not e["present"]:
            lines.append(f"  [--] {name}: not found -> {e['feeds']} may be skipped")
        elif e["missing_required"]:
            lines.append(f"  [!!] {name} ({e['file']}): MISSING required {', '.join(e['missing_required'])}")
        else:
            extra = f"; null-padded: {', '.join(e['missing_optional'])}" if e["missing_optional"] else ""
            lines.append(f"  [ok] {name} ({e['file']}){extra}")
    lines.append("OK" if report["ok"] else "FAILED:\n  " + "\n  ".join(report["errors"]))
    return "\n".join(lines)
