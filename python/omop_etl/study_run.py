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
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = ["assign_site_labels", "load_site_labels", "save_site_labels", "validate_site_labels", "RunPrivacyError", "run_environment", "run_study", "PROTOCOL_COHORT_PARAMS", "pin_stays", "STUDY_DECISIONS", "STUDY_PRESETS", "resolve_decisions"]

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
    """One site's pooled-ready frame, its attrition and ``meta`` (feature groups, skipped features, the address source used,
    pinned-list coverage). Data gaps it can see are raised as :class:`omop_etl.data_gaps.DataGapWarning` for run_study to judge."""
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
        _check_coverage_of_window(con, start, end, params)
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
            listed = int(from_site["visit_occurrence_id"].nunique()) if "visit_occurrence_id" in from_site else 0
            meta["pinned_found_share"] = round(len(cohort) / listed, 4) if listed else None
            if not listed:
                warnings.warn(DataGapWarning("the pinned stay list has no stay for this site", "pinned"), stacklevel=2)
            elif len(cohort) < listed:
                warnings.warn(DataGapWarning(f"{1 - len(cohort) / listed:.2%} of the pinned stays for this site are not in the cohort "
                                             "(different cohort definition, window or data cut); they were dropped", "pinned"), stacklevel=2)
            att = pd.concat([att, pd.DataFrame([{"step_name": "Stay selected for the patient (pinned list)", "subjects_retained": len(cohort), "site": site}])], ignore_index=True)
        path = work / f"cohort_{site}.parquet"
        cohort.to_parquet(path)
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
    return df, att, meta


