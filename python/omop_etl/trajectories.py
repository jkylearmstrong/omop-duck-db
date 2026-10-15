"""Longitudinal Temporal Trajectory Features (Slopes & Volatility) and Acute Episode Shifts in DuckDB.

Vectorized linear regression aggregations (regr_slope, regr_r2, rolling stddev)
for fast in-engine trajectory feature extraction over observational measurement look-back windows,
plus acute in-hospital episode extreme shift extraction (delta from admission baseline,
peak, nadir, KDIGO AKI staging, Shock Index, and glycemic volatility).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd

__all__ = ["extract_temporal_trajectories", "extract_acute_trajectories"]


def extract_temporal_trajectories(
    con: duckdb.DuckDBPyConnection | Any,
    cohort_table: str | pd.DataFrame,
    measurement_concept_ids: Sequence[int],
    window_days: int = 365,
    index_date_col: str = "cohort_start_date",
    id_col: str = "person_id",
    offset_anchor: str = "index_date",
) -> pd.DataFrame:
    """Extracts longitudinal measurement trajectory features (slopes, R^2, volatility) in DuckDB.

    Leverages DuckDB's vectorized `regr_slope(val, days)`, `regr_r2(val, days)`, and `stddev_samp(val)`
    aggregations directly on clinical measurement series within `[anchor_date - window_days, anchor_date]`.

    Args:
        con: Active DuckDB connection.
        cohort_table: Name of cohort table in DuckDB or a DataFrame containing patient index dates.
        measurement_concept_ids: Concept IDs of measurements to compute trajectories for.
        window_days: Lookback window in days prior to index date (default 365).
        index_date_col: Column indicating cohort index date (default 'cohort_start_date').
        id_col: Column identifying patient ('person_id' or 'subject_id').
        offset_anchor: Temporal anchor milestone ('index_date', 'visit_start_date', 'visit_end_date', 'start', 'end').

    Returns:
        pd.DataFrame: Wide patient-level feature matrix with columns:
            `{id_col}`, `{index_date_col}`, `traj_{cid}_slope`, `traj_{cid}_r2`,
            `traj_{cid}_volatility`, `traj_{cid}_min`, `traj_{cid}_max`, `traj_{cid}_count`.
    """
    cids = [int(c) for c in measurement_concept_ids if c is not None]
    if not cids:
        raise ValueError("At least one measurement concept ID must be specified.")

    src_name = "_cohort_traj_src"
    temp_registered = False

    if isinstance(cohort_table, pd.DataFrame):
        con.register(src_name, cohort_table)
        coh_rel = src_name
        temp_registered = True
    else:
        coh_rel = cohort_table

    try:
        # Detect actual ID and index date column names
        cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {coh_rel}").fetchall()]
        actual_id = id_col if id_col.lower() in cols else ("person_id" if "person_id" in cols else "subject_id")

        anchor_k = str(offset_anchor).lower().strip()
        if anchor_k in ("end", "visit_end_date", "discharge") and "cohort_end_date" in cols:
            actual_date = "cohort_end_date"
        elif anchor_k in ("end", "visit_end_date", "discharge") and "visit_end_date" in cols:
            actual_date = "visit_end_date"
        else:
            actual_date = (
                index_date_col
                if index_date_col.lower() in cols
                else ("cohort_start_date" if "cohort_start_date" in cols else "index_date")
            )

        cid_list_str = ", ".join(str(c) for c in cids)

        sql = f"""
        SELECT 
            c.{actual_id} AS person_id,
            c.{actual_date} AS index_date,
            m.measurement_concept_id,
            COUNT(m.value_as_number) AS n_obs,
            REGR_SLOPE(
                CAST(m.value_as_number AS DOUBLE),
                CAST(date_diff('day', c.{actual_date}, m.measurement_date) AS DOUBLE)
            ) AS slope_per_day,
            REGR_R2(
                CAST(m.value_as_number AS DOUBLE),
                CAST(date_diff('day', c.{actual_date}, m.measurement_date) AS DOUBLE)
            ) AS r2,
            STDDEV_SAMP(CAST(m.value_as_number AS DOUBLE)) AS volatility_stddev,
            MIN(CAST(m.value_as_number AS DOUBLE)) AS min_val,
            MAX(CAST(m.value_as_number AS DOUBLE)) AS max_val
        FROM {coh_rel} c
        JOIN measurement m ON c.{actual_id} = m.person_id
        WHERE m.measurement_concept_id IN ({cid_list_str})
          AND m.value_as_number IS NOT NULL
          AND date_diff('day', m.measurement_date, c.{actual_date}) BETWEEN 0 AND {int(window_days)}
        GROUP BY c.{actual_id}, c.{actual_date}, m.measurement_concept_id
        """

        long_df = con.execute(sql).fetchdf()

        # Get base distinct cohort subjects
        base_df = con.execute(f"SELECT DISTINCT {actual_id} AS person_id, {actual_date} AS index_date FROM {coh_rel}").fetchdf()

        if long_df.empty:
            out_df = base_df.copy()
            for cid in cids:
                out_df[f"traj_{cid}_slope"] = np.nan
                out_df[f"traj_{cid}_r2"] = np.nan
                out_df[f"traj_{cid}_volatility"] = np.nan
                out_df[f"traj_{cid}_min"] = np.nan
                out_df[f"traj_{cid}_max"] = np.nan
                out_df[f"traj_{cid}_count"] = 0
            if actual_id != "person_id":
                out_df.rename(columns={"person_id": actual_id}, inplace=True)
            if actual_date != "index_date":
                out_df.rename(columns={"index_date": actual_date}, inplace=True)
            return out_df

        # Pivot to wide feature matrix
        out_df = base_df.copy()
        for cid in cids:
            sub = long_df[long_df["measurement_concept_id"] == cid]
            if sub.empty:
                out_df[f"traj_{cid}_slope"] = np.nan
                out_df[f"traj_{cid}_r2"] = np.nan
                out_df[f"traj_{cid}_volatility"] = np.nan
                out_df[f"traj_{cid}_min"] = np.nan
                out_df[f"traj_{cid}_max"] = np.nan
                out_df[f"traj_{cid}_count"] = 0
            else:
                m_dict = sub.set_index(["person_id", "index_date"])
                for metric, col_name in [
                    ("slope_per_day", f"traj_{cid}_slope"),
                    ("r2", f"traj_{cid}_r2"),
                    ("volatility_stddev", f"traj_{cid}_volatility"),
                    ("min_val", f"traj_{cid}_min"),
                    ("max_val", f"traj_{cid}_max"),
                    ("n_obs", f"traj_{cid}_count"),
                ]:
                    mapped = out_df.set_index(["person_id", "index_date"]).index.map(m_dict[metric])
                    out_df[col_name] = mapped.fillna(0 if metric == "n_obs" else np.nan).values

        if actual_id != "person_id":
            out_df.rename(columns={"person_id": actual_id}, inplace=True)
        if actual_date != "index_date":
            out_df.rename(columns={"index_date": actual_date}, inplace=True)

        return out_df
    finally:
        if temp_registered:
            try:
                con.unregister(src_name)
            except Exception:
                pass


def extract_acute_trajectories(
    con: duckdb.DuckDBPyConnection | Any,
    cohort_table: str | pd.DataFrame = "cohort",
    *,
    id_col: str = "subject_id",
    visit_col: str = "visit_occurrence_id",
    visit_table: str = "visit_occurrence",
    measurement_table: str = "measurement",
    creatinine_concept_ids: Sequence[int] = (3016723, 3015632, 3020564),
    glucose_concept_ids: Sequence[int] = (3004501, 3000483),
    heart_rate_concept_ids: Sequence[int] = (3027018,),
    sbp_concept_ids: Sequence[int] = (3004249,),
) -> pd.DataFrame:
    """Extracts intra-encounter acute trajectory shifts (admission baseline, nadir, peak, KDIGO AKI, Shock Index).

    Uses pure DuckDB window aggregations over the acute hospitalization episode `[visit_start_date, visit_end_date]`
    to extract:
    - Creatinine baseline, peak, delta from admission, and KDIGO Acute Kidney Injury staging (Stage 0-3).
    - Shock Index (Heart Rate / Systolic Blood Pressure) and binary high shock indicator (> 1.0).
    - Glycemic volatility (max Glucose - min Glucose) and intra-encounter glucose peak and nadir.
    """
    src_name = "_cohort_acute_src"
    temp_registered = False

    if isinstance(cohort_table, pd.DataFrame):
        con.register(src_name, cohort_table)
        coh_rel = src_name
        temp_registered = True
    else:
        coh_rel = cohort_table

    try:
        cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {coh_rel}").fetchall()]
        actual_id = id_col if id_col.lower() in cols else ("person_id" if "person_id" in cols else "subject_id")
        actual_vid = visit_col if visit_col.lower() in cols else "visit_occurrence_id"

        cr_cids = ", ".join(str(c) for c in creatinine_concept_ids)
        glu_cids = ", ".join(str(c) for c in glucose_concept_ids)
        hr_cids = ", ".join(str(c) for c in heart_rate_concept_ids)
        sbp_cids = ", ".join(str(c) for c in sbp_concept_ids)

        # Build episode query
        has_dates = "visit_start_date" in cols and "visit_end_date" in cols
        has_cohort_dates = "cohort_start_date" in cols and "cohort_end_date" in cols

        if has_dates:
            episode_sql = f"SELECT {actual_id} AS person_id, {actual_vid} AS visit_id, visit_start_date, visit_end_date FROM {coh_rel}"
        elif has_cohort_dates:
            episode_sql = f"SELECT {actual_id} AS person_id, {actual_vid} AS visit_id, cohort_start_date AS visit_start_date, cohort_end_date AS visit_end_date FROM {coh_rel}"
        else:
            episode_sql = f"""
            SELECT c.{actual_id} AS person_id, c.{actual_vid} AS visit_id,
                   v.visit_start_date, v.visit_end_date
            FROM {coh_rel} c
            JOIN {visit_table} v ON v.visit_occurrence_id = c.{actual_vid}
            """

        m_cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {measurement_table}").fetchall()]
        has_m_date = "measurement_date" in m_cols
        has_m_dt = "measurement_datetime" in m_cols

        if has_m_date and has_m_dt:
            m_date_expr = "m.measurement_date"
            m_time_expr = "COALESCE(m.measurement_datetime, CAST(m.measurement_date AS TIMESTAMP))"
        elif has_m_dt:
            m_date_expr = "CAST(m.measurement_datetime AS DATE)"
            m_time_expr = "m.measurement_datetime"
        elif has_m_date:
            m_date_expr = "m.measurement_date"
            m_time_expr = "CAST(m.measurement_date AS TIMESTAMP)"
        else:
            m_date_expr = "CURRENT_DATE"
            m_time_expr = "CURRENT_TIMESTAMP"

        m_id_order = ", m.measurement_id" if "measurement_id" in m_cols else ""

        sql = f"""
        WITH episodes AS (
            {episode_sql}
        ),
        in_stay_measurements AS (
            SELECT 
                e.person_id,
                e.visit_id,
                m.measurement_concept_id,
                {m_date_expr} AS measurement_date,
                {m_time_expr} AS m_time,
                CAST(m.value_as_number AS DOUBLE) AS val,
                ROW_NUMBER() OVER (
                    PARTITION BY e.person_id, e.visit_id, m.measurement_concept_id 
                    ORDER BY {m_time_expr}{m_id_order}
                ) AS rn_first
            FROM episodes e
            JOIN {measurement_table} m ON m.person_id = e.person_id
            WHERE m.value_as_number IS NOT NULL
              AND {m_date_expr} >= e.visit_start_date
              AND {m_date_expr} <= e.visit_end_date
        ),
        cr_stats AS (
            SELECT 
                person_id, visit_id,
                MAX(CASE WHEN rn_first = 1 THEN val END) AS cr_baseline,
                MAX(val) AS cr_peak,
                MIN(val) AS cr_nadir
            FROM in_stay_measurements
            WHERE measurement_concept_id IN ({cr_cids})
            GROUP BY person_id, visit_id
        ),
        glu_stats AS (
            SELECT 
                person_id, visit_id,
                MAX(CASE WHEN rn_first = 1 THEN val END) AS glu_baseline,
                MAX(val) AS glu_peak,
                MIN(val) AS glu_nadir
            FROM in_stay_measurements
            WHERE measurement_concept_id IN ({glu_cids})
            GROUP BY person_id, visit_id
        ),
        vitals_stats AS (
            SELECT 
                person_id, visit_id,
                MAX(CASE WHEN measurement_concept_id IN ({hr_cids}) THEN val END) AS hr_peak,
                AVG(CASE WHEN measurement_concept_id IN ({hr_cids}) THEN val END) AS hr_mean,
                MIN(CASE WHEN measurement_concept_id IN ({sbp_cids}) THEN val END) AS sbp_nadir,
                AVG(CASE WHEN measurement_concept_id IN ({sbp_cids}) THEN val END) AS sbp_mean
            FROM in_stay_measurements
            WHERE measurement_concept_id IN ({hr_cids}, {sbp_cids})
            GROUP BY person_id, visit_id
        )
        SELECT 
            e.person_id AS {actual_id},
            e.visit_id AS {actual_vid},
            cr.cr_baseline,
            cr.cr_peak,
            cr.cr_nadir,
            (cr.cr_peak - cr.cr_baseline) AS cr_delta,
            cr.cr_baseline AS creatinine_admission,
            cr.cr_peak AS creatinine_peak,
            cr.cr_nadir AS creatinine_nadir,
            (cr.cr_peak - cr.cr_baseline) AS creatinine_delta,
            CASE 
                WHEN cr.cr_peak IS NULL OR cr.cr_baseline IS NULL THEN 0
                WHEN (cr.cr_peak / NULLIF(cr.cr_baseline, 0)) >= 3.0 OR (cr.cr_peak >= 4.0 AND (cr.cr_peak - cr.cr_baseline) >= 0.3) THEN 3
                WHEN (cr.cr_peak / NULLIF(cr.cr_baseline, 0)) >= 2.0 THEN 2
                WHEN (cr.cr_peak / NULLIF(cr.cr_baseline, 0)) >= 1.5 OR (cr.cr_peak - cr.cr_baseline) >= 0.3 THEN 1
                ELSE 0 
            END AS kdigo_aki_stage,
            glu.glu_baseline,
            glu.glu_peak,
            glu.glu_nadir,
            (glu.glu_peak - glu.glu_nadir) AS glycemic_volatility,
            (glu.glu_peak - glu.glu_nadir) AS glucose_volatility,
            v.hr_peak,
            v.sbp_nadir,
            ROUND(v.hr_peak / NULLIF(v.sbp_nadir, 0), 3) AS shock_index,
            CASE WHEN (v.hr_peak / NULLIF(v.sbp_nadir, 0)) >= 1.0 THEN 1 ELSE 0 END AS shock_index_elevated
        FROM episodes e
        LEFT JOIN cr_stats cr ON cr.person_id = e.person_id AND cr.visit_id = e.visit_id
        LEFT JOIN glu_stats glu ON glu.person_id = e.person_id AND glu.visit_id = e.visit_id
        LEFT JOIN vitals_stats v ON v.person_id = e.person_id AND v.visit_id = e.visit_id
        ORDER BY e.person_id, e.visit_id
        """

        return con.execute(sql).df()
    finally:
        if temp_registered:
            try:
                con.unregister(src_name)
            except Exception:
                pass
