"""Pipeline Reconciliation, Cohort Overlap, and Parity Ledger.

Provides tools for comparing and validating new OMOP pipelines against legacy implementations:
- ``cohort_from_index_stays``: Maps external/legacy index stays to OMOP person_id and visit_occurrence_id.
- ``compare_feature_tables``: Feature-by-feature parity ledger with SMD, correlations, and scale checks.
- ``cohort_overlap``: Reconciles two cohort definitions with Jaccard overlap and outcome rate deltas.
- ``run_study_grid``: Sensitivity grid executing multiple study specification variants side by side.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import duckdb
import numpy as np
import pandas as pd


def cohort_from_index_stays(
    con: duckdb.DuckDBPyConnection,
    stays: pd.DataFrame | str | Path,
    *,
    id_type: str = "person_source_value",
    person_source_col: str = "person_source_value",
    visit_source_col: str | None = "visit_source_value",
    index_date_col: str = "index_date",
    end_date_col: str | None = "end_date",
    cohort_definition_id: int = 1,
    site_col: str = "site",
    target_table: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Forces study cohort index stays from an external legacy list or table.

    Maps source patient and encounter identifiers to standard OMOP person_id and
    visit_occurrence_id, producing a compliant OMOP cohort table while reporting
    match/unmatched diagnostic tallies.

    Args:
        con: Active DuckDB connection.
        stays: Input DataFrame, CSV path, or existing table name containing index stays.
        id_type: How patient identifier is represented ('person_source_value' or 'person_id').
        person_source_col: Patient identifier column in input stays (default 'person_source_value').
        visit_source_col: Optional encounter identifier column (default 'visit_source_value').
        index_date_col: Index/admission date column in input stays (default 'index_date').
        end_date_col: Optional discharge/end date column in input stays (default 'end_date').
        cohort_definition_id: OMOP cohort definition identifier (default 1).
        site_col: Site column name if present (default 'site').
        target_table: Optional table name to persist the cohort in DuckDB.

    Returns:
        tuple[pd.DataFrame, dict[str, Any]]:
            - Cohort DataFrame with [cohort_definition_id, subject_id, cohort_start_date,
              cohort_end_date, visit_occurrence_id, (site)].
            - Audit dict with matching diagnostics (total_stays, matched_patients, etc.).
    """
    if isinstance(stays, (str, Path)):
        p = str(stays)
        if p.endswith((".csv", ".tsv", ".csv.gz", ".parquet")) or Path(p).exists():
            if p.endswith(".parquet"):
                input_df = con.execute(f"SELECT * FROM read_parquet('{p}')").df()
            else:
                input_df = con.execute(f"SELECT * FROM read_csv_auto('{p}')").df()
        else:
            input_df = con.execute(f"SELECT * FROM {p}").df()
    else:
        input_df = stays.copy()

    total_stays = len(input_df)
    unique_input_pts = len(input_df[person_source_col].unique()) if person_source_col in input_df.columns else total_stays

    # Normalize column names
    cols_lower = {c.lower(): c for c in input_df.columns}
    actual_pid_col = cols_lower.get(person_source_col.lower(), person_source_col)
    actual_idx_col = cols_lower.get(index_date_col.lower(), index_date_col)
    actual_end_col = cols_lower.get(end_date_col.lower()) if end_date_col else None
    actual_vis_col = cols_lower.get(visit_source_col.lower()) if visit_source_col else None
    actual_site_col = cols_lower.get(site_col.lower())

    con.register("_ext_stays_df", input_df)

    try:
        # 1. Map person identifier
        if id_type == "person_id" or actual_pid_col == "person_id":
            person_join = "JOIN person p ON p.person_id = s." + actual_pid_col
        else:
            person_join = f"JOIN person p ON UPPER(TRIM(CAST(p.person_source_value AS VARCHAR))) = UPPER(TRIM(CAST(s.{actual_pid_col} AS VARCHAR)))"

        # 2. Map visit occurrence if encounter col present
        visit_join = ""
        visit_sel = "NULL AS visit_occurrence_id"
        if actual_vis_col and actual_vis_col in input_df.columns:
            visit_join = f"LEFT JOIN visit_occurrence vo ON vo.person_id = p.person_id AND UPPER(TRIM(CAST(vo.visit_source_value AS VARCHAR))) = UPPER(TRIM(CAST(s.{actual_vis_col} AS VARCHAR)))"
            visit_sel = "vo.visit_occurrence_id"
        elif actual_idx_col in input_df.columns:
            visit_join = f"LEFT JOIN visit_occurrence vo ON vo.person_id = p.person_id AND CAST(vo.visit_start_date AS DATE) = CAST(s.{actual_idx_col} AS DATE)"
            visit_sel = "vo.visit_occurrence_id"

        site_sel = f", s.{actual_site_col} AS site" if actual_site_col else ""
        end_date_expr = f"COALESCE(CAST(s.{actual_end_col} AS DATE), CAST(s.{actual_idx_col} AS DATE))" if actual_end_col else f"CAST(s.{actual_idx_col} AS DATE)"

        sql = f"""
        SELECT DISTINCT
            {int(cohort_definition_id)} AS cohort_definition_id,
            p.person_id AS subject_id,
            CAST(s.{actual_idx_col} AS DATE) AS cohort_start_date,
            {end_date_expr} AS cohort_end_date,
            {visit_sel}
            {site_sel}
        FROM _ext_stays_df s
        {person_join}
        {visit_join}
        """

        cohort_df = con.execute(sql).df()
    finally:
        try:
            con.unregister("_ext_stays_df")
        except Exception:
            pass

    matched_pts = len(cohort_df["subject_id"].unique()) if not cohort_df.empty else 0
    unmatched_pts = max(0, unique_input_pts - matched_pts)
    matched_encs = int(cohort_df["visit_occurrence_id"].notna().sum()) if not cohort_df.empty else 0
    unmatched_encs = total_stays - matched_encs

    audit = {
        "total_input_stays": total_stays,
        "matched_patients": matched_pts,
        "unmatched_patients": unmatched_pts,
        "patient_match_rate": round(matched_pts / unique_input_pts, 4) if unique_input_pts > 0 else 1.0,
        "matched_encounters": matched_encs,
        "unmatched_encounters": unmatched_encs,
    }

    if target_table:
        con.register("_cohort_out", cohort_df)
        con.execute(f"CREATE OR REPLACE TABLE {target_table} AS SELECT * FROM _cohort_out")
        con.unregister("_cohort_out")

    return cohort_df, audit