def _check_coverage_of_window(con: Any, start: str, end: str, params: Mapping[str, Any], slack_days: int = 90) -> None:
    """Gap warnings when a site's stays do not cover the study window, or its observation periods pile up on the build date. Reports
    day counts and shares, never dates (a site's data end could identify it)."""
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

    from omop_etl.data_gaps import DataGapWarning, mask_counts, sanitize

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for w in caught:
        msg = sanitize(str(w.message), scrub)
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
              age_method: str | None = None,
              sheet_site_labels: Mapping[str, Mapping[str, str]] | None = None, preset: str = "package",
              private_dir: str | Path | None = None, document: bool = True, document_formats: Sequence[str] = ("docx", "pdf"),
              document_title: str | None = None, document_author: str = "", site_names: Sequence[str] | str | Path | None = None,
              privacy_check: bool = True, document_cache_dir: str | Path | None = None, allow_data_gaps: Sequence[str] = (),
              data_gap_threshold: float = 0.95, run_args: Mapping[str, Any] | None = None, check_only: bool = False) -> dict[str, Any]:
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
    formats ``document_formats``; without Quarto the run record says what was not rendered; ``document_cache_dir``: a writable folder for
    Quarto's cache in a sandbox, see :func:`omop_etl.manuscript_document.renderer_env`). ``privacy_check=True`` (default) then scans
    every file in ``out_dir`` (text, workbook, DOCX and PDF text and metadata, :mod:`omop_etl.run_privacy`) for ``site_names`` (a list or
    a names file, one per line) and the site keys; a hit raises :class:`RunPrivacyError` after the run record is written, and the
    manuscript files are not moved into ``out_dir``.
    ``address_tie_break``: see :func:`omop_etl.geography.derive_geography_features`. ``preset`` names a set of the decisions above
    (:data:`STUDY_PRESETS`: ``"package"`` or ``"delivered_2026_11_11"``); any of ``attrition_mode``, ``table5``, ``sdi_missing_term``,
    ``address_tie_break``, ``address_undated``, ``table6_increments``, ``consort_final_steps``, ``age_method`` given (not None) overrides the preset. :data:`STUDY_DECISIONS` lists the options. The result has ``timings`` (seconds per stage).
    **Data gaps fail the run.** Each site is checked as soon as it is built (:mod:`omop_etl.data_gaps`): no usable address table,
    a feature group missing (or all zero) for at least ``data_gap_threshold`` of the stays (SDI / RUCA included), a group that
    could not be computed, no reachable vocabulary, pinned stays not found, a site with no stays. Any of these raises
    :class:`omop_etl.data_gaps.DataGapError` (after writing ``<prefix>_data_gaps.json``) unless its check or group is named in
    ``allow_data_gaps`` (``"all"`` allows every one). Every finding, allowed or a plain warning, goes into ``<prefix>_run.json``
    with the address table used per site. ``check_only=True`` builds every site, collects every finding (no fail-fast), writes
    ``<prefix>_data_gaps.json`` and stops before any modelling: it raises ``DataGapError`` listing every gap of every site, or returns
    ``{"data_checks": [...], ...}`` when there is none. ``run_args`` (the command line) and the ``sites`` order are written only to
    ``<prefix>_run_private.json`` in ``private_dir``: together with real names they would reveal which label is which site."""
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

    from omop_etl.data_gaps import DataGapError, assess, group_missing_shares, sanitize

    allow = [str(x) for x in (allow_data_gaps or ())]
    assess([], allow)  # unknown names are refused before any site is built
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
    from omop_etl.concept_sets import concept_set_fingerprint

    concept_sets = concept_set_fingerprint()
    settings = json.loads(json.dumps({"window": [start, end], "profile": profile, "concept_sets": concept_sets["sha256"], "cohort_params": c_params, "attrition_mode": attrition_mode,
                                      "address_table": address_table, "address_tie_break": address_tie_break, "address_undated": undated,
                                      "ruca": (_file_fingerprint(ruca_crosswalk) or {}).get("sha256", None if ruca_crosswalk is None else "in memory"),
                                      "sdi": (_file_fingerprint(sdi_table) or {}).get("sha256", None if sdi_table is None else "in memory"),
                                      "sdi_cut_points": list(sdi_cut_points or ()), "transfer_source_values": list(transfer_source_values),
                                      "pinned": None if pinned is None else pd.util.hash_pandas_object(pinned, index=False).sum().item(),
                                      "feature_kwargs": feature_kwargs or {}}, default=str, sort_keys=True))

    def _write_gaps() -> None:
        (out / f"{prefix}_data_gaps.json").write_text(json.dumps({"threshold": data_gap_threshold, "allowed": allow, "findings": findings,
                                                                "share_missing_by_group": coverage, "geography": geography},
                                                               indent=1, default=str), encoding="utf-8")

    def _gate(new: list[dict[str, Any]]) -> None:
        findings.extend(new)
        try:
            assess(new, allow)
        except DataGapError:
            if not check_only:  # check_only: keep going, every site's gaps are reported together at the end
                _write_gaps()
                raise

    if compare_to is not None and preset == "package":
        findings.append({"site": "-", "check": "preset", "severity": "warning",
                         "message": "--compare-to with the default preset 'package': a workbook delivered under another preset (such as "
                                    "delivered_2026_11_11) differs in its decisions too, so not every difference is a data difference"})

    if pinned is not None and "site_source" in pinned:
        stray = sorted(set(pinned["site_source"].dropna().astype(str)) - set(map(str, sites)))
        if stray:
            _gate([{"site": "-", "check": "pinned", "severity": "error",
                    "message": f"the pinned stay list names {len(stray)} site(s) that are not in --sites; their stays are not used"}])

    t_start = time.time()
    timings: dict[str, float] = {}
    frames, atts = [], []
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
            elif meta["settings"] != settings:
                diff = sorted(k for k in set(settings) | set(meta["settings"]) if settings.get(k) != meta["settings"].get(k))
                saved.append({"site": labels[site], "check": "stale_features", "severity": "error",
                              "message": f"reused features were built with different settings ({', '.join(diff)}); rerun without --reuse-features"})
            address_used[labels[site]] = sanitize(str(meta.get("address_table")), scrub)
            coverage[labels[site]] = group_missing_shares(df, meta.get("feature_groups"))
            if meta.get("geography"):
                geography[labels[site]] = {k: v for k, v in meta["geography"].items() if k != "address_table"}
            _gate(_with_coverage(saved, df, meta, labels[site], data_gap_threshold))
            frames.append(df)
            atts.append(pd.read_csv(a))
            continue
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            df, att, meta = _site_frame(db, site, start=start, end=end, params=c_params, profile=profile, pinned=pinned,
                                        ruca=ruca_crosswalk, sdi=sdi_table, cuts=sdi_cut_points, address_table=address_table,
                                        transfer_source_values=transfer_source_values, feature_kwargs={"address_tie_break": address_tie_break, "address_undated": undated, **dict(feature_kwargs or {})}, work=priv,
                                        attrition_mode=attrition_mode)
        df["site_anon"] = labels.get(site, site)
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
        frames.append(df)
        atts.append(att)
        timings[f"site_{labels.get(site, site)}"] = round(time.time() - t_site, 1)  # keyed by the anonymous letter: the run record is shareable and must not name sites
    if check_only:
        _write_gaps()
        assess(findings, allow)  # raises with every site's gaps at once
        return {"data_checks": findings, "address_table": address_used, "share_missing_by_group": coverage, "geography": geography,
                "private_dir": priv, "checked_only": True}
    pooled = _canonical_order(frames)
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
                                       author=document_author, formats=document_formats, min_cell=min_cell, cache_dir=document_cache_dir)
        staged = [d["qmd"], *d["files"].values()]
        doc_record.update({"renderer": d["renderer"], "files": [p.name for p in staged], "missing": d["missing"], "note": d["note"],
                           "cache": d["cache"]})
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
                                                       "n": int(len(pooled)), "bootstraps": n_bootstraps, "random_state": random_state, "timings_seconds": timings,
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
                                                                       "n_warnings": sum(1 for x in findings if x.get("severity") == "warning")},
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
