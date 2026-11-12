"""Manuscript layout: reshape :mod:`omop_etl.study_report` results into the tables of a readmission-prediction manuscript (unreleased).

``build_study_tables`` returns generic tables. This module turns them (and the patient-level feature table) into the layout a
manuscript prints, one sheet per table, so a rerun with newer data fills the same sheets:

* :func:`manuscript_frame`        - feature table -> labelled categories (age band, race group, discharge status, BMI band ...).
* :func:`manuscript_table1`       - Table 1: counts / mean +- SD for the whole cohort, the readmitted and the not readmitted.
* :func:`manuscript_completeness` - Table S2: missing / unmeasured counts per feature.
* :func:`manuscript_consort`      - Table S3: stepwise attrition, all sites combined and per site.
* :func:`manuscript_regression`   - Table 6: multivariable logistic regression, adjusted OR (95% CI), standardised beta, p.
* :func:`manuscript_workbook`     - all of the above plus Tables 2-5, S1 and S4 in one workbook (sheet names ``Table 1 - Descriptive`` ...).
* :func:`compare_workbooks`       - cell-by-cell comparison of two such workbooks (a rerun against a previous deliverable).

Everything is aggregate. Counts under ``min_cell`` are shown as ``<min_cell``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from omop_etl.study_report import _newton_logit, export_workbook

__all__ = ["manuscript_frame", "manuscript_table1", "manuscript_completeness", "manuscript_consort", "manuscript_regression",
           "manuscript_workbook", "compare_workbooks", "workbook_similarity", "workbook_deltas", "delta_summary", "SIMILARITY_TOLERANCES", "MANUSCRIPT_LABS", "MANUSCRIPT_ELIXHAUSER"]

EN = "–"
_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
PM = "±"
#: (feature-table column names tried in order, label)
MANUSCRIPT_LABS = [(("lab_albumin",), "Albumin"), (("lab_alt",), "ALT"), (("lab_anion_gap",), "Anion gap"), (("lab_ast",), "AST"),
                   (("lab_bicarbonate",), "Bicarbonate (CO2)"), (("lab_bun",), "BUN"), (("lab_creatinine",), "Creatinine"),
                   (("lab_egfr",), "eGFR (2021)"), (("lab_glucose",), "Glucose"), (("lab_hba1c",), "HbA1c"), (("lab_hematocrit",), "Hematocrit"),
                   (("lab_ldl",), "LDL"), (("lab_wbc",), "Leukocytes (WBC)"), (("lab_sodium",), "Sodium")]
_MEDS = [("med_insulins", "Medication: Insulins"), ("med_biguanides", "Medication: Biguanides (Metformin)"),
         ("med_sulfonylureas", "Medication: Sulfonylureas"), ("med_lipid_modifying", "Medication: Lipid Modifying Agents (Statins)"),
         ("med_antihypertensives", "Medication: Antihypertensives"), ("med_raas", "Medication: RAAS Inhibitors (ACEi/ARB)"),
         ("med_beta_blockers", "Medication: Beta Blockers"), ("med_systemic_corticosteroids", "Medication: Systemic Corticosteroids")]
#: (names tried in order, label): Elixhauser flags in the manuscript's order
MANUSCRIPT_ELIXHAUSER = [(("elix_mets", "elix_metastatic_cancer"), "Metastatic Cancer"), (("elix_chf",), "Congestive Heart Failure"),
                         (("elix_fluid_electrolyte",), "Fluid & Electrolyte Disorders"), (("elix_solid_tumor",), "Solid Tumor (Non-Metastatic)"),
                         (("elix_weight_loss",), "Weight Loss / Malnutrition"), (("elix_arrhythmia", "elix_cardiac_arrhythmias"), "Cardiac Arrhythmia"),
                         (("elix_pvd", "elix_peripheral_vascular"), "Peripheral Vascular Disorder"), (("elix_lymphoma",), "Lymphoma"),
                         (("elix_liver_disease",), "Liver Disease"), (("elix_renal_failure",), "Renal Failure / CKD"), (("elix_psychoses",), "Psychoses"),
                         (("elix_pulm_circ", "elix_pulmonary_circulation"), "Pulmonary Circulation Disorder"),
                         (("elix_copd",), "Chronic Pulmonary Disease (COPD)"), (("elix_dm_uncomp", "elix_diabetes_uncomplicated"), "Diabetes (Uncomplicated)"),
                         (("elix_dm_comp", "elix_diabetes_complicated"), "Diabetes (Complicated)"), (("elix_paralysis",), "Paralysis"),
                         (("elix_depression",), "Depression"), (("elix_valvular", "elix_valvular_disease"), "Valvular Heart Disease"),
                         (("elix_pud", "elix_peptic_ulcer"), "Peptic Ulcer Disease"), (("elix_coagulopathy",), "Coagulopathy"),
                         (("elix_htn_uncomp", "elix_htn_uncomplicated"), "Hypertension (Uncomplicated)"), (("elix_alcohol_abuse",), "Alcohol Abuse"),
                         (("elix_htn_comp", "elix_htn_complicated"), "Hypertension (Complicated)"), (("elix_hypothyroid", "elix_hypothyroidism"), "Hypothyroidism"),
                         (("elix_drug_abuse",), "Drug Abuse"), (("elix_blood_loss_anemia",), "Blood Loss Anemia"), (("elix_deficiency_anemia",), "Deficiency Anemia"),
                         (("elix_neuro_other", "elix_other_neuro"), "Other Neurological Disorders"), (("elix_hiv", "elix_aids_hiv"), "AIDS / HIV"),
                         (("elix_rheumatic", "elix_rheumatoid_arthritis"), "Rheumatoid Arthritis / Collagen Vascular"), (("elix_obesity",), "Obesity")]
_SDI = ["SDI Quintile 1 (Least Deprived)", "SDI Quintile 2", "SDI Quintile 3", "SDI Quintile 4", "SDI Quintile 5 (Most Deprived)"]
_RUCA = ["RUCA: Metropolitan (1.0)", "RUCA: Micropolitan (2.0)", "RUCA: Small Town / Rural (3+)", "RUCA: Missing / Unknown"]
_DISC = ["Home", "Home Health", "SNF / Rehab", "Against Medical Advice", "Missing / Unknown"]
_BMI = [f"BMI: Underweight (<18.5 kg/m²)", f"BMI: Normal (18.5{EN}24.9 kg/m²)", f"BMI: Overweight (25.0{EN}29.9 kg/m²)",
        "BMI: Obese (30.0+ kg/m²)", "BMI: Missing / Unknown"]
_AGE = [f"Age: 18{EN}44 years", f"Age: 45{EN}64 years", f"Age: 65{EN}74 years", "Age: 75+ years"]
_RACE = ["Race: White", "Race: Black / African American", "Race: Other / Unknown"]
_TOB = ["Tobacco: Never user", "Tobacco: Former user", "Tobacco: Current user"]


def _col(df: pd.DataFrame, *names: str) -> pd.Series | None:
    for n in names:
        if n in df:
            return df[n]
    return None


def _need(df: pd.DataFrame, *names: str) -> pd.Series:
    s = _col(df, *names)
    if s is None:
        raise ValueError(f"none of the columns {names} found in the feature table.")
    return s


# ------------------------------------------------------------------ labelled categories
def manuscript_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Adds the labelled categories the manuscript tables use: ``m_age``, ``m_sex`` ("Female" / "Male"), ``m_race``, ``m_hispanic``,
    ``m_disch`` (discharge status), ``m_bmi`` (BMI band), ``m_sdi``, ``m_ruca``, ``m_tobacco``. Works from this package's feature columns
    (``age_at_index``, ``female``, ``race_concept_id``, ``disp_*``, ``vital_bmi`` ...) or from a table that already has text
    categories (``age_category``, ``sex``, ``race``, ``discharge_status`` ...)."""
    o = df.copy()
    age = _col(o, "age_at_index", "age")
    if age is not None:
        o["m_age"] = pd.cut(age, [-np.inf, 44, 64, 74, np.inf], labels=_AGE).astype(object)
    elif "age_category" in o:
        o["m_age"] = o["age_category"].map({"18-44": _AGE[0], "45-64": _AGE[1], "65-74": _AGE[2], "75+": _AGE[3]})
    if "female" in o:
        o["m_sex"] = np.where(o["female"].astype(float) == 1, "Female", "Male")
    elif "sex" in o:
        o["m_sex"] = np.where(o["sex"].astype(str).str.upper().str[0] == "F", "Female", "Male")
    if "race_concept_id" in o:
        o["m_race"] = o["race_concept_id"].map(lambda c: _RACE[0] if c == 8527 else _RACE[1] if c == 8516 else _RACE[2])
    elif "race" in o:
        o["m_race"] = o["race"].map(lambda r: _RACE[0] if r == "White" else _RACE[1] if str(r).startswith("Black") else _RACE[2])
    if "ethnicity_concept_id" in o:
        o["m_hispanic"] = o["ethnicity_concept_id"] == 38003563
    elif "ethnicity" in o:
        o["m_hispanic"] = o["ethnicity"] == "Hispanic"
    disp = [c for c in ("disp_home", "disp_home_health", "disp_snf", "disp_ama") if c in o]
    if disp:
        names = dict(zip(("disp_home", "disp_home_health", "disp_snf", "disp_ama"), _DISC))
        d = pd.Series(_DISC[4], index=o.index, dtype=object)
        for c in reversed(disp):
            d[o[c].astype(float) == 1] = names[c]
        o["m_disch"] = d
    elif "discharge_status" in o:
        o["m_disch"] = o["discharge_status"].map({"Home": _DISC[0], "Home Health": _DISC[1], "SNF / Rehab": _DISC[2],
                                                  "Against Medical Advice": _DISC[3]}).fillna(_DISC[4])
    bmi = _col(o, "vital_bmi", "bmi")
    if bmi is not None:
        o["m_bmi"] = pd.cut(bmi, [-np.inf, 18.5, 25, 30, np.inf], labels=_BMI[:4], right=False).astype(object)
        o.loc[bmi.isna(), "m_bmi"] = _BMI[4]
    if "sdi_quintile" in o:
        o["m_sdi"] = o["sdi_quintile"].astype(object).where(o["sdi_quintile"].isin(_SDI), "SDI: Missing / Unknown")
    if "ruca_category" in o:
        o["m_ruca"] = o["ruca_category"].astype(object).where(o["ruca_category"].isin(_RUCA[:3]), _RUCA[3])
    if "tobacco" in o:
        o["m_tobacco"] = "Tobacco: " + o["tobacco"].astype(str)
    return o