def compare_feature_tables(
    left: pd.DataFrame,
    right: pd.DataFrame,
    keys: Sequence[str] | str,
    *,
    by: str | None = None,
    tolerances: Mapping[str, float] | None = None,
    smd_threshold: float = 0.10,
    missingness_gap_threshold: float = 0.05,
    scale_ratio_threshold: float = 5.0,
    min_cell_count: int = 11,
) -> pd.DataFrame:
    """Computes a feature-by-feature parity ledger comparing two derivations.

    Quantifies standardized mean differences (SMD), Pearson correlation (or Cohen's kappa),
    missingness gaps, and variance ratios to validate a new OMOP feature pipeline against an older one.

    Args:
        left: First feature matrix (e.g. new OMOP pipeline).
        right: Second feature matrix (e.g. legacy pipeline).
        keys: Key column or columns to join on (e.g. 'person_id' or ['person_id', 'index_date']).
        by: Optional site column to stratify parity ledger by (default None).
        tolerances: Optional dict of custom per-feature SMD tolerances.
        smd_threshold: Standardized mean difference threshold flagging discrepancy (default 0.10).
        missingness_gap_threshold: Maximum allowable missingness gap (default 0.05 = 5%).
        scale_ratio_threshold: Maximum allowable ratio of standard deviations (default 5.0).
        min_cell_count: Cell count threshold for suppression (default 11).

    Returns:
        pd.DataFrame: Parity ledger with columns [site, feature, matched_n, left_mean,
            right_mean, left_sd, right_sd, smd, correlation, left_missing_rate,
            right_missing_rate, missingness_gap, flagged, reason].
    """
    key_list = [keys] if isinstance(keys, str) else list(keys)
    common_cols = sorted(set(left.columns) & set(right.columns) - set(key_list))
    if by and by in common_cols:
        common_cols.remove(by)

    # Inner merge on keys
    merged = pd.merge(left, right, on=key_list, suffixes=("_L", "_R"))
    if merged.empty:
        return pd.DataFrame(columns=[
            "site", "feature", "matched_n", "left_mean", "right_mean", "left_sd", "right_sd",
            "smd", "correlation", "left_missing_rate", "right_missing_rate", "missingness_gap",
            "flagged", "reason"
        ])

    site_col_l = f"{by}_L" if by and f"{by}_L" in merged.columns else (by if by in merged.columns else None)
    if site_col_l is None:
        merged["_site"] = "ALL"
        site_col_l = "_site"

    records = []
    sites = [s for s in merged[site_col_l].unique() if s is not None]
    sites_to_process = list(sites) + ["ALL"] if ("ALL" not in sites and len(sites) > 1) else list(sites)

    tol_map = dict(tolerances or {})

    for site in sites_to_process:
        s_df = merged if site == "ALL" else merged[merged[site_col_l] == site]
        n_matched = len(s_df)
        if n_matched == 0:
            continue

        for feat in common_cols:
            col_l = f"{feat}_L"
            col_r = f"{feat}_R"
            if col_l not in s_df.columns or col_r not in s_df.columns:
                continue

            s1 = s_df[col_l]
            s2 = s_df[col_r]

            m_rate_l = float(s1.isna().mean())
            m_rate_r = float(s2.isna().mean())
            miss_gap = float(abs(m_rate_l - m_rate_r))

            valid_mask = s1.notna() & s2.notna()
            v1 = s1[valid_mask]
            v2 = s2[valid_mask]

            if len(v1) == 0:
                continue

            try:
                v1_num = v1.astype(float)
                v2_num = v2.astype(float)
                is_numeric = True
            except (ValueError, TypeError):
                is_numeric = False

            if not is_numeric:
                continue

            mean_l = float(v1_num.mean())
            mean_r = float(v2_num.mean())
            sd_l = float(v1_num.std()) if len(v1_num) > 1 else 0.0
            sd_r = float(v2_num.std()) if len(v2_num) > 1 else 0.0

            # SMD calculation
            pooled_sd = math.sqrt((sd_l**2 + sd_r**2) / 2.0) if (sd_l > 0 or sd_r > 0) else 0.0
            smd = float(abs(mean_l - mean_r) / pooled_sd) if pooled_sd > 0 else 0.0

            # Correlation or agreement
            if sd_l > 0 and sd_r > 0:
                corr = float(np.corrcoef(v1_num, v2_num)[0, 1])
            else:
                corr = 1.0 if mean_l == mean_r else 0.0

            # Scale ratio
            scale_ratio = (sd_l / sd_r) if (sd_r > 0 and sd_l > 0) else 1.0
            scale_ratio_inv = (sd_r / sd_l) if (sd_l > 0 and sd_r > 0) else 1.0
            max_scale_ratio = max(scale_ratio, scale_ratio_inv)

            # Failure / flag reasons
            reasons = []
            feat_smd_thresh = tol_map.get(feat, smd_threshold)
            if smd > feat_smd_thresh:
                reasons.append("large_smd")
            if miss_gap > missingness_gap_threshold:
                reasons.append("missingness_gap")
            if max_scale_ratio > scale_ratio_threshold:
                reasons.append("scale_mismatch")
            if sd_l == 0.0 and sd_r == 0.0:
                if mean_l == 0.0 and mean_r == 0.0:
                    reasons.append("all_zero")
                else:
                    reasons.append("zero_variance")

            records.append({
                "site": site,
                "feature": feat,
                "matched_n": n_matched if n_matched >= min_cell_count else f"<{min_cell_count}",
                "left_mean": round(mean_l, 4),
                "right_mean": round(mean_r, 4),
                "left_sd": round(sd_l, 4),
                "right_sd": round(sd_r, 4),
                "smd": round(smd, 4),
                "correlation": round(corr, 4),
                "left_missing_rate": round(m_rate_l, 4),
                "right_missing_rate": round(m_rate_r, 4),
                "missingness_gap": round(miss_gap, 4),
                "flagged": len(reasons) > 0,
                "reason": ",".join(reasons),
            })

    out = pd.DataFrame(records)
    if out.empty:
        return pd.DataFrame(columns=[
            "site", "feature", "matched_n", "left_mean", "right_mean", "left_sd", "right_sd",
            "smd", "correlation", "left_missing_rate", "right_missing_rate", "missingness_gap",
            "flagged", "reason"
        ])
    return out


