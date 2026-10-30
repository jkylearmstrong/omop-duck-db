"""Clinical-velocity feature set (FR-A4): utilization trajectory, LACE proxy, hemodynamic and organ-dysfunction markers.

Two layers, so the same formulas serve both a DuckDB cohort and an already-assembled feature table:

* :func:`engineer_clinical_features` - pure pandas, a faithful port of ``scripts/engineer_clinical_features.py``
  (``engineer_features``, sections 1-5). It is pinned to that script by ``tests/test_v069_velocity.py`` (golden file
  produced by the script itself). Quirks of the reference are kept on purpose and documented below.
* :func:`extract_velocity_features` - builds the raw utilization inputs from ``visit_occurrence`` (leak-free: only visits
  strictly before the admission date), joins first-admission-day vitals / BUN / creatinine and any extra feature frame
  (labs from ``extract_standard_labs`` etc.), then applies :func:`engineer_clinical_features`.

Reference semantics (all inputs are columns of the feature table):

* ``days_since_prior_encounter`` / ``days_since_prior_ip_ed`` missing -> :data:`NO_PRIOR_VISIT_DAYS` (365), then clipped
  to >= 1. (The sentinel is applied in pandas, not SQL; the result is the same.)
* ``ip_velocity = prior_ip_count / (days_since_prior_ip_ed / 30 + 1)``, ``ed_velocity`` likewise with ``prior_ed_count``;
  ``utilization_density = prior_ip_os_ed_count / (days_since_prior_encounter / 30 + 1)``;
  ``recent_ip_ratio = prior_ip_count / (prior_encounters_all_count + 1)``;
  ``readmission_intensity = readmission_count_twelve_months / (prior_ip_count + 1)``;
  ``mean_prior_ip_los = prior_ip_los_days / (prior_ip_count + 1)``.
* LACE proxy: length-of-stay points by *exact integer match* (1->1, 2->2, 3->3, 4-6->4, 7-13->5, >=14->7; a fractional LOS
  such as 1.5, or 0, scores 0 - a quirk of the reference), ED points (1,2,3,>=4 -> 1,2,3,4), comorbidity count capped at 5,
  plus 3 for the acute index admission.
* ``shock_index = heart_rate / systolic_bp`` after imputing missing vitals with 75 / 120 (SBP clipped >= 60); labs are
  imputed with fixed clinical defaults (BUN 15, creatinine 1.0 clipped >= 0.2, hemoglobin 13, albumin 3.8, eGFR 85,
  sodium 140, potassium 4.2, anion gap 10) before ratios and flags. **Missing values are therefore indistinguishable from
  normal values downstream**; a missing input *column* is created as all-missing and reported with a warning.

The reference's section 6 (``site_baseline_risk``, a target encoding that uses the label of every row, including validation
rows) is NOT part of :func:`engineer_clinical_features`; use :func:`site_baseline_risk` with a training mask instead.
"""

from __future__ import annotations

import re
import warnings
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd

__all__ = ["NO_PRIOR_VISIT_DAYS", "VELOCITY_FEATURES", "VELOCITY_CONCEPTS", "REQUIRED_INPUTS",
           "engineer_clinical_features", "extract_velocity_features", "site_baseline_risk",
           "compute_ckd_epi_2021"]

NO_PRIOR_VISIT_DAYS = 365
# Standard OMOP measurement concepts (LOINC): heart rate, systolic BP, diastolic BP, BUN, serum creatinine.
VELOCITY_CONCEPTS = {
    "heart_rate": (3027018,),
    "systolic_bp": (3004249,),
    "diastolic_bp": (3012888,),
    "lab_bun": (3013682,),
    "lab_creatinine": (3016723,),
    "lab_sodium": (3019550, 3014576),
    "lab_potassium": (3023103, 3028437),
    "lab_chloride": (3014576, 3010813),
    "lab_bicarbonate": (3007220, 3016462),
    "lab_anion_gap": (3037278, 3013444, 3020564),
    "lab_egfr": (40764999, 1619025, 36306178, 36303797),
    "lab_glucose": (3004501, 3000483),
}


