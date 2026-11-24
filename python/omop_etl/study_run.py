"""One call from per-site OMOP DuckDB files to the manuscript tables, figures and workbook (unreleased).

``run_study`` is the repeatable form of the multi-site readmission study: for each site it builds the cohort
(:func:`omop_etl.cohort.define_study_cohort`, one random stay per patient in a date window, or the stays you pin from a file),
the benchmark features (:func:`omop_etl.benchmark_features.extract_benchmark_features`, ``profile="ederri"`` by default), pools
the sites, runs :func:`omop_etl.study_report.build_study_tables` and writes the manuscript workbook
(:func:`omop_etl.manuscript_tables.manuscript_workbook`); with ``compare_to`` it also compares the new workbook with a previous
one. Site databases are read-only. ``scripts/reproduce_study.py`` is the command-line form.
"""

from __future__ import annotations

import contextlib

import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["assign_site_labels", "load_site_labels", "save_site_labels", "validate_site_labels", "RunPrivacyError", "run_environment", "run_study", "PROTOCOL_COHORT_PARAMS", "pin_stays", "figure1_attrition", "redraw_cohort_flowchart", "mark_random_draw", "drop_last_lab_values", "drop_duplicate_predictors", "hemoglobin_is_hematocrit", "DUPLICATE_PREDICTORS", "STUDY_DECISIONS", "STUDY_PRESETS", "resolve_decisions"]

from omop_etl.concept_sets import CONCEPT_SET_VERSION, LEGACY_CONCEPT_SETS  # noqa: E402

#: Version of the package's default feature rules, saved with every site's features. Bumped when a default changes what the same
#: database gives (0.35.0: combination drugs count for each ingredient's class, SI-unit labs are converted), so --reuse-features
#: refuses features saved under older rules (stale_features) instead of reusing them silently.
FEATURE_RULES_VERSION = "0.35.0"

