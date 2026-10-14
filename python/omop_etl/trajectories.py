"""Longitudinal Temporal Trajectory Features (Slopes & Volatility) in DuckDB.

Vectorized linear regression aggregations (regr_slope, regr_r2, rolling stddev)
for fast in-engine trajectory feature extraction over observational measurement look-back windows.
"""

from __future__ import annotations

from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd

__all__ = ["extract_temporal_trajectories"]


def extract_temporal_trajectories(
    con: duckdb.DuckDBPyConnection | Any,
    cohort_table: str | pd.DataFrame,
    measurement_concept_ids: Sequence[int],
    window_days: int = 365,
    index_date_col: str = "cohort_start_date",
    id_col: str = "person_id",
) -> pd.DataFrame:
    """Extracts longitudinal measurement trajectory features (slopes, R^2, volatility) in DuckDB.

    Leverages DuckDB's vectorized `regr_slope(val, days)`, `regr_r2(val, days)`, and `stddev_samp(val)`
    aggregations directly on clinical measurement series within `[index_date - window_days, index_date]`.

    Args:
        con: Active DuckDB connection.
        cohort_table: Name of cohort table in DuckDB or a DataFrame containing patient index dates.
        measurement_concept_ids: Concept IDs of measurements to compute trajectories for.
        window_days: Lookback window in days prior to index date (default 365).
        index_date_col: Column indicating cohort index date (default 'cohort_start_date').
        id_col: Column identifying patient ('person_id' or 'subject_id').

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