def compute_ckd_epi_2021(
    creatinine: Any,
    age: Any,
    is_female: Any,
) -> Any:
    """Calculates race-free CKD-EPI 2021 eGFR (mL/min/1.73m^2) (FR-A6).

    Formula:
      eGFR = 142 * min(Scr/kappa, 1)^alpha * max(Scr/kappa, 1)^(-1.200) * 0.9938^Age * (1.012 if female)
      where kappa = 0.7 (female) / 0.9 (male), alpha = -0.241 (female) / -0.302 (male).
    """
    creat = np.asarray(creatinine, dtype=float)
    a = np.asarray(age, dtype=float)
    fem = np.asarray(is_female, dtype=bool)

    creat_clipped = np.maximum(creat, 0.1)
    kappa = np.where(fem, 0.7, 0.9)
    alpha = np.where(fem, -0.241, -0.302)
    fem_factor = np.where(fem, 1.012, 1.0)

    scr_kappa = creat_clipped / kappa
    min_term = np.minimum(scr_kappa, 1.0) ** alpha
    max_term = np.maximum(scr_kappa, 1.0) ** (-1.200)
    egfr = 142.0 * min_term * max_term * (0.9938 ** a) * fem_factor
    if isinstance(creatinine, pd.Series):
        return pd.Series(egfr, index=creatinine.index)
    return egfr