def _grp(df: pd.DataFrame, label_col: str):
    y = df[label_col].astype(int).to_numpy()
    return [("Whole cohort", np.ones(len(df), bool)), ("Subset with readmission", y == 1), ("Subset without readmission", y == 0)]


def _n_pct(mask: np.ndarray, base: np.ndarray, min_cell: int | None) -> str:
    n, d = int((mask & base).sum()), int(base.sum())
    shown = f"<{min_cell}" if (min_cell and 0 < n < min_cell) else f"{n}"
    return f"{shown} ({100 * n / d:.2f}%)" if d else ""


def _ms(v: pd.Series) -> str:
    v = pd.to_numeric(v, errors="coerce").dropna()
    return f"{v.mean():.2f} {PM} {v.std():.2f}" if len(v) > 1 else ""


# ------------------------------------------------------------------ Table 1
def manuscript_table1(df: pd.DataFrame, *, label_col: str = "readmitted_30d", min_cell: int | None = 11,
                      frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Table 1 in the manuscript layout. ``Feature`` rows with ``n (%)`` for categories and ``mean +- SD`` for continuous measures
    (labs and blood pressure among the stays that have a value)."""
    m = frame if frame is not None else manuscript_frame(df)
    groups = _grp(m, label_col)
    rows: list[tuple[str, Any]] = [("# Unique Patients", lambda mk: f"{int(mk.sum())} Unique Patients")]

    def cont(label: str, s: pd.Series | None):
        if s is not None:
            rows.append((label, lambda mk, s=s: _ms(s[mk])))

    def cat(label: str, s: pd.Series | None, level: Any):
        if s is not None:
            rows.append((label, lambda mk, s=s, level=level: _n_pct((s == level).to_numpy(), mk, min_cell)))

    ip = _col(m, "prior_ip_os_ed_count")
    allp = _col(m, "prior_encounters_all_count")
    cont("Prior Inpatient / Observation / ED Encounter Count", ip)
    if ip is not None and allp is not None:
        cont("Prior Outpatient / Other Encounter Count", allp - ip)
    cont("Secondary Diagnosis Count (CCS Categories)", _col(m, "n_secondary_dx_ccs"))
    cont("Index Admission Length of Stay (Days)", _col(m, "index_los_days", "los_days"))
    cont("Days Since Prior Encounter", _col(m, "days_since_prior_encounter"))
    cont("Days Since Prior Inpatient / Observation / ED Encounter", _col(m, "days_since_prior_ip_ed"))
    cont("Prior 12-Month Readmission Count", _col(m, "readmission_count_twelve_months"))
    for lv in _AGE:
        cat(lv, m.get("m_age"), lv)
    cat("Sex: Female", m.get("m_sex"), "Female")
    cat("Ethnicity: Hispanic", m.get("m_hispanic"), True)
    for lv in _RACE:
        cat(lv, m.get("m_race"), lv)
    for lv in _SDI:
        cat(lv, m.get("m_sdi"), lv)
    for lv in _RUCA:
        cat(lv, m.get("m_ruca"), lv)
    for lv in _DISC:
        cat("Discharge Status: " + lv, m.get("m_disch"), lv)
    for lv in _TOB:
        cat(lv, m.get("m_tobacco"), lv)
    for lv in _BMI:
        cat(lv, m.get("m_bmi"), lv)
    cont("Systolic Blood Pressure (mmHg)", _col(m, "vital_systolic_bp", "systolic_bp"))
    cont("Diastolic Blood Pressure (mmHg)", _col(m, "vital_diastolic_bp", "diastolic_bp"))
    for c, lab in _MEDS:
        cat(lab, _col(m, c), 1)
    for names, lab in MANUSCRIPT_LABS:
        cont(lab, _col(m, *names))
    out = [{"Feature": lab, **{g: f(mk) for g, mk in groups}} for lab, f in rows]
    return pd.DataFrame(out)


# ------------------------------------------------------------------ Table S2
def manuscript_completeness(df: pd.DataFrame, *, label_col: str = "readmitted_30d", min_cell: int | None = 11,
                            frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Table S2: how many stays lack each feature (missing category or not measured at the index admission)."""
    m = frame if frame is not None else manuscript_frame(df)
    groups = _grp(m, label_col)
    items: list[tuple[str, np.ndarray]] = []
    if "m_race" in m:
        items.append(("Race: Other / Unknown", (m["m_race"] == _RACE[2]).to_numpy()))
    if "m_disch" in m:
        items.append(("Discharge Status: Missing / Unknown", (m["m_disch"] == _DISC[4]).to_numpy()))
    if "m_bmi" in m:
        items.append(("BMI: Missing / Unknown", (m["m_bmi"] == _BMI[4]).to_numpy()))
    if "m_sdi" in m:
        items.append(("SDI Quintile: Missing / Unknown", (m["m_sdi"] == "SDI: Missing / Unknown").to_numpy()))
    if "m_ruca" in m:
        items.append(("RUCA: Missing / Unknown", (m["m_ruca"] == _RUCA[3]).to_numpy()))
    for names, lab in ((("vital_systolic_bp", "systolic_bp"), "Systolic Blood Pressure not recorded"),
                       (("vital_diastolic_bp", "diastolic_bp"), "Diastolic Blood Pressure not recorded")):
        s = _col(m, *names)
        if s is not None:
            items.append((lab, s.isna().to_numpy()))
    for names, lab in MANUSCRIPT_LABS:
        s = _col(m, *names)
        if s is not None:
            items.append((f"{lab} not measured at index admission", s.isna().to_numpy()))

    def fmt(mask: np.ndarray, base: np.ndarray) -> str:
        n, d = int((mask & base).sum()), int(base.sum())
        shown = f"<{min_cell}" if (min_cell and 0 < n < min_cell) else f"{n:,}"
        return f"{shown} ({100 * n / d:.2f}%)" if d else ""

    return pd.DataFrame([{"Feature": lab, **{g: fmt(mk, base) for g, base in groups}} for lab, mk in items])


# ------------------------------------------------------------------ Table S3
def manuscript_consort(attrition: pd.DataFrame, *, site_col: str = "site", site_labels: Mapping[str, str] | None = None,
                       reason: str = "Patient-level step from omop-duck-db define_study_cohort()") -> pd.DataFrame:
    """Table S3 from per-site attrition (``site``, ``step_name``, ``subjects_retained`` and optionally ``rows_retained``): the combined
    cohort first, then each site (anonymous labels from ``site_labels``, sorted). Stays equal patients unless ``rows_retained`` is given."""
    a = attrition.copy()
    if site_labels:
        a[site_col] = a[site_col].map(lambda s: site_labels.get(s, s))
    rows_col = "rows_retained" if "rows_retained" in a else "subjects_retained"

    def block(g: pd.DataFrame, name: str) -> list[dict[str, Any]]:
        out, prev_p, prev_r = [], None, None
        for i, r in enumerate(g.itertuples(index=False), 1):
            p, rr = int(getattr(r, "subjects_retained")), int(getattr(r, rows_col))
            out.append({"Site": name, "Step": f"Step {i}", "Stage / Description": r.step_name, "Remaining Stays (N)": rr,
                        "Remaining Patients (P)": p, "Excluded Stays (Delta N)": 0 if prev_r is None else prev_r - rr,
                        "Excluded Patients (Delta P)": 0 if prev_p is None else prev_p - p, "Exclusion Reason": reason})
            prev_p, prev_r = p, rr
        return out

    order = list(dict.fromkeys(a["step_name"]))
    combined = a.groupby("step_name", sort=False).agg(subjects_retained=("subjects_retained", "sum"), **{rows_col: (rows_col, "sum")}).reindex(order).reset_index() \
        if rows_col != "subjects_retained" else a.groupby("step_name", sort=False).agg(subjects_retained=("subjects_retained", "sum")).reindex(order).reset_index()
    out = block(combined, "All_Sites_Combined")
    for site in sorted(a[site_col].unique()):
        out += block(a[a[site_col] == site], site)
    return pd.DataFrame(out)


# ------------------------------------------------------------------ Table 6
# Table 6 increments as the delivered workbook prints them: label -> (increment text, size in the measure's own unit).
CLINICAL_INCREMENTS: dict[str, tuple[str, float]] = {
    "Prior Inpatient / Obs / ED Count": ("1 stay", 1), "Prior Outpatient / Other Encounter Count": ("10 encounters", 10),
    "Number of Secondary Diagnosis CCS Categories": ("1 category", 1), "Index Admission Length of Stay (days)": ("1 day", 1),
    "Days Since Prior Encounter": ("30 days", 30), "Days Since Prior Inpatient / Obs / ED Encounter": ("30 days", 30),
    "Prior Readmissions in Past 12 Months": ("1 readmission", 1), "Systolic Blood Pressure (mmHg)": ("10 mmHg", 10),
    "Diastolic Blood Pressure (mmHg)": ("10 mmHg", 10), "Albumin": ("1 g/dL", 1), "ALT": ("10 U/L", 10), "Anion gap": ("1 mEq/L", 1),
    "AST": ("10 U/L", 10), "Bicarbonate (CO2)": ("5 mEq/L", 5), "BUN": ("10 mg/dL", 10), "Creatinine": ("1 mg/dL", 1),
    "eGFR (2021)": ("10 mL/min/1.73 m²", 10), "Glucose": ("50 mg/dL", 50), "HbA1c": ("1 percentage point", 1),
    "Hematocrit": ("5 percentage points", 5), "LDL": ("30 mg/dL", 30), "Leukocytes (WBC)": ("5 x10^9/L", 5), "Sodium": ("5 mEq/L", 5)}


def manuscript_regression(df: pd.DataFrame, *, label_col: str = "readmitted_30d", frame: pd.DataFrame | None = None,
                          sdi_missing_term: bool = True, increments: str | Mapping[str, tuple[str, float]] = "sd") -> pd.DataFrame:
    """Table 6: one multivariable logistic regression, rows grouped as the manuscript prints them. Flags enter as 0/1 (OR for yes vs
    no, reference categories: age 18-44, female, White, non-Hispanic, SDI quintile 1, metropolitan, discharged home, never tobacco,
    BMI 18.5-24.9); continuous measures are standardised (OR per 1 SD) and missing values median imputed. ``Standardized Beta`` is
    the coefficient times the predictor's SD (unchanged by ``increments``).

    ``increments``: ``"sd"`` (default) reports continuous measures per 1 SD; ``"clinical"`` reports them per the clinical unit of the
    delivered workbook (:data:`CLINICAL_INCREMENTS`, for example 10 mmHg or 30 days); a mapping ``label -> (text, size)`` sets
    its own (labels not in it stay per 1 SD). The fit is the same model, so p-values do not change; only OR and CI are rescaled."""
    if isinstance(increments, str) and increments not in ("sd", "clinical"):
        raise ValueError("increments must be 'sd', 'clinical' or a mapping.")
    inc_map = CLINICAL_INCREMENTS if increments == "clinical" else ({} if increments == "sd" else dict(increments))
    m = frame if frame is not None else manuscript_frame(df)
    y = m[label_col].astype(float).to_numpy()
    terms: list[tuple[str, str, str, pd.Series]] = []  # group, label, increment, values

    def flag(group: str, label: str, mask: pd.Series | None):
        if mask is not None:
            terms.append((group, label, "Yes vs. no", mask.astype(float)))

    def num(group: str, label: str, s: pd.Series | None):
        if s is not None:
            terms.append((group, label, "Per 1 SD", pd.to_numeric(s, errors="coerce")))

    g = "Demographics"
    for lv in _AGE[1:]:
        flag(g, lv, (m["m_age"] == lv) if "m_age" in m else None)
    flag(g, "Sex: Male", (m["m_sex"] == "Male") if "m_sex" in m else None)
    flag(g, "Ethnicity: Hispanic", m["m_hispanic"] if "m_hispanic" in m else None)
    for lv in _RACE[1:]:
        flag(g, lv, (m["m_race"] == lv) if "m_race" in m else None)
    for lv in _SDI[1:] + (["SDI: Missing / Unknown"] if sdi_missing_term else []):
        flag(g, lv.replace("SDI: Missing / Unknown", "SDI Quintile: Missing / Unknown"), (m["m_sdi"] == lv) if "m_sdi" in m else None)
    for lv in _RUCA[1:]:
        flag(g, lv, (m["m_ruca"] == lv) if "m_ruca" in m else None)
    g = "Encounter Information"
    ip, allp = _col(m, "prior_ip_os_ed_count"), _col(m, "prior_encounters_all_count")
    num(g, "Prior Inpatient / Obs / ED Count", ip)
    if ip is not None and allp is not None:
        num(g, "Prior Outpatient / Other Encounter Count", allp - ip)
    num(g, "Number of Secondary Diagnosis CCS Categories", _col(m, "n_secondary_dx_ccs"))
    num(g, "Index Admission Length of Stay (days)", _col(m, "index_los_days", "los_days"))
    num(g, "Days Since Prior Encounter", _col(m, "days_since_prior_encounter"))
    num(g, "Days Since Prior Inpatient / Obs / ED Encounter", _col(m, "days_since_prior_ip_ed"))
    num(g, "Prior Readmissions in Past 12 Months", _col(m, "readmission_count_twelve_months"))
    for lv in _DISC[1:]:
        flag(g, "Discharge Status: " + lv, (m["m_disch"] == lv) if "m_disch" in m else None)
    g = "Vital Signs"
    num(g, "Systolic Blood Pressure (mmHg)", _col(m, "vital_systolic_bp", "systolic_bp"))
    num(g, "Diastolic Blood Pressure (mmHg)", _col(m, "vital_diastolic_bp", "diastolic_bp"))
    g = "Laboratory values"
    for names, lab in MANUSCRIPT_LABS:
        num(g, lab, _col(m, *names))
    g = "Other clinical features"
    for lv in _TOB[1:]:
        flag(g, lv, (m["m_tobacco"] == lv) if "m_tobacco" in m else None)
    for lv in [_BMI[0], _BMI[2], _BMI[3], _BMI[4]]:
        flag(g, lv, (m["m_bmi"] == lv) if "m_bmi" in m else None)
    for c, lab in _MEDS:
        flag(g, lab, _col(m, c))
    for names, lab in MANUSCRIPT_ELIXHAUSER:
        flag(g, lab, _col(m, *names))

    cols = {}
    sd: dict[str, float] = {}
    for _, label, inc, s in terms:
        s = s.fillna(s.median()) if s.isna().any() else s
        if s.nunique() < 2:
            continue
        sd[label] = float(s.std(ddof=0))
        cols[label] = ((s - s.mean()) / s.std(ddof=0)).to_numpy() if inc == "Per 1 SD" else s.to_numpy(float)
    A = np.column_stack([np.ones(len(y))] + list(cols.values()))
    beta, se = _newton_logit(A, y)
    from scipy import stats

    res = {lab: (b, e) for lab, b, e in zip(cols, beta[1:], se[1:])}
    out: list[dict[str, Any]] = []
    last = None
    for group, label, inc, _ in terms:
        if label not in res:
            continue
        if group != last:
            out.append({"Feature": group, "Increment": None, "Adjusted OR (95% CI)*": None, "Standardized Beta": None, "P-Value": None})
            last = group
        b, e = res[label]
        sdv = 1.0 if inc == "Per 1 SD" else sd[label]
        p = 2 * (1 - stats.norm.cdf(abs(b / e))) if e > 0 else np.nan
        beta_std = round(float(b * sdv), 2)  # standardised beta is per SD whichever increment the OR is shown in
        shown = inc
        if inc == "Per 1 SD" and label in inc_map:  # same fit, rescaled from 1 SD to the clinical unit
            shown, size = inc_map[label]
            b, e = b * size / sd[label], e * size / sd[label]
        out.append({"Feature": label, "Increment": shown, "Adjusted OR (95% CI)*": f"{np.exp(b):.2f} ({np.exp(b - 1.96 * e):.2f}{EN}{np.exp(b + 1.96 * e):.2f})",
                    "Standardized Beta": beta_std, "P-Value": "<0.001" if p < 0.001 else f"{p:.3f}"})
    return pd.DataFrame(out)


# ------------------------------------------------------------------ workbook
def manuscript_workbook(df: pd.DataFrame, tables: Mapping[str, pd.DataFrame], path: str | Path, *, attrition: pd.DataFrame | None = None,
                        site_labels: Mapping[str, str] | None = None, label_col: str = "readmitted_30d", min_cell: int | None = 11,
                        title: str = "Manuscript tables", table5: str = "manuscript", sdi_missing_term: bool = True,
                        table6_increments: str | Mapping[str, tuple[str, float]] = "sd",
                        sheet_site_labels: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Builds the manuscript sheets from the ``tables`` returned by :func:`omop_etl.study_report.build_study_tables` and the feature table
    ``df``, and writes one workbook. ``attrition`` is the per-site attrition (``site``, ``step_name``, ``subjects_retained``) for
    Table S3. ``table5``: ``"manuscript"`` (default; AUC-ROC with CI, uncalibrated Brier, sensitivity and specificity at the 0.5 threshold of
    the balanced-trained model, as the manuscript reports) or ``"calibrated"`` (prior-shift calibrated Brier, top-10% sensitivity and PPV).
    ``sdi_missing_term``: keep an "SDI Quintile: Missing / Unknown" term in Table 6 (default; ``False`` leaves it out, as the manuscript did,
    so those stays join the reference quintile). ``table6_increments``: ``"sd"`` or ``"clinical"``, see :func:`manuscript_regression`. ``sheet_site_labels``: ``{"Table S1": {label: other label}, "Table S3": {...}}`` relabels the sites of those sheets only (the delivered workbook labels the sites one way in Table 5 and another in Tables S1 and S3; the default uses one labelling throughout). Returns ``{"sheets": {sheet name: DataFrame}, "path": Path}``."""
    m = manuscript_frame(df)
    n = len(df)
    sheets: dict[str, tuple[str, pd.DataFrame]] = {}
    sheets["Table 1 - Descriptive"] = (f"Table 1: Baseline Descriptive Characteristics at the Randomly Drawn Index Stay (N = {n:,})",
                                      manuscript_table1(df, label_col=label_col, min_cell=min_cell, frame=m))
    if "Model_Performance" in tables:
        sheets["Table 2 - Model Performance"] = ("Table 2: Primary Benchmark Model Performance (50/50 Balanced Training, F1-Tuned, 5-Fold CV)", tables["Model_Performance"])
    if "Tuning_AUC_vs_F1" in tables:
        sheets["Table 3 - Tuning AUC vs F1"] = ("Table 3: Hyperparameter Selection Comparison: Fixed Benchmark vs. Tuned for AUC-ROC vs. Tuned for F1", tables["Tuning_AUC_vs_F1"])
    if "By_Stay_Type_AUC" in tables:
        t = tables["By_Stay_Type_AUC"]
        auc_col = [c for c in t.columns if c.endswith("AUC")][0]
        sheets["Table 4 - Stay Type Decomp"] = ("Table 4: Cohort Partition by Whether the Drawn Stay Was the Patient's First Admission (Simpson's Paradox)", pd.DataFrame({
            "Stay drawn for the patient": t["Subset"], "Patients (N)": t["Stays"], "Share of patients": (100 * t["Share"]).map("{:.1f}%".format),
            "30-Day Readmission Rate": (100 * t["Readmit rate"]).map("{:.2f}%".format), auc_col.replace("AUC", "AUC-ROC"): t[auc_col].round(3)}))
    if "LOSO_By_Site" in tables:
        if table5 not in ("manuscript", "calibrated"):
            raise ValueError("table5 must be 'manuscript' or 'calibrated'.")
        t = tables["LOSO_By_Site"]
        base = {"Held-out site": t["site"], "Test patients": t["n"].map("{:,}".format),
                "Training patients (other systems)": (t["n"].sum() - t["n"]).map("{:,}".format),
                "AUC-ROC (95% CI)": [f"{a:.3f} ({l:.3f}{EN}{h:.3f})" for a, l, h in zip(t["auroc"], t["auroc_lo"], t["auroc_hi"])]}
        if table5 == "manuscript" and "sensitivity_050" in t:
            cols = [("Brier", "brier_uncalibrated"), ("Sensitivity", "sensitivity_050"), ("Specificity", "specificity_050")]
        else:
            cols = [("Brier", "brier"), ("Top-10% sensitivity", "top10_sensitivity"), ("Top-10% PPV", "top10_ppv")]
        lo = pd.DataFrame({**base, **{name: t[src].round(3) for name, src in cols}})
        mean_row = [(f"Mean of {_WORDS.get(len(t), len(t))} sites", None, None, f"{t['auroc'].mean():.3f}")] + [round(float(t[src].mean()), 3) for _, src in cols]
        lo.loc[len(lo)] = [x for part in mean_row for x in (part if isinstance(part, tuple) else (part,))]
        sheets["Table 5 - Cross-Site LOSO"] = (f"Table 5: Harmonized Leave-One-Site-Out (LOSO) Cross-Site External Validation Across {len(t)} Health Systems", lo)
    sheets["Table 6 - Multivariable LR"] = ("Table 6: Multivariable Logistic Regression Independent Risk Factors for 30-Day Readmission",
                                           manuscript_regression(df, label_col=label_col, frame=m, sdi_missing_term=sdi_missing_term, increments=table6_increments))
    if "LOS_Distribution_By_Site" in tables:
        s1 = tables["LOS_Distribution_By_Site"].copy()
        if "Table S1" in (sheet_site_labels or {}):
            s1[s1.columns[0]] = s1[s1.columns[0]].map(lambda x: sheet_site_labels["Table S1"].get(x, x))
        sheets["Table S1 - LOS Distribution"] = ("Table S1: Distribution of Index Stay Length of Stay (Days) by Participating Health System", s1)
    sheets["Table S2 - Data Completeness"] = ("Table S2: Missingness and Measurement Completeness Across Clinical Features",
                                              manuscript_completeness(df, label_col=label_col, min_cell=min_cell, frame=m))
    if attrition is not None:
        sheets["Table S3 - CONSORT Attrition"] = ("Table S3: Multi-Center CONSORT Stepwise Cohort Attrition and Eligibility Accounting",
                                                  manuscript_consort(attrition, site_labels=(
                                                      {k: sheet_site_labels["Table S3"].get(v, v) for k, v in site_labels.items()}
                                                      if site_labels and "Table S3" in (sheet_site_labels or {}) else site_labels)))
    if "Tuning_All_Configs" in tables:
        sheets["Table S4 - All Tuning Grid"] = ("Table S4: Full Grid Evaluation Metrics for All Evaluated Hyperparameter Configurations", tables["Tuning_All_Configs"])
    out = export_workbook([(name, cap, t) for name, (cap, t) in sheets.items()], path, title=title, subtitle=f"N = {n:,}; aggregate results only")
    return {"sheets": {k: v[1] for k, v in sheets.items()}, "path": out}


# ------------------------------------------------------------------ comparison
def _numbers(v: Any) -> list[float]:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return []
    if isinstance(v, (int, float, np.number)):
        return [float(v)]
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))]


def compare_workbooks(new: str | Path, old: str | Path, *, key_columns: Mapping[str, int] | None = None, atol: float = 0.01,
                      rtol: float = 0.02, header_row: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compares two workbooks in the layout of :func:`manuscript_workbook` (title row, blank row, header row, then the table).

    Sheets are paired by name; rows by their first ``key_columns[sheet]`` columns (default 1; a repeated key is numbered); cells by the
    numbers they contain (``"0.731 (0.728-0.735)"`` is three numbers). A number differs when it is off by more than both ``atol`` and
    ``rtol`` of the old value. Returns ``(summary, differences)``: per sheet the rows only in one workbook, the cells compared and
    the cells that differ; and every differing cell."""
    kc = dict(key_columns or {})
    kc.setdefault("Table 3 - Tuning AUC vs F1", 2)
    kc.setdefault("Table S4 - All Tuning Grid", 2)
    kc.setdefault("Table S3 - CONSORT Attrition", 2)
    a_all, b_all = pd.read_excel(new, sheet_name=None, header=None), pd.read_excel(old, sheet_name=None, header=None)
    summ, diffs = [], []

    def tidy(raw: pd.DataFrame, n_key: int) -> pd.DataFrame:
        hdr = raw.iloc[header_row].astype(str).tolist()
        t = raw.iloc[header_row + 1:].reset_index(drop=True)
        t.columns = hdr
        key = t.iloc[:, :n_key].astype(str).agg(" | ".join, axis=1)
        key = key + key.groupby(key).cumcount().map(lambda i: "" if i == 0 else f" #{i + 1}")
        return t.set_index(key)

    for sheet in b_all:
        if sheet == "Manifest & Index" or sheet == "Manifest":
            continue
        if sheet not in a_all:
            summ.append({"sheet": sheet, "rows_new": None, "rows_old": None, "only_new": None, "only_old": None, "cells": 0, "differ": None, "note": "missing in new"})
            continue
        n_key = kc.get(sheet, 1)
        A, B = tidy(a_all[sheet], n_key), tidy(b_all[sheet], n_key)
        common = [k for k in B.index if k in A.index]
        cols = [c for c in B.columns[n_key:] if c in A.columns]
        cells = differ = 0
        for k in common:
            for c in cols:
                x, y = _numbers(A.at[k, c]), _numbers(B.at[k, c])
                if not x and not y:
                    continue
                cells += 1
                bad = len(x) != len(y) or any(abs(p - q) > atol and abs(p - q) > rtol * abs(q) for p, q in zip(x, y))
                if bad:
                    differ += 1
                    diffs.append({"sheet": sheet, "row": k, "column": c, "new": A.at[k, c], "old": B.at[k, c]})
        summ.append({"sheet": sheet, "rows_new": len(A), "rows_old": len(B), "only_new": len(A.index.difference(B.index)),
                     "only_old": len(B.index.difference(A.index)), "cells": cells, "differ": differ, "note": ""})
    for sheet in a_all:
        if sheet not in b_all and not sheet.startswith("Manifest"):
            summ.append({"sheet": sheet, "rows_new": None, "rows_old": None, "only_new": None, "only_old": None, "cells": 0, "differ": None, "note": "missing in old"})
    return pd.DataFrame(summ), pd.DataFrame(diffs, columns=["sheet", "row", "column", "new", "old"])


def workbook_deltas(new: str | Path, old: str | Path, **kw: Any) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The deltas between two manuscript workbooks, ready to read or sort. Returns ``(cells, rows)``.

    ``cells``: every differing cell of :func:`compare_workbooks` (same tolerances; pass ``atol``, ``rtol``, ``key_columns`` through
    ``kw``) with ``new_value`` / ``old_value`` (the first number in the cell), ``delta`` (new - old), ``rel_delta`` (delta / |old|) and
    ``kind`` (``"numeric"``, ``"text"`` for labels, ``"shape"`` when the cells hold a different count of numbers), largest ``|rel_delta|``
    first. ``rows``: the rows present in only one workbook (``side`` is ``"only_new"`` or ``"only_old"``), which is where an extra
    attrition step or a missing model term shows up."""
    summary, diffs = compare_workbooks(new, old, **kw)
    rows_out: list[dict[str, Any]] = []
    for r in diffs.itertuples(index=False):
        x, y = _numbers(r.new), _numbers(r.old)
        if x and y and len(x) == len(y):
            kind, d = "numeric", x[0] - y[0]
            rel = d / abs(y[0]) if y[0] else float("nan")
            rows_out.append({"sheet": r.sheet, "row": r.row, "column": r.column, "kind": kind, "new_value": x[0], "old_value": y[0],
                             "delta": d, "rel_delta": rel, "new": r.new, "old": r.old})
        else:
            rows_out.append({"sheet": r.sheet, "row": r.row, "column": r.column, "kind": "shape" if (x or y) else "text", "new_value": None,
                             "old_value": None, "delta": None, "rel_delta": None, "new": r.new, "old": r.old})
    cells = pd.DataFrame(rows_out, columns=["sheet", "row", "column", "kind", "new_value", "old_value", "delta", "rel_delta", "new", "old"])
    if len(cells):
        cells = cells.assign(_m=cells["rel_delta"].abs().fillna(-1)).sort_values("_m", ascending=False).drop(columns="_m").reset_index(drop=True)
    kc = dict(kw.get("key_columns") or {})
    for name in ("Table 3 - Tuning AUC vs F1", "Table S4 - All Tuning Grid", "Table S3 - CONSORT Attrition"):
        kc.setdefault(name, 2)
    a_all, b_all = pd.read_excel(new, sheet_name=None, header=None), pd.read_excel(old, sheet_name=None, header=None)
    hdr = kw.get("header_row", 2)
    extra: list[dict[str, Any]] = []
    for sheet in b_all:
        if sheet.startswith("Manifest") or sheet not in a_all:
            continue
        n = kc.get(sheet, 1)

        def keys(raw: pd.DataFrame) -> list[str]:
            t = raw.iloc[hdr + 1:]
            kk = t.iloc[:, :n].astype(str).agg(" | ".join, axis=1)
            return (kk + kk.groupby(kk).cumcount().map(lambda i: "" if i == 0 else f" #{i + 1}")).tolist()

        ka, kb = keys(a_all[sheet]), keys(b_all[sheet])
        extra += [{"sheet": sheet, "row": r, "side": "only_new"} for r in ka if r not in set(kb)]
        extra += [{"sheet": sheet, "row": r, "side": "only_old"} for r in kb if r not in set(ka)]
    return cells, pd.DataFrame(extra, columns=["sheet", "row", "side"])


def delta_summary(cells: pd.DataFrame) -> pd.DataFrame:
    """Per sheet: how many cells moved, how many are numeric, and the median and largest relative change (from :func:`workbook_deltas`)."""
    if cells.empty:
        return pd.DataFrame(columns=["sheet", "cells", "numeric", "median_abs_rel", "max_abs_rel"])
    g = cells.assign(a=cells["rel_delta"].abs()).groupby("sheet", sort=False)
    return pd.DataFrame({"cells": g.size(), "numeric": g["kind"].apply(lambda s: int((s == "numeric").sum())),
                         "median_abs_rel": g["a"].median().round(4), "max_abs_rel": g["a"].max().round(4)}).reset_index()


#: What "similar to the delivered manuscript" means, as checks a run must pass. These are proposals to be agreed with the study
#: lead, not a published standard; pass your own ``tolerances`` to :func:`workbook_similarity` to change them.
SIMILARITY_TOLERANCES: dict[str, float] = {
    "table2_auc_abs": 0.01,        # every Table 2 AUC within this of the delivered value
    "table5_auc_abs": 0.02,        # every Table 5 per-site AUC within this (sites paired by label)
    "table6_or_rel": 0.10,         # Table 6 odds ratio within this relative difference ...
    "table6_or_share": 0.95,       # ... for at least this share of rows
    "table6_sig_share": 0.95,      # share of rows whose p < 0.05 verdict agrees
    "table6_dir_share": 0.95,      # share of rows (old OR outside 0.97-1.03) whose direction agrees
    "table1_rel": 0.05,            # Table 1 value within this relative difference ...
    "table1_share": 0.90,          # ... for at least this share of numeric cells
}


def _first_number(v: Any) -> float | None:
    n = _numbers(v)
    return n[0] if n else None


def _p_value(v: Any) -> float | None:
    s = str(v).strip()
    if s.startswith("<"):
        return float(s[1:]) / 2 if _numbers(s[1:]) else None
    return _first_number(s)


def _sheet(path: str | Path, name: str, header_row: int = 2) -> pd.DataFrame | None:
    try:
        raw = pd.read_excel(path, sheet_name=name, header=None)
    except ValueError:
        return None
    t = raw.iloc[header_row + 1:].reset_index(drop=True)
    t.columns = raw.iloc[header_row].astype(str).tolist()
    return t.set_index(t.columns[0])


def workbook_similarity(new: str | Path, old: str | Path, *, tolerances: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Checks a new manuscript workbook against a delivered one with explicit, named tolerances (:data:`SIMILARITY_TOLERANCES`).

    Unlike :func:`compare_workbooks`, which counts every cell that moved, this asks whether the conclusions are the same: Table 2 AUCs,
    Table 5 per-site AUCs (rows paired by site label, so pass ``site_labels`` to ``run_study`` to match the delivered convention), the Table 6 odds ratios, their direction and their p < 0.05 verdicts, and the share of
    Table 1 cells that agree. Returns one row per check with the number of items, how many passed, the observed worst case, the
    threshold and ``passed``."""
    tol = {**SIMILARITY_TOLERANCES, **dict(tolerances or {})}
    out: list[dict[str, Any]] = []

    def add(check: str, n: int, ok: int, worst: float | None, threshold: float, need_all: bool) -> None:
        share = ok / n if n else float("nan")
        passed = bool(n) and (ok == n if need_all else share >= threshold)
        out.append({"check": check, "items": n, "within": ok, "share": round(share, 3) if n else None,
                    "worst": None if worst is None else round(worst, 4), "threshold": threshold, "passed": passed})

    def pairs(sheet: str, col: str):
        a, b = _sheet(new, sheet), _sheet(old, sheet)
        if a is None or b is None or col not in a.columns or col not in b.columns:
            return []
        keep = [k for k in b.index if k in a.index and not str(k).startswith("Mean")]
        return [(k, _first_number(a.at[k, col]), _first_number(b.at[k, col])) for k in keep
                if _first_number(a.at[k, col]) is not None and _first_number(b.at[k, col]) is not None]

    d2 = [abs(x - y) for _, x, y in pairs("Table 2 - Model Performance", "AUC-ROC (95% CI)")]
    add("Table 2 AUC within abs tolerance", len(d2), sum(d <= tol["table2_auc_abs"] for d in d2), max(d2, default=None), tol["table2_auc_abs"], True)
    d5 = [abs(x - y) for _, x, y in pairs("Table 5 - Cross-Site LOSO", "AUC-ROC (95% CI)")]
    add("Table 5 per-site AUC within abs tolerance", len(d5), sum(d <= tol["table5_auc_abs"] for d in d5), max(d5, default=None), tol["table5_auc_abs"], True)
    a6, b6 = _sheet(new, "Table 6 - Multivariable LR"), _sheet(old, "Table 6 - Multivariable LR")
    if a6 is not None and b6 is not None:
        orc, pc = "Adjusted OR (95% CI)*", "P-Value"
        rows = [k for k in b6.index if k in a6.index and _first_number(a6.at[k, orc]) is not None and _first_number(b6.at[k, orc]) is not None]
        rel = [abs(_first_number(a6.at[k, orc]) / _first_number(b6.at[k, orc]) - 1) for k in rows]
        add("Table 6 odds ratio within relative tolerance", len(rows), sum(r <= tol["table6_or_rel"] for r in rel), max(rel, default=None), tol["table6_or_share"], False)
        sig = [(_p_value(a6.at[k, pc]) < 0.05) == (_p_value(b6.at[k, pc]) < 0.05) for k in rows
               if _p_value(a6.at[k, pc]) is not None and _p_value(b6.at[k, pc]) is not None]
        add("Table 6 p < 0.05 verdict agrees", len(sig), sum(sig), None, tol["table6_sig_share"], False)
        far = [k for k in rows if not 0.97 <= _first_number(b6.at[k, orc]) <= 1.03]
        dr = [(_first_number(a6.at[k, orc]) > 1) == (_first_number(b6.at[k, orc]) > 1) for k in far]
        add("Table 6 direction agrees (old OR outside 0.97-1.03)", len(dr), sum(dr), None, tol["table6_dir_share"], False)
        add("Table 6 rows missing from the new workbook", len(b6.index), len(b6.index) - len(b6.index.difference(a6.index)), None, 1.0, True)
    a1, b1 = _sheet(new, "Table 1 - Descriptive"), _sheet(old, "Table 1 - Descriptive")
    if a1 is not None and b1 is not None:
        cells = [(x, y) for k in b1.index if k in a1.index for c in b1.columns if c in a1.columns
                 for x, y in [(_first_number(a1.at[k, c]), _first_number(b1.at[k, c]))] if x is not None and y is not None]
        ok = [abs(x - y) <= tol["table1_rel"] * max(abs(y), 1e-9) or abs(x - y) <= 0.01 for x, y in cells]
        add("Table 1 cells within relative tolerance", len(cells), sum(ok), None, tol["table1_share"], False)
    return pd.DataFrame(out)