# Every choice that moves the published numbers, with the options and what each does. Nothing is hard-wired: each can be set on
# run_study (or on reproduce_study.py) and the values used are written to <prefix>_run.json.
STUDY_DECISIONS: dict[str, dict[str, Any]] = {
    "concept_sets": {"options": ("bundled", LEGACY_CONCEPT_SETS, CONCEPT_SET_VERSION),
                     "about": "Lab and vital definitions: 'bundled' (the concept sets of this release, now version " + CONCEPT_SET_VERSION + ", with "
                              "troponin I, lactate, pCO2 and pO2), '" + LEGACY_CONCEPT_SETS + "' (the plain LOINC lists of 0.20.0 to 0.28.1 behind the "
                              "workbook delivered on 2026-11-11), or an exact bundled version, which fails on a release that ships another one."},
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
    "table6_missing": {"options": ("indicator", "median"),
                       "about": "How a continuous Table 6 measure with missing values enters the model: 'indicator' (a 'not measured' term next to it, "
                                "the measure's OR estimated from the stays where it was measured; right when a lab is measured only when ordered) or "
                                "'median' (median imputation only, as delivered; biases the OR of a mostly-unmeasured lab towards 1)."},
    "last_lab_values": {"options": ("drop", "keep"),
                        "about": "The latest in-stay laboratory values (lab_<name>_last) as model features. 'drop' (both presets since 0.33.5): every model "
                                 "uses the index-stay mean of each laboratory only, as the Methods, Table 1 and Table 4 describe; 'keep' also offers the "
                                 "last value to the models. The saved features keep both, so features can be reused either way."},
    "ineligible_draw": {"options": ("redraw", "exclude"),
                        "about": "A patient whose stay in the pinned draw is not eligible in this data (for example a discharge status corrected to a "
                                 "transfer or death) but who has other eligible stays: 'redraw' (both presets since 0.34.1, Kyle: the draw samples "
                                 "eligible stays only) gives the patient one of those stays at random (seeded by random_state); 'exclude' leaves the "
                                 "patient out at the random-draw step, as 0.34.0 did."},
}
STUDY_PRESETS: dict[str, dict[str, Any]] = {
    # The package follows the Nov 11 deliverable except where it was wrong (Kyle, 2026-11-17): hierarchy-based concept sets, a
    # "not measured" term instead of median imputation alone, an SDI-missing term (without it missing SDI joins the reference
    # quintile), a CONSORT table that ends at the analysed cohort, and dated addresses ahead of undated ones (the delivered order
    # came from an operator-precedence slip in the study pipeline).
    "package": {"consort_final_steps": True, "address_undated": "last", "address_tie_break": "first_listed", "sdi_missing_term": True, "table5": "manuscript",
                "attrition_mode": "full_history", "table6_increments": "clinical", "age_method": "completed_years",
                "concept_sets": "bundled", "table6_missing": "indicator", "last_lab_values": "drop",
                "ineligible_draw": "redraw"},
    "delivered_2026_11_11": {"consort_final_steps": False, "address_undated": "first", "address_tie_break": "first_listed", "sdi_missing_term": False, "table5": "manuscript", "attrition_mode": "full_history",
                             "table6_increments": "clinical", "age_method": "completed_years",
                             # pinned: a rerun on a later release must not mix feature-definition changes into the comparison with Nov 11
                             "concept_sets": LEGACY_CONCEPT_SETS, "table6_missing": "median",
                             # Kyle, 2026-11-22: the manuscript describes index-stay mean labs only, so the models use only those
                             "last_lab_values": "drop",
                             # Kyle, 2026-11-24: sample from eligible stays only (the delivered draw predates the discharge-status repair)
                             "ineligible_draw": "redraw"},
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


def validate_site_labels(sites: Sequence[str], labels: Mapping[str, str]) -> dict[str, str]:
    """Checks a ``site -> label`` mapping for a run over ``sites`` and returns the part of it those sites use.

    Raises ``ValueError`` when a site has no label (it would appear in the tables under its own name), when two sites share a label,
    when a label is empty, or when a label is one of the site names. Extra entries are allowed, so one consortium-wide mapping file can be
    handed to every site and each runs with only its own databases. Any number of sites is fine."""
    sites = list(dict.fromkeys(str(s) for s in sites))
    lab = {str(k): str(v).strip() for k, v in dict(labels).items()}
    missing = [s for s in sites if s not in lab]
    if missing:
        raise ValueError(f"{len(missing)} of {len(sites)} site(s) have no label (site number(s) {[sites.index(m) + 1 for m in missing]} in the order "
                         "given); every site needs one, or it would appear under its own name. Give one label per site.")
    used = {s: lab[s] for s in sites}
    if any(not v for v in used.values()):
        raise ValueError("A site label is empty.")
    vals = [v.casefold() for v in used.values()]
    if len(set(vals)) != len(vals):
        raise ValueError("Two sites share a label; each site needs its own.")
    keys = {s.casefold() for s in sites}
    if any(v in keys for v in vals):
        raise ValueError("A label is the same as a site name; labels must be anonymous.")
    return used


def run_environment() -> dict[str, str | None]:
    """Versions of Python and the libraries that move the model numbers, plus the platform (operating system, CPU count, the XGBoost build
    and the BLAS / OpenMP thread pools), for ``<prefix>_run.json``: two runs on the same data that differ in a model-performance cell
    usually differ here (a newer scikit-learn or XGBoost, or Windows versus Linux), not in the package's seeds."""
    import platform
    from importlib import metadata

    out: dict[str, str | None] = {"python": platform.python_version()}
    for pkg in ("omop-duck-db", "numpy", "pandas", "scikit-learn", "xgboost", "scipy", "duckdb", "pyarrow", "shap"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    from omop_etl.package_citations import omop_duck_db_version

    out["omop-duck-db"] = omop_duck_db_version()  # the code that ran, even from a checkout over an older installed package
    out.update(_platform_details())
    return out


def _platform_details() -> dict[str, str | None]:
    """Operating system, CPU and the native builds under the model libraries. The same library versions can still give slightly
    different model numbers on Windows and Linux (another compiler and BLAS build), so the run record says which platform made it.
    No host name, user name or file path is recorded."""
    import os
    import platform

    out: dict[str, str | None] = {"platform": platform.platform(terse=True), "machine": platform.machine() or None,
                                  "cpu_count": str(os.cpu_count()) if os.cpu_count() else None,
                                  "container": "yes" if (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")) else "no"}
    try:
        import xgboost

        info = xgboost.build_info()
        keep = {k: v for k, v in info.items() if any(t in k.upper() for t in ("GCC", "CLANG", "MSVC", "COMPILER", "OPENMP"))}
        out["xgboost_build"] = ", ".join(f"{k}={'.'.join(map(str, v)) if isinstance(v, (list, tuple)) else v}" for k, v in sorted(keep.items())) or None
    except Exception:  # noqa: BLE001 - not installed, or an older build without build_info()
        out["xgboost_build"] = None
    try:
        from threadpoolctl import threadpool_info

        pools = [f"{p.get('internal_api')} {p.get('version')} ({p.get('num_threads')} threads)" for p in threadpool_info()]
        out["thread_pools"] = "; ".join(sorted(set(pools))) or None  # BLAS / OpenMP libraries and thread counts; file paths left out
    except Exception:  # noqa: BLE001
        out["thread_pools"] = None
    return out


class RunPrivacyError(RuntimeError):
    """A shareable study output contains a real site name (raised by :func:`run_study` after the scan; the message names files, never sites)."""


def _privacy_names(sites: Sequence[str], labels: Mapping[str, str], site_names: Sequence[str] | str | Path | None) -> list[str]:
    """Names the output scan looks for: the given real names plus the site keys, except keys too short or too label-like to search for
    (a key ``A`` would match ``Site A``)."""
    from omop_etl.run_privacy import read_names_file

    names = (read_names_file(site_names) if isinstance(site_names, (str, Path)) else list(site_names or []))
    words = {w.casefold() for v in labels.values() for w in re.split(r"[^A-Za-z0-9]+", v) if w}
    names += [s for s in sites if len(s) >= 3 and s.casefold() not in words and not any(s.casefold() == v.casefold() for v in labels.values())]
    return list(dict.fromkeys(n for n in names if n.strip()))


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
    transfer_discharge_concept_ids=[8717], still_in_hospital_status=["SH"], exclude_metformin_only_prediabetes_pcos=True)


def pin_stays(cohort: pd.DataFrame, selection: str | Path | pd.DataFrame, *, on: str = "visit_occurrence_id", strict: bool = True,
              redraw: bool = False, random_state: int = 42, patients: Sequence[Any] | None = None) -> pd.DataFrame:
    """Keeps the cohort rows whose ``on`` value is listed in ``selection`` (a DataFrame or a CSV path), to reproduce a previous
    stay-per-patient draw exactly. Build the cohort with ``sample_stay_rule="all"`` so every candidate stay is present.

    ``strict=True`` raises if a selected stay is not in the cohort (a different cohort definition, window or data cut); ``False`` keeps
    what is found. Raises if the selection lists a patient more than once.

    ``redraw=True``: a patient of the selection whose selected stay is not among the cohort rows (it is no longer eligible) but who has
    other cohort stays gets one of them drawn at random, seeded by ``random_state``, so the result samples eligible stays only. The
    selection's patients are ``patients`` (ids), else its ``subject_id`` / ``person_id`` column; a patient outside the selection is not
    added. The draw does not depend on row order. ``attrs["redrawn"]`` is the number of patients drawn this way."""
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
    n_redrawn = 0
    if redraw:
        if pid is None:
            raise ValueError("redraw needs a subject_id or person_id column in the cohort.")
        if patients is None:
            col = next((c for c in ("subject_id", "person_id") if c in sel), None)
            if col is None:
                raise ValueError("redraw needs the selection's patients: pass patients= or a subject_id / person_id column.")
            patients = sel[col].dropna().tolist()
        listed = {int(x) for x in patients}
        rest = cohort[cohort[pid].astype("int64").isin(listed) & ~cohort[pid].isin(set(out[pid]))]
        if len(rest):
            rest = rest.sort_values([pid, on], kind="mergesort")
            key = np.random.default_rng(random_state).random(len(rest))
            pick = rest.assign(_k=key).sort_values([pid, "_k"], kind="mergesort").drop_duplicates(pid).drop(columns="_k")
            n_redrawn = len(pick)
            out = pd.concat([out, pick]).sort_index(kind="mergesort")
    out = out.reset_index(drop=True)
    out.attrs["redrawn"] = n_redrawn
    return out


def _implied_feature_rules(meta: Mapping[str, Any]) -> dict[str, str]:
    """Features saved by the first 0.35.0 build (before ``feature_rules`` was recorded) already used the 0.35.0 rules: their metadata
    has ``unit_converted``, which 0.34.2 and earlier never wrote."""
    if "feature_rules" not in meta.get("settings", {}) and "unit_converted" in meta:
        return {"feature_rules": "0.35.0"}
    return {}


@contextlib.contextmanager
def _period_end_check_reported():
    """Silences define_study_cohort()'s own observation-period-end warning: _site_frame() has already reported the spike as an
    ``observation_period`` finding (or capped the periods, which piles them on the cap date on purpose), so the library warning
    would only repeat it as an unexplained ``warning`` (C18 review)."""
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r".* observation periods end on ", category=UserWarning)
        yield


def _site_frame(db_dir: Path, site: str, *, start: str, end: str, params: Mapping[str, Any], profile: str | None, pinned: pd.DataFrame | None,
                ruca: Any, sdi: Any, cuts: Sequence[float] | None, address_table: str | None, transfer_source_values: Sequence[str],
                feature_kwargs: Mapping[str, Any], work: Path, attrition_mode: str = "windowed", redraw: bool = False, random_state: int = 42,
                observation_period_cap: str | None = None, all_stays: bool = False):
    """One site's pooled-ready frame, its attrition and ``meta`` (feature groups, skipped features, the address source used,
    pinned-list coverage). Data gaps it can see are raised as :class:`omop_etl.data_gaps.DataGapWarning` for run_study to judge.
    ``observation_period_cap``: observation periods are read through a temporary view that ends them at this date (the database
    is not changed). ``all_stays``: the features are extracted for every eligible stay in the window, and ``meta["all_stays_frame"]``
    holds them (the sensitivity analyses); the primary frame is the same subset of rows as without it."""
    import warnings

    import duckdb

    from omop_etl.data_gaps import DataGapWarning

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
        meta: dict[str, Any] = {"address_table": None}
        if observation_period_cap is not None:
            from omop_etl.cohort import check_observation_period_ends

            raw = check_observation_period_ends(con, warn=False)
            tot, short = con.execute(f"SELECT count(*), count(*) FILTER (WHERE observation_period_end_date > DATE '{observation_period_cap}') "
                                     "FROM observation_period").fetchone()
            meta["observation_period_cap"] = {"cap": observation_period_cap, "share_shortened": round(short / tot, 4) if tot else None,
                                              "spike_before_cap": bool(raw.get("flagged"))}
            # CASE, not least(): least() ignores NULL and would give an open period the cap as its end date
            con.execute("CREATE OR REPLACE TEMP VIEW observation_period AS SELECT * REPLACE (CASE WHEN observation_period_end_date > "
                        f"DATE '{observation_period_cap}' THEN DATE '{observation_period_cap}' ELSE observation_period_end_date END "
                        "AS observation_period_end_date) FROM site_db.main.observation_period "
                        f"WHERE observation_period_start_date <= DATE '{observation_period_cap}'")
        _check_coverage_of_window(con, start, end, params, spike=observation_period_cap is None)
        if ruca is not None and sdi is not None:
            # before the (slow) cohort build, so a missing address table fails in seconds, not after the whole site
            from omop_etl.geography import PERSON_LOCATION, resolve_address_table
            try:
                address_table = resolve_address_table(con, address_table)
            except ValueError as exc:
                warnings.warn(DataGapWarning(str(exc), "address_table"), stacklevel=2)
                address_table = None
            meta["address_table"] = address_table or PERSON_LOCATION
        rule = {"sample_stay_rule": "all"} if pinned is not None else {"sample_stay_rule": "random"}
        with _period_end_check_reported():
            cohort = define_study_cohort(con, materialise=False, min_admit_date=start, max_admit_date=end, **rule, **dict(params))
        att = pd.DataFrame(cohort.attrs.get("attrition"))
        att["site"] = site
        cohort.attrs = {}
        every = None
        if all_stays:  # every eligible stay in the window: the pinned path already built them, the random path builds them once more
            with _period_end_check_reported():
                every = cohort.copy() if pinned is not None else define_study_cohort(con, materialise=False, min_admit_date=start, max_admit_date=end,
                                                                                     sample_stay_rule="all", attrition=False, **dict(params))
            every.attrs = {}
        if attrition_mode == "full_history":
            # the manuscript's CONSORT: every rule over the whole history, then the study window and the one stay per patient last
            with _period_end_check_reported():
                full = define_study_cohort(con, materialise=False, sample_stay_rule="all", **dict(params))
            att = pd.DataFrame(full.attrs.get("attrition"))
            att["site"] = site
            n_final = int(cohort["subject_id"].nunique()) if "subject_id" in cohort else len(cohort)
            # the qualifying stays the one-per-patient draw chose from: every eligible stay with an index admission in the window.
            # Recorded only when those stays belong to exactly the analysed patients, so the text never prints a patient count as stays.
            if {"cohort_start_date", "subject_id"} <= set(full.columns):
                d = pd.to_datetime(full["cohort_start_date"])
                in_win = full[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
                if int(in_win["subject_id"].nunique()) == n_final:
                    meta["qualifying_stays"] = int(len(in_win))
            att = pd.concat([att, pd.DataFrame([{"step_name": f"Index admission {start} to {end}, one stay per patient", "subjects_retained": n_final, "site": site}])],
                            ignore_index=True)
        elif attrition_mode != "windowed":
            raise ValueError("attrition_mode must be 'windowed' or 'full_history'.")
        if pinned is not None:
            from_site = pinned[pinned["site_source"] == site] if "site_source" in pinned else pinned
            patients = None
            if redraw:  # the patients of the listed stays, so a patient outside the draw is never added
                ids = from_site["visit_occurrence_id"].dropna().astype("int64").tolist() if "visit_occurrence_id" in from_site else []
                patients = [r[0] for r in con.execute("SELECT DISTINCT person_id FROM visit_occurrence WHERE visit_occurrence_id IN "
                                                      "(SELECT unnest(?::BIGINT[]))", [ids]).fetchall()] if ids else []
            cohort = pin_stays(cohort, from_site, strict=False, redraw=redraw, random_state=random_state, patients=patients)
            redrawn = int(cohort.attrs.get("redrawn", 0))
            cohort.attrs = {}
            meta["redrawn"] = redrawn  # pooled for the text; the per-site value stays in the private metadata
            listed = int(from_site["visit_occurrence_id"].nunique()) if "visit_occurrence_id" in from_site else 0
            meta["pinned_found_share"] = round((len(cohort) - redrawn) / listed, 4) if listed else None
            if not listed:
                warnings.warn(DataGapWarning("the pinned stay list has no stay for this site", "pinned"), stacklevel=2)
            elif len(cohort) - redrawn < listed:
                warnings.warn(DataGapWarning(f"{1 - (len(cohort) - redrawn) / listed:.2%} of the pinned stays for this site are not in the cohort "
                                             "(different cohort definition, window or data cut); "
                                             + ("their patients were given another eligible stay at random" if redraw else "they were dropped"), "pinned"), stacklevel=2)
            att = pd.concat([att, pd.DataFrame([{"step_name": "Stay selected for the patient (pinned list)", "subjects_retained": len(cohort), "site": site}])], ignore_index=True)
        path = work / f"cohort_{site}.parquet"
        cohort.to_parquet(path)
        if every is not None:  # features once, for every eligible stay; the primary rows are picked out of them below
            path_all = work / f"cohort_all_{site}.parquet"
            every.to_parquet(path_all)
            con.execute(f"CREATE OR REPLACE TEMP VIEW cohort_t AS SELECT * FROM read_parquet('{path_all.as_posix()}')")
        else:
            con.execute(f"CREATE OR REPLACE TEMP VIEW cohort_t AS SELECT * FROM read_parquet('{path.as_posix()}')")
        kw: dict[str, Any] = dict(feature_kwargs)
        if ruca is not None and sdi is not None:
            kw.update(include_sdoh=True, ruca_crosswalk=str(ruca), sdi_table=str(sdi), sdi_cut_points=list(cuts or ()), address_table=address_table)
        feats = extract_benchmark_features(con, "cohort_t", vocabulary_db=None, profile=profile, **kw)
        meta["feature_groups"] = {k: list(v) for k, v in (feats.attrs.get("feature_groups") or {}).items()} or None
        meta["skipped"] = list(feats.attrs.get("skipped") or [])
        if feats.attrs.get("address_table"):
            meta["address_table"] = feats.attrs["address_table"]
        meta["geography"] = feats.attrs.get("geography")
        meta["bmi_filled_share"] = ({k: round(v / len(feats), 4) for k, v in (feats.attrs.get("bmi_filled") or {}).items()}
                                    if len(feats) else None)  # shares, not counts: the per-site metadata stays shareable
        meta["unit_converted"] = feats.attrs.get("unit_converted") or {}   # shares of each lab's values converted from SI units
        meta["combination_share"] = ({k: round(v / len(feats), 4) for k, v in (feats.attrs.get("combination_flags") or {}).items() if v}
                                     if len(feats) else {})  # share of stays a med class reached only through an ingredient of a combination
    finally:
        con.close()

    def _frame(c: pd.DataFrame) -> pd.DataFrame:
        d = c.merge(feats, on="visit_occurrence_id", suffixes=("", "_f"))
        d = d.drop(columns=[x for x in d.columns if x.endswith("_f")])
        d["readmitted_30d"] = d["outcome_flag"].astype(int)
        d["site_source"] = site
        d["admit_date"] = pd.to_datetime(d["cohort_start_date"])
        d["discharge_date"] = pd.to_datetime(d["cohort_end_date"])
        d["index_los_days"] = (d["discharge_date"] - d["admit_date"]).dt.days
        d["person_id"] = d["subject_id"]
        return d

    df = _frame(cohort)
    if every is not None:
        meta["all_stays_frame"] = _frame(every)
    return df, att, meta


def _check_coverage_of_window(con: Any, start: str, end: str, params: Mapping[str, Any], slack_days: int = 90, spike: bool = True) -> None:
    """Gap warnings when a site's stays do not cover the study window, or (``spike``) its observation periods pile up on the build
    date. Reports day counts and shares, never dates (a site's data end could identify it)."""
    import warnings

    import duckdb

    from omop_etl.cohort import check_observation_period_ends
    from omop_etl.data_gaps import DataGapWarning

    types = [int(x) for x in (params.get("visit_type") or (9201, 262))] if not isinstance(params.get("visit_type"), int) else [int(params["visit_type"])]
    try:
        lo, hi = con.execute(f"SELECT min(CAST(visit_start_date AS DATE)), max(CAST(visit_start_date AS DATE)) FROM visit_occurrence "
                             f"WHERE visit_concept_id IN ({', '.join(map(str, types))})").fetchone()
    except duckdb.Error:
        return
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if hi is None:
        warnings.warn(DataGapWarning("the site has no inpatient stay at all", "window"), stacklevel=2)
        return
    short_end, late_start = (e - pd.Timestamp(hi)).days, (pd.Timestamp(lo) - s).days
    if short_end > slack_days:
        warnings.warn(DataGapWarning(f"the site's inpatient stays end {short_end} days before the study window ends, so its later "
                                     "admissions and their readmissions are missing", "window"), stacklevel=2)
    if late_start > slack_days:
        warnings.warn(DataGapWarning(f"the site's inpatient stays begin {late_start} days after the study window starts", "window"), stacklevel=2)
    if not spike:  # the periods are capped at a data cutoff (run_study observation_period_cap): a pile on the cap is intended
        return
    try:
        op = check_observation_period_ends(con, warn=False)
    except Exception:  # pragma: no cover - the check itself never raises; a broken table is reported by the cohort build
        return
    if op.get("flagged"):
        warnings.warn(DataGapWarning(
            f"{op['periods'] / max(op['total'], 1):.3%} of observation periods end on the last end date, about "
            f"{op['periods'] / max(op['typical_daily'], 1.0):.0f} times a typical day: source events were probably filled with the build "
            "date, so follow-up there is unreliable (omop_etl.build_omop_cdm.cap_observation_period)", "observation_period"), stacklevel=2)


def _file_fingerprint(path: Any) -> dict[str, Any] | None:
    """File name (no folder: paths can name a user) and SHA-256 of an input file, for the run record."""
    if path is None or not isinstance(path, (str, Path)) or not Path(path).is_file():
        return None
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"file": Path(path).name, "sha256": h.hexdigest()}


def _caught_findings(caught: Sequence[Any], label: str, scrub: Mapping[str, str]) -> list[dict[str, Any]]:
    """The gap warnings raised while one site was built (errors) and the other warnings the library printed (warnings)."""
    import warnings

    from omop_etl.data_gaps import ConceptSetExclusionWarning, DataGapWarning, mask_counts, sanitize

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for w in caught:
        msg = sanitize(str(w.message), scrub)
        if isinstance(w.message, ConceptSetExclusionWarning):
            # an intentional concept-set exclusion: listed once per study by run_study, not once per site (0.35.0)
            out.append({"site": label, "check": "concept_set_exclusion", "severity": "info", "column": w.message.column,
                        "codes": list(w.message.codes), "message": mask_counts(msg)})
            continue
        if not isinstance(w.message, DataGapWarning):
            # library warnings were not written for a shareable record and can carry row counts ("33 of 61,204 periods"):
            # the run record keeps their wording, not their numbers
            msg = mask_counts(msg)
        if isinstance(w.message, DataGapWarning):
            key = (w.message.check, msg)
            if key not in seen:
                seen.add(key)
                out.append({"site": label, "check": w.message.check, "severity": "error", "message": msg,
                            **({"column": w.message.column} if getattr(w.message, "column", None) else {})})
        elif issubclass(w.category, UserWarning):
            if ("warning", msg) not in seen:
                seen.add(("warning", msg))
                out.append({"site": label, "check": "warning", "severity": "warning", "message": msg})
        else:  # deprecations and the like: show them as before
            warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)
    return out


def _with_coverage(raised: list[dict[str, Any]], frame: pd.DataFrame, meta: Mapping[str, Any], label: str, threshold: float) -> list[dict[str, Any]]:
    """``raised`` plus the feature-coverage findings; a group already reported by the code that saw the gap is not reported twice."""
    from omop_etl.data_gaps import feature_coverage, geography_findings

    cover = feature_coverage(frame, site=label, groups=meta.get("feature_groups"), skipped=meta.get("skipped") or (), threshold=threshold)
    cover += geography_findings(meta.get("geography"), site=label)
    have = {f["check"] for f in raised}
    return raised + [f for f in cover if not (f["check"] in have and f["check"] in ("sdoh", "group_skipped", "empty_site", "address_table"))
                     and not (f["check"] == "sdoh" and "address_table" in have)]


def _final_steps(attrition: pd.DataFrame) -> pd.Series:
    """Rows of the steps run_study adds after the eligibility rules: the window / one-stay-per-patient step and the pinned-list step."""
    name = attrition["step_name"].astype(str)
    return name.str.startswith("Index admission ") | name.str.startswith("Stay selected for the patient")


def figure1_attrition(attrition: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """The steps Figure 1 draws, summed over sites: every eligibility rule, then the grant protocol window (index admission from
    ``start`` to ``end``, one stay per patient) and, with pinned stays, the pinned list, so the figure ends at the analysed cohort.
    Returns ``step_name``, ``subjects_retained``, ``subjects_dropped``, ``excluded_label`` (the text of the exclusion arrow) and
    ``excluded_shown`` and ``into_final`` (True for the random-draw step, :func:`mark_random_draw`)."""
    from omop_etl.manuscript_text import _date_text

    first = attrition.groupby("step_name", sort=False)["subjects_retained"].sum().rename("subjects_retained").reset_index()
    first["subjects_dropped"] = (first["subjects_retained"].shift(1) - first["subjects_retained"]).fillna(0).astype(int)
    first["excluded_label"] = None
    win = first["step_name"].astype(str).str.startswith("Index admission ")
    first.loc[win, "excluded_label"] = "Excluded: no qualifying index admission in the grant protocol window"
    first.loc[win, "step_name"] = f"Grant protocol window: index admission {_date_text(start)} to {_date_text(end)}"
    # the pinned list is the study's own random draw of one qualifying stay per patient. Since 0.33.7 the figure draws no box for it: the
    # grant window box leads straight into the analytic cohort box, and the patients whose qualifying stay is not in the draw branch off
    # that last down arrow (Kyle, 2026-11-23: one random-stay box, no "pinned" anywhere); the run record keeps the step's counts
    return mark_random_draw(first)


RANDOM_DRAW_STEP = "Randomly selected qualifying stay per patient"
RANDOM_DRAW_EXCLUDED = "Excluded: qualifying stay not in the study's original random draw"


def mark_random_draw(steps: pd.DataFrame) -> pd.DataFrame:
    """Marks the random-draw step of a Figure 1 attrition table (named "Stay selected for the patient ..." by the run, or
    :data:`RANDOM_DRAW_STEP` in a saved ``<prefix>_CONSORT_Attrition.csv``): it gets the name :data:`RANDOM_DRAW_STEP`, the exclusion text
    :data:`RANDOM_DRAW_EXCLUDED` and ``into_final=True``, so :func:`omop_etl.study_report.plot_cohort_flowchart` draws no box for it and
    shows its excluded patients on the arrow into the analytic cohort box."""
    out = steps.copy()
    name = out["step_name"].astype(str)
    pin = name.str.startswith("Stay selected for the patient") | (name == RANDOM_DRAW_STEP)
    if "excluded_label" not in out:
        out["excluded_label"] = None
    out["excluded_label"] = out["excluded_label"].astype(object)
    out.loc[pin, "step_name"] = RANDOM_DRAW_STEP
    out.loc[pin, "excluded_label"] = RANDOM_DRAW_EXCLUDED
    out["excluded_shown"] = True
    out["into_final"] = pin.to_numpy()
    return out


def redraw_cohort_flowchart(run_dir: str | Path, *, prefix: str = "Study", path: str | Path | None = None, label_col: str = "readmitted_30d") -> Path:
    """Draws Figure 1 again from a finished run's shareable outputs, with no data access: the pooled steps in
    ``<prefix>_CONSORT_Attrition.csv`` and the cohort and readmission counts in the "N" row of ``<prefix>_Table1.csv``. Writes
    ``<prefix>_Figure1_Cohort_Flowchart.png`` in ``run_dir`` (or ``path``) and returns its path. Use it to bring an older run's figure up to
    the current layout before re-rendering its manuscript."""
    from omop_etl.study_report import plot_cohort_flowchart

    run_dir = Path(run_dir)
    steps = pd.read_csv(run_dir / f"{prefix}_CONSORT_Attrition.csv")
    t1 = pd.read_csv(run_dir / f"{prefix}_Table1.csv", dtype=str)
    row = t1[t1["Variable"].astype(str) == "N"].iloc[0]
    n, events = int(float(row["Overall"])), int(float(row[f"{label_col}=1"]))
    out = Path(path) if path is not None else run_dir / f"{prefix}_Figure1_Cohort_Flowchart.png"
    win = steps["step_name"].astype(str).str.startswith("Grant protocol window")
    steps["excluded_label"] = None
    steps.loc[win, "excluded_label"] = "Excluded: no qualifying index admission in the grant protocol window"
    plot_cohort_flowchart(mark_random_draw(steps), n, events, path=out)
    return out


def drop_last_lab_values(df: pd.DataFrame) -> pd.DataFrame:
    """The feature table without the latest in-stay lab values (``lab_<name>_last``): the models see each laboratory's index-stay
    mean only (decision ``last_lab_values="drop"``)."""
    return df.drop(columns=[c for c in df.columns if re.fullmatch(r"lab_.+_last", str(c))])


# a predictor that repeats another one (same value in every row, or the same quantity capped differently) -> the column it repeats;
# both in the model split one effect between two coefficients and list the same thing twice among the predictors
DUPLICATE_PREDICTORS: dict[str, str] = {"los_days": "index_los_days", "days_since_last_encounter": "days_since_prior_encounter"}


def drop_duplicate_predictors(df: pd.DataFrame, duplicates: Mapping[str, str] | None = None) -> pd.DataFrame:
    """The feature table without the columns of :data:`DUPLICATE_PREDICTORS` whose counterpart is present (``los_days`` repeats
    ``index_los_days``; ``days_since_last_encounter`` is ``days_since_prior_encounter`` capped differently). A column whose counterpart
    is absent is kept."""
    dup = DUPLICATE_PREDICTORS if duplicates is None else duplicates
    drop = [c for c, keep in dup.items() if c in df.columns and keep in df.columns]
    if hemoglobin_is_hematocrit(df):  # hemoglobin was filled from hematocrit / 3 for every stay: one feature, kept as hemoglobin (Kyle, C18)
        drop += [c for c in ("lab_hematocrit", "lab_hematocrit_last") if c in df.columns]
    return df.drop(columns=drop)


def hemoglobin_is_hematocrit(df: pd.DataFrame) -> bool:
    """True when ``lab_hemoglobin`` is ``lab_hematocrit / 3`` in every stay that has either (no measured hemoglobin at all, so the
    eDERRI fill made it a rescaled copy of hematocrit; run C18 found this at every site). Then the two are one predictor."""
    if "lab_hemoglobin" not in df.columns or "lab_hematocrit" not in df.columns:
        return False
    hgb = pd.to_numeric(df["lab_hemoglobin"], errors="coerce")
    hct = pd.to_numeric(df["lab_hematocrit"], errors="coerce")
    if not hct.notna().any() or (hgb.notna() != hct.notna()).any():
        return False
    both = hgb.notna()
    return bool(np.allclose(hgb[both].to_numpy(float), hct[both].to_numpy(float) / 3.0, rtol=1e-9, atol=1e-9))


def _canonical_order(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Pools the site frames in an order that depends only on the data: sites by their key (``site_source``), then rows by person and
    stay. The cross-validation folds and the upsampled training rows are drawn by row position, so before this the model tables changed
    with the order of ``--sites`` (and with any change in the order a query returned rows), not only with the data."""
    def key(d: pd.DataFrame) -> str:
        return str(d["site_source"].iloc[0]) if "site_source" in d and len(d) else ""

    pooled = pd.concat(sorted(frames, key=key), ignore_index=True, sort=False)
    cols = [c for c in ("site_source", "person_id", "visit_occurrence_id", "admit_date") if c in pooled]
    if cols:
        pooled = pooled.sort_values(cols, kind="mergesort", na_position="last").reset_index(drop=True)
    return pooled


def run_study(db_dir: str | Path, out_dir: str | Path, *, sites: Sequence[str], site_labels: Mapping[str, str] | str | Path | None = None, label_seed: int | None = None, labels_out: str | Path | None = None,
              start: str = "2017-01-01", end: str = "2022-09-30", cohort_params: Mapping[str, Any] | None = None, profile: str | None = "ederri",
              pinned_stays: str | Path | pd.DataFrame | None = None, ruca_crosswalk: str | Path | None = None, sdi_table: str | Path | None = None,
              sdi_cut_points: Sequence[float] | None = (25, 41, 58, 77), address_table: str | None = "auto", transfer_source_values: Sequence[str] = ("IP",),
              feature_kwargs: Mapping[str, Any] | None = None, n_bootstraps: int = 100, loso: bool = True, figures: bool = True,
              reuse_features: bool = False, compare_to: str | Path | None = None, prefix: str = "Study", random_state: int = 42,
              min_cell: int | None = 11, attrition_mode: str | None = None, table5: str | None = None, sdi_missing_term: bool | None = None,
              address_tie_break: str | None = None, address_undated: str | None = None, table6_increments: str | None = None, consort_final_steps: bool | None = None,
              age_method: str | None = None, concept_sets: str | None = None, table6_missing: str | None = None, last_lab_values: str | None = None,
              ineligible_draw: str | None = None,
              sheet_site_labels: Mapping[str, Mapping[str, str]] | None = None, preset: str = "package",
              private_dir: str | Path | None = None, document: bool = True, document_formats: Sequence[str] = ("docx", "pdf"),
              document_title: str | None = None, document_author: str = "", site_names: Sequence[str] | str | Path | None = None,
              privacy_check: bool = True, document_cache_dir: str | Path | None = None, allow_data_gaps: Sequence[str] = (),
              data_gap_threshold: float = 0.95, run_args: Mapping[str, Any] | None = None, check_only: bool = False,
              manuscript_profile: str | None = None, manuscript_params: Mapping[str, Any] | str | Path | None = None,
              known_source_gaps: Mapping[str, str] | Sequence[str] | str | Path | None = None, observation_period_cap: str | None = None,
              sensitivity: bool = False, sensitivity_seeds: Sequence[int] = tuple(range(1, 11))) -> dict[str, Any]:
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
    **Anonymity and sharing.** ``out_dir`` holds only shareable, anonymous outputs. The per-site intermediates (``features_<site>.parquet``,
    ``cohort_<site>.parquet``, ``attrition_<site>.csv``) carry the site key in their name and a ``site_source`` column, so they go to
    ``private_dir`` (default ``<out_dir>_private`` next to ``out_dir``; keep it unshared). Every site must have its own label
    (:func:`validate_site_labels`; a site without one used to appear under its own name). ``document=True`` (default) also writes
    ``<prefix>_Manuscript.docx`` / ``.pdf`` / ``.qmd`` from the workbook (:func:`omop_etl.manuscript_document.render_manuscript_document`,
    formats ``document_formats``; ``manuscript_profile`` ``"full"`` (default) writes the manuscript text from the package's versioned
    template, ``"tables_only"`` the tables under their sheet names; ``manuscript_params`` is the study team's own file of title, authors and
    review-board statement, never stored in the package; without Quarto the run record says what was not rendered; ``document_cache_dir``: a writable folder for
    Quarto's cache in a sandbox, see :func:`omop_etl.manuscript_document.renderer_env`). ``privacy_check=True`` (default) then scans
    every file in ``out_dir`` (text, workbook, DOCX and PDF text and metadata, :mod:`omop_etl.run_privacy`) for ``site_names`` (a list or
    a names file, one per line) and the site keys; a hit raises :class:`RunPrivacyError` after the run record is written, and the
    manuscript files are not moved into ``out_dir``.
    ``address_tie_break``: see :func:`omop_etl.geography.derive_geography_features`. ``preset`` names a set of the decisions above
    (:data:`STUDY_PRESETS`: ``"package"`` or ``"delivered_2026_11_11"``); any of ``attrition_mode``, ``table5``, ``sdi_missing_term``,
    ``address_tie_break``, ``address_undated``, ``table6_increments``, ``consort_final_steps``, ``age_method``, ``concept_sets``, ``table6_missing`` given (not None) overrides the preset. ``concept_sets`` picks the lab and vital definitions (see :data:`STUDY_DECISIONS`); the ``delivered_2026_11_11`` preset pins the pre-0.29.0 LOINC lists, so a rerun on a later release compares only cohort and modelling changes with the delivered workbook. The run record names the version used. :data:`STUDY_DECISIONS` lists the options. The result has ``timings`` (seconds per stage).
    **Data gaps fail the run.** Each site is checked as soon as it is built (:mod:`omop_etl.data_gaps`): no usable address table,
    a feature group missing (or all zero) for at least ``data_gap_threshold`` of the stays (SDI / RUCA included), a group that
    could not be computed, no reachable vocabulary, pinned stays not found, a site with no stays. Any of these raises
    :class:`omop_etl.data_gaps.DataGapError` (after writing ``<prefix>_data_gaps.json``) unless its check or group is named in
    ``allow_data_gaps`` (``"all"`` allows every one). Every finding, allowed or a plain warning, goes into ``<prefix>_run.json``
    with the address table used per site. ``check_only=True`` builds every site, collects every finding (no fail-fast), writes
    ``<prefix>_data_gaps.json`` and stops before any modelling: it raises ``DataGapError`` listing every gap of every site, or returns
    ``{"data_checks": [...], ...}`` when there is none. ``run_args`` (the command line) and the ``sites`` order are written only to
    ``<prefix>_run_private.json`` in ``private_dir``: together with real names they would reveal which label is which site.
    **Expected and unexpected findings (0.35.0).** Every finding gets a ``cause`` (:data:`omop_etl.data_gaps.GAP_CAUSES`):
    ``source_gap`` when ``known_source_gaps`` lists it (a file or mapping of the study team's confirmed source-extract gaps, see
    :func:`omop_etl.data_gaps.load_known_source_gaps`; such a gap never fails the run), ``intentional`` for a concept set that leaves
    codes out on purpose (listed once per study, not once per site), ``etl_mapping`` for rows that were not mapped or read, and
    ``unexplained`` for the rest. The run record counts them by cause.
    ``observation_period_cap`` (ISO date): observation periods are read as ending no later than this date (a temporary view; the
    databases are not changed), as :func:`omop_etl.build_omop_cdm.cap_observation_period` would write them, so the end-date spike of
    periods filled with the build date needs no manual repair. It must be at least the study end plus the follow-up window.
    ``sensitivity=True`` also extracts the features of every eligible stay in the window (``features_all_<site>.parquet`` in
    ``private_dir``) and runs :func:`omop_etl.study_report.stay_sensitivity`: the random draw repeated with each of
    ``sensitivity_seeds`` and every eligible stay scored with patient-grouped folds. The results go to ``<prefix>_Sensitivity.csv``,
    to ``sensitivity`` in the run record and to the manuscript's sensitivity paragraphs. The primary analysis does not change."""
    from omop_etl.manuscript_tables import compare_workbooks, manuscript_workbook
    from omop_etl.study_report import build_study_tables

    dec = resolve_decisions(preset, attrition_mode=attrition_mode, table5=table5, sdi_missing_term=sdi_missing_term,
                            address_tie_break=address_tie_break, address_undated=address_undated, table6_increments=table6_increments,
                            consort_final_steps=consort_final_steps, age_method=age_method, concept_sets=concept_sets,
                            table6_missing=table6_missing, last_lab_values=last_lab_values,
                            ineligible_draw=ineligible_draw)
    attrition_mode, table5, sdi_missing_term, address_tie_break = (dec["attrition_mode"], dec["table5"], dec["sdi_missing_term"], dec["address_tie_break"])
    undated = dec["address_undated"]
    c_params = dict(cohort_params or PROTOCOL_COHORT_PARAMS)
    # an explicit age_method wins over cohort_params; the preset's value only fills a cohort_params that does not set one
    if age_method is not None or "age_method" not in c_params:
        c_params["age_method"] = dec["age_method"]
    dec["age_method"] = c_params["age_method"]  # the run record shows the rule that was actually used
    db, out = Path(db_dir), Path(out_dir)
    site_labels_arg = site_labels
    out.mkdir(parents=True, exist_ok=True)
    if isinstance(site_labels, (str, Path)) and str(site_labels) not in ("random", "ordered"):
        site_labels = load_site_labels(site_labels)  # a JSON file written by save_site_labels
    elif isinstance(site_labels, str):
        site_labels = assign_site_labels(sites, mode=site_labels, seed=label_seed)
    labels = validate_site_labels(sites, site_labels or assign_site_labels(sites, mode="ordered"))
    priv = Path(private_dir) if private_dir is not None else out.parent / f"{out.name}_private"
    if priv.resolve() == out.resolve():
        raise ValueError("private_dir must not be out_dir: out_dir is the shareable folder.")
    priv.mkdir(parents=True, exist_ok=True)
    if labels_out is not None:
        save_site_labels(labels, labels_out)
    pinned = None if pinned_stays is None else (pd.read_csv(pinned_stays) if isinstance(pinned_stays, (str, Path)) else pinned_stays)
    import time
    import warnings

    from omop_etl.data_gaps import GAP_CAUSES, DataGapError, assess, group_missing_shares, sanitize

    allow = [str(x) for x in (allow_data_gaps or ())]
    assess([], allow)  # unknown names are refused before any site is built
    from omop_etl.data_gaps import classify_findings, collapse_concept_set_exclusions, load_known_source_gaps

    known_gaps = load_known_source_gaps(known_source_gaps)
    site_keys = {labels[s_]: s_ for s_ in sites}
    if observation_period_cap is not None:
        from omop_etl.build_omop_cdm import _iso_date

        observation_period_cap = _iso_date(observation_period_cap, "observation_period_cap")
        need = pd.Timestamp(end) + pd.Timedelta(days=int(c_params.get("followup_days") or 30))
        if pd.Timestamp(observation_period_cap) < need:
            raise ValueError(f"observation_period_cap ({observation_period_cap}) is before the study end plus the follow-up window "
                             f"({need.date()}): the last stays would lose their follow-up.")
    findings: list[dict[str, Any]] = []
    address_used: dict[str, Any] = {}
    coverage: dict[str, dict[str, float]] = {}
    geography: dict[str, Any] = {}
    home = str(Path.home())
    scrub: dict[str, str] = {str(db.resolve()): "<db_dir>", str(db): "<db_dir>", **({home: "~"} if len(home) > 1 else {})}
    for s_ in sites:
        scrub.update({str((db / f"omop_{s_}.duckdb").resolve()): f"<db_dir>/omop_<{labels[s_]}>.duckdb", f"omop_{s_}": f"omop_<{labels[s_]}>",
                      **({s_: labels[s_]} if len(s_) >= 3 else {})})

    # what the saved features of a site depend on: reuse_features refuses features built with other settings
    from omop_etl.concept_sets import concept_set_version_fingerprint, legacy_feature_codes

    concept_sets = concept_set_version_fingerprint(dec["concept_sets"])
    if dec["concept_sets"] == LEGACY_CONCEPT_SETS:
        # the pre-concept-set LOINC lists; an explicit lab_loincs / vital_loincs in feature_kwargs still wins
        legacy = legacy_feature_codes()
        feature_kwargs = {"lab_loincs": legacy["labs"], "vital_loincs": legacy["vitals"], **dict(feature_kwargs or {})}
        if "all" not in allow and "concept_set_coverage" not in allow:
            # the old lists are known to miss codes the bundled sets hold: reproducing them must not fail on it (still recorded)
            allow.append("concept_set_coverage")
    # how a missing in-stay BMI is filled (extract_benchmark_features' default for the profile); the Methods text describes it
    bmi_fill_used = (feature_kwargs or {}).get("bmi_fill") or ("derive_lookback" if profile in ("ederri", "both") else "derive")
    settings = json.loads(json.dumps({"window": [start, end], "profile": profile, "concept_sets": concept_sets["sha256"], "cohort_params": c_params, "attrition_mode": attrition_mode,
                                      "address_table": address_table, "address_tie_break": address_tie_break, "address_undated": undated,
                                      "ruca": (_file_fingerprint(ruca_crosswalk) or {}).get("sha256", None if ruca_crosswalk is None else "in memory"),
                                      "sdi": (_file_fingerprint(sdi_table) or {}).get("sha256", None if sdi_table is None else "in memory"),
                                      "sdi_cut_points": list(sdi_cut_points or ()), "transfer_source_values": list(transfer_source_values),
                                      "pinned": None if pinned is None else pd.util.hash_pandas_object(pinned, index=False).sum().item(),
                                      **({"ineligible_draw": dec["ineligible_draw"]} if pinned is not None and dec["ineligible_draw"] != "exclude" else {}),
                                      **({"observation_period_cap": observation_period_cap} if observation_period_cap is not None else {}),
                                      "feature_rules": FEATURE_RULES_VERSION,
                                      "feature_kwargs": feature_kwargs or {}}, default=str, sort_keys=True))

    def _write_gaps() -> None:
        (out / f"{prefix}_data_gaps.json").write_text(json.dumps({"threshold": data_gap_threshold, "allowed": allow, "findings": findings,
                                                                "share_missing_by_group": coverage, "geography": geography},
                                                               indent=1, default=str), encoding="utf-8")

    def _gate(new: list[dict[str, Any]]) -> None:
        classify_findings(new, known_gaps, site_keys)
        for f_ in new:  # the reason comes from the private known-gaps file and may name a site: it stays out of the shareable record
            f_.pop("known_reason", None)
        findings.extend(new)
        try:
            assess(new, allow)
        except DataGapError:
            if not check_only:  # check_only: keep going, every site's gaps are reported together at the end
                _write_gaps()
                raise

    if compare_to is not None and preset == "package":
        findings.extend(classify_findings([{"site": "-", "check": "preset", "severity": "warning", "cause": "intentional",
                         "message": "--compare-to with the default preset 'package': a workbook delivered under another preset (such as "
                                    "delivered_2026_11_11) differs in its decisions too, so not every difference is a data difference"}]))

    if pinned is not None and "site_source" in pinned:
        stray = sorted(set(pinned["site_source"].dropna().astype(str)) - set(map(str, sites)))
        if stray:
            _gate([{"site": "-", "check": "pinned", "severity": "error",
                    "message": f"the pinned stay list names {len(stray)} site(s) that are not in --sites; their stays are not used"}])

    t_start = time.time()
    timings: dict[str, float] = {}
    frames, atts, all_frames = [], [], []
    op_capped: dict[str, Any] = {}
    unit_converted: dict[str, Any] = {}
    combination_share: dict[str, Any] = {}
    qualifying_stays: dict[str, int | None] = {}
    redrawn_total = 0
    for site in sites:
        t_site = time.time()
        f = priv / f"features_{site}.parquet"
        a = priv / f"attrition_{site}.csv"
        if reuse_features and not (f.exists() and a.exists()) and (out / f.name).exists() and (out / a.name).exists():
            shutil.move(str(out / f.name), f)  # a folder from 0.24.0 or earlier: move the site-named files out of the shareable folder
            shutil.move(str(out / a.name), a)
            if (out / f"cohort_{site}.parquet").exists():
                shutil.move(str(out / f"cohort_{site}.parquet"), priv / f"cohort_{site}.parquet")
        m = priv / f"features_{site}.meta.json"
        if reuse_features and f.exists() and a.exists():
            df = pd.read_parquet(f).assign(site_anon=labels[site])  # the current labels, even if the saved run used others
            meta = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {"address_table": "unknown (reused features from before 0.29.0)"}
            saved = [dict(x, site=labels[site]) for x in meta.get("raised", [])]
            if "settings" not in meta:
                saved.append({"site": labels[site], "check": "stale_features", "severity": "warning",
                              "message": "reused features were saved before 0.29.0: the settings they were built with are unknown"})
            elif {**meta["settings"], **_implied_feature_rules(meta)} != settings:
                saved_settings = {**meta["settings"], **_implied_feature_rules(meta)}
                diff = sorted(k for k in set(settings) | set(saved_settings) if settings.get(k) != saved_settings.get(k))
                saved.append({"site": labels[site], "check": "stale_features", "severity": "error",
                              "message": f"reused features were built with different settings ({', '.join(diff)}); rerun without --reuse-features"})
            address_used[labels[site]] = sanitize(str(meta.get("address_table")), scrub)
            coverage[labels[site]] = group_missing_shares(df, meta.get("feature_groups"))
            if meta.get("geography"):
                geography[labels[site]] = {k: v for k, v in meta["geography"].items() if k != "address_table"}
            _gate(_with_coverage(saved, df, meta, labels[site], data_gap_threshold))
            qualifying_stays[labels[site]] = meta.get("qualifying_stays")
            redrawn_total += int(meta.get("redrawn") or 0)
            if meta.get("observation_period_cap"):
                op_capped[labels[site]] = meta["observation_period_cap"]
            if meta.get("unit_converted"):
                unit_converted[labels[site]] = meta["unit_converted"]
            if meta.get("combination_share"):
                combination_share[labels[site]] = meta["combination_share"]
            if sensitivity:
                fa = priv / f"features_all_{site}.parquet"
                if not fa.exists():
                    raise ValueError(f"sensitivity=True with reuse_features=True needs the saved all-stays features of every site "
                                     f"({labels[site]} has none): rerun once without reuse_features.")
                all_frames.append(pd.read_parquet(fa).assign(site_anon=labels[site]))
            frames.append(df)
            atts.append(pd.read_csv(a))
            continue
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            df, att, meta = _site_frame(db, site, start=start, end=end, params=c_params, profile=profile, pinned=pinned,
                                        ruca=ruca_crosswalk, sdi=sdi_table, cuts=sdi_cut_points, address_table=address_table,
                                        transfer_source_values=transfer_source_values, feature_kwargs={"address_tie_break": address_tie_break, "address_undated": undated, **dict(feature_kwargs or {})}, work=priv,
                                        attrition_mode=attrition_mode, redraw=dec["ineligible_draw"] == "redraw", random_state=random_state,
                                        observation_period_cap=observation_period_cap, all_stays=sensitivity)
        df["site_anon"] = labels.get(site, site)
        every = meta.pop("all_stays_frame", None)
        if every is not None:
            every["site_anon"] = labels.get(site, site)
            every.to_parquet(priv / f"features_all_{site}.parquet")
            all_frames.append(every)
        if meta.get("observation_period_cap"):
            op_capped[labels[site]] = meta["observation_period_cap"]
        if meta.get("unit_converted"):
            unit_converted[labels[site]] = meta["unit_converted"]
        if meta.get("combination_share"):
            combination_share[labels[site]] = meta["combination_share"]
        raised = _caught_findings(caught, labels[site], scrub)
        address_used[labels[site]] = sanitize(str(meta.get("address_table")), scrub)
        df.to_parquet(f)
        att.to_csv(a, index=False)
        m.write_text(json.dumps({**meta, "settings": settings, "raised": [{k: v for k, v in x.items() if k != "site"} for x in raised]}, indent=1, default=str),
                     encoding="utf-8")
        coverage[labels[site]] = group_missing_shares(df, meta.get("feature_groups"))
        if meta.get("geography"):
            geography[labels[site]] = {k: v for k, v in meta["geography"].items() if k != "address_table"}
        _gate(_with_coverage(raised, df, meta, labels[site], data_gap_threshold))
        qualifying_stays[labels[site]] = meta.get("qualifying_stays")
        redrawn_total += int(meta.get("redrawn") or 0)
        frames.append(df)
        atts.append(att)
        timings[f"site_{labels.get(site, site)}"] = round(time.time() - t_site, 1)  # keyed by the anonymous letter: the run record is shareable and must not name sites
    findings[:] = collapse_concept_set_exclusions(findings)   # an intentional concept-set exclusion is listed once per study
    if check_only:
        _write_gaps()
        assess(findings, allow)  # raises with every site's gaps at once
        return {"data_checks": findings, "address_table": address_used, "share_missing_by_group": coverage, "geography": geography,
                "private_dir": priv, "checked_only": True}
    pooled = _canonical_order(frames)
    hgb_from_hct = hemoglobin_is_hematocrit(pooled)
    pooled = drop_duplicate_predictors(pooled)
    if dec["last_lab_values"] == "drop":  # the models see the index-stay mean of each laboratory only (Methods 2.2, Tables 1 and 4)
        pooled = drop_last_lab_values(pooled)
    attrition = pd.concat(atts, ignore_index=True)
    # Figure 1 always shows every step down to the analysed cohort, the grant protocol window included, so the figure, the text and
    # the tables give the same N; consort_final_steps only decides whether Table S3 of the workbook lists the last two steps
    first = figure1_attrition(attrition, start, end)
    if int(first["subjects_retained"].iloc[-1]) != int(len(pooled)):
        _gate([{"site": "all", "check": "figure1_cohort", "severity": "warning",
                "message": f"Figure 1 ends at {int(first['subjects_retained'].iloc[-1]):,} patients but the analysed cohort has {len(pooled):,}"}])
    if not dec["consort_final_steps"]:
        attrition = attrition[~_final_steps(attrition)].reset_index(drop=True)
    qualifying = [qualifying_stays.get(labels[s_]) for s_ in sites]
    n_qualifying = sum(qualifying) if qualifying and all(q is not None for q in qualifying) else None
    t_tab = time.time()
    res = build_study_tables(pooled, out, prefix=prefix, n_bootstraps=n_bootstraps, loso=loso, attrition=first, figures=figures, workbook=False,
                             window=(start, end), min_cell=min_cell, random_state=random_state)
    timings["tables_figures"] = round(time.time() - t_tab, 1)
    wb = manuscript_workbook(pooled, res["tables"], out / f"{prefix}_Manuscript_Tables.xlsx", attrition=attrition, site_labels=labels, min_cell=min_cell,
                             title=document_title or f"{prefix} manuscript tables", byline=f"Authors: {document_author}" if document_author else "",
                             table5=table5, sdi_missing_term=sdi_missing_term,
                             table6_increments=dec["table6_increments"], table6_missing=dec["table6_missing"], sheet_site_labels=sheet_site_labels,
                             check_bootstrap_intervals=n_bootstraps >= 50)  # fewer draws: a smoke run, its percentile CIs are not meaningful
    sens_summary = None
    if sensitivity:
        from omop_etl.study_report import stay_sensitivity

        t_sens = time.time()
        every_pooled = drop_duplicate_predictors(_canonical_order(all_frames))
        if dec["last_lab_values"] == "drop":
            every_pooled = drop_last_lab_values(every_pooled)
        every_pooled = every_pooled[[c for c in pooled.columns if c in every_pooled.columns]]   # the primary models' columns
        if {"site_source", "person_id"} <= set(every_pooled.columns) and {"site_source", "person_id"} <= set(pooled.columns):
            # the primary cohort's patients only: a patient left out of the primary draw (outside a pinned draw) is not in the
            # sensitivity analyses either, so each seed draw has exactly the primary N (C18 had one stay more)
            keys_ = pd.MultiIndex.from_frame(pooled[["site_source", "person_id"]].astype(str))
            every_pooled = every_pooled[pd.MultiIndex.from_frame(every_pooled[["site_source", "person_id"]].astype(str)).isin(keys_)]
        sens_tab, sens_summary = stay_sensitivity(every_pooled, sensitivity_seeds, random_state=random_state)
        sens_tab.to_csv(out / f"{prefix}_Sensitivity.csv", index=False)
        res["files"].append(str(out / f"{prefix}_Sensitivity.csv"))
        timings["sensitivity"] = round(time.time() - t_sens, 1)
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
    files = res["files"] + [str(wb["path"])]
    doc_record: dict[str, Any] = {"requested": bool(document)}
    staged: list[Path] = []
    stage = priv / f"{prefix}_manuscript_build"
    if document:
        from omop_etl.manuscript_document import render_manuscript_document

        t_doc = time.time()
        shutil.rmtree(stage, ignore_errors=True)
        figs = [Path(x) for x in res["files"] if str(x).lower().endswith(".png")]
        d = render_manuscript_document(wb["path"], stage, prefix=prefix, figures=figs, window=(start, end), title=document_title,
                                       author=document_author, formats=document_formats, min_cell=min_cell, cache_dir=document_cache_dir,
                                       software_versions=run_environment(), profile=manuscript_profile, params=manuscript_params,
                                       run={"n_sites": len(labels), "window": [start, end], "decisions": dec, "bootstraps": n_bootstraps,
                                            "random_state": random_state, "preset": preset, "environment": run_environment(),
                                            "cohort_params": c_params, "qualifying_stays": n_qualifying,
                                            **({"random_draw_redrawn": redrawn_total} if pinned is not None else {}), "bmi_fill": bmi_fill_used,
                                            **({"sensitivity": sens_summary} if sens_summary else {})},
                                       summary=res.get("summary"))
        staged = [d["qmd"], *([d["bib"]] if d.get("bib") else []), *d["files"].values()]
        doc_record.update({"renderer": d["renderer"], "files": [p.name for p in staged], "missing": d["missing"], "note": d["note"],
                           "cache": d["cache"], "cites_omop_duck_db": d.get("software"), "profile": d.get("profile"),
                           "template": d.get("template"), "text_missing": d.get("text_missing", [])})
        timings["document"] = round(time.time() - t_doc, 1)
    privacy: dict[str, Any] = {"checked": False}
    hits: list = []
    if privacy_check:
        from omop_etl.run_privacy import scan, scan_files

        names = _privacy_names(sites, labels, site_names)
        if names:
            hits, skipped = scan(out, names)
            s_hits, s_skipped = scan_files(staged, names)
            hits += s_hits
            skipped += s_skipped
            # a document that could not be read is not called clean; images carry no searchable text here
            unread = [x for x in skipped if not x.split(" (")[0].lower().endswith((".png", ".jpg", ".jpeg", ".svg"))]
            privacy = {"checked": True, "n_names": len(names), "hits": len(hits), "not_scanned": skipped,
                       "hit_files": sorted({h[0] for h in hits}), "unreadable": unread}
            if unread and not hits:
                hits = [(x, 0, "not readable, so not shown to be clean") for x in unread]
    if staged and not hits:
        for p_ in staged:
            shutil.move(str(p_), out / p_.name)
        files += [str(out / p_.name) for p_ in staged]
    elif staged:  # the record must not say the manuscript was written when it was held back
        doc_record.update({"withheld": True, "files": [], "rendered": doc_record.get("files", []), "missing": list(document_formats),
                           "note": "; ".join(x for x in [doc_record.get("note"), "withheld: the privacy check found a site name; nothing was written to the output folder"] if x)})
    shutil.rmtree(stage, ignore_errors=True)
    timings["total"] = round(time.time() - t_start, 1)
    label_mode = ("explicit" if isinstance(site_labels_arg, Mapping) else "file" if isinstance(site_labels_arg, (str, Path)) and
                  str(site_labels_arg) not in ("random", "ordered") else str(site_labels_arg or "ordered"))
    (out / f"{prefix}_run.json").write_text(json.dumps({"n_sites": len(labels), "labels": sorted(labels.values()), "window": [start, end], "profile": profile, "preset": preset, "decisions": dec, "pinned": pinned is not None,
                                                     **({"random_draw_redrawn": redrawn_total} if pinned is not None else {}),
                                                       "bmi_fill": bmi_fill_used,
                                                       **({"sensitivity": sens_summary} if sens_summary else {}),
                                                       **({"observation_period_cap": {"cap": observation_period_cap, "by_site": dict(sorted(op_capped.items()))}}
                                                          if observation_period_cap is not None else {}),
                                                       "unit_converted": dict(sorted(unit_converted.items())),
                                                       "medication_combination_share": dict(sorted(combination_share.items())),
                                                       "hemoglobin_from_hematocrit": hgb_from_hct,
                                                       "n": int(len(pooled)), "qualifying_stays": n_qualifying, "bootstraps": n_bootstraps, "random_state": random_state, "timings_seconds": timings,
                                                       "cohort_params": json.loads(json.dumps(c_params, default=str)),
                                                       "address_table": {"requested": address_table, "used": dict(sorted(address_used.items()))},
                                                       "sdoh": {"on": ruca_crosswalk is not None and sdi_table is not None, "sdi_cut_points": list(sdi_cut_points or ())},
                                                       "transfer_source_values": list(transfer_source_values), "site_labels": label_mode,
                                                       "concept_sets": concept_sets,
                                                       "inputs": {k: v for k, v in {"ruca_crosswalk": _file_fingerprint(ruca_crosswalk), "sdi_table": _file_fingerprint(sdi_table),
                                                                                     "pinned_stays": _file_fingerprint(pinned_stays), "compare_to": _file_fingerprint(compare_to)}.items() if v},
                                                       "data_checks": {"threshold": data_gap_threshold, "allowed": allow, "findings": findings,
                                                                       "share_missing_by_group": dict(sorted(coverage.items())),
                                                                       "geography": dict(sorted(geography.items())),
                                                                       "n_errors_allowed": sum(1 for x in findings if x.get("severity") == "error"),
                                                                       "n_warnings": sum(1 for x in findings if x.get("severity") == "warning"),
                                                                       "by_cause": {c: sum(1 for x in findings if x.get("cause") == c) for c in GAP_CAUSES},
                                                                       "n_unexpected": sum(1 for x in findings if not x.get("expected")),
                                                                       "known_source_gaps": len(known_gaps)},
                                                       "document": doc_record, "privacy_check": privacy, "environment": run_environment()}, indent=1, default=str), encoding="utf-8")
    # what only the operating site may see: the --sites order and the command line (with real names they reveal the labels)
    (priv / f"{prefix}_run_private.json").write_text(json.dumps({"sites": list(sites), "label_seed": label_seed, "address_table_by_site":
                                                                {s_: address_used.get(labels[s_]) for s_ in sites}, "run_args": dict(run_args or {})},
                                                               indent=1, default=str), encoding="utf-8")
    if hits:
        where = "; ".join(sorted({f"{h[0]} ({h[2]})" for h in hits})[:20])
        raise RunPrivacyError(f"{len(hits)} privacy finding(s) in the study outputs, reported by file only: {where}. "
                              "The manuscript DOCX/PDF were not written to the output folder. Do not share the folder until this is fixed.")
    return {"data": pooled, "tables": res["tables"], "workbook": wb["path"], "files": files, "comparison": comparison, "summary": res["summary"], "timings": timings,
            "document": doc_record, "privacy_check": privacy, "private_dir": priv, "data_checks": findings, "address_table": address_used}
