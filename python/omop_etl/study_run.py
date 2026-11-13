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

__all__ = ["assign_site_labels", "load_site_labels", "save_site_labels", "run_study", "PROTOCOL_COHORT_PARAMS", "pin_stays", "STUDY_DECISIONS", "STUDY_PRESETS", "resolve_decisions"]

# Every choice that moves the published numbers, with the options and what each does. Nothing is hard-wired: each can be set on
# run_study (or on reproduce_study.py) and the values used are written to <prefix>_run.json.
STUDY_DECISIONS: dict[str, dict[str, Any]] = {
    "age_method": {"options": ("completed_years", "year_difference"),
                   "about": "How age at index is computed: 'completed_years' (exact whole years on index date, the 0.21.0 default) "
                            "or 'year_difference' (calendar year boundary difference, deprecated 0.20.0 default)."},
    "address_undated": {"options": ("last", "first"),
                        "about": "Rank of an address with no start date among those in effect at admission: after dated ones (documented rule) or before them "
                                 "(what the study pandas pipeline did; with first_listed it reproduces the delivered geography)."},
    "address_tie_break": {"options": ("deterministic", "highest_zip", "ambiguous_missing", "arbitrary", "first_listed"),
                          "about": "Which ZIP a patient gets when several addresses tie for the index date (feeds SDI, RUCA, Table 1 geography). "
                                   "'first_listed' (address-table order) reproduces the study pipeline and is the same on every run; 'arbitrary' (engine order) is not reproducible; 'deterministic' lowest ZIP."},
    "sdi_missing_term": {"options": (True, False),
                         "about": "Keep an 'SDI Quintile: Missing / Unknown' term in Table 6. False is what the delivered workbook did."},
    "table5": {"options": ("manuscript", "calibrated"),
               "about": "Table 5 metrics: delivered rule (0.5 threshold, uncalibrated, tuned XGBoost on 50/50 upsampled training) or prior-shift calibrated."},
    "attrition_mode": {"options": ("windowed", "full_history"),
                       "about": "CONSORT layout: steps that built the cohort, or every rule over all history then window and one stay last (the manuscript layout)."},
    "consort_final_steps": {"options": (True, False),
                            "about": "Table S3 ends with the date window / one-stay-per-patient step and the pinned-list step (True), or with the last eligibility rule "
                                     "as in the delivered workbook (False)."},
    "table6_increments": {"options": ("sd", "clinical"),
                          "about": "Continuous Table 6 rows per 1 SD, or per clinical unit (10 mmHg, 30 days, ...) as delivered. Same model; only OR and CI are rescaled."},
}
STUDY_PRESETS: dict[str, dict[str, Any]] = {
    "package": {"consort_final_steps": True, "address_undated": "last", "address_tie_break": "deterministic", "sdi_missing_term": True, "table5": "manuscript", "attrition_mode": "windowed", "table6_increments": "sd", "age_method": "completed_years"},
    "delivered_2026_11_11": {"consort_final_steps": False, "address_undated": "first", "address_tie_break": "first_listed", "sdi_missing_term": False, "table5": "manuscript", "attrition_mode": "full_history",
                             "table6_increments": "clinical", "age_method": "completed_years"},
}


def assign_site_labels(sites: Sequence[str], *, mode: str = "random", seed: int | None = None, prefix: str = "Site ") -> dict[str, str]:
    """Anonymous labels (``Site A``, ``Site B``, ...) for ``sites``, so that tables and figures do not identify a health system.

    ``mode="random"`` (default) gives each site a random letter: a fresh shuffle every call, or a repeatable one with ``seed``;
    ``mode="ordered"`` follows the order of ``sites``. Keep the mapping private and **outside the repository and the shared outputs**
    (:func:`save_site_labels`), and assign it once for a whole consortium and hand each site its own letter, or every site's "Site A"
    would be a different system. With a ``seed`` the mapping can be rebuilt only by whoever holds both the seed and the site list."""
    import random

    sites = list(dict.fromkeys(sites))
    def _letters(i: int) -> str:  # A..Z, AA..AZ, BA.. (any number of sites)
        out, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            out = chr(65 + r) + out
        return out

    letters = [_letters(i) for i in range(len(sites))]
    if mode == "random":
        (random.Random(seed) if seed is not None else random.SystemRandom()).shuffle(letters)
    elif mode != "ordered":
        raise ValueError("mode must be 'random' or 'ordered'.")
    return {s: f"{prefix}{c}" for s, c in zip(sites, letters)}


def save_site_labels(labels: Mapping[str, str], path: str | Path) -> Path:
    """Writes a ``site -> label`` mapping as JSON. Put it somewhere that is not committed or shared with the results."""
    p = Path(path)
    p.write_text(json.dumps(dict(labels), indent=1), encoding="utf-8")
    return p


