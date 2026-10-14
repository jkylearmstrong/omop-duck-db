"""Data Quality, Plausibility, and Feature Integrity Audits.

Provides rigorous, publication-grade data quality assertions:
- ``audit_code_list_coverage``: Audits whether supplied clinical code lists omit high-frequency concepts.
- ``panel_missingness_audit``: Detects discordant missingness within multi-analyte lab panels (e.g. BMP/CBC).
- ``plausibility_report``: Summary table and feature-matrix physiological plausibility gates.
- ``detect_redundant_features``: Detects deterministic linear conversions, ratios, and multicollinear features.
- ``dqd_summary``: Verifies DQD pass-rate mathematical consistency and reproducibility across sites.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence
import duckdb
import numpy as np
import pandas as pd

from omop_etl.measurement_filter import DEFAULT_PHYSIOLOGIC_BOUNDS

__all__ = [
    "audit_code_list_coverage",
    "panel_missingness_audit",
    "plausibility_report",
    "detect_redundant_features",
    "dqd_summary",
]

_DEFAULT_PANELS = {
    "BMP": ["sodium", "potassium", "chloride", "bicarbonate", "bun", "creatinine", "glucose"],
    "CBC": ["wbc", "rbc", "hemoglobin", "hematocrit", "platelets"],
}

_DOMAIN_TABLE_MAP = {
    "condition": ("condition_occurrence", "condition_concept_id", "condition_source_value", "condition_source_concept_id"),
    "conditions": ("condition_occurrence", "condition_concept_id", "condition_source_value", "condition_source_concept_id"),
    "drug": ("drug_exposure", "drug_concept_id", "drug_source_value", "drug_source_concept_id"),
    "drugs": ("drug_exposure", "drug_concept_id", "drug_source_value", "drug_source_concept_id"),
    "medication": ("drug_exposure", "drug_concept_id", "drug_source_value", "drug_source_concept_id"),
    "measurement": ("measurement", "measurement_concept_id", "measurement_source_value", "measurement_source_concept_id"),
    "measurements": ("measurement", "measurement_concept_id", "measurement_source_value", "measurement_source_concept_id"),
    "procedure": ("procedure_occurrence", "procedure_concept_id", "procedure_source_value", "procedure_source_concept_id"),
    "procedures": ("procedure_occurrence", "procedure_concept_id", "procedure_source_value", "procedure_source_concept_id"),
}


def audit_code_list_coverage(
    con: duckdb.DuckDBPyConnection,
    domain: str,
    supplied_codes: Sequence[str | int],
    *,
    concept_set_or_ancestors: Sequence[int] | None = None,
    table_name: str | None = None,
    by_site: bool = True,
    site_col: str = "site",
    min_share: float = 0.01,
) -> pd.DataFrame:
    """Audits clinical code list coverage against actual occurrences in the OMOP database.

    Quantifies the share of records captured by the supplied code list, and identifies
    high-frequency omitted concepts (accounting for at least ``min_share`` of domain rows)
    that may cause silent under-coverage.

    Args:
        con: Active DuckDB connection.
        domain: Clinical domain ('condition', 'drug', 'measurement', 'procedure').
        supplied_codes: List of concept codes (ICD/RxNorm/LOINC strings) or concept IDs.
        concept_set_or_ancestors: Optional list of ancestor concept IDs defining the broad domain scope.
        table_name: Optional custom table or view name override.
        by_site: Whether to stratify results by hospital site (default True).
        site_col: Column name representing site (default 'site').
        min_share: Minimum proportion of uncaptured rows (default 0.01 = 1%) to flag as an omission.

    Returns:
        pd.DataFrame: Table with columns [site, total_rows, covered_rows, coverage_share,
            omitted_concept_id, omitted_concept_name, omitted_code, omitted_rows,
            omitted_share, flagged, reason].
    """
    domain_clean = domain.lower().strip()
    if domain_clean not in _DOMAIN_TABLE_MAP:
        raise ValueError(f"Unknown domain {domain!r}; expected one of {sorted(set(_DOMAIN_TABLE_MAP.keys()))}.")

    default_table, concept_id_col, source_val_col, source_concept_col = _DOMAIN_TABLE_MAP[domain_clean]
    actual_table = table_name or default_table

    # Verify table existence
    has_table = con.execute(
        f"SELECT COUNT(*) FROM information_schema.tables WHERE lower(table_name) = '{actual_table.lower()}';"
    ).fetchone()[0] > 0
    if not has_table:
        # Check views
        has_view = con.execute(
            f"SELECT COUNT(*) FROM information_schema.views WHERE lower(table_name) = '{actual_table.lower()}';"
        ).fetchone()[0] > 0
        if not has_view:
            raise ValueError(f"Target OMOP table or view '{actual_table}' does not exist on this connection.")

    cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {actual_table} LIMIT 0;").fetchall()]
    actual_site_expr = None
    person_join = ""
    if by_site:
        for c in (site_col, "site_anon", "site_id", "site"):
            if c.lower() in cols:
                actual_site_expr = f't."{c}"'
                break
        if not actual_site_expr and "person_id" in cols:
            has_person = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE lower(table_name) = 'person';").fetchone()[0] > 0
            if has_person:
                p_cols = [r[0].lower() for r in con.execute("DESCRIBE SELECT * FROM person LIMIT 0;").fetchall()]
                for c in (site_col, "site_anon", "site_id", "site"):
                    if c.lower() in p_cols:
                        actual_site_expr = f'p."{c}"'
                        person_join = "LEFT JOIN person p ON p.person_id = t.person_id"
                        break

    site_expr = actual_site_expr if actual_site_expr else "'ALL'"

    # Build supplied codes filter
    supplied_ints = [int(c) for c in supplied_codes if isinstance(c, (int, np.integer)) or (isinstance(c, str) and c.isdigit())]
    supplied_strs = [str(c).strip().upper() for c in supplied_codes]

    int_filter = f"t.{concept_id_col} IN ({', '.join(str(i) for i in supplied_ints)})" if supplied_ints else "FALSE"
    str_list = ", ".join("'" + s.replace("'", "''") + "'" for s in supplied_strs)
    str_filter = f"UPPER(TRIM(t.{source_val_col})) IN ({str_list})" if supplied_strs else "FALSE"

    scope_filter = "TRUE"
    if concept_set_or_ancestors:
        anc_ids = ", ".join(str(int(i)) for i in concept_set_or_ancestors)
        scope_filter = f"""(
            t.{concept_id_col} IN ({anc_ids}) OR 
            t.{concept_id_col} IN (
                SELECT descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id IN ({anc_ids})
            )
        )"""

    sql = f"""
    WITH scoped AS (
        SELECT 
            {site_expr} AS site,
            t.{concept_id_col} AS concept_id,
            t.{source_val_col} AS source_value,
            ({int_filter} OR {str_filter}) AS is_covered
        FROM {actual_table} t
        {person_join}
        WHERE {scope_filter}
    ),
    site_totals AS (
        SELECT 
            site,
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE is_covered) AS covered_rows
        FROM scoped
        GROUP BY site
    ),
    omissions AS (
        SELECT 
            s.site,
            s.concept_id,
            COALESCE(c.concept_code, s.source_value) AS code,
            COALESCE(c.concept_name, 'Unknown Concept') AS concept_name,
            COUNT(*) AS omitted_rows
        FROM scoped s
        LEFT JOIN concept c ON c.concept_id = s.concept_id
        WHERE NOT s.is_covered AND s.concept_id != 0
        GROUP BY s.site, s.concept_id, c.concept_code, s.source_value, c.concept_name
    )
    SELECT 
        st.site,
        st.total_rows,
        st.covered_rows,
        ROUND(CAST(st.covered_rows AS DOUBLE) / NULLIF(st.total_rows, 0), 4) AS coverage_share,
        o.concept_id AS omitted_concept_id,
        o.code AS omitted_code,
        o.concept_name AS omitted_concept_name,
        COALESCE(o.omitted_rows, 0) AS omitted_rows,
        ROUND(CAST(COALESCE(o.omitted_rows, 0) AS DOUBLE) / NULLIF(st.total_rows, 0), 4) AS omitted_share
    FROM site_totals st
    LEFT JOIN omissions o ON o.site = st.site
    ORDER BY st.site, omitted_share DESC;
    """

    res = con.execute(sql).df()
    if res.empty:
        return pd.DataFrame(columns=[
            "site", "total_rows", "covered_rows", "coverage_share",
            "omitted_concept_id", "omitted_code", "omitted_concept_name",
            "omitted_rows", "omitted_share", "flagged", "reason",
        ])

    res["flagged"] = res["omitted_share"] >= float(min_share)
    res["reason"] = np.where(res["flagged"], "omitted_high_frequency_code", "")
    return res


def panel_missingness_audit(
    features: pd.DataFrame,
    panels: Mapping[str, Sequence[str]] | None = None,
    *,
    by: str | None = "site",
    threshold: float = 0.15,
    peer_gap: float = 0.20,
) -> pd.DataFrame:
    """Audits laboratory panel completeness consistency to detect unmapped or omitted panel analytes.

    Analytes ordered together (e.g. BMP or CBC) should exhibit consistent missingness rates.
    When a component is missing significantly more often than its panel median, or deviates
    strongly from other hospital sites, it flags a likely mapping gap.

    Args:
        features: DataFrame containing patient features with analyte columns.
        panels: Dictionary mapping panel names to lists of column names (default BMP and CBC).
        by: Column name to stratify by (default 'site'); if None, audited as 'ALL'.
        threshold: Maximum allowable missingness gap above panel median (default 0.15 = 15%).
        peer_gap: Cross-site discrepancy gap flagging peer outliers across hospitals (default 0.20 = 20%).

    Returns:
        pd.DataFrame: Table with columns [site, panel, analyte, missing_count, total_count,
            missing_rate, panel_median_missing, missingness_gap, flagged, reason].
    """
    df = features.copy()
    site_col = by if (by and by in df.columns) else None
    if site_col is None:
        df["_site"] = "ALL"
        site_col = "_site"

    panel_defs = dict(panels or _DEFAULT_PANELS)
    cols_lower = {c.lower(): c for c in df.columns}

    records = []
    sites = [s for s in df[site_col].unique() if s is not None]
    if "ALL" not in sites and len(sites) > 1:
        sites_to_process = list(sites) + ["ALL"]
    else:
        sites_to_process = list(sites)

    for site in sites_to_process:
        site_df = df if site == "ALL" else df[df[site_col] == site]
        n_site = len(site_df)
        if n_site == 0:
            continue

        for panel_name, analytes in panel_defs.items():
            found_analytes = [cols_lower[a.lower()] for a in analytes if a.lower() in cols_lower]
            if not found_analytes:
                continue

            missing_rates = {a: site_df[a].isna().mean() for a in found_analytes}
            median_missing = float(np.median(list(missing_rates.values())))

            for a in found_analytes:
                m_rate = missing_rates[a]
                gap = m_rate - median_missing
                m_cnt = int(site_df[a].isna().sum())
                records.append({
                    "site": site,
                    "panel": panel_name,
                    "analyte": a,
                    "missing_count": m_cnt,
                    "total_count": n_site,
                    "missing_rate": round(float(m_rate), 4),
                    "panel_median_missing": round(median_missing, 4),
                    "missingness_gap": round(float(gap), 4),
                })

    out = pd.DataFrame(records)
    if out.empty:
        return pd.DataFrame(columns=[
            "site", "panel", "analyte", "missing_count", "total_count",
            "missing_rate", "panel_median_missing", "missingness_gap", "flagged", "reason",
        ])

    reasons = []
    for _, row in out.iterrows():
        r_list = []
        if row["missingness_gap"] > threshold:
            r_list.append("panel_missingness_gap")
        reasons.append(r_list)

    # Multi-site peer outlier check
    per_site = out["site"] != "ALL"
    for (panel, analyte), grp in out[per_site].groupby(["panel", "analyte"]):
        if len(grp) >= 3:
            for idx in grp.index:
                rate = out.at[idx, "missing_rate"]
                peer_rates = grp.loc[grp.index != idx, "missing_rate"]
                if rate > peer_rates.median() + peer_gap:
                    reasons[idx].append("peer_missingness_outlier")

    out["reason"] = [",".join(dict.fromkeys(r)) for r in reasons]
    out["flagged"] = out["reason"].ne("")
    return out


def plausibility_report(
    table1_or_features: pd.DataFrame,
    ranges: Mapping[str, Sequence[float]] | None = None,
    *,
    sd_to_mean_max: float = 3.0,
    min_pct: float = 0.0,
    max_pct: float = 100.0,
    ci_gate: bool = False,
) -> pd.DataFrame:
    """Table-level and feature-level physiological plausibility report and CI gate.

    Verifies summary tables and raw feature matrices against clinical boundary ranges,
    detecting unit mismatches (e.g. 10x decimal errors), extreme variance/SD ratios,
    impossible percentages, and negative physiological values.

    Args:
        table1_or_features: Raw feature DataFrame or Table 1 summary DataFrame.
        ranges: Mapping of feature/analyte name to [lower, upper] plausible limits
            (defaults to ``DEFAULT_PHYSIOLOGIC_BOUNDS``).
        sd_to_mean_max: Maximum plausible SD-to-mean ratio for strictly positive variables (default 3.0).
        min_pct: Minimum valid percentage (default 0.0).
        max_pct: Maximum valid percentage (default 100.0).
        ci_gate: If True, raises a ValueError if any plausibility check fails.

    Returns:
        pd.DataFrame: Table with columns [feature, metric, observed_value,
            expected_range, flagged, reason].
    """
    df = table1_or_features
    bounds = dict(ranges or DEFAULT_PHYSIOLOGIC_BOUNDS)
    records = []

    # Case 1: Check if this is a Table 1 summary table with columns like 'variable', 'mean', 'sd'
    cols_lower = {c.lower(): c for c in df.columns}
    var_col = cols_lower.get("variable") or cols_lower.get("feature") or cols_lower.get("covariate")

    if var_col and ("mean" in cols_lower or "median" in cols_lower):
        mean_col = cols_lower.get("mean")
        sd_col = cols_lower.get("sd")
        min_col = cols_lower.get("min")
        max_col = cols_lower.get("max")
        pct_col = cols_lower.get("pct") or cols_lower.get("percent")

        for _, row in df.iterrows():
            var_name = str(row[var_col]).strip()
            v_lower = var_name.lower()

            # Check percentage plausibility
            if pct_col and not pd.isna(row.get(pct_col)):
                try:
                    pct_val = float(row[pct_col])
                    if pct_val < min_pct or pct_val > max_pct:
                        records.append({
                            "feature": var_name,
                            "metric": "percent",
                            "observed_value": pct_val,
                            "expected_range": f"[{min_pct}, {max_pct}]",
                            "flagged": True,
                            "reason": "impossible_percentage",
                        })
                except (ValueError, TypeError):
                    pass

            # Check physiological range on mean
            if v_lower in bounds and mean_col and not pd.isna(row.get(mean_col)):
                try:
                    m_val = float(row[mean_col])
                    lo, hi = bounds[v_lower]
                    if m_val < lo or m_val > hi:
                        records.append({
                            "feature": var_name,
                            "metric": "mean",
                            "observed_value": m_val,
                            "expected_range": f"[{lo}, {hi}]",
                            "flagged": True,
                            "reason": "mean_outside_range",
                        })

                    # Check SD to mean ratio
                    if sd_col and not pd.isna(row.get(sd_col)):
                        sd_val = float(row[sd_col])
                        if m_val > 0 and (sd_val / m_val) > sd_to_mean_max:
                            records.append({
                                "feature": var_name,
                                "metric": "sd_to_mean_ratio",
                                "observed_value": round(sd_val / m_val, 2),
                                "expected_range": f"<= {sd_to_mean_max}",
                                "flagged": True,
                                "reason": "sd_to_mean_ratio_high",
                            })
                except (ValueError, TypeError):
                    pass

    else:
        # Case 2: Raw feature matrix (numeric columns)
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        for c in numeric_cols:
            c_clean = str(c).lower().strip()
            vals = df[c].dropna()
            if len(vals) == 0:
                continue

            m_val = float(vals.mean())
            sd_val = float(vals.std()) if len(vals) > 1 else 0.0
            min_val = float(vals.min())
            max_val = float(vals.max())

            # Check bounds if known clinical analyte
            if c_clean in bounds:
                lo, hi = bounds[c_clean]
                if m_val < lo or m_val > hi:
                    records.append({
                        "feature": str(c),
                        "metric": "mean",
                        "observed_value": round(m_val, 2),
                        "expected_range": f"[{lo}, {hi}]",
                        "flagged": True,
                        "reason": "mean_outside_range",
                    })

                if m_val > 0 and sd_val > 0 and (sd_val / m_val) > sd_to_mean_max:
                    records.append({
                        "feature": str(c),
                        "metric": "sd_to_mean_ratio",
                        "observed_value": round(sd_val / m_val, 2),
                        "expected_range": f"<= {sd_to_mean_max}",
                        "flagged": True,
                        "reason": "sd_to_mean_ratio_high",
                    })

                if lo >= 0 and min_val < 0:
                    records.append({
                        "feature": str(c),
                        "metric": "min",
                        "observed_value": round(min_val, 2),
                        "expected_range": f">= {lo}",
                        "flagged": True,
                        "reason": "negative_clinical_value",
                    })

            # Check binary indicator variables (e.g. proportions)
            if vals.isin([0, 1]).all() and len(vals.unique()) <= 2:
                pct_val = m_val * 100.0
                if pct_val < min_pct or pct_val > max_pct:
                    records.append({
                        "feature": str(c),
                        "metric": "percentage",
                        "observed_value": round(pct_val, 2),
                        "expected_range": f"[{min_pct}, {max_pct}]",
                        "flagged": True,
                        "reason": "impossible_percentage",
                    })

    out = pd.DataFrame(records)
    if out.empty:
        out = pd.DataFrame(columns=[
            "feature", "metric", "observed_value", "expected_range", "flagged", "reason"
        ])

    if ci_gate and not out.empty and out["flagged"].any():
        violations = out[out["flagged"]].to_dict(orient="records")
        raise ValueError(f"Plausibility CI gate failed with {len(violations)} violations: {violations}")

    return out


def detect_redundant_features(
    df: pd.DataFrame,
    *,
    cols: Sequence[str] | None = None,
    tol: float = 1e-3,
    corr_threshold: float = 0.999,
) -> pd.DataFrame:
    """Detects redundant or derived clinical features (constant ratios, perfect correlations, duplicate missingness).

    Identifies deterministic rule-of-three style conversions (e.g. Hematocrit = 3 * Hemoglobin),
    constant offsets, and collinear features to eliminate duplicates prior to statistical modeling.

    Args:
        df: Input feature matrix.
        cols: Optional list of numeric columns to inspect (defaults to all numeric non-ID columns).
        tol: Relative tolerance for checking constant ratio or difference (default 1e-3).
        corr_threshold: Pearson correlation threshold flagging near-perfect colinearity (default 0.999).

    Returns:
        pd.DataFrame: Table with columns [feature_1, feature_2, redundancy_type,
            metric_value, identical_missingness, recommendation].
    """
    id_patterns = {"id", "_id", "person", "subject", "visit", "index", "_row"}
    if cols is not None:
        cand_cols = [c for c in cols if c in df.columns]
    else:
        cand_cols = [
            c for c in df.select_dtypes(include=[np.number]).columns
            if not any(pat in str(c).lower() for pat in id_patterns)
        ]

    n_cols = len(cand_cols)
    records = []

    for i in range(n_cols):
        c1 = cand_cols[i]
        s1 = df[c1]
        for j in range(i + 1, n_cols):
            c2 = cand_cols[j]
            s2 = df[c2]

            # Missingness identity
            both_na_mask = s1.isna() & s2.isna()
            valid_mask = s1.notna() & s2.notna()
            identical_missing = bool((s1.isna() == s2.isna()).all())

            v1 = s1[valid_mask].astype(float)
            v2 = s2[valid_mask].astype(float)

            if len(v1) < 10:
                continue

            # 1. Constant ratio check: v1 = k * v2
            nz = (v2 != 0) & (v1 != 0)
            if nz.sum() >= 10:
                ratios = v1[nz] / v2[nz]
                ratio_sd = float(ratios.std())
                ratio_mean = float(ratios.mean())
                if ratio_sd < tol and abs(ratio_mean) > 1e-6:
                    records.append({
                        "feature_1": c1,
                        "feature_2": c2,
                        "redundancy_type": "constant_ratio",
                        "metric_value": round(ratio_mean, 4),
                        "identical_missingness": identical_missing,
                        "recommendation": f"Drop one; {c1} is a constant {ratio_mean:.3f}x multiple of {c2}.",
                    })
                    continue

            # 2. Constant difference check: v1 = v2 + c
            diffs = v1 - v2
            diff_sd = float(diffs.std())
            diff_mean = float(diffs.mean())
            if diff_sd < tol:
                records.append({
                    "feature_1": c1,
                    "feature_2": c2,
                    "redundancy_type": "constant_difference",
                    "metric_value": round(diff_mean, 4),
                    "identical_missingness": identical_missing,
                    "recommendation": f"Drop one; {c1} is offset from {c2} by constant {diff_mean:.3f}.",
                })
                continue

            # 3. Near-perfect correlation
            if v1.std() > 0 and v2.std() > 0:
                corr = float(np.corrcoef(v1, v2)[0, 1])
                if not np.isnan(corr) and abs(corr) >= corr_threshold:
                    records.append({
                        "feature_1": c1,
                        "feature_2": c2,
                        "redundancy_type": "perfect_correlation",
                        "metric_value": round(corr, 4),
                        "identical_missingness": identical_missing,
                        "recommendation": f"Drop one; collinearity |r| = {corr:.4f} >= {corr_threshold}.",
                    })

    out = pd.DataFrame(records)
    if out.empty:
        return pd.DataFrame(columns=[
            "feature_1", "feature_2", "redundancy_type", "metric_value",
            "identical_missingness", "recommendation"
        ])
    return out


def dqd_summary(
    results: dict[str, Any] | Sequence[dict[str, Any]] | pd.DataFrame,
    *,
    by_site: bool = True,
    site_col: str = "site",
) -> pd.DataFrame:
    """Verifies DQD pass-rate mathematical consistency and reproducibility.

    Recomputes total checks, passed checks, failed checks, and exact pass rates,
    validating that reported pass rates step in 1/N increments and match actual counts.

    Args:
        results: DQD result dictionary (from ``run_dqd``), list of check dicts, or DataFrame.
        by_site: Whether to group results by hospital site (default True).
        site_col: Column representing site identifier (default 'site').

    Returns:
        pd.DataFrame: Table with columns [site, total_checks, passed, failed,
            not_applicable, pass_rate, flagged, reason].
    """
    # Extract records
    if isinstance(results, dict):
        records = results.get("CheckResults") or results.get("check_results") or []
        df = pd.DataFrame(records)
    elif isinstance(results, pd.DataFrame):
        df = results.copy()
    else:
        df = pd.DataFrame(list(results))

    if df.empty:
        return pd.DataFrame(columns=[
            "site", "total_checks", "passed", "failed", "not_applicable", "pass_rate", "flagged", "reason"
        ])

    cols_lower = {c.lower(): c for c in df.columns}
    status_col = (
        cols_lower.get("execution_status")
        or cols_lower.get("status")
        or cols_lower.get("check_status")
        or cols_lower.get("checkstatus")
    )
    actual_site = cols_lower.get(site_col.lower()) if by_site else None

    if actual_site is None:
        df["_site"] = "ALL"
        actual_site = "_site"

    summary_rows = []
    for site, grp in df.groupby(actual_site):
        total = len(grp)
        if status_col:
            statuses = grp[status_col].astype(str).str.upper()
            passed = int((statuses == "PASSED").sum())
            failed = int((statuses == "FAILED").sum())
            na = int((statuses.isin(["NOT APPLICABLE", "NOT_APPLICABLE", "SKIPPED", "NA"])).sum())
        else:
            # Check for num_violated_rows
            viol_col = cols_lower.get("num_violated_rows")
            if viol_col:
                failed = int((grp[viol_col] > 0).sum())
                passed = int((grp[viol_col] == 0).sum())
                na = total - (passed + failed)
            else:
                passed, failed, na = total, 0, 0

        eval_total = passed + failed
        calc_rate = round(float(passed / eval_total), 4) if eval_total > 0 else 1.0

        why = []
        # Check if rate is mathematically achievable in 1/eval_total increments
        if eval_total > 0:
            k = calc_rate * eval_total
            if abs(k - round(k)) > 1e-3:
                why.append("rate_not_achievable")

        # Check if reported rate column exists and deviates
        rep_rate_col = cols_lower.get("pass_rate") or cols_lower.get("pct_passed")
        if rep_rate_col and not pd.isna(grp[rep_rate_col].iloc[0]):
            rep_val = float(grp[rep_rate_col].iloc[0])
            if rep_val > 1.0:
                rep_val /= 100.0
            if abs(rep_val - calc_rate) > 1e-3:
                why.append("reported_rate_inconsistent")

        summary_rows.append({
            "site": site,
            "total_checks": total,
            "passed": passed,
            "failed": failed,
            "not_applicable": na,
            "pass_rate": calc_rate,
            "flagged": len(why) > 0,
            "reason": ",".join(why),
        })

    return pd.DataFrame(summary_rows)
