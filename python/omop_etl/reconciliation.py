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
    group_by: str | None = None,
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
        group_by: Optional column name (e.g. 'SITE') to stratify match diagnostics by.

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
    enc_match_rate = round(matched_encs / total_stays, 4) if total_stays > 0 else 1.0

    audit = {
        "total_input_stays": total_stays,
        "matched_patients": matched_pts,
        "unmatched_patients": unmatched_pts,
        "patient_match_rate": round(matched_pts / unique_input_pts, 4) if unique_input_pts > 0 else 1.0,
        "matched_encounters": matched_encs,
        "unmatched_encounters": unmatched_encs,
        "encounter_match_rate": enc_match_rate,
    }

    if group_by:
        cols_map = {c.lower(): c for c in input_df.columns}
        grp_col = cols_map.get(str(group_by).lower())
        if grp_col:
            by_group = {}
            for grp_val, grp_stays in input_df.groupby(grp_col):
                grp_pts = grp_stays[actual_pid_col].nunique() if actual_pid_col in grp_stays.columns else len(grp_stays)
                if grp_col in cohort_df.columns:
                    grp_coh = cohort_df[cohort_df[grp_col] == grp_val]
                elif "site" in cohort_df.columns and grp_col.lower() in ("site", "site_id", "site_anon"):
                    grp_coh = cohort_df[cohort_df["site"] == grp_val]
                else:
                    sub_pids = set(grp_stays[actual_pid_col])
                    if id_type == "person_id" or actual_pid_col == "person_id":
                        grp_coh = cohort_df[cohort_df["subject_id"].isin(sub_pids)]
                    else:
                        grp_coh = cohort_df
                g_tot = len(grp_stays)
                g_matched_pts = len(grp_coh["subject_id"].unique()) if not grp_coh.empty else 0
                g_unmatched_pts = max(0, grp_pts - g_matched_pts)
                g_matched_encs = int(grp_coh["visit_occurrence_id"].notna().sum()) if not grp_coh.empty else 0
                g_unmatched_encs = g_tot - g_matched_encs
                by_group[str(grp_val)] = {
                    "total_input_stays": g_tot,
                    "matched_patients": g_matched_pts,
                    "unmatched_patients": g_unmatched_pts,
                    "patient_match_rate": round(g_matched_pts / grp_pts, 4) if grp_pts > 0 else 1.0,
                    "matched_encounters": g_matched_encs,
                    "unmatched_encounters": g_unmatched_encs,
                    "encounter_match_rate": round(g_matched_encs / g_tot, 4) if g_tot > 0 else 1.0,
                }
            audit["by_group"] = by_group
            if str(group_by).upper() in ("SITE", "SITE_ID", "SITE_ANON"):
                audit["by_site"] = by_group

    if target_table:
        con.register("_cohort_out", cohort_df)
        con.execute(f"CREATE OR REPLACE TABLE {target_table} AS SELECT * FROM _cohort_out")
        con.unregister("_cohort_out")

    return cohort_df, audit


