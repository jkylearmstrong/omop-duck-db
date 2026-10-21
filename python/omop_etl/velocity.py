"""Clinical-velocity feature set (FR-A4): utilization trajectory and bedside ratios, leak-free by construction.

:func:`extract_velocity_features` returns one row per distinct cohort (``subject_id``, ``cohort_start_date``) with:

======================== ======================================================================================
``ip_velocity``          prior inpatient visits per 30 days over the look-back window
``ed_velocity``          prior emergency visits per 30 days over the look-back window
``recent_ip_ratio``      inpatient visits in the last ``recent_days`` / inpatient visits in the window (0 if none)
``readmission_intensity`` prior ``readmit_gap_days`` readmissions / prior inpatient visits (0 if none)
``days_since_last_ip``   days from the last prior inpatient discharge to admission, :data:`NO_PRIOR_VISIT_DAYS` if none
``days_since_last_ed``   same for emergency visits
``shock_index``          heart rate / systolic BP (first admission-day values; NULL if either is missing or SBP <= 0)
``bun_to_creatinine``    BUN / creatinine (first admission-day values; NULL if either is missing or creatinine <= 0)
``lace_proxy``           L + A + E of LACE (length of stay, acuity, ED visits in 6 months), without the Charlson term
======================== ======================================================================================

Everything uses visits strictly before the admission date, so nothing from the index stay leaks. The 365-day sentinel for
"no prior visit" is :data:`NO_PRIOR_VISIT_DAYS` (``COALESCE(date_diff('day', last_visit_end, admission), 365)``).
The look-back counts and the sentinel are independent: a patient with no prior visit gets velocity 0 and sentinel 365.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb
import pandas as pd

__all__ = ["NO_PRIOR_VISIT_DAYS", "VELOCITY_FEATURES", "VELOCITY_CONCEPTS", "extract_velocity_features"]

NO_PRIOR_VISIT_DAYS = 365
VELOCITY_FEATURES = ["ip_velocity", "ed_velocity", "recent_ip_ratio", "readmission_intensity", "days_since_last_ip",
                     "days_since_last_ed", "shock_index", "bun_to_creatinine", "lace_proxy"]
# Standard OMOP measurement concepts (LOINC): heart rate, systolic BP, BUN, serum creatinine.
VELOCITY_CONCEPTS = {"heart_rate": (3027018,), "systolic_bp": (3004249,), "bun": (3013682,), "creatinine": (3016723,)}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _ids(values) -> str:
    return ", ".join(str(int(v)) for v in values)


def extract_velocity_features(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    *,
    lookback_days: int = 365,
    recent_days: int = 90,
    readmit_gap_days: int = 30,
    ip_concepts=(9201, 262),
    ed_concepts=(9203, 262),
    concepts: dict[str, Any] | None = None,
    cohort_id: int | None = None,
) -> pd.DataFrame:
    """See module docstring. ``concepts`` overrides :data:`VELOCITY_CONCEPTS` (keys heart_rate, systolic_bp, bun, creatinine)."""
    if not _IDENT.match(cohort_table):
        raise ValueError(f"Invalid cohort_table {cohort_table!r}.")
    lb, rc, gap = int(lookback_days), int(recent_days), int(readmit_gap_days)
    if lb < 1 or rc < 1 or rc > lb or gap < 1:
        raise ValueError("Need lookback_days >= recent_days >= 1 and readmit_gap_days >= 1.")
    cc = {**VELOCITY_CONCEPTS, **(concepts or {})}
    cols = {r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_table}").fetchall()}
    if not {"subject_id", "cohort_start_date"} <= cols:
        raise ValueError("cohort_table needs subject_id and cohort_start_date.")
    where = f"WHERE cohort_definition_id = {int(cohort_id)}" if cohort_id is not None and "cohort_definition_id" in cols else ""
    end_expr = "cohort_end_date" if "cohort_end_date" in cols else "cohort_start_date"
    ip, ed = _ids(ip_concepts), _ids(ed_concepts)
    sent = NO_PRIOR_VISIT_DAYS
    sql = f"""
    WITH c AS (
        SELECT subject_id, cohort_start_date, max({end_expr}) AS cohort_end_date
        FROM {cohort_table} {where} GROUP BY subject_id, cohort_start_date
    ),
    v AS (
        SELECT c.subject_id, c.cohort_start_date,
               v.visit_concept_id AS vc, v.visit_start_date AS vs,
               COALESCE(v.visit_end_date, v.visit_start_date) AS ve
        FROM c JOIN visit_occurrence v ON v.person_id = c.subject_id
        WHERE v.visit_start_date < c.cohort_start_date
          AND v.visit_start_date >= c.cohort_start_date - INTERVAL ({lb}) DAY
    ),
    ipv AS (SELECT * FROM v WHERE vc IN ({ip})),
    edv AS (SELECT * FROM v WHERE vc IN ({ed})),
    ipa AS (
        SELECT subject_id, cohort_start_date, count(*) AS n_ip,
               count(*) FILTER (WHERE vs >= cohort_start_date - INTERVAL ({rc}) DAY) AS n_ip_recent,
               max(ve) AS last_ip_end
        FROM ipv GROUP BY 1, 2
    ),
    eda AS (
        SELECT subject_id, cohort_start_date, count(*) AS n_ed, max(ve) AS last_ed_end,
               count(*) FILTER (WHERE vs >= cohort_start_date - INTERVAL (183) DAY) AS n_ed_6m
        FROM edv GROUP BY 1, 2
    ),
    readm AS (  -- prior IP visits starting within `gap` days after the discharge of an earlier IP visit (any earlier date)
        SELECT i.subject_id, i.cohort_start_date, count(DISTINCT i.vs) AS n_readmit
        FROM ipv i
        JOIN visit_occurrence p ON p.person_id = i.subject_id AND p.visit_concept_id IN ({ip})
         AND COALESCE(p.visit_end_date, p.visit_start_date) < i.vs
         AND i.vs <= COALESCE(p.visit_end_date, p.visit_start_date) + INTERVAL ({gap}) DAY
        GROUP BY 1, 2
    ),
    m AS (
        SELECT c.subject_id, c.cohort_start_date,
               arg_min(CASE WHEN m.measurement_concept_id IN ({_ids(cc['heart_rate'])}) THEN m.value_as_number END,
                       CASE WHEN m.measurement_concept_id IN ({_ids(cc['heart_rate'])}) AND m.value_as_number IS NOT NULL
                            THEN m.measurement_id END) AS hr,
               arg_min(CASE WHEN m.measurement_concept_id IN ({_ids(cc['systolic_bp'])}) THEN m.value_as_number END,
                       CASE WHEN m.measurement_concept_id IN ({_ids(cc['systolic_bp'])}) AND m.value_as_number IS NOT NULL
                            THEN m.measurement_id END) AS sbp,
               arg_min(CASE WHEN m.measurement_concept_id IN ({_ids(cc['bun'])}) THEN m.value_as_number END,
                       CASE WHEN m.measurement_concept_id IN ({_ids(cc['bun'])}) AND m.value_as_number IS NOT NULL
                            THEN m.measurement_id END) AS bun,
               arg_min(CASE WHEN m.measurement_concept_id IN ({_ids(cc['creatinine'])}) THEN m.value_as_number END,
                       CASE WHEN m.measurement_concept_id IN ({_ids(cc['creatinine'])}) AND m.value_as_number IS NOT NULL
                            THEN m.measurement_id END) AS creat
        FROM c JOIN measurement m ON m.person_id = c.subject_id AND m.measurement_date = c.cohort_start_date
        GROUP BY 1, 2
    ),
    idx AS (
        SELECT c.subject_id, c.cohort_start_date,
               GREATEST(date_diff('day', c.cohort_start_date, c.cohort_end_date), 0) AS los,
               EXISTS (SELECT 1 FROM visit_occurrence x WHERE x.person_id = c.subject_id
                       AND x.visit_start_date = c.cohort_start_date AND x.visit_concept_id IN ({ed})) AS emergency
        FROM c
    )
    SELECT c.subject_id, c.cohort_start_date,
           COALESCE(ipa.n_ip, 0) * 30.0 / {lb}                                  AS ip_velocity,
           COALESCE(eda.n_ed, 0) * 30.0 / {lb}                                  AS ed_velocity,
           CASE WHEN COALESCE(ipa.n_ip, 0) = 0 THEN 0.0 ELSE ipa.n_ip_recent * 1.0 / ipa.n_ip END AS recent_ip_ratio,
           CASE WHEN COALESCE(ipa.n_ip, 0) = 0 THEN 0.0 ELSE COALESCE(readm.n_readmit, 0) * 1.0 / ipa.n_ip END
                                                                                AS readmission_intensity,
           COALESCE(date_diff('day', ipa.last_ip_end, c.cohort_start_date), {sent}) AS days_since_last_ip,
           COALESCE(date_diff('day', eda.last_ed_end, c.cohort_start_date), {sent}) AS days_since_last_ed,
           CASE WHEN m.sbp > 0 AND m.hr IS NOT NULL THEN m.hr / m.sbp END       AS shock_index,
           CASE WHEN m.creat > 0 AND m.bun IS NOT NULL THEN m.bun / m.creat END AS bun_to_creatinine,
           (CASE WHEN idx.los < 1 THEN 0 WHEN idx.los < 4 THEN idx.los WHEN idx.los < 7 THEN 4
                 WHEN idx.los < 14 THEN 5 ELSE 7 END)
           + (CASE WHEN idx.emergency THEN 3 ELSE 0 END)
           + LEAST(COALESCE(eda.n_ed_6m, 0), 4)                                 AS lace_proxy
    FROM c
    LEFT JOIN ipa  USING (subject_id, cohort_start_date)
    LEFT JOIN eda  USING (subject_id, cohort_start_date)
    LEFT JOIN readm USING (subject_id, cohort_start_date)
    LEFT JOIN m    USING (subject_id, cohort_start_date)
    LEFT JOIN idx  USING (subject_id, cohort_start_date)
    ORDER BY c.subject_id, c.cohort_start_date
    """
    return con.execute(sql).df()
