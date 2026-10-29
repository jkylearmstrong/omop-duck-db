"""Reconcile a reference patient list with a cohort built here, and say why patients are missing.

A study that replaces a legacy extraction (SAS, a site's own SQL, another ETL) has to answer two questions: do we find the same
patients, and for those we do not, which rule dropped them? :func:`cohort_overlap` counts the overlap; :func:`explain_cohort_gaps`
walks a Circe definition step by step (entry event, correlated criteria, observation window, each inclusion rule) and gives every
reference patient the first step that excludes them. :func:`reconcile_cohort` combines both. Nothing here is specific to a study: the
reference is any list of identifiers and the cohort is any table or Circe definition.

The reports are counts by reason. Patient-level rows are returned only on request (``include_person_rows=True``) and stay on the
machine that ran the function; ``min_cell`` suppresses small counts so a report can leave a site.
"""
from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from omop_etl.circe import compile_circe_to_duckdb

__all__ = ["explain_cohort_gaps", "reconcile_cohort", "suppress_small_cells", "GAP_REASONS"]

# the reasons, in the order they are checked
GAP_REASONS = ("in_cohort", "not_in_cdm", "no_observation_period", "no_entry_event", "failed_entry_correlated_criteria",
               "entry_outside_observation_window", "failed_inclusion_rule", "in_definition_not_in_cohort",
               "not_in_cohort_no_definition_given")