def export_companion_manifest(
    source: pd.DataFrame | duckdb.DuckDBPyConnection | str | Path,
    output_path: str | Path | None = None,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    cohort_table: str | None = None,
    person_col: str | None = None,
    visit_col: str | None = None,
    index_date_col: str | None = None,
    person_source_col: str | None = None,
    visit_source_col: str | None = None,
) -> pd.DataFrame:
    """Exports a lightweight companion identifier manifest with strict row-order preservation.

    Outputs a 5-column manifest:
    ['person_id', 'person_source_value', 'visit_occurrence_id', 'visit_source_value', 'index_date']
    allowing privacy-preserving de-identified feature matrices to be joined or reconciled
    against OMOP records without leaking identifiers into model training arrays.

    Args:
        source: DataFrame, active connection, table name, or CSV/Parquet path.
        output_path: Optional file path (.csv or .parquet) to write the manifest.
        con: Optional DuckDB connection for looking up source or standard IDs.
        cohort_table: Table name if source is a connection.
        person_col: Column name for person_id.
        visit_col: Column name for visit_occurrence_id.
        index_date_col: Column name for index_date.
        person_source_col: Column name for person_source_value.
        visit_source_col: Column name for visit_source_value.

    Returns:
        pd.DataFrame: Manifest with the 5 required columns and identical row order.
    """
    if isinstance(source, (str, Path)):
        p = str(source)
        if p.endswith((".csv", ".tsv", ".csv.gz", ".parquet")) or Path(p).exists():
            if p.endswith(".parquet"):
                df = pd.read_parquet(p)
            else:
                df = pd.read_csv(p)
        elif con is not None:
            df = con.execute(f"SELECT * FROM {p}").df()
        else:
            raise ValueError(f"Cannot resolve table '{p}' without a database connection.")
    elif isinstance(source, duckdb.DuckDBPyConnection):
        tbl = cohort_table or "cohort"
        df = source.execute(f"SELECT * FROM {tbl}").df()
        con = source
    elif hasattr(source, "df") and callable(getattr(source, "df")):
        df = source.df()
    elif isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        df = pd.DataFrame(source)

    orig_order = df.index
    cols_map = {c.lower(): c for c in df.columns}

    # Resolve person_id
    pid_c = person_col or cols_map.get("person_id") or cols_map.get("subject_id") or cols_map.get("pid")
    pids = df[pid_c].values if pid_c else [None] * len(df)

    # Resolve person_source_value
    psv_c = person_source_col or cols_map.get("person_source_value") or cols_map.get("mrn") or cols_map.get("pat_id") or cols_map.get("patient_id")
    psvs = df[psv_c].values if psv_c else [None] * len(df)

    # Resolve visit_occurrence_id
    vid_c = visit_col or cols_map.get("visit_occurrence_id") or cols_map.get("visit_id") or cols_map.get("encounter_id") or cols_map.get("hadm_id")
    vids = df[vid_c].values if vid_c else [None] * len(df)

    # Resolve visit_source_value
    vsv_c = visit_source_col or cols_map.get("visit_source_value") or cols_map.get("encounter_source_value") or cols_map.get("visit_source") or cols_map.get("enc_id")
    vsvs = df[vsv_c].values if vsv_c else [None] * len(df)

    # Resolve index_date
    idx_c = index_date_col or cols_map.get("index_date") or cols_map.get("cohort_start_date") or cols_map.get("visit_start_date") or cols_map.get("admit_date")
    idxs = df[idx_c].values if idx_c else [None] * len(df)

    manifest = pd.DataFrame({
        "person_id": pids,
        "person_source_value": psvs,
        "visit_occurrence_id": vids,
        "visit_source_value": vsvs,
        "index_date": idxs,
    }, index=orig_order)

    # If con is provided, fill missing psvs or pids from person table
    if con is not None:
        try:
            # Missing person_source_value
            if manifest["person_source_value"].isna().any() and not manifest["person_id"].isna().all():
                needed_pids = manifest.loc[manifest["person_source_value"].isna() & manifest["person_id"].notna(), "person_id"].dropna().unique()
                if len(needed_pids) > 0:
                    pids_in = ", ".join(str(int(x)) for x in needed_pids if str(x).isdigit())
                    if pids_in:
                        res = con.execute(f"SELECT person_id, person_source_value FROM person WHERE person_id IN ({pids_in})").fetchall()
                        pmap = {r[0]: r[1] for r in res}
                        manifest["person_source_value"] = manifest["person_source_value"].combine_first(manifest["person_id"].map(pmap))

            # Missing person_id
            if manifest["person_id"].isna().any() and not manifest["person_source_value"].isna().all():
                needed_psvs = manifest.loc[manifest["person_id"].isna() & manifest["person_source_value"].notna(), "person_source_value"].dropna().unique()
                if len(needed_psvs) > 0:
                    psvs_in = ", ".join(f"'{str(x).strip()}'" for x in needed_psvs)
                    res = con.execute(f"SELECT person_source_value, person_id FROM person WHERE UPPER(TRIM(person_source_value)) IN ({psvs_in.upper()})").fetchall()
                    psv_map = {str(r[0]).strip().upper(): r[1] for r in res}
                    manifest["person_id"] = manifest["person_id"].combine_first(manifest["person_source_value"].astype(str).str.strip().str.upper().map(psv_map))

            # Missing visit_source_value
            if manifest["visit_source_value"].isna().any() and not manifest["visit_occurrence_id"].isna().all():
                needed_vids = manifest.loc[manifest["visit_source_value"].isna() & manifest["visit_occurrence_id"].notna(), "visit_occurrence_id"].dropna().unique()
                if len(needed_vids) > 0:
                    vids_in = ", ".join(str(int(x)) for x in needed_vids if str(x).isdigit())
                    if vids_in:
                        res = con.execute(f"SELECT visit_occurrence_id, visit_source_value, visit_start_date FROM visit_occurrence WHERE visit_occurrence_id IN ({vids_in})").fetchall()
                        vmap = {r[0]: r[1] for r in res}
                        dmap = {r[0]: r[2] for r in res}
                        manifest["visit_source_value"] = manifest["visit_source_value"].combine_first(manifest["visit_occurrence_id"].map(vmap))
                        manifest["index_date"] = manifest["index_date"].combine_first(manifest["visit_occurrence_id"].map(dmap))

            # Missing visit_occurrence_id
            if manifest["visit_occurrence_id"].isna().any() and not manifest["visit_source_value"].isna().all():
                needed_vsvs = manifest.loc[manifest["visit_occurrence_id"].isna() & manifest["visit_source_value"].notna(), "visit_source_value"].dropna().unique()
                if len(needed_vsvs) > 0:
                    vsvs_in = ", ".join(f"'{str(x).strip()}'" for x in needed_vsvs)
                    res = con.execute(f"SELECT visit_source_value, visit_occurrence_id, visit_start_date FROM visit_occurrence WHERE UPPER(TRIM(visit_source_value)) IN ({vsvs_in.upper()})").fetchall()
                    vsv_map = {str(r[0]).strip().upper(): r[1] for r in res}
                    vsv_dmap = {str(r[0]).strip().upper(): r[2] for r in res}
                    v_keys = manifest["visit_source_value"].astype(str).str.strip().str.upper()
                    manifest["visit_occurrence_id"] = manifest["visit_occurrence_id"].combine_first(v_keys.map(vsv_map))
                    manifest["index_date"] = manifest["index_date"].combine_first(v_keys.map(vsv_dmap))
        except Exception:
            pass

    # Cast index_date to string YYYY-MM-DD or date if present
    if not manifest["index_date"].isna().all():
        try:
            manifest["index_date"] = pd.to_datetime(manifest["index_date"]).dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    if output_path is not None:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if out_p.suffix.lower() == ".parquet":
            manifest.to_parquet(out_p, index=False)
        else:
            manifest.to_csv(out_p, index=False)

    return manifest


