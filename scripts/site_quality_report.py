#!/usr/bin/env python3
"""Per-site data-quality report of a study run: every model feature and every Table 1 category, site by site, plus the cohort
attrition, the outcome mix and (with ``--db-dir``) the unmapped source rows and out-of-range measurements of each site database.

It answers "does one site differ from the others?" for each study input (Dr. Rubin, 2026-11-23: discharge status 44% missing
against 10% in the legacy table; Kyle's per-site DQD list). Read-only: it reads the run's private per-site files and, with
``--db-dir``, runs a few GROUP BY queries per site database.

Two outputs, as in ``check_discharge_status.py``:

* ``--detail`` (Markdown, keep it private): one table per domain with a column per site, under the sites' anonymous labels.
* ``--flags`` (JSON, shareable): the measures where the sites differ by at least ``--pct-gap`` percentage points (shares and
  missingness) or whose largest site mean is at least ``--ratio`` times the smallest (continuous measures), with the range across
  sites only: no site label, no count.

Example:
    python scripts/site_quality_report.py --private-dir <PRIVATE>/run_C8_private --labels <PRIVATE>/labels.json \\
        --db-dir D:/omop --start 2017-01-01 --end 2022-09-30 \\
        --detail <PRIVATE>/dq/site_quality.md --flags <PRIVATE>/dq/site_quality_flags.json --max-seconds 1200
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

MIN_CELL = 11
#: measures the report groups by domain (column-name patterns of the benchmark features)
DOMAINS = [("Outcome and stay mix", r"^(readmitted_30d|index_los_days|prior_ip_count)$"),
           ("Prior utilization", r"^(prior_|days_since_|readmission_count|n_prior)"),
           ("Diagnoses and comorbidities", r"^(elix_|charlson_|n_secondary_dx|dx_|ccs_)"),
           ("Medications", r"^med_"),
           ("Laboratories", r"^lab_"),
           ("Vital signs", r"^vital_"),
           ("Discharge status", r"^disp_"),
           ("Demographics and social", r"^(age_at_index|female|race_|ethnicity_|hispanic|sdi_|ruca_|tobacco|sdoh_)")]


def site_frames(private_dir: Path, labels: Mapping[str, str]) -> dict[str, pd.DataFrame]:
    """``features_<site>.parquet`` of each labelled site, keyed by the site's anonymous label (sites without a label are skipped)."""
    out = {}
    for f in sorted(private_dir.glob("features_*.parquet")):
        site = f.stem[len("features_"):]
        if site in labels:
            out[labels[site]] = pd.read_parquet(f)
    return dict(sorted(out.items()))


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce") if not pd.api.types.is_bool_dtype(s) else s.astype(float)