REQUIRED_INPUTS = [
    "days_since_prior_encounter", "days_since_prior_ip_ed", "prior_ip_os_ed_count", "prior_ip_count", "prior_ed_count",
    "prior_encounters_all_count", "readmission_count_twelve_months", "prior_ip_los_days", "index_los_days",
    "elix_pre_index_conditions_count", "systolic_bp", "diastolic_bp", "heart_rate", "lab_bun", "lab_creatinine",
    "lab_hemoglobin", "lab_albumin", "lab_egfr", "lab_sodium", "lab_potassium", "lab_anion_gap",
]
VELOCITY_FEATURES = [
    "utilization_density", "ip_velocity", "ed_velocity", "recent_ip_ratio", "readmission_intensity", "mean_prior_ip_los",
    "lace_los_score", "lace_ed_score", "lace_comorb_score", "lace_proxy_total", "shock_index", "pulse_pressure",
    "mean_arterial_pressure", "bun_to_creatinine", "bun_creat_azotemia_flag", "severe_anemia_flag",
    "hypoalbuminemia_flag", "egfr_stage4_5_flag", "hyponatremia_flag", "hyperkalemia_flag", "elevated_anion_gap_flag",
    "polypharmacy_count",
]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def engineer_clinical_features(df: pd.DataFrame, *, warn_missing: bool = True) -> pd.DataFrame:
    """Adds the :data:`VELOCITY_FEATURES` (plus ``combo_*`` interactions when their inputs exist) to a copy of ``df``."""
    feat = df.copy()
    for col in feat.columns:
        if feat[col].dtype == object or "decimal" in str(feat[col].dtype).lower():
            try:  # unlike the reference, text columns (e.g. site_source) are left alone instead of becoming NaN
                num = pd.to_numeric(feat[col], errors="coerce")
                if num.notna().sum() == feat[col].notna().sum():
                    feat[col] = num
            except Exception:
                pass
    absent = [c for c in REQUIRED_INPUTS if c not in feat.columns]
    if absent:
        if warn_missing:
            warnings.warn(f"engineer_clinical_features: input columns absent, treated as all-missing (default-imputed): {absent}",
                          UserWarning, stacklevel=2)
        for c in absent:
            feat[c] = np.nan

    sent = float(NO_PRIOR_VISIT_DAYS)
    days_enc = feat["days_since_prior_encounter"].fillna(sent).clip(lower=1.0)
    days_ip_ed = feat["days_since_prior_ip_ed"].fillna(sent).clip(lower=1.0)
    feat["utilization_density"] = feat["prior_ip_os_ed_count"] / (days_enc / 30.0 + 1.0)
    feat["ip_velocity"] = feat["prior_ip_count"] / (days_ip_ed / 30.0 + 1.0)
    feat["ed_velocity"] = feat["prior_ed_count"] / (days_ip_ed / 30.0 + 1.0)
    feat["recent_ip_ratio"] = feat["prior_ip_count"] / (feat["prior_encounters_all_count"] + 1.0)
    feat["readmission_intensity"] = feat["readmission_count_twelve_months"] / (feat["prior_ip_count"] + 1.0)
    feat["mean_prior_ip_los"] = feat["prior_ip_los_days"] / (feat["prior_ip_count"] + 1.0)

    los = feat["index_los_days"].fillna(1.0)
    lace_los = pd.Series(0, index=feat.index)
    lace_los[los == 1] = 1
    lace_los[los == 2] = 2
    lace_los[los == 3] = 3
    lace_los[(los >= 4) & (los <= 6)] = 4
    lace_los[(los >= 7) & (los <= 13)] = 5
    lace_los[los >= 14] = 7
    feat["lace_los_score"] = lace_los
    ed = feat["prior_ed_count"].fillna(0)
    lace_ed = pd.Series(0, index=feat.index)
    lace_ed[ed == 1] = 1
    lace_ed[ed == 2] = 2
    lace_ed[ed == 3] = 3
    lace_ed[ed >= 4] = 4
    feat["lace_ed_score"] = lace_ed
    comorb = feat["elix_pre_index_conditions_count"].fillna(0).clip(upper=5)
    feat["lace_comorb_score"] = comorb
    feat["lace_proxy_total"] = lace_los + lace_ed + comorb + 3.0

    sbp = feat["systolic_bp"].fillna(120.0).clip(lower=60.0)
    dbp = feat["diastolic_bp"].fillna(80.0).clip(lower=30.0)
    hr = feat["heart_rate"].fillna(75.0)
    feat["shock_index"] = hr / sbp
    feat["pulse_pressure"] = sbp - dbp
    feat["mean_arterial_pressure"] = dbp + (feat["pulse_pressure"] / 3.0)

    bun = feat["lab_bun"].fillna(15.0)
    creat = feat["lab_creatinine"].fillna(1.0).clip(lower=0.2)
    feat["bun_to_creatinine"] = bun / creat
    feat["bun_creat_azotemia_flag"] = (feat["bun_to_creatinine"] > 20.0).astype(float)
    feat["severe_anemia_flag"] = (feat["lab_hemoglobin"].fillna(13.0) < 8.5).astype(float)
    feat["hypoalbuminemia_flag"] = (feat["lab_albumin"].fillna(3.8) < 3.2).astype(float)

    # Dynamic Race-Free CKD-EPI 2021 eGFR Calculation (FR-A6)
    # Eliminates cross-site missingness and artificial reporting ceilings (>60 mL/min).
    has_demog = ("age" in feat.columns or "age_at_admission" in feat.columns) and ("gender_concept_id" in feat.columns or "is_female" in feat.columns)
    if has_demog:
        age_col = feat["age"] if "age" in feat.columns else feat["age_at_admission"]
        is_fem = (feat["gender_concept_id"] == 8532) if "gender_concept_id" in feat.columns else feat["is_female"].astype(bool)
        computed_egfr = compute_ckd_epi_2021(creat, age_col.fillna(65.0), is_fem)
        resolved_egfr = np.where(feat["lab_egfr"].isna() | (feat["lab_egfr"] >= 60.0), computed_egfr, feat["lab_egfr"])
        feat["egfr_stage4_5_flag"] = (pd.Series(resolved_egfr, index=feat.index).fillna(85.0) < 30.0).astype(float)
    else:
        feat["egfr_stage4_5_flag"] = (feat["lab_egfr"].fillna(85.0) < 30.0).astype(float)

    feat["hyponatremia_flag"] = (feat["lab_sodium"].fillna(140.0) < 135.0).astype(float)
    feat["hyperkalemia_flag"] = (feat["lab_potassium"].fillna(4.2) > 5.1).astype(float)

    # Dynamic Anion Gap Stoichiometric Fallback (FR-A6): Na - (Cl + HCO3)
    has_lytes = "lab_sodium" in feat.columns and "lab_chloride" in feat.columns and "lab_bicarbonate" in feat.columns
    if has_lytes:
        calc_ag = feat["lab_sodium"] - (feat["lab_chloride"] + feat["lab_bicarbonate"])
        resolved_ag = np.where(feat["lab_anion_gap"].isna(), calc_ag, feat["lab_anion_gap"])
        feat["elevated_anion_gap_flag"] = (pd.Series(resolved_ag, index=feat.index).fillna(10.0) > 14.0).astype(float)
    else:
        feat["elevated_anion_gap_flag"] = (feat["lab_anion_gap"].fillna(10.0) > 14.0).astype(float)

    med_cols = [c for c in feat.columns if c.startswith("med_") and not c.endswith("_legacy")]
    feat["polypharmacy_count"] = feat[med_cols].sum(axis=1)
    if "elix_chf" in feat.columns and "elix_diabetes_complicated" in feat.columns:
        feat["combo_chf_dm"] = feat["elix_chf"] * feat["elix_diabetes_complicated"]
    if "elix_copd" in feat.columns and "med_systemic_corticosteroids" in feat.columns:
        feat["combo_copd_steroids"] = feat["elix_copd"] * feat["med_systemic_corticosteroids"]
    if "elix_renal_failure" in feat.columns:
        feat["combo_ckd_hyperk"] = feat["elix_renal_failure"] * feat["hyperkalemia_flag"]
    if "elix_chf" in feat.columns and "med_raas" in feat.columns:
        feat["combo_chf_raas"] = feat["elix_chf"] * feat["med_raas"]
    return feat