def compare_feature_tables(
    left: pd.DataFrame,
    right: pd.DataFrame,
    keys: Sequence[str] | str | None = None,
    *,
    left_manifest: pd.DataFrame | str | Path | None = None,
    right_manifest: pd.DataFrame | str | Path | None = None,
    by: str | None = None,
    tolerances: Mapping[str, float] | None = None,
    smd_threshold: float = 0.10,
    missingness_gap_threshold: float = 0.05,
    scale_ratio_threshold: float = 5.0,
    min_cell_count: int = 11,
    stream_to_parquet: str | Path | None = None,
    partition_by: Sequence[str] | None = None,
    to_arrow_dataset: bool = False,
) -> Any:
    """Computes a feature-by-feature parity ledger comparing two derivations.

    Quantifies standardized mean differences (SMD), Pearson correlation (or Cohen's kappa),
    missingness gaps, and variance ratios to validate a new OMOP feature pipeline against an older one.

    Args:
        left: First feature matrix (e.g. new OMOP pipeline).
        right: Second feature matrix (e.g. legacy pipeline).
        keys: Key column or columns to join on (e.g. 'person_id' or ['person_id', 'index_date']).
        left_manifest: Optional companion manifest for left matrix.
        right_manifest: Optional companion manifest for right matrix.
        by: Optional site column to stratify parity ledger by (default None).
        tolerances: Optional dict of custom per-feature SMD tolerances.
        smd_threshold: Standardized mean difference threshold flagging discrepancy (default 0.10).
        missingness_gap_threshold: Maximum allowable missingness gap (default 0.05 = 5%).
        scale_ratio_threshold: Maximum allowable ratio of standard deviations (default 5.0).
        min_cell_count: Cell count threshold for suppression (default 11).
        stream_to_parquet: Optional file or directory path to stream the parity ledger directly to Parquet.
        partition_by: Optional columns to partition parquet by (e.g. ['site']).
        to_arrow_dataset: Whether to return an Arrow Table/Dataset.

    Returns:
        pd.DataFrame or pyarrow.Table: Parity ledger with columns [site, feature, matched_n, left_mean,
            right_mean, left_sd, right_sd, smd, correlation, left_missing_rate,
            right_missing_rate, missingness_gap, flagged, reason].
    """
    left_df = left.copy()
    if left_manifest is not None:
        if isinstance(left_manifest, (str, Path)):
            p = str(left_manifest)
            lm = pd.read_parquet(p) if p.endswith(".parquet") else pd.read_csv(p)
        else:
            lm = left_manifest
        for c in ["person_id", "visit_occurrence_id", "index_date", "person_source_value", "visit_source_value"]:
            if c in lm.columns and c not in left_df.columns and len(lm) == len(left_df):
                left_df[c] = lm[c].values

    right_df = right.copy()
    if right_manifest is not None:
        if isinstance(right_manifest, (str, Path)):
            p = str(right_manifest)
            rm = pd.read_parquet(p) if p.endswith(".parquet") else pd.read_csv(p)
        else:
            rm = right_manifest
        for c in ["person_id", "visit_occurrence_id", "index_date", "person_source_value", "visit_source_value"]:
            if c in rm.columns and c not in right_df.columns and len(rm) == len(right_df):
                right_df[c] = rm[c].values

    if keys is None:
        cand_keys = [
            ["person_id", "visit_occurrence_id"],
            ["person_id", "index_date"],
            ["person_id"],
            ["subject_id", "visit_occurrence_id"],
            ["subject_id"],
        ]
        found_key = None
        for cand in cand_keys:
            if all(k in left_df.columns and k in right_df.columns for k in cand):
                found_key = cand
                break
        key_list = found_key or ["person_id"]
    else:
        key_list = [keys] if isinstance(keys, str) else list(keys)

    common_cols = sorted(set(left_df.columns) & set(right_df.columns) - set(key_list))
    if by and by in common_cols:
        common_cols.remove(by)

    # Inner merge on keys
    merged = pd.merge(left_df, right_df, on=key_list, suffixes=("_L", "_R"))
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
        out = pd.DataFrame(columns=[
            "site", "feature", "matched_n", "left_mean", "right_mean", "left_sd", "right_sd",
            "smd", "correlation", "left_missing_rate", "right_missing_rate", "missingness_gap",
            "flagged", "reason"
        ])

    if stream_to_parquet is not None:
        out_p = Path(stream_to_parquet)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if partition_by and len(out) > 0 and all(c in out.columns for c in partition_by):
            import pyarrow as pa
            import pyarrow.parquet as pq
            tbl = pa.Table.from_pandas(out)
            pq.write_to_dataset(tbl, root_path=str(out_p), partition_cols=list(partition_by))
        else:
            out.to_parquet(out_p, index=False)

    if to_arrow_dataset:
        import pyarrow as pa
        return pa.Table.from_pandas(out)

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