def feature_rows(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per feature column and site: % missing; for a 0/1 column the % with 1; otherwise mean, SD, median, Q1, Q3."""
    cols = sorted({c for d in frames.values() for c in d.columns if not str(c).endswith(("_id", "_date", "_datetime")) and c not in ("site_source",)})
    rows = []
    for c in cols:
        for site, d in frames.items():
            if c not in d:
                rows.append({"feature": c, "site": site, "present": False})
                continue
            raw = d[c]
            if not (pd.api.types.is_numeric_dtype(raw) or pd.api.types.is_bool_dtype(raw)):
                # a text column (ruca_category, tobacco, site_anon, ...): % missing only, never coerced to numbers (C10 printed
                # every text column as 100% missing)
                n = len(raw)
                miss = raw.isna() | raw.astype(str).str.strip().isin(["", "nan", "None"])
                rows.append({"feature": c, "site": site, "present": True, "n": n, "pct_missing": round(100 * float(miss.mean()), 2) if n else None})
                continue
            v = _num(raw)
            n = len(v)
            nn = v.dropna()
            r: dict[str, Any] = {"feature": c, "site": site, "present": True, "n": n, "pct_missing": round(100 * (1 - len(nn) / n), 2) if n else None}
            if len(nn) and set(pd.unique(nn)) <= {0, 1}:
                r["pct_yes"] = round(100 * float(nn.mean()), 2)
            elif len(nn):
                r.update(mean=float(nn.mean()), sd=float(nn.std()) if len(nn) > 1 else None, median=float(nn.median()),
                         q1=float(nn.quantile(.25)), q3=float(nn.quantile(.75)))
            rows.append(r)
    return pd.DataFrame(rows)


def category_rows(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """The Table 1 categories (age, sex, race, ethnicity, SDI, RUCA, discharge status, tobacco, BMI) as % of each site's stays."""
    from omop_etl.manuscript_tables import manuscript_frame

    rows = []
    for site, d in frames.items():
        m = manuscript_frame(d)
        for col, name in (("m_age", "Age"), ("m_sex", "Sex"), ("m_race", "Race"), ("m_hispanic", "Hispanic"), ("m_sdi", "SDI"),
                          ("m_ruca", "RUCA"), ("m_disch", "Discharge Status"), ("m_tobacco", "Tobacco"), ("m_bmi", "BMI")):
            if col not in m:
                continue
            vc = m[col].astype(str).value_counts()
            for lv, k in vc.items():
                rows.append({"measure": f"{name}: {lv}" if not lv.startswith(name) else lv, "site": site, "n": int(k),
                             "pct": round(100 * k / len(m), 2)})
    t = pd.DataFrame(rows)
    if t.empty:
        return t
    # a category a site never uses is 0% there (one site with 100% "Missing / Unknown" must show as 0-100, not 100-100)
    full = pd.MultiIndex.from_product([t["measure"].unique(), list(frames)], names=["measure", "site"])
    return t.set_index(["measure", "site"]).reindex(full).fillna({"n": 0, "pct": 0.0}).reset_index()


def attrition_rows(private_dir: Path, labels: Mapping[str, str]) -> pd.DataFrame:
    rows = []
    for f in sorted(private_dir.glob("attrition_*.csv")):
        site = f.stem[len("attrition_"):]
        if site not in labels:
            continue
        a = pd.read_csv(f)
        kept = pd.to_numeric(a["subjects_retained"], errors="coerce")
        prev = kept.shift(1)
        for name, k, p in zip(a["step_name"], kept, prev):
            rows.append({"step": str(name), "site": labels[site], "retained": k,
                         "pct_excluded": round(100 * (p - k) / p, 2) if pd.notna(p) and p else None})
    return pd.DataFrame(rows)


def db_rows(db_dir: Path, sites: Mapping[str, str], start: str, end: str, deadline: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per site: rows with concept 0 in measurement, drug_exposure and condition_occurrence (dated in the window), with the 10 most
    frequent unmapped source values; and the share of measurement values outside the bundled physiologic limits per concept."""
    import duckdb

    from omop_etl.dqd import _sanitize_bundled_limits

    limits = pd.DataFrame(_sanitize_bundled_limits(), columns=["concept_id", "unit_concept_id", "lo", "hi"])
    unmapped, bounds = [], []
    for site, label in sites.items():
        if time.time() > deadline:
            print(f"time limit reached before {label}; stopping the database part", file=sys.stderr)
            break
        con = duckdb.connect(str(db_dir / f"omop_{site}.duckdb"), read_only=True)
        try:
            for table, cid, src, date in (("measurement", "measurement_concept_id", "measurement_source_value", "measurement_date"),
                                          ("drug_exposure", "drug_concept_id", "drug_source_value", "drug_exposure_start_date"),
                                          ("condition_occurrence", "condition_concept_id", "condition_source_value", "condition_start_date")):
                tot, zero = con.execute(f"SELECT count(*), count(*) FILTER (WHERE coalesce({cid}, 0) = 0) FROM {table} "
                                        f"WHERE {date} BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)", [start, end]).fetchone()
                top = con.execute(f"SELECT {src} AS v, count(*) AS n FROM {table} WHERE coalesce({cid}, 0) = 0 AND {date} "
                                  f"BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) GROUP BY 1 ORDER BY 2 DESC LIMIT 10", [start, end]).df()
                unmapped.append({"site": label, "table": table, "rows": int(tot), "pct_concept_0": round(100 * zero / tot, 2) if tot else None,
                                 "top_unmapped_source_values": "; ".join(f"{v} ({n:,})" for v, n in zip(top["v"], top["n"]))})
            con.register("_limits", limits)
            b = con.execute("""SELECT m.measurement_concept_id AS concept_id, count(*) AS n,
                                      count(*) FILTER (WHERE m.value_as_number < l.lo OR m.value_as_number > l.hi) AS out_of_range
                               FROM measurement m JOIN _limits l ON m.measurement_concept_id = l.concept_id
                                    AND (l.unit_concept_id IS NULL OR coalesce(m.unit_concept_id, 0) = l.unit_concept_id)
                               WHERE m.value_as_number IS NOT NULL AND m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                               GROUP BY 1""", [start, end]).df()
            b["site"] = label
            b["pct_out_of_range"] = (100 * b["out_of_range"] / b["n"]).round(2)
            bounds.append(b)
        finally:
            con.close()
    return pd.DataFrame(unmapped), (pd.concat(bounds, ignore_index=True) if bounds else pd.DataFrame())


def lab_funnel(db_dir: Path, sites: Mapping[str, str], start: str, end: str, deadline: float) -> pd.DataFrame:
    """Per site and lab: where the lab's rows are lost on the way to the feature. Rows whose source LOINC is in the lab's concept set
    (dated in the window, or undated), then the % of them mapped to one of the set's concepts, the % with no date, the % with no
    number, the % outside the feature's physiologic bounds (after the WBC rescale) and the three most frequent units. A lab that is
    mostly missing at one site while its LOINC is in the raw file shows here which step drops it (0.34.0, per-site check of Nov 23)."""
    import duckdb

    from omop_etl.benchmark_features import (BENCHMARK_LAB_CONCEPT_SETS, BENCHMARK_LAB_LOINCS, DEFAULT_BOUNDS, DEFAULT_RESCALE,
                                             DEFAULT_UNIT_CONVERSIONS, _UNIT_NORM_SQL, _bounds_frame, _concept_map, _conversion_frame,
                                             _ensure_vocabulary)

    src = pd.DataFrame([(n, str(c)) for n, cs in BENCHMARK_LAB_LOINCS.items() for c in cs], columns=["name", "loinc"])
    out = []
    for site, label in sites.items():
        if time.time() > deadline:
            print(f"time limit reached before {label}; stopping the lab funnel", file=sys.stderr)
            break
        con = duckdb.connect(str(db_dir / f"omop_{site}.duckdb"), read_only=True)
        try:
            # as feature extraction does: a site file whose concept tables are views on a central vocabulary file needs that file
            # attached, or only the set items that carry a CONCEPT_ID resolve (troponin I and pCO2 showed 0% mapped, C10 Nov 23)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                _ensure_vocabulary(con, "auto")
            cmap = _bounds_frame(_concept_map(con, None, None, BENCHMARK_LAB_CONCEPT_SETS), DEFAULT_BOUNDS, DEFAULT_RESCALE)
            con.register("_lf_src", src)
            con.register("_lf_map", cmap)
            con.register("_lf_conv", _conversion_frame(DEFAULT_UNIT_CONVERSIONS))
            mcols = {r[0].lower() for r in con.execute("DESCRIBE measurement").fetchall()}
            uid = "m.unit_concept_id" if "unit_concept_id" in mcols else "CAST(NULL AS INTEGER)"
            utxt = _UNIT_NORM_SQL if "unit_source_value" in mcols else "CAST(NULL AS VARCHAR)"
            # the bounds are checked after the package's SI-unit conversions, as feature extraction does (review of PR 107)
            f = con.execute(f"""
                WITH r AS (
                    SELECT s.name, m.measurement_concept_id AS cid, m.measurement_date AS d, m.value_as_number * coalesce(cv.factor, 1.0) AS x,
                           m.unit_source_value AS u
                    FROM measurement m JOIN _lf_src s ON m.measurement_source_value = s.loinc
                    LEFT JOIN _lf_conv cv ON cv.name = s.name AND (cv.unit_concept_id = {uid}
                         OR (coalesce({uid}, 0) = 0 AND cv.unit_text = {utxt}))
                    WHERE m.measurement_date IS NULL OR m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)),
                b AS (SELECT DISTINCT name, lo, hi, rs_lo, rs_hi, rs_div FROM _lf_map),
                k AS (SELECT DISTINCT name, concept_id FROM _lf_map)
                SELECT r.name, count(*) AS source_rows,
                       round(100.0 * count(k.concept_id) / count(*), 1) AS pct_mapped,
                       round(100.0 * count(*) FILTER (WHERE r.d IS NULL) / count(*), 1) AS pct_no_date,
                       round(100.0 * count(*) FILTER (WHERE r.x IS NULL) / count(*), 1) AS pct_no_number,
                       round(100.0 * count(*) FILTER (WHERE r.x IS NOT NULL AND NOT (
                           CASE WHEN r.x > b.rs_lo AND r.x <= b.rs_hi THEN r.x / b.rs_div ELSE r.x END BETWEEN b.lo AND b.hi))
                             / count(*), 1) AS pct_out_of_bounds
                FROM r LEFT JOIN k ON k.name = r.name AND k.concept_id = r.cid LEFT JOIN b ON b.name = r.name
                GROUP BY r.name""", [start, end]).df()
            units = con.execute("""
                SELECT name, string_agg(u || ' ' || pct || '%', '; ' ORDER BY n DESC) AS top_units FROM (
                    SELECT s.name, coalesce(m.unit_source_value, '(none)') AS u, count(*) AS n,
                           CAST(round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY s.name)) AS INTEGER) AS pct,
                           row_number() OVER (PARTITION BY s.name ORDER BY count(*) DESC) AS k
                    FROM measurement m JOIN _lf_src s ON m.measurement_source_value = s.loinc
                    WHERE m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) GROUP BY 1, 2) WHERE k <= 3
                GROUP BY name""", [start, end]).df()
            # rows the vocabulary puts under the lab's concepts whatever their source text (one site stores lab names, not LOINC, in
            # measurement_source_value), and the concepts the unmapped source rows carry instead (C10 prep, Nov 23)
            mapped = con.execute("""
                SELECT k.name, count(*) AS concept_rows FROM measurement m JOIN (SELECT DISTINCT name, concept_id FROM _lf_map) k
                  ON k.concept_id = m.measurement_concept_id
                WHERE m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) GROUP BY 1""", [start, end]).df()
            other = con.execute("""
                SELECT name, string_agg(CAST(cid AS VARCHAR) || ' ' || pct || '%', '; ' ORDER BY n DESC) AS unmapped_concepts FROM (
                    SELECT s.name, m.measurement_concept_id AS cid, count(*) AS n,
                           CAST(round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY s.name)) AS INTEGER) AS pct,
                           row_number() OVER (PARTITION BY s.name ORDER BY count(*) DESC) AS k
                    FROM measurement m JOIN _lf_src s ON m.measurement_source_value = s.loinc
                    WHERE (m.measurement_date IS NULL OR m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE))
                      AND NOT EXISTS (SELECT 1 FROM _lf_map x WHERE x.name = s.name AND x.concept_id = m.measurement_concept_id)
                    GROUP BY 1, 2) WHERE k <= 3
                GROUP BY name""", [start, end]).df()
            f = f.merge(units, on="name", how="outer").merge(mapped, on="name", how="outer").merge(other, on="name", how="left")
            # every lab gets a row at every site, so a site with no rows for a lab shows 0 instead of dropping out of the tables
            f = pd.DataFrame({"name": sorted(BENCHMARK_LAB_LOINCS)}).merge(f, on="name", how="left")
            f["source_rows"] = f["source_rows"].fillna(0).astype(int)
            f["concept_rows"] = f["concept_rows"].fillna(0).astype(int)
            f["site"] = label
            out.append(f)
        finally:
            con.close()
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def readmission_overlap(private_dir: Path, db_dir: Path, sites: Mapping[str, str], deadline: float,
                        visit_types: tuple[int, ...] = (9201, 262)) -> pd.DataFrame:
    """Per site: index stays with another inpatient stay that starts on their discharge day (``day 0``) or inside them
    (``overlapping``). The readmission rule (start after the discharge day) counts neither, as the legacy rule did; the pre-C16
    audit found them at 1 to 15% of the counted readmissions per site (issue 106). Shares only: % of index stays and % of the counted
    readmissions. ``define_study_cohort(merge_episodes_days=0)`` is the sensitivity analysis that merges them into one episode."""
    import duckdb

    types = ", ".join(str(int(t)) for t in visit_types)
    out = []
    for site, label in sites.items():
        f = private_dir / f"cohort_{site}.parquet"
        if not f.exists():
            continue
        if time.time() > deadline:
            print(f"time limit reached before {label}; stopping the readmission overlap check", file=sys.stderr)
            break
        con = duckdb.connect(str(db_dir / f"omop_{site}.duckdb"), read_only=True)
        try:
            c = pd.read_parquet(f, columns=["subject_id", "visit_occurrence_id", "cohort_start_date", "cohort_end_date", "outcome_flag"])
            con.register("_ro_c", c)
            r = con.execute(f"""
                WITH k AS (
                    SELECT c.visit_occurrence_id, max(CAST(c.outcome_flag AS INTEGER)) AS y,
                           bool_or(CAST(v.visit_start_date AS DATE) = CAST(c.cohort_end_date AS DATE)) AS day0,
                           bool_or(CAST(v.visit_start_date AS DATE) >= CAST(c.cohort_start_date AS DATE)
                                   AND CAST(v.visit_start_date AS DATE) < CAST(c.cohort_end_date AS DATE)) AS overlap
                    FROM _ro_c c LEFT JOIN visit_occurrence v ON v.person_id = c.subject_id AND v.visit_occurrence_id <> c.visit_occurrence_id
                         AND v.visit_concept_id IN ({types})
                    GROUP BY c.visit_occurrence_id)
                SELECT count(*), sum(y), count(*) FILTER (WHERE day0), count(*) FILTER (WHERE overlap),
                       count(*) FILTER (WHERE day0 OR overlap), count(*) FILTER (WHERE (day0 OR overlap) AND y = 0)
                FROM k""").fetchone()
            n, y, d0, ov, either, uncounted = (int(x or 0) for x in r)
            out.append({"site": label, "pct_stays_day0": round(100 * d0 / n, 2) if n else None,
                        "pct_stays_overlapping": round(100 * ov / n, 2) if n else None,
                        "pct_of_readmissions": round(100 * either / y, 2) if y else None,
                        "pct_stays_not_counted": round(100 * uncounted / n, 2) if n else None})
        finally:
            con.close()
    return pd.DataFrame(out)


def unit_medians(db_dir: Path, sites: Mapping[str, str], start: str, end: str, deadline: float, factor: float = 5.0,
                 min_share: float = 0.01) -> pd.DataFrame:
    """Per site and lab: the median value of each unit (``unit_source_value``) that holds at least ``min_share`` of the lab's rows (and
    at least 11 rows), after the package's UCUM conversions (``DEFAULT_UNIT_CONVERSIONS``), and ``ratio`` = largest / smallest
    median. A ratio of ``factor`` or more means a unit the package does not convert, or a mislabelled unit (issue 106)."""
    import duckdb

    from omop_etl.benchmark_features import (BENCHMARK_LAB_CONCEPT_SETS, DEFAULT_UNIT_CONVERSIONS, _UNIT_NORM_SQL, _concept_map,
                                             _conversion_frame, _ensure_vocabulary)

    conv = _conversion_frame(DEFAULT_UNIT_CONVERSIONS)
    out = []
    for site, label in sites.items():
        if time.time() > deadline:
            print(f"time limit reached before {label}; stopping the unit check", file=sys.stderr)
            break
        con = duckdb.connect(str(db_dir / f"omop_{site}.duckdb"), read_only=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                _ensure_vocabulary(con, "auto")
            con.register("_um_map", _concept_map(con, None, None, BENCHMARK_LAB_CONCEPT_SETS))
            con.register("_um_conv", conv)
            u = con.execute(f"""
                WITH r AS (
                    SELECT k.name, coalesce(nullif(trim(m.unit_source_value), ''), '(none)') AS unit,
                           m.value_as_number * coalesce(cv.factor, 1.0) AS x
                    FROM measurement m JOIN (SELECT DISTINCT name, concept_id FROM _um_map) k ON k.concept_id = m.measurement_concept_id
                    LEFT JOIN _um_conv cv ON cv.name = k.name AND (cv.unit_concept_id = m.unit_concept_id
                         OR (coalesce(m.unit_concept_id, 0) = 0 AND cv.unit_text = {_UNIT_NORM_SQL}))
                    WHERE m.value_as_number IS NOT NULL AND m.measurement_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE))
                SELECT name, unit, count(*) AS n, median(x) AS med FROM r GROUP BY 1, 2""", [start, end]).df()
            for name, g in u.groupby("name"):
                g = g[(g["n"] >= 11) & (g["n"] >= min_share * g["n"].sum())]
                pos = g[g["med"] > 0]
                ratio = float(pos["med"].max() / pos["med"].min()) if len(pos) >= 2 else None
                out.append({"site": label, "name": name, "units": "; ".join(f"{r.unit} {r.med:.3g} ({100 * r.n / g['n'].sum():.0f}%)"
                                                                            for r in g.sort_values("n", ascending=False).itertuples()),
                            "ratio": round(ratio, 2) if ratio else None, "flag": bool(ratio and ratio >= factor)})
        finally:
            con.close()
    return pd.DataFrame(out)


def funnel_causes(funnel: pd.DataFrame, known: Mapping[str, str], site_keys: Mapping[str, str]) -> pd.Series:
    """Why a lab is lost at a site, from its funnel row: a gap the study team confirmed (``--known-source-gaps``), no row with the lab's
    codes at all (likely a source gap), rows that exist but are not mapped to the lab's concepts (an ETL mapping gap), or rows with no
    number (a source gap, or a raw result the ETL did not read)."""
    from omop_etl.data_gaps import _known_match

    out = []
    for r in funnel.itertuples():
        f = {"site": r.site, "column": f"lab_{r.name}", "check": "column_missing", "group": "labs"}
        if known and _known_match(f, known, site_keys) is not None:
            out.append("source gap (confirmed)")
        elif not r.source_rows and not r.concept_rows:
            out.append("no rows with its codes (likely a source gap)")
        elif r.source_rows and (pd.isna(r.pct_mapped) or r.pct_mapped < 50) and r.concept_rows < r.source_rows:
            out.append("ETL mapping gap (rows not mapped to the lab's concepts)")
        elif pd.notna(r.pct_no_number) and r.pct_no_number >= 50:
            out.append("no numeric value (source gap, or a raw result the ETL did not read)")
        else:
            out.append("")
    return pd.Series(out, index=funnel.index, dtype=object)


def flags(feat: pd.DataFrame, cats: pd.DataFrame, att: pd.DataFrame, unm: pd.DataFrame, bnd: pd.DataFrame,
          pct_gap: float, ratio: float, funnel: pd.DataFrame | None = None, overlap: pd.DataFrame | None = None,
          units: pd.DataFrame | None = None, known: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Measures where the sites differ: ranges only, no site label or count. A measure the study team listed as a confirmed source
    gap (``known``) is marked ``expected``."""
    out: list[dict[str, Any]] = []

    def spread(kind: str, name: str, vals: pd.Series, unit: str = "%") -> None:
        v = pd.to_numeric(vals, errors="coerce").dropna()
        if len(v) < 2:
            return
        lo, hi = float(v.min()), float(v.max())
        if unit == "%" and hi - lo >= pct_gap:
            out.append({"domain": kind, "measure": name, "min": round(lo, 1), "max": round(hi, 1), "unit": "%"})
        elif unit == "mean" and lo > 0 and hi / lo >= ratio:
            out.append({"domain": kind, "measure": name, "min": round(lo, 2), "max": round(hi, 2), "unit": "site mean"})

    if len(feat):
        for c, g in feat[feat["present"]].groupby("feature"):
            dom = next((d for d, pat in DOMAINS if re.search(pat, c)), "Other")
            spread(dom, f"{c} % missing", g["pct_missing"])
            if "pct_yes" in g and g["pct_yes"].notna().any():
                spread(dom, f"{c} % yes", g["pct_yes"])
            if "mean" in g and g["mean"].notna().any():
                spread(dom, f"{c} mean", g["mean"], "mean")
            if (~feat.loc[feat["feature"] == c, "present"]).any():
                out.append({"domain": dom, "measure": f"{c} absent at some sites", "min": None, "max": None, "unit": ""})
    if len(cats):
        for m, g in cats.groupby("measure"):
            spread("Table 1 categories", m, g["pct"])
    if len(att):
        for s, g in att.groupby("step"):
            spread("Attrition", f"{s} % excluded", g["pct_excluded"])
    if len(unm):
        for t, g in unm.groupby("table"):
            spread("Unmapped rows", f"{t} % concept_id 0", g["pct_concept_0"])
    if len(bnd):
        for c, g in bnd.groupby("concept_id"):
            spread("Physiologic bounds", f"concept {c} % out of range", g["pct_out_of_range"])
    if funnel is not None and len(funnel):
        for n, g in funnel.groupby("name"):
            for col, what in (("pct_mapped", "mapped to the set"), ("pct_no_date", "no date"), ("pct_no_number", "no number"),
                              ("pct_out_of_bounds", "outside bounds")):
                spread("Lab funnel", f"lab_{n} source rows: % {what}", g[col])
            if "cause" in g:  # each cause once per lab, with the number of sites (no site label)
                for cause, k in g.loc[g["cause"].astype(bool), "cause"].value_counts().items():
                    out.append({"domain": "Lab funnel", "measure": f"lab_{n}: {cause}", "min": None, "max": None, "unit": f"{k} site(s)",
                                "cause": cause})
    if overlap is not None and len(overlap):
        for col, what in (("pct_stays_day0", "% of index stays with an inpatient stay starting on the discharge day"),
                          ("pct_stays_overlapping", "% of index stays with an inpatient stay starting inside them"),
                          ("pct_of_readmissions", "day-0 or overlapping stays as % of the counted readmissions")):
            v = pd.to_numeric(overlap[col], errors="coerce").dropna()
            if len(v):  # always reported (issue 106), not only when the sites differ
                out.append({"domain": "Readmission rule", "measure": what, "min": round(float(v.min()), 1), "max": round(float(v.max()), 1), "unit": "%"})
    if units is not None and len(units):
        for n, g in units[units["flag"]].groupby("name"):
            out.append({"domain": "Lab units", "measure": f"lab_{n}: per-unit medians differ by a factor of 5 or more after the package's "
                        "conversions", "min": round(float(g["ratio"].min()), 1), "max": round(float(g["ratio"].max()), 1),
                        "unit": f"ratio, {len(g)} site(s)"})
    if known:
        from omop_etl.data_gaps import _known_match

        for f in out:
            if f.get("cause") == "source gap (confirmed)":  # a lab funnel cause, which also honours site-scoped keys
                f["expected"] = True
                continue
            m = re.match(r"((?:lab|vital|med|elix|disp|ederri)_[a-z0-9_]+)", str(f.get("measure", "")))
            if m and _known_match({"column": m.group(1), "site": None}, {k: v for k, v in known.items() if ":" not in k}, {}) is not None:
                f["expected"] = True
                f["cause"] = "source gap (confirmed)"
    return out


