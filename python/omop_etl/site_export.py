"""Aggregate-only site export ("summary package") and its pooled counterpart (ODD-7).

``export_site_summary`` writes the de-identified aggregates a site hands to a coordinator for a multi-site or two-machine
harmonization study - CONSORT attrition counts, Table 1 aggregates, parity ledgers and any other aggregate tables - and
nothing else. It refuses patient-level inputs (identifier-like columns, date or datetime columns, tables with more rows than
``max_rows``), suppresses small counts with :mod:`omop_etl.disclosure`, and writes a ``manifest.json`` listing the SHA-256
of every file (and, with ``con=``, the vocabulary release) so the coordinator can check what it received.

``pool_site_packages`` is the coordinator's side: it verifies each package against its manifest and pools attrition and
Table 1 across sites. Both are generic: the site label is a short anonymous tag you choose (``"site_a"``), never a real name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from omop_etl.consort import pool_consort_tables
from omop_etl.disclosure import DEFAULT_MIN_CELL, suppress_attrition_table, suppress_small_counts
from omop_etl.run_manifest import RunManifest, file_sha256
from omop_etl.table1_pooled import SUMMARY_COLUMNS, build_table1, pool_table1_summaries

__all__ = ["export_site_summary", "pool_site_packages", "assert_aggregate_only", "SiteSummaryError"]

PACKAGE_VERSION = 1
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
#: column names that identify a person, an encounter or a place, or carry a date of service
_IDENTIFIER_RE = re.compile(
    r"(^|_)(person|patient|subject|patid|mrn|ssn|encounter|encounterid|visit_occurrence|visit_detail|address|zip|zipcode|"
    r"postal|phone|email|dob|birth|first_name|last_name|full_name|index_date)(_|$)|_(date|datetime)$|^(date|datetime)$",
    re.I)


class SiteSummaryError(ValueError):
    """The data offered for a site summary package is not aggregate-only."""


def assert_aggregate_only(df: pd.DataFrame, name: str = "table", *, max_rows: int = 5000) -> None:
    """Raises :class:`SiteSummaryError` if ``df`` looks patient-level: identifier or date columns, date dtypes, too many rows.

    This is a guard against accidents, not proof of de-identification: it catches the usual slips (a person_id left in, a
    date column, a whole cohort passed by mistake). Aggregates keyed by ``concept_id`` are fine.
    """
    bad_cols = [c for c in df.columns if _IDENTIFIER_RE.search(str(c))]
    if bad_cols:
        raise SiteSummaryError(f"{name}: columns look like identifiers or dates and cannot be exported: {bad_cols}")
    dated = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if dated:
        raise SiteSummaryError(f"{name}: date/datetime columns cannot be exported: {dated}")
    if len(df) > max_rows:
        raise SiteSummaryError(f"{name}: {len(df):,} rows is more than an aggregate table should hold (max_rows={max_rows:,}); "
                               "is this patient-level data?")


def _check_label(site_label: str) -> str:
    if not isinstance(site_label, str) or not _LABEL_RE.match(site_label):
        raise SiteSummaryError("site_label must be a short anonymous tag of letters, digits, '_' or '-' (max 32 characters); "
                               "do not use a real site name or a path")
    return site_label


def _suppress_summary(summary: pd.DataFrame, min_cell: int) -> pd.DataFrame:
    s = summary.copy()
    cat = s["kind"] != "continuous"
    s["suppressed"] = False
    if cat.any():
        sub = suppress_small_counts(s.loc[cat], ["count"], min_cell, group_by=["group", "variable"], complementary=True,
                                    flag_col="suppressed")
        s.loc[cat, "count"] = sub["count"].values
        s.loc[cat, "suppressed"] = sub["suppressed"].values
    small = (s["n"] > 0) & (s["n"] < min_cell)
    s.loc[small, ["n", "n_missing", "mean", "sd", "count"]] = float("nan")
    s.loc[small, "suppressed"] = True
    s.loc[(s["n_missing"] > 0) & (s["n_missing"] < min_cell), "n_missing"] = float("nan")
    return s


def export_site_summary(
    out_dir: str | Path,
    site_label: str,
    *,
    attrition: Any = None,
    table1_summary: pd.DataFrame | None = None,
    parity: Any = None,
    tables: Mapping[str, pd.DataFrame] | None = None,
    count_cols: Mapping[str, Sequence[str]] | None = None,
    min_cell: int = DEFAULT_MIN_CELL,
    con: Any = None,
    inputs: Sequence[str | Path] = (),
    extra: Mapping[str, Any] | None = None,
    max_rows: int = 5000,
) -> dict[str, Path]:
    """Writes an aggregate-only summary package to ``out_dir`` and returns ``{name: path}`` (including ``manifest``).

    Args:
        site_label: Anonymous short tag for this site.
        attrition: Attrition table or :class:`~omop_etl.cohort.ConsortAttrition`; written as ``attrition.csv``, suppressed.
        table1_summary: Output of :func:`omop_etl.table1_pooled.summarize_table1_site`; written as ``table1_summary.csv``,
            suppressed.
        parity: A :class:`~omop_etl.parity.ParityAudit` created with a ``min_cell``; its three CSVs are written.
        tables: Extra aggregate tables ``{name: DataFrame}`` -> ``<name>.csv``. ``count_cols[name]`` lists the count columns to
            suppress; a table without count columns is checked for identifiers but written as it is.
        min_cell: Counts from 1 to ``min_cell - 1`` are suppressed. Must be at least 2: a package never ships raw small cells.
        con: Optional DuckDB connection; its vocabulary release goes into the manifest.
        inputs: Input files (hashed into the manifest). Only their file names are recorded, never their paths.
    """
    if isinstance(min_cell, bool) or not isinstance(min_cell, int) or min_cell < 2:
        raise SiteSummaryError("min_cell must be an integer >= 2 for a package that leaves the site")
    label = _check_label(site_label)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def _write(name: str, df: pd.DataFrame) -> None:
        p = out / f"{name}.csv"
        df.to_csv(p, index=False)
        written[name] = p

    if attrition is not None:
        att = attrition.to_dataframe() if hasattr(attrition, "to_dataframe") else attrition
        assert_aggregate_only(att, "attrition", max_rows=max_rows)
        _write("attrition", suppress_attrition_table(att, min_cell))
    if table1_summary is not None:
        missing = [c for c in SUMMARY_COLUMNS if c not in table1_summary.columns]
        if missing:
            raise SiteSummaryError(f"table1_summary lacks columns {missing}; build it with summarize_table1_site()")
        assert_aggregate_only(table1_summary[SUMMARY_COLUMNS].rename(columns={"group": "grp", "level": "lvl"}),
                              "table1_summary", max_rows=max_rows)
        _write("table1_summary", _suppress_summary(table1_summary[SUMMARY_COLUMNS], min_cell))
    if parity is not None:
        if getattr(parity, "min_cell", None) is None or parity.min_cell < min_cell:
            raise SiteSummaryError(f"the parity audit was not suppressed at min_cell >= {min_cell}; "
                                   f"re-run audit_matrix_parity(min_cell={min_cell})")
        for name, frame in (("parity_ledger", parity.ledger), ("parity_levels", parity.levels), ("parity_keys", parity.keys)):
            assert_aggregate_only(frame, name, max_rows=max_rows)
            _write(name, frame)
    for name, frame in (tables or {}).items():
        if not re.match(r"^[A-Za-z0-9_-]+$", name) or name in written or name == "manifest":
            raise SiteSummaryError(f"table name {name!r} is not a valid, unused file stem")
        assert_aggregate_only(frame, name, max_rows=max_rows)
        cc = list((count_cols or {}).get(name, []))
        if cc:
            frame = suppress_small_counts(frame, cc, min_cell, complementary=False)
        _write(name, frame)

    if not written:
        raise SiteSummaryError("nothing to export: give attrition, table1_summary, parity or tables")
    m = RunManifest("site-summary", extra={"package_version": PACKAGE_VERSION, "site_label": label, "min_cell": min_cell,
                                           "files": sorted(p.name for p in written.values()), **dict(extra or {})}, con=con)
    for p in written.values():
        m.add_output(p)
    manifest = m.to_dict()
    # a local path can name a site, a user or a project: keep file names only, and leave out the git checkout's commit
    manifest["inputs"] = {Path(p).name: file_sha256(p) for p in inputs}
    manifest["outputs"] = {Path(k).name: v for k, v in manifest["outputs"].items()}
    manifest["git_commit"] = None
    manifest["platform"] = None
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    written["manifest"] = path
    return written


def pool_site_packages(package_dirs: Mapping[str, str | Path], *, min_cell: int | None = DEFAULT_MIN_CELL,
                       group_order: Sequence[str] | None = None) -> dict[str, Any]:
    """Verifies and pools site summary packages.

    Each directory's files are re-hashed against its ``manifest.json`` (a changed or missing file raises). Attrition is pooled
    step by step and Table 1 is pooled from the aggregates; a cell a site suppressed stays unavailable in the pooled table.
    Returns ``{"sites": [...], "attrition": DataFrame | None, "table1": DataFrame | None, "table1_summary": DataFrame | None}``.
    Releasing the pooled output together with the per-site files lets a reader subtract the other sites from the pool, so
    release one or the other.
    """
    attrition, summaries, labels, mc = {}, {}, [], 0
    for key, d in package_dirs.items():
        d = Path(d)
        mpath = d / "manifest.json"
        if not mpath.is_file():
            raise SiteSummaryError(f"{key}: no manifest.json in the package")
        man = json.loads(mpath.read_text(encoding="utf-8"))
        for fname, want in (man.get("outputs") or {}).items():
            f = d / fname
            if not f.is_file():
                raise SiteSummaryError(f"{key}: {fname} listed in the manifest is missing")
            if file_sha256(f) != want:
                raise SiteSummaryError(f"{key}: {fname} changed since the manifest was written")
        label = (man.get("extra") or {}).get("site_label", str(key))
        if label in labels:
            raise SiteSummaryError(f"two packages claim the site label {label!r}")
        labels.append(label)
        mc = max(mc, int((man.get("extra") or {}).get("min_cell") or 0))
        if (d / "attrition.csv").is_file():
            attrition[label] = pd.read_csv(d / "attrition.csv")
        if (d / "table1_summary.csv").is_file():
            summaries[label] = (pd.read_csv(d / "table1_summary.csv", keep_default_na=True)
                                .drop(columns=["suppressed"], errors="ignore").fillna({"level": ""}))
    cell = max(min_cell or 0, mc) or None
    pooled_att = pool_consort_tables(attrition, min_cell=cell) if attrition else None
    pooled_sum = pool_table1_summaries(summaries) if summaries else None
    table1 = build_table1(pooled_sum, group_order=group_order, min_cell=cell) if pooled_sum is not None else None
    return {"sites": labels, "attrition": pooled_att, "table1_summary": pooled_sum, "table1": table1}