def cohort_overlap(
    con: duckdb.DuckDBPyConnection | None,
    cohort_a: pd.DataFrame | str,
    cohort_b: pd.DataFrame | str,
    *,
    by: str | None = "site",
    person_col: str = "subject_id",
    outcome_col: str | None = None,
) -> pd.DataFrame:
    """Compares membership and outcome rates between two cohort derivations.

    Reconciles participants included in Cohort A only, Cohort B only, or both,
    computing Jaccard similarity indices and event rate deltas.

    Args:
        con: Optional DuckDB connection (if cohort_a/b are table names).
        cohort_a: First cohort DataFrame or table name.
        cohort_b: Second cohort DataFrame or table name.
        by: Optional site stratification column (default 'site').
        person_col: Person identifier column name (default 'subject_id').
        outcome_col: Optional binary outcome column name to compare event rates.

    Returns:
        pd.DataFrame: Table with columns [site, n_cohort_a, n_cohort_b, both_count,
            a_only_count, b_only_count, jaccard_index, concordance_rate,
            (a_only_event_rate, b_only_event_rate, both_event_rate)].
    """
    df_a = con.execute(f"SELECT * FROM {cohort_a}").df() if isinstance(cohort_a, str) else cohort_a.copy()
    df_b = con.execute(f"SELECT * FROM {cohort_b}").df() if isinstance(cohort_b, str) else cohort_b.copy()

    actual_pcol_a = person_col if person_col in df_a.columns else ("person_id" if "person_id" in df_a.columns else "subject_id")
    actual_pcol_b = person_col if person_col in df_b.columns else ("person_id" if "person_id" in df_b.columns else "subject_id")

    site_a = by if (by and by in df_a.columns) else None
    if site_a is None:
        df_a["_site"] = "ALL"
        site_a = "_site"

    site_b = by if (by and by in df_b.columns) else None
    if site_b is None:
        df_b["_site"] = "ALL"
        site_b = "_site"

    sites = sorted(set(df_a[site_a].unique()) | set(df_b[site_b].unique()))
    sites_to_process = list(sites) + ["ALL"] if ("ALL" not in sites and len(sites) > 1) else list(sites)

    records = []
    has_outcome = outcome_col and outcome_col in df_a.columns and outcome_col in df_b.columns

    for s in sites_to_process:
        sub_a = df_a if s == "ALL" else df_a[df_a[site_a] == s]
        sub_b = df_b if s == "ALL" else df_b[df_b[site_b] == s]

        pids_a = set(sub_a[actual_pcol_a].dropna().unique())
        pids_b = set(sub_b[actual_pcol_b].dropna().unique())

        both_pids = pids_a & pids_b
        a_only_pids = pids_a - pids_b
        b_only_pids = pids_b - pids_a
        total_union = pids_a | pids_b

        both_cnt = len(both_pids)
        a_only_cnt = len(a_only_pids)
        b_only_cnt = len(b_only_pids)
        union_cnt = len(total_union)

        jaccard = round(both_cnt / union_cnt, 4) if union_cnt > 0 else 1.0
        concordance = round(both_cnt / (a_only_cnt + b_only_cnt + both_cnt), 4) if (a_only_cnt + b_only_cnt + both_cnt) > 0 else 1.0

        rec: dict[str, Any] = {
            "site": s,
            "n_cohort_a": len(pids_a),
            "n_cohort_b": len(pids_b),
            "both_count": both_cnt,
            "a_only_count": a_only_cnt,
            "b_only_count": b_only_cnt,
            "jaccard_index": jaccard,
            "concordance_rate": concordance,
        }

        if has_outcome:
            a_only_rate = float(sub_a.loc[sub_a[actual_pcol_a].isin(a_only_pids), outcome_col].mean()) if a_only_cnt > 0 else 0.0
            b_only_rate = float(sub_b.loc[sub_b[actual_pcol_b].isin(b_only_pids), outcome_col].mean()) if b_only_cnt > 0 else 0.0
            both_rate = float(sub_a.loc[sub_a[actual_pcol_a].isin(both_pids), outcome_col].mean()) if both_cnt > 0 else 0.0

            rec["a_only_event_rate"] = round(a_only_rate, 4)
            rec["b_only_event_rate"] = round(b_only_rate, 4)
            rec["both_event_rate"] = round(both_rate, 4)

        records.append(rec)

    return pd.DataFrame(records)