def _table(con: duckdb.DuckDBPyConnection, x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    p = str(x)
    if p.endswith(".parquet"):
        return con.execute(f"SELECT * FROM read_parquet('{p}')").df()
    if p.endswith((".csv", ".tsv", ".csv.gz")) or Path(p).exists():
        return con.execute(f"SELECT * FROM read_csv_auto('{p}')").df()
    return con.execute(f"SELECT * FROM {p}").df()


def _definition(d: Any) -> dict:
    if isinstance(d, dict):
        return copy.deepcopy(d)
    if isinstance(d, str) and d.lstrip().startswith("{"):
        return json.loads(d)                                   # JSON text, never a path (a long string is "file name too long" on Linux)
    return json.loads(Path(d).read_text(encoding="utf-8"))


def _persons(con: duckdb.DuckDBPyConnection, data: dict, stage: str | None, cdm_schema: str) -> set[int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sql = compile_circe_to_duckdb(data, cdm_schema=cdm_schema, stage=stage)
    return {int(v) for v in con.execute(f"SELECT DISTINCT subject_id FROM ({sql})").df()["subject_id"]}


def _strip_correlated(data: dict) -> dict:
    out = copy.deepcopy(data)
    for crit in out.get("PrimaryCriteria", {}).get("CriteriaList", []):
        for item in crit.values():
            if isinstance(item, dict):
                item.pop("CorrelatedCriteria", None)
    return out


def _map_reference(con: duckdb.DuckDBPyConnection, reference: Any, id_col: str, id_type: str, date_col: str | None,
                   group_col: str | None) -> pd.DataFrame:
    """One row per reference row: ``ref_id`` (text), ``ref_date``, ``group``, ``person_id`` (NULL when it maps to nobody).
    A reference id that maps to several persons yields several rows."""
    ref = _table(con, reference)
    cols = {c.lower(): c for c in ref.columns}
    if id_col.lower() not in cols:
        raise ValueError(f"reference has no column {id_col!r}; columns: {list(ref.columns)}")
    frame = pd.DataFrame({"ref_id": ref[cols[id_col.lower()]].astype(str).str.strip()})
    frame["ref_date"] = pd.to_datetime(ref[cols[date_col.lower()]]).dt.date if date_col and date_col.lower() in cols else None
    frame["grp"] = ref[cols[group_col.lower()]].astype(str) if group_col and group_col.lower() in cols else "ALL"
    con.register("_recon_ref", frame)
    try:
        if id_type == "person_id":
            join = "p.person_id = TRY_CAST(r.ref_id AS BIGINT)"
        elif id_type == "person_source_value":
            join = "UPPER(TRIM(CAST(p.person_source_value AS VARCHAR))) = UPPER(r.ref_id)"
        else:
            raise ValueError("id_type must be 'person_source_value' or 'person_id'")
        return con.execute(f"SELECT r.ref_id, r.ref_date, r.grp, p.person_id FROM _recon_ref r LEFT JOIN person p ON {join}").df()
    finally:
        con.unregister("_recon_ref")


def suppress_small_cells(df: pd.DataFrame, count_cols: list[str], min_cell: int, *, also: list[str] | None = None) -> pd.DataFrame:
    """Blanks counts from 1 to ``min_cell - 1`` (and the ``also`` columns of those rows) and adds a ``suppressed`` flag. A total that
    is shown beside suppressed cells can reveal them by subtraction: suppress the total too, or merge the small reasons, when that matters."""
    out = df.copy()
    out["suppressed"] = False
    if min_cell and min_cell > 1:
        mask = pd.Series(False, index=out.index)
        for c in count_cols:
            mask |= (out[c] > 0) & (out[c] < min_cell)
        for c in count_cols + list(also or []):
            out[c] = out[c].astype("float64")
            out.loc[mask, c] = np.nan
        out["suppressed"] = mask
    return out


def explain_cohort_gaps(
    con: duckdb.DuckDBPyConnection,
    reference: Any,
    *,
    definition: Any = None,
    cohort: Any = None,
    id_col: str = "person_source_value",
    id_type: str = "person_source_value",
    group_col: str | None = None,
    cdm_schema: str = "main",
    min_cell: int = 0,
    include_person_rows: bool = False,
) -> dict[str, Any]:
    """Gives every reference patient the first step that keeps them out of the cohort.

    ``reference`` is a DataFrame, CSV/parquet path or table name with one identifier column (``id_col``; ``id_type`` says whether it
    holds ``person_source_value`` or ``person_id``). ``cohort`` is a table name or DataFrame with ``subject_id`` (or ``person_id``);
    ``definition`` is a Circe definition (dict, JSON text or path) that the native compiler can evaluate. With a ``definition`` the
    reasons name the step; with only a ``cohort`` a patient who is simply absent is ``not_in_cohort_no_definition_given``. When only a
    ``definition`` is given the cohort is the definition's own.

    Reasons, in the order they are checked: ``in_cohort``; ``not_in_cdm`` (the identifier maps to no person);
    ``no_observation_period``; ``no_entry_event`` (no event of the entry criteria, including their concept sets, age and values);
    ``failed_entry_correlated_criteria``; ``entry_outside_observation_window``; ``failed_inclusion_rule`` (one row per rule, named in
    ``detail``); ``in_definition_not_in_cohort`` (the definition keeps them but the cohort table does not, e.g. it was built another way).

    Returns ``{"gaps": DataFrame[reason, detail, n, pct, suppressed], "n_reference": int, "persons": DataFrame | None}``. ``persons``
    (``include_person_rows=True``) has one row per reference row with its reason and stays local."""
    if definition is None and cohort is None:
        raise ValueError("give a Circe definition, a cohort table, or both")
    data = _definition(definition) if definition is not None else None
    mapped = _map_reference(con, reference, id_col, id_type, None, group_col)

    steps: list[tuple[str, str, set[int]]] = []
    if data is not None:
        steps.append(("no_entry_event", "", _persons(con, {**_strip_correlated(data), "InclusionRules": []}, "_primary_events_raw", cdm_schema)))
        steps.append(("failed_entry_correlated_criteria", "", _persons(con, {**data, "InclusionRules": []}, "_primary_events_raw", cdm_schema)))
        steps.append(("entry_outside_observation_window", "", _persons(con, {**data, "InclusionRules": []}, "_primary_limited", cdm_schema)))
        rules = data.get("InclusionRules") or []
        for k, rule in enumerate(rules, start=1):
            steps.append(("failed_inclusion_rule", str(rule.get("name") or f"rule {k}"),
                          _persons(con, {**data, "InclusionRules": rules[:k]}, "_primary_qualified", cdm_schema)))
    if cohort is not None:
        c = _table(con, cohort)
        pcol = "subject_id" if "subject_id" in c.columns else "person_id"
        in_cohort = {int(v) for v in c[pcol].dropna().unique()}
    else:
        # the cohort proper (after era collapse and the expression limit) has the same persons as the qualified events
        in_cohort = _persons(con, data, "_primary_qualified", cdm_schema)
    observed = {int(v) for v in con.execute("SELECT DISTINCT person_id FROM observation_period").df()["person_id"]}

    def classify(pid: Any) -> tuple[str, str]:
        if pd.isna(pid):
            return "not_in_cdm", ""
        pid = int(pid)
        if pid not in observed:
            return "no_observation_period", ""
        if pid in in_cohort:
            return "in_cohort", ""
        if data is None:
            return "not_in_cohort_no_definition_given", ""
        for reason, detail, kept in steps:
            if pid not in kept:
                return reason, detail
        return "in_definition_not_in_cohort", ""

    reasons = mapped["person_id"].map(classify)
    mapped["reason"] = [r[0] for r in reasons]
    mapped["detail"] = [r[1] for r in reasons]
    # one verdict per reference identifier: a patient who maps to several persons counts once, as the best (first in GAP_REASONS order) reason
    order = {r: i for i, r in enumerate(GAP_REASONS)}
    per_patient = (mapped.assign(_o=mapped["reason"].map(order)).sort_values("_o", kind="stable")
                   .drop_duplicates(["ref_id"]).drop(columns="_o"))
    n_ref = len(per_patient)
    counts = per_patient.groupby(["reason", "detail"], sort=False).size().reset_index(name="n")
    counts["_o"] = counts["reason"].map(order)
    counts = counts.sort_values(["_o", "detail"]).drop(columns="_o").reset_index(drop=True)
    counts["pct"] = (100.0 * counts["n"] / n_ref).round(2) if n_ref else 0.0
    gaps = suppress_small_cells(counts, ["n"], min_cell, also=["pct"])
    return {"gaps": gaps, "n_reference": n_ref,
            "persons": per_patient.rename(columns={"grp": "group"}).reset_index(drop=True) if include_person_rows else None,
            "_in_cohort": in_cohort, "_mapped": mapped}


def reconcile_cohort(
    con: duckdb.DuckDBPyConnection,
    reference: Any,
    *,
    definition: Any = None,
    cohort: Any = None,
    id_col: str = "person_source_value",
    id_type: str = "person_source_value",
    index_date_col: str | None = None,
    group_col: str | None = None,
    cdm_schema: str = "main",
    min_cell: int = 0,
    include_person_rows: bool = False,
) -> dict[str, Any]:
    """Compares a reference patient list with a cohort built here and explains the patients we miss.

    Returns ``{"counts", "gaps", "dates", "persons"}``. ``counts`` is the patient-level overlap (``n_reference``, ``n_cohort``,
    ``both``, ``reference_only``, ``cohort_only``, ``jaccard``); ``gaps`` is :func:`explain_cohort_gaps`; ``dates`` (when
    ``index_date_col`` names a date in the reference) says how many matched patients have the same index date and how far apart the
    others are: ``cohort_start_date`` is compared with the reference date, taking the nearest start when the cohort has several.
    Counts only unless ``include_person_rows``; ``min_cell`` suppresses small cells in ``gaps``."""
    res = explain_cohort_gaps(con, reference, definition=definition, cohort=cohort, id_col=id_col, id_type=id_type, group_col=group_col,
                              cdm_schema=cdm_schema, min_cell=min_cell, include_person_rows=include_person_rows)
    mapped, in_cohort = res.pop("_mapped"), res.pop("_in_cohort")
    ref_persons = {int(v) for v in mapped["person_id"].dropna().unique()}
    n_ref = res["n_reference"]
    # a reference patient counts as found when any person they map to is in the cohort
    found_ids = set(mapped.loc[mapped["person_id"].isin(in_cohort), "ref_id"])
    both = len(found_ids)
    cohort_only = len(in_cohort - ref_persons)
    union = n_ref + cohort_only
    counts = {"n_reference": n_ref, "n_mapped_to_cdm": int(mapped.loc[mapped["person_id"].notna(), "ref_id"].nunique()),
              "n_cohort": len(in_cohort), "both": both, "reference_only": n_ref - both, "cohort_only": cohort_only,
              "jaccard": round(both / union, 4) if union else 1.0}
    dates = None
    if index_date_col:
        ref_dates = _map_reference(con, reference, id_col, id_type, index_date_col, group_col)
        ref_dates = ref_dates[ref_dates["person_id"].notna() & ref_dates["ref_date"].notna()]
        if cohort is not None:
            c = _table(con, cohort)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                c = con.execute(compile_circe_to_duckdb(_definition(definition), cdm_schema=cdm_schema)).df()
        pcol = "subject_id" if "subject_id" in c.columns else "person_id"
        if "cohort_start_date" in c.columns:
            starts = c.assign(_s=pd.to_datetime(c["cohort_start_date"]).dt.normalize()).groupby(pcol)["_s"].apply(list).to_dict()
            diffs = []
            for pid, d in zip(ref_dates["person_id"], pd.to_datetime(ref_dates["ref_date"])):
                s = starts.get(int(pid))
                if s:
                    diffs.append(min(abs((x - d).days) for x in s))
            diffs = np.array(diffs, dtype=float)
            dates = {"n_compared": int(len(diffs)), "n_same_date": int((diffs == 0).sum()), "n_different_date": int((diffs > 0).sum()),
                     "median_abs_days": float(np.median(diffs[diffs > 0])) if (diffs > 0).any() else 0.0,
                     "max_abs_days": float(diffs.max()) if len(diffs) else 0.0}
    return {"counts": counts, "gaps": res["gaps"], "dates": dates, "persons": res["persons"]}