def load_site_labels(path: str | Path) -> dict[str, str]:
    """Reads a mapping written by :func:`save_site_labels`."""
    return {str(k): str(v) for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def resolve_decisions(preset: str = "package", **overrides: Any) -> dict[str, Any]:
    """The decisions in force: the named preset in :data:`STUDY_PRESETS`, then any explicit (not None) override. Unknown names or values raise."""
    if preset not in STUDY_PRESETS:
        raise ValueError(f"preset must be one of {sorted(STUDY_PRESETS)}.")
    out = dict(STUDY_PRESETS[preset])
    for k, v in overrides.items():
        if k not in STUDY_DECISIONS:
            raise ValueError(f"Unknown decision {k!r}; known: {sorted(STUDY_DECISIONS)}.")
        if v is not None:
            if v not in STUDY_DECISIONS[k]["options"]:
                raise ValueError(f"{k} must be one of {STUDY_DECISIONS[k]['options']}, got {v!r}.")
            out[k] = v
    return out

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


def run_study(db_dir: str | Path, out_dir: str | Path, *, sites: Sequence[str], site_labels: Mapping[str, str] | str | Path | None = None, label_seed: int | None = None, labels_out: str | Path | None = None,
              start: str = "2017-01-01", end: str = "2022-09-30", cohort_params: Mapping[str, Any] | None = None, profile: str | None = "ederri",
              pinned_stays: str | Path | pd.DataFrame | None = None, ruca_crosswalk: str | Path | None = None, sdi_table: str | Path | None = None,
              sdi_cut_points: Sequence[float] | None = (25, 41, 58, 77), address_table: str | None = "auto", transfer_source_values: Sequence[str] = ("IP",),
              feature_kwargs: Mapping[str, Any] | None = None, n_bootstraps: int = 100, loso: bool = True, figures: bool = True,
              reuse_features: bool = False, compare_to: str | Path | None = None, prefix: str = "Study", random_state: int = 42,
              min_cell: int | None = 11, attrition_mode: str | None = None, table5: str | None = None, sdi_missing_term: bool | None = None,
              address_tie_break: str | None = None, address_undated: str | None = None, table6_increments: str | None = None, consort_final_steps: bool | None = None,
              age_method: str | None = None,
              sheet_site_labels: Mapping[str, Mapping[str, str]] | None = None, preset: str = "package") -> dict[str, Any]:
    """Runs the whole study; returns ``{"data": pooled DataFrame, "tables": {...}, "workbook": Path, "files": [...], "comparison": (summary, differences) | None}``.

    ``db_dir`` holds ``omop_<site>.duckdb`` per site (and, optionally, ``central_vocabulary.duckdb``). ``site_labels`` maps site to the
    anonymous label used in the tables (default Site A, B, ... in the order of ``sites``); ``"random"`` assigns a random letter to each site
    (repeatable with ``label_seed``), a path reads a mapping saved by :func:`save_site_labels`, and ``labels_out`` saves the mapping used. The
    mapping is never written to the run record or the workbook: keep it private. ``pinned_stays`` (CSV or frame with
    ``site_source`` and ``visit_occurrence_id``) reproduces an earlier draw instead of the package's random stay rule.
    ``ruca_crosswalk`` + ``sdi_table`` turn on the SDI / RUCA features (cut points ``sdi_cut_points``, address history ``address_table``).
    ``transfer_source_values``: discharge source values recoded to a transfer (8717) so the transfer exclusion applies (``("IP",)`` for
    PCORnet-derived sites; pass ``()`` for none). ``reuse_features=True`` reads ``features_<site>.parquet`` from ``out_dir`` when present.
    ``compare_to``: a previous workbook to compare the new one with (:func:`omop_etl.manuscript_tables.compare_workbooks`).
    ``attrition_mode``: ``"windowed"`` (default; the steps that built the cohort, the date window second) or ``"full_history"`` (every rule over
    all history, then the window and one stay per patient last, which is how the manuscript's CONSORT table is laid out; costs one extra
    cohort build per site). ``table5`` / ``sdi_missing_term``: see :func:`omop_etl.manuscript_tables.manuscript_workbook`.
    ``sheet_site_labels``: ``{"Table S1": {label: other label}, "Table S3": {...}}`` relabels the sites of those sheets only (see
    :func:`omop_etl.manuscript_tables.manuscript_workbook`).
    ``address_tie_break``: see :func:`omop_etl.geography.derive_geography_features`. ``preset`` names a set of the decisions above
    (:data:`STUDY_PRESETS`: ``"package"`` or ``"delivered_2026_11_11"``); any of ``attrition_mode``, ``table5``, ``sdi_missing_term``,
    ``address_tie_break``, ``address_undated``, ``table6_increments``, ``consort_final_steps``, ``age_method`` given (not None) overrides the preset. :data:`STUDY_DECISIONS` lists the options. The result has ``timings`` (seconds per stage)."""
    from omop_etl.manuscript_tables import compare_workbooks, manuscript_workbook
    from omop_etl.study_report import build_study_tables

    dec = resolve_decisions(preset, attrition_mode=attrition_mode, table5=table5, sdi_missing_term=sdi_missing_term,
                            address_tie_break=address_tie_break, address_undated=address_undated, table6_increments=table6_increments,
                            consort_final_steps=consort_final_steps, age_method=age_method)
    attrition_mode, table5, sdi_missing_term, address_tie_break = (dec["attrition_mode"], dec["table5"], dec["sdi_missing_term"], dec["address_tie_break"])
    undated = dec["address_undated"]
    c_params = dict(cohort_params or PROTOCOL_COHORT_PARAMS)
    # an explicit age_method wins over cohort_params; the preset's value only fills a cohort_params that does not set one
    if age_method is not None or "age_method" not in c_params:
        c_params["age_method"] = dec["age_method"]
    dec["age_method"] = c_params["age_method"]  # the run record shows the rule that was actually used
    db, out = Path(db_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if isinstance(site_labels, (str, Path)) and str(site_labels) not in ("random", "ordered"):
        site_labels = load_site_labels(site_labels)  # a JSON file written by save_site_labels
    elif isinstance(site_labels, str):
        site_labels = assign_site_labels(sites, mode=site_labels, seed=label_seed)
    labels = dict(site_labels or assign_site_labels(sites, mode="ordered"))
    if labels_out is not None:
        save_site_labels(labels, labels_out)
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
        df, att = _site_frame(db, site, start=start, end=end, params=c_params, profile=profile, pinned=pinned,
                              ruca=ruca_crosswalk, sdi=sdi_table, cuts=sdi_cut_points, address_table=address_table,
                              transfer_source_values=transfer_source_values, feature_kwargs={"address_tie_break": address_tie_break, "address_undated": undated, **dict(feature_kwargs or {})}, work=out,
                              attrition_mode=attrition_mode)
        df["site_anon"] = labels.get(site, site)
        df.to_parquet(f)
        att.to_csv(a, index=False)
        frames.append(df)
        atts.append(att)
        timings[f"site_{site}"] = round(time.time() - t_site, 1)
    pooled = pd.concat(frames, ignore_index=True, sort=False)
    attrition = pd.concat(atts, ignore_index=True)
    if not dec["consort_final_steps"]:
        final = attrition["step_name"].astype(str).str.startswith("Index admission ") | attrition["step_name"].astype(str).str.startswith("Stay selected for the patient")
        attrition = attrition[~final].reset_index(drop=True)
    first = (attrition.groupby("step_name", sort=False)["subjects_retained"].sum().rename("subjects_retained").reset_index())
    first["subjects_dropped"] = (first["subjects_retained"].shift(1) - first["subjects_retained"]).fillna(0).astype(int)
    t_tab = time.time()
    res = build_study_tables(pooled, out, prefix=prefix, n_bootstraps=n_bootstraps, loso=loso, attrition=first, figures=figures, workbook=False,
                             window=(start, end), min_cell=min_cell, random_state=random_state)
    timings["tables_figures"] = round(time.time() - t_tab, 1)
    wb = manuscript_workbook(pooled, res["tables"], out / f"{prefix}_Manuscript_Tables.xlsx", attrition=attrition, site_labels=labels, min_cell=min_cell,
                             title=f"{prefix} manuscript tables", table5=table5, sdi_missing_term=sdi_missing_term,
                             table6_increments=dec["table6_increments"], sheet_site_labels=sheet_site_labels)
    timings["total"] = round(time.time() - t_start, 1)
    comparison = None
    if compare_to is not None:
        summary, diffs = compare_workbooks(wb["path"], compare_to)
        summary.to_csv(out / f"{prefix}_Comparison_Summary.csv", index=False)
        diffs.to_csv(out / f"{prefix}_Comparison_Differences.csv", index=False)
        from omop_etl.manuscript_tables import workbook_similarity

        workbook_similarity(wb["path"], compare_to).to_csv(out / f"{prefix}_Similarity.csv", index=False)
        from omop_etl.manuscript_tables import workbook_deltas

        d_cells, d_rows = workbook_deltas(wb["path"], compare_to)
        d_cells.to_csv(out / f"{prefix}_Deltas.csv", index=False)
        d_rows.to_csv(out / f"{prefix}_Delta_Rows.csv", index=False)
        comparison = (summary, diffs)
    (out / f"{prefix}_run.json").write_text(json.dumps({"n_sites": len(labels), "labels": sorted(labels.values()), "window": [start, end], "profile": profile, "preset": preset, "decisions": dec, "pinned": pinned is not None,
                                                       "n": int(len(pooled)), "bootstraps": n_bootstraps, "random_state": random_state, "timings_seconds": timings}, indent=1), encoding="utf-8")
    return {"data": pooled, "tables": res["tables"], "workbook": wb["path"], "files": res["files"] + [str(wb["path"])], "comparison": comparison, "summary": res["summary"], "timings": timings}
