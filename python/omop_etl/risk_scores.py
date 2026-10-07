"""Bedside Clinical Risk Score Calculators for OMOP CDM cohorts.

Calculates standardized clinical risk scores directly in DuckDB:
- LACE Index (30-day readmission/mortality risk: Length of stay, Acuity, Charlson, ED visits)
- HOSPITAL Score (30-day readmission risk: Hemoglobin, Oncology, Sodium, Procedure, Index acuity, prior Admissions, LOS)
- SOFA Score (Sequential Organ Failure Assessment across 6 organ systems)
- CHA2DS2-VASc (Thromboembolism / stroke risk in cardiovascular disease)
"""

from __future__ import annotations

from typing import Any, Sequence
import duckdb
import pandas as pd

from omop_etl.comorbidity import extract_charlson_index


def calculate_bedside_scores(
    con: duckdb.DuckDBPyConnection,
    cohort_table: str = "cohort",
    scores: Sequence[str] = ("lace", "hospital", "chads_vasc", "sofa"),
) -> pd.DataFrame:
    """Calculate clinical bedside risk scores for an index cohort in DuckDB.

    Args:
        con: Active DuckDB connection with populated OMOP CDM tables.
        cohort_table: Name of cohort table or view. Must contain subject_id (or person_id),
            cohort_start_date, cohort_end_date.
        scores: Sequence of scores to compute: 'lace', 'hospital', 'chads_vasc', 'sofa'.

    Returns:
        pd.DataFrame: DataFrame containing subject_id, cohort_start_date, and score columns.
    """
    scores_lower = {s.lower().replace("-", "_").replace("2", "_") for s in scores}
    # Resolve cohort columns
    cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {cohort_table} LIMIT 0;").fetchall()]
    person_col = "subject_id" if "subject_id" in cols else "person_id"
    start_col = "cohort_start_date" if "cohort_start_date" in cols else "visit_start_date"
    end_col = "cohort_end_date" if "cohort_end_date" in cols else "visit_end_date"

    # Base cohort dataframe
    base_df = con.execute(f"""
        SELECT 
            {person_col} AS person_id,
            {start_col} AS cohort_start_date,
            {end_col} AS cohort_end_date,
            CAST(GREATEST(1, date_diff('day', {start_col}, {end_col})) AS INTEGER) AS los_days
        FROM {cohort_table}
    """).df()

    if len(base_df) == 0:
        res = base_df.copy()
        if "lace" in scores_lower:
            res["lace_score"] = []
            res["lace_risk"] = []
        if "hospital" in scores_lower:
            res["hospital_score"] = []
            res["hospital_risk"] = []
        if any(k in scores_lower for k in ("chads_vasc", "cha_ds_vasc", "cha2ds2_vasc")):
            res["chads_vasc_score"] = []
        if "sofa" in scores_lower:
            res["sofa_score"] = []
        return res

    # Register temporary table for joins
    con.register("_score_cohort", base_df)

    try:
        # Precompute Charlson index if lace is requested
        cci_df = None
        if "lace" in scores_lower:
            cci_df = extract_charlson_index(con, cohort_table=cohort_table)
            if "subject_id" in cci_df.columns and "person_id" not in cci_df.columns:
                cci_df = cci_df.rename(columns={"subject_id": "person_id"})
            base_df = base_df.merge(
                cci_df[["person_id", "cohort_start_date", "charlson_index"]],
                on=["person_id", "cohort_start_date"],
                how="left",
            )
            base_df["charlson_index"] = base_df["charlson_index"].fillna(0).astype(int)


        # ------------------------------------------------------------- LACE Index
        if "lace" in scores_lower:
            # Emergency department visits in prior 180 days (6 months)
            # ED concept: 9203
            ed_counts = con.execute("""
                SELECT 
                    c.person_id,
                    c.cohort_start_date,
                    COUNT(DISTINCT v.visit_occurrence_id) AS prior_ed_visits,
                    MAX(CASE WHEN v.visit_concept_id = 9203 AND v.visit_start_date = c.cohort_start_date THEN 1 ELSE 0 END) AS is_ed_acuity
                FROM _score_cohort c
                LEFT JOIN visit_occurrence v
                  ON v.person_id = c.person_id
                 AND v.visit_concept_id = 9203
                 AND v.visit_start_date >= c.cohort_start_date - INTERVAL '180' DAY
                 AND v.visit_start_date < c.cohort_start_date
                GROUP BY c.person_id, c.cohort_start_date
            """).df()

            base_df = base_df.merge(ed_counts, on=["person_id", "cohort_start_date"], how="left")
            base_df["prior_ed_visits"] = base_df["prior_ed_visits"].fillna(0).astype(int)
            base_df["is_ed_acuity"] = base_df["is_ed_acuity"].fillna(0).astype(int)

            def _lace_los(d: int) -> int:
                if d < 1: return 0
                if d == 1: return 1
                if d == 2: return 2
                if d == 3: return 3
                if 4 <= d <= 6: return 4
                if 7 <= d <= 13: return 5
                return 7

            def _lace_cci(c: int) -> int:
                if c == 0: return 0
                if c == 1: return 1
                if c == 2: return 2
                if c == 3: return 3
                return 5

            def _lace_ed(e: int) -> int:
                if e == 0: return 0
                if e == 1: return 1
                if e == 2: return 2
                if e == 3: return 3
                return 4

            l_score = base_df["los_days"].apply(_lace_los)
            a_score = base_df["is_ed_acuity"] * 3
            c_score = base_df["charlson_index"].apply(_lace_cci)
            e_score = base_df["prior_ed_visits"].apply(_lace_ed)

            base_df["lace_score"] = (l_score + a_score + c_score + e_score).astype(int)
            base_df["lace_risk"] = base_df["lace_score"].apply(
                lambda s: "Low" if s <= 4 else ("Moderate" if s <= 9 else "High")
            )

        # ------------------------------------------------------------- HOSPITAL Score
        if "hospital" in scores_lower:
            # Check labs during stay (Hemoglobin < 12, Sodium < 135)
            # Procedures during stay
            # Oncology diagnosis in prior 365 days
            # Hospital admissions in prior 365 days
            hosp_metrics = con.execute("""
                SELECT 
                    c.person_id,
                    c.cohort_start_date,
                    -- Low hemoglobin (< 12 g/dL) during stay
                    MAX(CASE 
                        WHEN m.measurement_concept_id IN (3000963, 3004501, 3010813, 3023103, 3023599) 
                             OR m.measurement_source_value ILIKE '%hemo%' 
                             OR m.measurement_source_value ILIKE '%hgb%'
                        THEN CASE WHEN m.value_as_number < 12.0 THEN 1 ELSE 0 END
                        ELSE 0 
                    END) AS low_hemoglobin,
                    -- Low sodium (< 135 mEq/L) during stay
                    MAX(CASE 
                        WHEN m.measurement_concept_id IN (3019550, 3000285, 3014576) 
                             OR m.measurement_source_value ILIKE '%sodium%'
                        THEN CASE WHEN m.value_as_number < 135.0 THEN 1 ELSE 0 END
                        ELSE 0 
                    END) AS low_sodium,
                    -- Procedure during stay
                    MAX(CASE WHEN pr.procedure_occurrence_id IS NOT NULL THEN 1 ELSE 0 END) AS had_procedure,
                    -- Oncology diagnosis in 1-year lookback (neoplasm / cancer)
                    MAX(CASE 
                        WHEN cond.condition_concept_id IN (SELECT descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id = 443392)
                             OR cond.condition_source_value ILIKE 'C%' 
                             OR cond.condition_source_value ILIKE '14%'
                             OR cond.condition_source_value ILIKE '15%'
                             OR cond.condition_source_value ILIKE '16%'
                             OR cond.condition_source_value ILIKE '17%'
                             OR cond.condition_source_value ILIKE '18%'
                             OR cond.condition_source_value ILIKE '19%'
                             OR cond.condition_source_value ILIKE '20%'
                        THEN 1 ELSE 0 
                    END) AS oncology_history,
                    -- Prior inpatient admissions in past 365 days
                    COUNT(DISTINCT prev_v.visit_occurrence_id) AS prior_admissions
                FROM _score_cohort c
                LEFT JOIN measurement m
                  ON m.person_id = c.person_id
                 AND m.measurement_date >= c.cohort_start_date
                 AND m.measurement_date <= c.cohort_end_date
                LEFT JOIN procedure_occurrence pr
                  ON pr.person_id = c.person_id
                 AND pr.procedure_date >= c.cohort_start_date
                 AND pr.procedure_date <= c.cohort_end_date
                LEFT JOIN condition_occurrence cond
                  ON cond.person_id = c.person_id
                 AND cond.condition_start_date >= c.cohort_start_date - INTERVAL '365' DAY
                 AND cond.condition_start_date <= c.cohort_start_date
                LEFT JOIN visit_occurrence prev_v
                  ON prev_v.person_id = c.person_id
                 AND prev_v.visit_concept_id = 9201
                 AND prev_v.visit_start_date >= c.cohort_start_date - INTERVAL '365' DAY
                 AND prev_v.visit_start_date < c.cohort_start_date
                GROUP BY c.person_id, c.cohort_start_date
            """).df()

            base_df = base_df.merge(hosp_metrics, on=["person_id", "cohort_start_date"], how="left")
            base_df["low_hemoglobin"] = base_df["low_hemoglobin"].fillna(0).astype(int)
            base_df["low_sodium"] = base_df["low_sodium"].fillna(0).astype(int)
            base_df["had_procedure"] = base_df["had_procedure"].fillna(0).astype(int)
            base_df["oncology_history"] = base_df["oncology_history"].fillna(0).astype(int)
            base_df["prior_admissions"] = base_df["prior_admissions"].fillna(0).astype(int)

            h_pts = base_df["low_hemoglobin"] * 1
            o_pts = base_df["oncology_history"] * 2
            s_pts = base_df["low_sodium"] * 1
            p_pts = base_df["had_procedure"] * 1
            i_pts = 1  # Inpatient index stay acuity
            t_pts = base_df["prior_admissions"].apply(lambda n: 2 if n >= 2 else (1 if n == 1 else 0))
            los_pts = base_df["los_days"].apply(lambda d: 2 if d >= 5 else 0)

            base_df["hospital_score"] = (h_pts + o_pts + s_pts + p_pts + i_pts + t_pts + los_pts).astype(int)
            base_df["hospital_risk"] = base_df["hospital_score"].apply(
                lambda s: "Low" if s <= 4 else ("Intermediate" if s <= 6 else "High")
            )

        # ------------------------------------------------------------- CHA2DS2-VASc
        if any(k in scores_lower for k in ("chads_vasc", "cha_ds_vasc", "cha2ds2_vasc")):
            chads_df = con.execute("""
                SELECT 
                    c.person_id,
                    c.cohort_start_date,
                    date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.cohort_start_date) AS age,
                    CASE WHEN p.gender_concept_id = 8532 THEN 1 ELSE 0 END AS is_female,
                    -- Congestive heart failure (1 pt)
                    MAX(CASE WHEN cond.condition_concept_id IN (316139, 434056, 433435, 4185932, 314378) 
                             OR cond.condition_source_value ILIKE 'I50%' OR cond.condition_source_value ILIKE '428%' THEN 1 ELSE 0 END) AS chf,
                    -- Hypertension (1 pt)
                    MAX(CASE WHEN cond.condition_concept_id IN (316866, 320128, 4326442) 
                             OR cond.condition_source_value ILIKE 'I10%' OR cond.condition_source_value ILIKE '401%' THEN 1 ELSE 0 END) AS ht,
                    -- Diabetes (1 pt)
                    MAX(CASE WHEN cond.condition_concept_id IN (201826, 443238, 316866) 
                             OR cond.condition_source_value ILIKE 'E11%' OR cond.condition_source_value ILIKE '250%' THEN 1 ELSE 0 END) AS dm,
                    -- Stroke / TIA / Thromboembolism (2 pts)
                    MAX(CASE WHEN cond.condition_concept_id IN (443454, 4253880, 372924, 375557, 4310564) 
                             OR cond.condition_source_value ILIKE 'I63%' OR cond.condition_source_value ILIKE 'G45%' OR cond.condition_source_value ILIKE '434%' THEN 1 ELSE 0 END) AS stroke,
                    -- Vascular disease (1 pt)
                    MAX(CASE WHEN cond.condition_concept_id IN (312327, 4329847, 4030506) 
                             OR cond.condition_source_value ILIKE 'I21%' OR cond.condition_source_value ILIKE 'I70%' OR cond.condition_source_value ILIKE '410%' THEN 1 ELSE 0 END) AS vasc
                FROM _score_cohort c
                JOIN person p ON p.person_id = c.person_id
                LEFT JOIN condition_occurrence cond
                  ON cond.person_id = c.person_id
                 AND cond.condition_start_date <= c.cohort_start_date
                GROUP BY c.person_id, c.cohort_start_date, p.year_of_birth, p.month_of_birth, p.day_of_birth, p.gender_concept_id
            """).df()

            base_df = base_df.merge(chads_df, on=["person_id", "cohort_start_date"], how="left")
            base_df["age"] = base_df["age"].fillna(60).astype(int)
            base_df["is_female"] = base_df["is_female"].fillna(0).astype(int)
            base_df["chf"] = base_df["chf"].fillna(0).astype(int)
            base_df["ht"] = base_df["ht"].fillna(0).astype(int)
            base_df["dm"] = base_df["dm"].fillna(0).astype(int)
            base_df["stroke"] = base_df["stroke"].fillna(0).astype(int)
            base_df["vasc"] = base_df["vasc"].fillna(0).astype(int)

            age_pts = base_df["age"].apply(lambda a: 2 if a >= 75 else (1 if a >= 65 else 0))
            chads_score = (
                base_df["chf"] * 1
                + base_df["ht"] * 1
                + age_pts
                + base_df["dm"] * 1
                + base_df["stroke"] * 2
                + base_df["vasc"] * 1
                + base_df["is_female"] * 1
            )
            base_df["chads_vasc_score"] = chads_score.astype(int)

        # ------------------------------------------------------------- SOFA Score
        if "sofa" in scores_lower:
            # Derives organ failure components from acute stay labs and vitals
            sofa_metrics = con.execute("""
                SELECT 
                    c.person_id,
                    c.cohort_start_date,
                    -- Platelets (< 150 -> 1, < 100 -> 2, < 50 -> 3, < 20 -> 4)
                    MIN(CASE WHEN m.measurement_concept_id IN (3024929, 3013650, 3007461) OR m.measurement_source_value ILIKE '%platelet%' THEN m.value_as_number END) AS min_platelets,
                    -- Bilirubin (1.2-1.9 -> 1, 2.0-5.9 -> 2, 6.0-11.9 -> 3, >= 12 -> 4)
                    MAX(CASE WHEN m.measurement_concept_id IN (3024128, 3017614) OR m.measurement_source_value ILIKE '%bilirubin%' THEN m.value_as_number END) AS max_bilirubin,
                    -- Creatinine (1.2-1.9 -> 1, 2.0-3.4 -> 2, 3.5-4.9 -> 3, >= 5.0 -> 4)
                    MAX(CASE WHEN m.measurement_concept_id IN (3016723, 3001802) OR m.measurement_source_value ILIKE '%creatinine%' THEN m.value_as_number END) AS max_creatinine,
                    -- Mean Arterial Pressure (MAP < 70 -> 1)
                    MIN(CASE WHEN m.measurement_concept_id IN (3027597, 21492241) OR m.measurement_source_value ILIKE '%map%' THEN m.value_as_number END) AS min_map
                FROM _score_cohort c
                LEFT JOIN measurement m
                  ON m.person_id = c.person_id
                 AND m.measurement_date >= c.cohort_start_date
                 AND m.measurement_date <= c.cohort_end_date
                GROUP BY c.person_id, c.cohort_start_date
            """).df()

            base_df = base_df.merge(sofa_metrics, on=["person_id", "cohort_start_date"], how="left")

            def _sofa_plt(p) -> int:
                if pd.isna(p) or p >= 150: return 0
                if p >= 100: return 1
                if p >= 50: return 2
                if p >= 20: return 3
                return 4

            def _sofa_bili(b) -> int:
                if pd.isna(b) or b < 1.2: return 0
                if b <= 1.9: return 1
                if b <= 5.9: return 2
                if b <= 11.9: return 3
                return 4

            def _sofa_creat(c) -> int:
                if pd.isna(c) or c < 1.2: return 0
                if c <= 1.9: return 1
                if c <= 3.4: return 2
                if c <= 4.9: return 3
                return 4

            def _sofa_map(m) -> int:
                if pd.isna(m) or m >= 70: return 0
                return 1

            s_plt = base_df["min_platelets"].apply(_sofa_plt)
            s_bili = base_df["max_bilirubin"].apply(_sofa_bili)
            s_creat = base_df["max_creatinine"].apply(_sofa_creat)
            s_map = base_df["min_map"].apply(_sofa_map)

            base_df["sofa_score"] = (s_plt + s_bili + s_creat + s_map).astype(int)

    finally:
        con.unregister("_score_cohort")

    # Select output columns
    out_cols = ["person_id", "cohort_start_date", "cohort_end_date", "los_days"]
    for col in ["lace_score", "lace_risk", "hospital_score", "hospital_risk", "chads_vasc_score", "sofa_score"]:
        if col in base_df.columns:
            out_cols.append(col)

    return base_df[out_cols]