def run_study_grid(
    con: duckdb.DuckDBPyConnection,
    study_fn: Callable[..., pd.DataFrame],
    variants: Mapping[str, Sequence[Any]],
    *,
    base_args: Mapping[str, Any] | None = None,
    outcome_col: str | None = "outcome",
    site_col: str = "site",
) -> pd.DataFrame:
    """Executes a study cohort sensitivity grid across multiple design choices.

    Runs Cartesian product of study options (e.g. sampling rules, lookback windows,
    wash-out days) and tabulates comparative $N$, events, and rates side by side.

    Args:
        con: Active DuckDB connection.
        study_fn: Callable taking keyword arguments and returning a cohort DataFrame.
        variants: Dict mapping parameter names to lists of candidate values.
        base_args: Fixed base arguments passed to ``study_fn``.
        outcome_col: Name of event/outcome indicator column (default 'outcome').
        site_col: Site column name if present (default 'site').

    Returns:
        pd.DataFrame: Sensitivity grid ledger with columns for each variant parameter,
            plus [site, total_n, event_count, event_rate].
    """
    keys = list(variants.keys())
    value_lists = [variants[k] for k in keys]
    combinations = list(itertools.product(*value_lists))

    args_base = dict(base_args or {})
    records = []

    for combo in combinations:
        variant_params = dict(zip(keys, combo))
        run_kwargs = {**args_base, **variant_params}

        cohort_df = study_fn(**run_kwargs)
        if cohort_df.empty:
            rec = {**variant_params, "site": "ALL", "total_n": 0, "event_count": 0, "event_rate": 0.0}
            records.append(rec)
            continue

        site_actual = site_col if site_col in cohort_df.columns else None
        if site_actual is None:
            cohort_df["_site"] = "ALL"
            site_actual = "_site"

        sites = [s for s in cohort_df[site_actual].unique() if s is not None]
        sites_to_process = list(sites) + ["ALL"] if ("ALL" not in sites and len(sites) > 1) else list(sites)

        for s in sites_to_process:
            sub = cohort_df if s == "ALL" else cohort_df[cohort_df[site_actual] == s]
            n = len(sub)
            if outcome_col and outcome_col in sub.columns:
                events = int(sub[outcome_col].sum())
                rate = round(events / n, 4) if n > 0 else 0.0
            else:
                events = 0
                rate = 0.0

            rec = {
                **variant_params,
                "site": s,
                "total_n": n,
                "event_count": events,
                "event_rate": rate,
            }
            records.append(rec)

    return pd.DataFrame(records)