def site_baseline_risk(df: pd.DataFrame, site_col: str = "site_source", label_col: str = "readmitted_30d",
                       train_mask: Sequence[bool] | pd.Series | None = None) -> pd.Series:
    """Per-site outcome rate as a feature, computed from TRAINING rows only (no label leakage into validation rows).

    Sites absent from the training rows get the training-wide rate. With ``train_mask=None`` every row is used, which is
    only appropriate when the result is computed *inside* a cross-validation fold on that fold's training data.
    """
    mask = pd.Series(True, index=df.index) if train_mask is None else pd.Series(np.asarray(train_mask, dtype=bool), index=df.index)
    train = df[mask]
    rates = train.groupby(site_col)[label_col].mean()
    return df[site_col].map(rates).fillna(train[label_col].mean())


def derive_labs(raw: pd.DataFrame) -> pd.DataFrame:
    """Fills ``lab_egfr`` (CKD-EPI 2021, FR-A6: removes the >60 ceiling) and ``lab_anion_gap`` (Na - (Cl + HCO3)) in place."""
    if "lab_creatinine" in raw.columns and "age" in raw.columns and "gender_concept_id" in raw.columns:
        creat_series = raw["lab_creatinine"]
        has_creat = creat_series.notna()
        if has_creat.any():
            calc_egfr = compute_ckd_epi_2021(creat_series.fillna(1.0), raw["age"].fillna(65.0), raw["gender_concept_id"] == 8532)
            egfr_current = raw["lab_egfr"] if "lab_egfr" in raw.columns else pd.Series(np.nan, index=raw.index)
            raw["lab_egfr"] = np.where(has_creat & (egfr_current.isna() | (egfr_current >= 60.0)), calc_egfr, egfr_current)
    if all(k in raw.columns for k in ("lab_sodium", "lab_chloride", "lab_bicarbonate")):
        calc_ag = raw["lab_sodium"] - (raw["lab_chloride"] + raw["lab_bicarbonate"])
        ag_current = raw["lab_anion_gap"] if "lab_anion_gap" in raw.columns else pd.Series(np.nan, index=raw.index)
        raw["lab_anion_gap"] = np.where(ag_current.isna(), calc_ag, ag_current)
    return raw