def _md(t: pd.DataFrame, index: bool = True) -> str:
    """A pipe table without the optional tabulate package."""
    t = t.reset_index() if index else t
    cells = [[("" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)) for v in row] for row in t.itertuples(index=False)]
    head = [str(c) for c in t.columns]
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(r) + " |" for r in cells]) + "\n"


def _wide(df: pd.DataFrame, index: str, value: str) -> str:
    if df.empty or value not in df:
        return "(none)\n"
    return _md(df.pivot_table(index=index, columns="site", values=value, aggfunc="first", dropna=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--private-dir", required=True, type=Path, help="the run's private folder (features_<site>.parquet, attrition_<site>.csv)")
    p.add_argument("--labels", required=True, type=Path, help="JSON file of site -> anonymous label (the run's private mapping)")
    p.add_argument("--db-dir", type=Path, help="folder with omop_<site>.duckdb: adds unmapped rows and physiologic bounds per site")
    p.add_argument("--start", default="2017-01-01")
    p.add_argument("--end", default="2022-09-30")
    p.add_argument("--detail", required=True, type=Path, help="per-site tables (keep it private)")
    p.add_argument("--flags", required=True, type=Path, help="measures where the sites differ, ranges only (shareable)")
    p.add_argument("--pct-gap", type=float, default=20.0)
    p.add_argument("--ratio", type=float, default=1.5)
    p.add_argument("--max-seconds", type=float, default=1200)
    p.add_argument("--known-source-gaps", type=Path,
                   help="the study team's confirmed source gaps (the file reproduce_study.py --known-source-gaps reads): marked as expected")
    a = p.parse_args(argv)
    t0 = time.time()
    labels = {str(k): str(v) for k, v in json.loads(a.labels.read_text(encoding="utf-8")).items()}
    frames = site_frames(a.private_dir, labels)
    if not frames:
        print("no features_<site>.parquet with a label in --labels", file=sys.stderr)
        return 1
    feat, cats, att = feature_rows(frames), category_rows(frames), attrition_rows(a.private_dir, labels)
    sites = {s: l for s, l in labels.items() if (a.private_dir / f"features_{s}.parquet").exists()}
    unm, bnd = (db_rows(a.db_dir, sites, a.start, a.end, t0 + a.max_seconds) if a.db_dir else (pd.DataFrame(), pd.DataFrame()))
    fun = lab_funnel(a.db_dir, sites, a.start, a.end, t0 + a.max_seconds) if a.db_dir else pd.DataFrame()
    from omop_etl.data_gaps import load_known_source_gaps

    known = load_known_source_gaps(a.known_source_gaps) if a.known_source_gaps else {}
    if len(fun):
        fun["cause"] = funnel_causes(fun, known, {v: k for k, v in sites.items()})
    ovl = readmission_overlap(a.private_dir, a.db_dir, sites, t0 + a.max_seconds) if a.db_dir else pd.DataFrame()
    uni = unit_medians(a.db_dir, sites, a.start, a.end, t0 + a.max_seconds) if a.db_dir else pd.DataFrame()
    fl = flags(feat, cats, att, unm, bnd, a.pct_gap, a.ratio, fun, overlap=ovl, units=uni, known=known)
    a.detail.parent.mkdir(parents=True, exist_ok=True)
    a.flags.parent.mkdir(parents=True, exist_ok=True)
    md = [f"# Per-site data quality ({len(frames)} sites, {a.start} to {a.end})", "", "Keep this file with the data: it has per-site counts.", ""]
    md += ["## Stays per site", "", pd.DataFrame({"site": list(frames), "stays": [len(d) for d in frames.values()]}).pipe(_md, index=False), ""]
    md += ["## Table 1 categories (% of the site's stays)", "", _wide(cats, "measure", "pct")]
    md += ["## Attrition (% excluded at each step)", "", _wide(att, "step", "pct_excluded")]
    for dom, pat in DOMAINS + [("Other", None)]:
        sub = feat[feat["feature"].map(lambda c: (re.search(pat, c) is not None) if pat else not any(re.search(q, c) for _, q in DOMAINS))]
        if sub.empty:
            continue
        md += [f"## {dom}: % missing", "", _wide(sub, "feature", "pct_missing")]
        if "pct_yes" in sub and sub["pct_yes"].notna().any():
            md += [f"## {dom}: % yes (0/1 features)", "", _wide(sub.dropna(subset=["pct_yes"]), "feature", "pct_yes")]
        if "mean" in sub and sub["mean"].notna().any():
            s2 = sub.dropna(subset=["mean"]).copy()
            s2["mean ± SD [Q1, Q3]"] = [f"{m:.2f} ± {sd if sd is not None and not pd.isna(sd) else float('nan'):.2f} [{q1:.2f}, {q3:.2f}]"
                                        for m, sd, q1, q3 in zip(s2["mean"], s2["sd"], s2["q1"], s2["q3"])]
            md += [f"## {dom}: mean ± SD [Q1, Q3]", "", _wide(s2, "feature", "mean ± SD [Q1, Q3]")]
    if len(unm):
        md += ["## Unmapped rows (concept_id 0) in the window", "", unm.pipe(_md, index=False), ""]
    if len(bnd):
        md += ["## Measurements outside the physiologic limits (%)", "", _wide(bnd, "concept_id", "pct_out_of_range")]
    if len(fun):
        md += ["## Lab funnel: rows with the lab's LOINC, and where they are lost", "",
               "source_rows: rows whose measurement_source_value is one of the lab's LOINC codes; concept_rows: rows mapped to the lab's "
               "concepts whatever their source text; unmapped_concepts: the concepts the unmapped source rows carry.", ""]
        for col in ("source_rows", "concept_rows", "pct_mapped", "unmapped_concepts", "pct_no_date", "pct_no_number", "pct_out_of_bounds",
                    "top_units", "cause"):
            md += [f"### {col}", "", _wide(fun, "name", col)]
    if len(ovl):
        md += ["## Day-0 and overlapping inpatient stays (not counted as readmissions)", "",
               "Index stays with another inpatient stay that starts on their discharge day (day 0) or inside them (overlapping), "
               "as % of index stays and of the counted readmissions; pct_stays_not_counted: such stays with no counted readmission. "
               "define_study_cohort(merge_episodes_days=0) is the episode-merging sensitivity analysis.", "",
               ovl.pipe(_md, index=False), ""]
    if len(uni):
        md += ["## Lab units: median per unit after the package's conversions", "",
               "ratio: largest / smallest unit median; 5 or more is flagged (an unconverted or mislabelled unit).", "",
               _wide(uni, "name", "units"), "### ratio", "", _wide(uni, "name", "ratio")]
    md += ["## Flags (measures where the sites differ)", "", pd.DataFrame(fl).pipe(_md, index=False) if fl else "(none)", ""]
    a.detail.write_text("\n".join(md), encoding="utf-8")
    a.flags.write_text(json.dumps({"sites": len(frames), "pct_gap": a.pct_gap, "ratio": a.ratio, "flags": fl}, indent=1), encoding="utf-8")
    print(f"{len(fl)} flag(s); done in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
