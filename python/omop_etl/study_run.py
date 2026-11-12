"""One call from per-site OMOP DuckDB files to the manuscript tables, figures and workbook (unreleased).

``run_study`` is the repeatable form of the multi-site readmission study: for each site it builds the cohort
(:func:`omop_etl.cohort.define_study_cohort`, one random stay per patient in a date window, or the stays you pin from a file),
the benchmark features (:func:`omop_etl.benchmark_features.extract_benchmark_features`, ``profile="ederri"`` by default), pools
the sites, runs :func:`omop_etl.study_report.build_study_tables` and writes the manuscript workbook
(:func:`omop_etl.manuscript_tables.manuscript_workbook`); with ``compare_to`` it also compares the new workbook with a previous
one. Site databases are read-only. ``scripts/reproduce_study.py`` is the command-line form.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = ["run_study", "PROTOCOL_COHORT_PARAMS", "pin_stays"]

#: The eDERRI protocol's cohort definition (all-cause 30-day readmission after an inpatient stay in a patient with diabetes).
PROTOCOL_COHORT_PARAMS: dict[str, Any] = dict(
    visit_type=[9201, 262], require_diabetes=True, min_age=18, min_los_days=1, washin_days=365, followup_days=30,
    require_verified_followup=True, target_outcome="all_cause_readmission", outcome_visit_type=[9201, 262],
    transfer_discharge_concept_ids=[8717], exclude_metformin_only_prediabetes_pcos=True)


def pin_stays(cohort: pd.DataFrame, selection: str | Path | pd.DataFrame, *, on: str = "visit_occurrence_id", strict: bool = True) -> pd.DataFrame:
    """Keeps the cohort rows whose ``on`` value is listed in ``selection`` (a DataFrame or a CSV path), to reproduce a previous
    stay-per-patient draw exactly. Build the cohort with ``sample_stay_rule="all"`` so every candidate stay is present.

    ``strict=True`` raises if a selected stay is not in the cohort (a different cohort definition, window or data cut); ``False`` keeps
    what is found. Raises if the selection lists a patient more than once."""
    sel = pd.read_csv(selection) if isinstance(selection, (str, Path)) else selection
    if on not in sel:
        raise ValueError(f"the selection needs a {on!r} column.")
    ids = sel[on].dropna().astype("int64")
    if ids.duplicated().any():
        raise ValueError(f"the selection lists {int(ids.duplicated().sum())} stay id(s) more than once.")
    out = cohort[cohort[on].isin(set(ids))]
    missing = len(ids) - len(out)
    if strict and missing:
        raise ValueError(f"{missing} of {len(ids)} selected stays are not in the cohort (different definition, window or data).")
    pid = "subject_id" if "subject_id" in out else ("person_id" if "person_id" in out else None)
    if pid and out[pid].duplicated().any():
        raise ValueError("the selection yields more than one stay for some patients.")
    return out.reset_index(drop=True)


def _site_frame(db_dir: Path, site: str, *, start: str, end: str, params: Mapping[str, Any], profile: str | None, pinned: pd.DataFrame | None,
                ruca: Any, sdi: Any, cuts: Sequence[float] | None, address_table: str | None, transfer_source_values: Sequence[str],
                feature_kwargs: Mapping[str, Any], work: Path, attrition_mode: str = "windowed"):
    import duckdb

    from omop_etl.benchmark_features import extract_benchmark_features
    from omop_etl.cohort import define_study_cohort

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{(db_dir / f'omop_{site}.duckdb').as_posix()}' AS site_db (READ_ONLY); USE site_db;")
        if transfer_source_values:
            vals = ", ".join("'" + v.upper().replace("'", "''") + "'" for v in transfer_source_values)
            con.execute("CREATE OR REPLACE TEMP VIEW visit_occurrence AS SELECT * REPLACE (CASE WHEN upper(discharged_to_source_value) IN "
                        f"({vals}) THEN 8717 ELSE discharged_to_concept_id END AS discharged_to_concept_id) FROM site_db.main.visit_occurrence")
        vocab = db_dir / "central_vocabulary.duckdb"
        if vocab.exists():
            con.execute(f"ATTACH '{vocab.as_posix()}' AS central_vocab (READ_ONLY);")
            con.execute("CREATE OR REPLACE TEMP VIEW concept_ancestor AS SELECT * FROM central_vocab.main.concept_ancestor")
        rule = {"sample_stay_rule": "all"} if pinned is not None else {"sample_stay_rule": "random"}
        cohort = define_study_cohort(con, materialise=False, min_admit_date=start, max_admit_date=end, **rule, **dict(params))
        att = pd.DataFrame(cohort.attrs.get("attrition"))
        att["site"] = site
        cohort.attrs = {}
        if attrition_mode == "full_history":
            # the manuscript's CONSORT: every rule over the whole history, then the study window and the one stay per patient last
            full = define_study_cohort(con, materialise=False, sample_stay_rule="all", **dict(params))
            att = pd.DataFrame(full.attrs.get("attrition"))
            att["site"] = site
            n_final = int(cohort["subject_id"].nunique()) if "subject_id" in cohort else len(cohort)
            att = pd.concat([att, pd.DataFrame([{"step_name": f"Index admission {start} to {end}, one stay per patient", "subjects_retained": n_final, "site": site}])],
                            ignore_index=True)
        elif attrition_mode != "windowed":
            raise ValueError("attrition_mode must be 'windowed' or 'full_history'.")
        if pinned is not None:
            from_site = pinned[pinned["site_source"] == site] if "site_source" in pinned else pinned
            cohort = pin_stays(cohort, from_site, strict=False)
            att = pd.concat([att, pd.DataFrame([{"step_name": "Stay selected for the patient (pinned list)", "subjects_retained": len(cohort), "site": site}])], ignore_index=True)
        path = work / f"cohort_{site}.parquet"
        cohort.to_parquet(path)
        con.execute(f"CREATE OR REPLACE TEMP VIEW cohort_t AS SELECT * FROM read_parquet('{path.as_posix()}')")
        kw: dict[str, Any] = dict(feature_kwargs)
        if ruca is not None and sdi is not None:
            kw.update(include_sdoh=True, ruca_crosswalk=str(ruca), sdi_table=str(sdi), sdi_cut_points=list(cuts or ()), address_table=address_table)
        feats = extract_benchmark_features(con, "cohort_t", vocabulary_db=None, profile=profile, **kw)
    finally:
        con.close()
    df = cohort.merge(feats, on="visit_occurrence_id", suffixes=("", "_f"))
    df = df.drop(columns=[c for c in df.columns if c.endswith("_f")])
    df["readmitted_30d"] = df["outcome_flag"].astype(int)
    df["site_source"] = site
    df["admit_date"] = pd.to_datetime(df["cohort_start_date"])
    df["discharge_date"] = pd.to_datetime(df["cohort_end_date"])
    df["index_los_days"] = (df["discharge_date"] - df["admit_date"]).dt.days
    df["person_id"] = df["subject_id"]
    return df, att


def run_study(db_dir: str | Path, out_dir: str | Path, *, sites: Sequence[str], site_labels: Mapping[str, str] | None = None,
              start: str = "2017-01-01", end: str = "2022-09-30", cohort_params: Mapping[str, Any] | None = None, profile: str | None = "ederri",
              pinned_stays: str | Path | pd.DataFrame | None = None, ruca_crosswalk: str | Path | None = None, sdi_table: str | Path | None = None,
              sdi_cut_points: Sequence[float] | None = (25, 41, 58, 77), address_table: str | None = "auto", transfer_source_values: Sequence[str] = ("IP",),
              feature_kwargs: Mapping[str, Any] | None = None, n_bootstraps: int = 100, loso: bool = True, figures: bool = True,
              reuse_features: bool = False, compare_to: str | Path | None = None, prefix: str = "Study", random_state: int = 42,
              min_cell: int | None = 11, attrition_mode: str = "windowed", table5: str = "manuscript", sdi_missing_term: bool = True,
              address_tie_break: str = "deterministic") -> dict[str, Any]:
    """Runs the whole study; returns ``{"data": pooled DataFrame, "tables": {...}, "workbook": Path, "files": [...], "comparison": (summary, differences) | None}``.

    ``db_dir`` holds ``omop_<site>.duckdb`` per site (and, optionally, ``central_vocabulary.duckdb``). ``site_labels`` maps site to the
    anonymous label used in the tables (default Site A, B, ... in the order of ``sites``). ``pinned_stays`` (CSV or frame with
    ``site_source`` and ``visit_occurrence_id``) reproduces an earlier draw instead of the package's random stay rule.
    ``ruca_crosswalk`` + ``sdi_table`` turn on the SDI / RUCA features (cut points ``sdi_cut_points``, address history ``address_table``).
    ``transfer_source_values``: discharge source values recoded to a transfer (8717) so the transfer exclusion applies (``("IP",)`` for
    PCORnet-derived sites; pass ``()`` for none). ``reuse_features=True`` reads ``features_<site>.parquet`` from ``out_dir`` when present.
    ``compare_to``: a previous workbook to compare the new one with (:func:`omop_etl.manuscript_tables.compare_workbooks`).
    ``attrition_mode``: ``"windowed"`` (default; the steps that built the cohort, the date window second) or ``"full_history"`` (every rule over
    all history, then the window and one stay per patient last, which is how the manuscript's CONSORT table is laid out; costs one extra
    cohort build per site). ``table5`` / ``sdi_missing_term``: see :func:`omop_etl.manuscript_tables.manuscript_workbook`.
    ``address_tie_break``: see :func:`omop_etl.geography.derive_geography_features`. The result has ``timings`` (seconds per stage)."""
    from omop_etl.manuscript_tables import compare_workbooks, manuscript_workbook
    from omop_etl.study_report import build_study_tables

    db, out = Path(db_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    labels = dict(site_labels or {s: f"Site {chr(65 + i)}" for i, s in enumerate(sites)})
    pinned = None if pinned_stays is None else (pd.read_csv(pinned_stays) if isinstance(pinned_stays, (str, Path)) else pinned_stays)
    import time

    t_start = time.time()
    timings: dict[str, float] = {}
    frames, atts = [], []
    for site in sites:
        t_site = time.time()
        f = out / f"features_{site}.parquet"
        a = out / f"attrition_{site}.csv"
        if reuse_features and f.exists() and a.exists():
            frames.append(pd.read_parquet(f))
            atts.append(pd.read_csv(a))
            continue
        df, att = _site_frame(db, site, start=start, end=end, params=cohort_params or PROTOCOL_COHORT_PARAMS, profile=profile, pinned=pinned,
                              ruca=ruca_crosswalk, sdi=sdi_table, cuts=sdi_cut_points, address_table=address_table,
                              transfer_source_values=transfer_source_values, feature_kwargs={"address_tie_break": address_tie_break, **dict(feature_kwargs or {})}, work=out,
                              attrition_mode=attrition_mode)
        df["site_anon"] = labels.get(site, site)
        df.to_parquet(f)
        att.to_csv(a, index=False)
        frames.append(df)
        atts.append(att)
        timings[f"site_{site}"] = round(time.time() - t_site, 1)
    pooled = pd.concat(frames, ignore_index=True, sort=False)
    attrition = pd.concat(atts, ignore_index=True)
    first = (attrition.groupby("step_name", sort=False)["subjects_retained"].sum().rename("subjects_retained").reset_index())
    first["subjects_dropped"] = (first["subjects_retained"].shift(1) - first["subjects_retained"]).fillna(0).astype(int)
    t_tab = time.time()
    res = build_study_tables(pooled, out, prefix=prefix, n_bootstraps=n_bootstraps, loso=loso, attrition=first, figures=figures, workbook=False,
                             window=(start, end), min_cell=min_cell, random_state=random_state)
    timings["tables_figures"] = round(time.time() - t_tab, 1)
    wb = manuscript_workbook(pooled, res["tables"], out / f"{prefix}_Manuscript_Tables.xlsx", attrition=attrition, site_labels=labels, min_cell=min_cell,
                             title=f"{prefix} manuscript tables", table5=table5, sdi_missing_term=sdi_missing_term)
    timings["total"] = round(time.time() - t_start, 1)
    comparison = None
    if compare_to is not None:
        summary, diffs = compare_workbooks(wb["path"], compare_to)
        summary.to_csv(out / f"{prefix}_Comparison_Summary.csv", index=False)
        diffs.to_csv(out / f"{prefix}_Comparison_Differences.csv", index=False)
        comparison = (summary, diffs)
    (out / f"{prefix}_run.json").write_text(json.dumps({"sites": list(sites), "labels": labels, "window": [start, end], "profile": profile, "pinned": pinned is not None,
                                                       "n": int(len(pooled)), "bootstraps": n_bootstraps, "random_state": random_state, "timings_seconds": timings}, indent=1), encoding="utf-8")
    return {"data": pooled, "tables": res["tables"], "workbook": wb["path"], "files": res["files"] + [str(wb["path"])], "comparison": comparison, "summary": res["summary"], "timings": timings}