def extract_velocity_features(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    *,
    lookback_days: int = 365,
    readmit_gap_days: int = 30,
    ip_concepts=(9201, 262),
    ed_concepts=(9203,),
    os_concepts=(9202, 581385),
    concepts: dict[str, Any] | None = None,
    extra_features: pd.DataFrame | None = None,
    cohort_id: int | None = None,
    engineer: bool = True,
) -> pd.DataFrame:
    """Raw utilization inputs + first-admission-day vitals/BUN/creatinine (+ ``extra_features``), then the engineered set.

    One row per distinct cohort (``subject_id``, ``cohort_start_date``). Only visits strictly before the admission date
    inside ``lookback_days`` are used. Raw columns (``prior_*``, ``days_since_*`` - NULL when there is no prior visit,
    ``readmission_count_twelve_months`` = prior IP visits starting within ``readmit_gap_days`` of an earlier IP discharge,
    ``index_los_days``) keep the reference script's names. ``extra_features`` (e.g. labs from ``extract_standard_labs``
    renamed ``lab_*``, ``elix_*``, ``med_*``) is left-joined on ``subject_id`` + ``cohort_start_date``.
    ``engineer=False`` returns only the raw inputs.
    """
    if not _IDENT.match(cohort_table):
        raise ValueError(f"Invalid cohort_table {cohort_table!r}.")
    lb, gap = int(lookback_days), int(readmit_gap_days)
    if lb < 1 or gap < 1:
        raise ValueError("lookback_days and readmit_gap_days must be >= 1.")
    cc = {**VELOCITY_CONCEPTS, **(concepts or {})}
    cols = {r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_table}").fetchall()}
    if not {"subject_id", "cohort_start_date"} <= cols:
        raise ValueError("cohort_table needs subject_id and cohort_start_date.")
    where = f"WHERE cohort_definition_id = {int(cohort_id)}" if cohort_id is not None and "cohort_definition_id" in cols else ""
    end_expr = "cohort_end_date" if "cohort_end_date" in cols else "cohort_start_date"

    def ids(values) -> str:
        return ", ".join(str(int(v)) for v in values)

    def pick(key: str) -> str:
        c = ids(cc[key])
        return (f"arg_min(CASE WHEN m.measurement_concept_id IN ({c}) THEN m.value_as_number END, "
                f"CASE WHEN m.measurement_concept_id IN ({c}) AND m.value_as_number IS NOT NULL THEN m.measurement_id END)")

    ip, ed, os_ = ids(ip_concepts), ids(ed_concepts), ids(os_concepts)
    sql = f"""
    WITH c AS (
        SELECT subject_id, cohort_start_date, max({end_expr}) AS cohort_end_date
        FROM {cohort_table} {where} GROUP BY subject_id, cohort_start_date
    ),
    v AS (
        SELECT c.subject_id, c.cohort_start_date, v.visit_concept_id AS vc, v.visit_start_date AS vs,
               COALESCE(v.visit_end_date, v.visit_start_date) AS ve
        FROM c JOIN visit_occurrence v ON v.person_id = c.subject_id
        WHERE v.visit_start_date < c.cohort_start_date
          AND v.visit_start_date >= c.cohort_start_date - INTERVAL ({lb}) DAY
    ),
    agg AS (
        SELECT subject_id, cohort_start_date,
               count(*) AS prior_encounters_all_count,
               count(*) FILTER (WHERE vc IN ({ip})) AS prior_ip_count,
               count(*) FILTER (WHERE vc IN ({ed})) AS prior_ed_count,
               count(*) FILTER (WHERE vc IN ({ip}, {ed}, {os_})) AS prior_ip_os_ed_count,
               COALESCE(sum(date_diff('day', vs, ve)) FILTER (WHERE vc IN ({ip})), 0) AS prior_ip_los_days,
               max(ve) AS last_any_end,
               max(ve) FILTER (WHERE vc IN ({ip}, {ed})) AS last_ip_ed_end
        FROM v GROUP BY 1, 2
    ),
    readm AS (
        SELECT i.subject_id, i.cohort_start_date, count(DISTINCT i.vs) AS n_readmit
        FROM v i
        JOIN visit_occurrence p ON p.person_id = i.subject_id AND p.visit_concept_id IN ({ip})
         AND COALESCE(p.visit_end_date, p.visit_start_date) < i.vs
         AND i.vs <= COALESCE(p.visit_end_date, p.visit_start_date) + INTERVAL ({gap}) DAY
        WHERE i.vc IN ({ip})
        GROUP BY 1, 2
    ),
    m AS (
        SELECT c.subject_id, c.cohort_start_date,
               {pick('heart_rate')} AS heart_rate, {pick('systolic_bp')} AS systolic_bp,
               {pick('diastolic_bp')} AS diastolic_bp, {pick('lab_bun')} AS lab_bun,
               {pick('lab_creatinine')} AS lab_creatinine,
               {pick('lab_sodium')} AS lab_sodium, {pick('lab_potassium')} AS lab_potassium,
               {pick('lab_chloride')} AS lab_chloride, {pick('lab_bicarbonate')} AS lab_bicarbonate,
               {pick('lab_anion_gap')} AS lab_anion_gap, {pick('lab_egfr')} AS lab_egfr
        FROM c JOIN measurement m ON m.person_id = c.subject_id AND m.measurement_date = c.cohort_start_date
        GROUP BY 1, 2
    ),
    demo AS (
        SELECT person_id AS subject_id, gender_concept_id,
               COALESCE(year_of_birth, date_part('year', birth_datetime)) AS birth_year
        FROM person
    )
    SELECT c.subject_id, c.cohort_start_date,
           date_diff('day', agg.last_any_end, c.cohort_start_date)    AS days_since_prior_encounter,
           date_diff('day', agg.last_ip_ed_end, c.cohort_start_date)  AS days_since_prior_ip_ed,
           COALESCE(agg.prior_ip_os_ed_count, 0)                      AS prior_ip_os_ed_count,
           COALESCE(agg.prior_ip_count, 0)                            AS prior_ip_count,
           COALESCE(agg.prior_ed_count, 0)                            AS prior_ed_count,
           COALESCE(agg.prior_encounters_all_count, 0)                AS prior_encounters_all_count,
           COALESCE(readm.n_readmit, 0)                               AS readmission_count_twelve_months,
           COALESCE(agg.prior_ip_los_days, 0)                         AS prior_ip_los_days,
           date_diff('day', c.cohort_start_date, c.cohort_end_date)   AS index_los_days,
           demo.gender_concept_id,
           (date_part('year', c.cohort_start_date) - demo.birth_year) AS age,
           m.heart_rate, m.systolic_bp, m.diastolic_bp, m.lab_bun, m.lab_creatinine,
           m.lab_sodium, m.lab_potassium, m.lab_chloride, m.lab_bicarbonate,
           m.lab_anion_gap, m.lab_egfr
    FROM c
    LEFT JOIN agg USING (subject_id, cohort_start_date)
    LEFT JOIN readm USING (subject_id, cohort_start_date)
    LEFT JOIN m USING (subject_id, cohort_start_date)
    LEFT JOIN demo ON demo.subject_id = c.subject_id
    ORDER BY c.subject_id, c.cohort_start_date
    """
    raw = derive_labs(con.execute(sql).df())

    if extra_features is not None:
        extra = extra_features.drop(columns=[c for c in extra_features.columns if c in raw.columns
                                             and c not in ("subject_id", "cohort_start_date")])
        raw = raw.merge(extra, on=["subject_id", "cohort_start_date"], how="left")
    if not engineer:
        return raw
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="engineer_clinical_features: input columns absent")
        out = engineer_clinical_features(raw, warn_missing=False)
    absent = [c for c in REQUIRED_INPUTS if c not in raw.columns]
    if absent:
        warnings.warn(f"extract_velocity_features: inputs not supplied (add them via extra_features); they are "
                      f"default-imputed as if normal: {absent}", UserWarning, stacklevel=2)
    return out
