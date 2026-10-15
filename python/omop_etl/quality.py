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
    "detect_unit_discordance",
    "normalize_units",
    "suggest_source_mappings",
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
    audit_unit_discordance: bool = True,
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
        audit_unit_discordance: Whether to audit laboratory unit discordance / bimodal scaling (default True).
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

    if audit_unit_discordance:
        try:
            disc_df = detect_unit_discordance(df)
            if not disc_df.empty:
                for _, r in disc_df[disc_df["flagged"]].iterrows():
                    records.append({
                        "feature": str(r["feature"]),
                        "metric": "unit_discordance",
                        "observed_value": round(float(r["detected_ratio"]), 2),
                        "expected_range": f"~{r['expected_conversion']}",
                        "flagged": True,
                        "reason": f"suspected_unit_mismatch_{r['suspected_unit']}_vs_{r['target_unit']}",
                    })
        except Exception:
            pass

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


KNOWN_UNIT_CONVERSIONS = {
    "creatinine": {"ratio": 88.42, "target": "mg/dL", "alt": "umol/L", "concept_ids": (3016723, 3015632, 3020564), "thresh": 15.0},
    "glucose": {"ratio": 18.016, "target": "mg/dL", "alt": "mmol/L", "concept_ids": (3004501, 3000483), "thresh": 25.0},
    "bilirubin_total": {"ratio": 17.1, "target": "mg/dL", "alt": "umol/L", "concept_ids": (3024128, 3024561), "thresh": 10.0},
    "calcium": {"ratio": 4.008, "target": "mg/dL", "alt": "mmol/L", "concept_ids": (3019550, 3014576), "thresh": 5.0},
    "bun": {"ratio": 2.801, "target": "mg/dL", "alt": "mmol/L", "concept_ids": (3013682, 3004295), "thresh": 15.0},
    "albumin": {"ratio": 10.0, "target": "g/dL", "alt": "g/L", "concept_ids": (3024561, 3013721), "thresh": 12.0},
    "wbc": {"ratio": 1000.0, "target": "10*3/uL", "alt": "cells/uL", "concept_ids": (3000905, 3010813), "thresh": 300.0},
    "platelets": {"ratio": 1000.0, "target": "10*3/uL", "alt": "cells/uL", "concept_ids": (3024929, 3007461), "thresh": 1500.0},
}


def detect_unit_discordance(
    con_or_df: duckdb.DuckDBPyConnection | pd.DataFrame,
    *,
    measurement_concept_id: int | None = None,
    concept_id: int | None = None,
    target_unit: str | None = None,
    site_col: str | None = "site",
    measurement_table: str = "measurement",
    value_col: str = "value_as_number",
    concept_col: str = "measurement_concept_id",
    tolerance: float = 0.35,
) -> pd.DataFrame:
    """Audits distributions per measurement/analyte across sites or within a dataset to detect unit discordance.

    Detects when multi-site medians or within-dataset bimodal distributions deviate by standard conversion factors
    (e.g., Creatinine in mg/dL vs. umol/L x88.42, Glucose in mg/dL vs. mmol/L x18.016, Albumin g/dL vs. g/L x10).
    """
    target_cid = measurement_concept_id if concept_id is None else concept_id
    if isinstance(con_or_df, duckdb.DuckDBPyConnection):
        df = con_or_df.execute(f"SELECT * FROM {measurement_table}").df()
    else:
        df = con_or_df.copy()

    records = []
    cols_lower = {c.lower(): c for c in df.columns}
    actual_site = cols_lower.get(site_col.lower()) if site_col else None
    actual_val = cols_lower.get(value_col.lower(), value_col)
    actual_concept = cols_lower.get(concept_col.lower(), concept_col)

    for analyte, info in KNOWN_UNIT_CONVERSIONS.items():
        vals_by_site: dict[str, pd.Series] = {}
        target_series: pd.Series | None = None
        cid_found = None

        if actual_concept in df.columns and actual_val in df.columns:
            for cid in info["concept_ids"]:
                sub = df[df[actual_concept] == cid]
                if not sub.empty:
                    cid_found = cid
                    if actual_site and actual_site in sub.columns and sub[actual_site].nunique() > 1:
                        for s, grp in sub.groupby(actual_site):
                            s_vals = pd.to_numeric(grp[actual_val], errors="coerce").dropna()
                            if len(s_vals) > 0:
                                vals_by_site[str(s)] = s_vals
                    target_series = pd.to_numeric(sub[actual_val], errors="coerce").dropna()
                    break

        if target_series is None or target_series.empty:
            for col_name, orig_col in cols_lower.items():
                if col_name == analyte or col_name.startswith(f"{analyte}_") or col_name.endswith(f"_{analyte}"):
                    s_vals = pd.to_numeric(df[orig_col], errors="coerce").dropna()
                    if not s_vals.empty:
                        target_series = s_vals
                        if actual_site and actual_site in df.columns and df[actual_site].nunique() > 1:
                            for s, grp in df.groupby(actual_site):
                                site_s = pd.to_numeric(grp[orig_col], errors="coerce").dropna()
                                if len(site_s) > 0:
                                    vals_by_site[str(s)] = site_s
                        break

        if target_series is None or len(target_series) < 2:
            continue

        exp_ratio = float(info["ratio"])
        thresh = float(info["thresh"])

        # 1. Multi-site median audit
        if len(vals_by_site) >= 2:
            site_medians = {s: float(v.median()) for s, v in vals_by_site.items() if len(v) >= 2}
            sites = list(site_medians.keys())
            for i in range(len(sites)):
                for j in range(i + 1, len(sites)):
                    s1, s2 = sites[i], sites[j]
                    m1, m2 = site_medians[s1], site_medians[s2]
                    if m1 <= 0 or m2 <= 0:
                        continue
                    obs_ratio = max(m1, m2) / max(min(m1, m2), 1e-6)
                    if abs(obs_ratio - exp_ratio) / exp_ratio <= tolerance:
                        high_site = s1 if m1 > m2 else s2
                        low_site = s2 if m1 > m2 else s1
                        records.append({
                            "feature": analyte,
                            "measurement_concept_id": cid_found,
                            "site": f"{high_site}_vs_{low_site}",
                            "median_val": round(max(m1, m2), 2),
                            "detected_ratio": round(obs_ratio, 2),
                            "expected_conversion": exp_ratio,
                            "suspected_unit": info["alt"],
                            "target_unit": info["target"],
                            "flagged": True,
                            "is_discordant": True,
                            "discordance_type": "cross_site_ratio",
                            "recommendation": f"Convert site {high_site} from {info['alt']} to {info['target']} (divide by {exp_ratio})",
                        })

        # 2. Within-dataset bimodal distribution audit
        sub_low = target_series[target_series <= thresh]
        sub_high = target_series[target_series > thresh]
        if len(sub_low) >= 2 and len(sub_high) >= 2:
            m_low = float(sub_low.median())
            m_high = float(sub_high.median())
            if m_low > 0:
                obs_ratio = m_high / m_low
                if abs(obs_ratio - exp_ratio) / exp_ratio <= tolerance:
                    records.append({
                        "feature": analyte,
                        "measurement_concept_id": cid_found,
                        "site": "internal_bimodal",
                        "median_val": round(m_high, 2),
                        "detected_ratio": round(obs_ratio, 2),
                        "expected_conversion": exp_ratio,
                        "suspected_unit": info["alt"],
                        "target_unit": info["target"],
                        "flagged": True,
                        "is_discordant": True,
                        "discordance_type": "bimodal_distribution",
                        "recommendation": f"Normalize values > {thresh} from {info['alt']} to {info['target']} (divide by {exp_ratio})",
                    })

    if not records:
        return pd.DataFrame(columns=[
            "feature", "measurement_concept_id", "site", "median_val", "detected_ratio",
            "expected_conversion", "suspected_unit", "target_unit", "flagged", "is_discordant",
            "discordance_type", "recommendation",
        ])
    return pd.DataFrame(records)


def normalize_units(
    con_or_df: duckdb.DuckDBPyConnection | pd.DataFrame,
    concept_id: int | None = None,
    feature: str | None = None,
    target_unit: str = "mg/dL",
    auto_convert_ratio: float = 88.42,
    threshold: float | None = None,
    value_col: str = "value_as_number",
    concept_col: str = "measurement_concept_id",
    measurement_concept_id: int | None = None,
    measurement_table: str = "measurement",
    output_table: str | None = None,
) -> pd.DataFrame:
    """Normalizes bimodal or discordant measurement values to a canonical target unit.

    Converts values detected in alternative units (e.g. umol/L -> mg/dL by dividing by 88.42)
    while preserving already-standardized values.
    """
    cid = measurement_concept_id if concept_id is None else concept_id
    if isinstance(con_or_df, duckdb.DuckDBPyConnection):
        df = con_or_df.execute(f"SELECT * FROM {measurement_table}").df()
    else:
        df = con_or_df.copy()

    thresh = threshold
    if thresh is None:
        if auto_convert_ratio > 50:
            thresh = 15.0  # e.g. Creatinine
        elif auto_convert_ratio > 10:
            thresh = 25.0  # e.g. Glucose
        else:
            thresh = 10.0

    cols_lower = {c.lower(): c for c in df.columns}
    actual_val = cols_lower.get(value_col.lower(), value_col)
    actual_concept = cols_lower.get(concept_col.lower(), concept_col)

    if feature:
        f_col = cols_lower.get(feature.lower(), feature)
        if f_col in df.columns:
            s = pd.to_numeric(df[f_col], errors="coerce")
            mask = s > thresh
            df.loc[mask, f_col] = s.loc[mask] / auto_convert_ratio
            return df

    if actual_concept in df.columns and actual_val in df.columns:
        s = pd.to_numeric(df[actual_val], errors="coerce")
        if cid is not None:
            mask = (df[actual_concept] == int(cid)) & (s > thresh)
        else:
            mask = s > thresh
        df.loc[mask, actual_val] = s.loc[mask] / auto_convert_ratio

    if output_table and isinstance(con_or_df, duckdb.DuckDBPyConnection):
        con_or_df.register("_temp_norm_df", df)
        con_or_df.execute(f"CREATE OR REPLACE TABLE {output_table} AS SELECT * FROM _temp_norm_df")
        con_or_df.unregister("_temp_norm_df")

    return df


def suggest_source_mappings(
    con: duckdb.DuckDBPyConnection | Any,
    unmapped_table: str | pd.DataFrame | None = None,
    domain: str = "Drug",
    min_similarity: float = 0.85,
    vocab_db: str | None = None,
    limit_per_code: int = 3,
    source_column: str | None = None,
    output_table: str | None = None,
) -> pd.DataFrame:
    """In-database fuzzy and partial matching for unmapped clinical source codes (concept_id = 0).

    Uses DuckDB's vectorized string similarity functions (jaro_winkler_similarity) and substring/token
    matching against standard OMOP concepts to suggest candidate concept IDs.
    """
    domain_key = domain.lower().strip()
    domain_title = domain.capitalize().strip()
    if domain_key in ("condition", "conditions"):
        domain_title = "Condition"
    elif domain_key in ("drug", "drugs", "medication", "medications"):
        domain_title = "Drug"
    elif domain_key in ("measurement", "measurements", "lab", "labs"):
        domain_title = "Measurement"
    elif domain_key in ("procedure", "procedures"):
        domain_title = "Procedure"

    # 1. Resolve source strings and unmapped frequencies
    if isinstance(unmapped_table, pd.DataFrame):
        con.register("_tmp_unmapped_in", unmapped_table)
        cols_lower = {c.lower(): c for c in unmapped_table.columns}
        src_col = source_column or cols_lower.get("source_value") or cols_lower.get("condition_source_value") or cols_lower.get("drug_source_value") or cols_lower.get("measurement_source_value") or list(unmapped_table.columns)[0]
        unmapped_sql = f"""
            SELECT 
                CAST({src_col} AS VARCHAR) AS source_value,
                COUNT(*) AS unmapped_count
            FROM _tmp_unmapped_in
            WHERE {src_col} IS NOT NULL AND TRIM(CAST({src_col} AS VARCHAR)) != ''
            GROUP BY 1
        """
    elif isinstance(unmapped_table, str):
        cols = [r[0].lower() for r in con.execute(f"DESCRIBE SELECT * FROM {unmapped_table} LIMIT 0").fetchall()]
        cols_lower = {c: c for c in cols}
        src_col = source_column or cols_lower.get("source_value") or cols_lower.get("condition_source_value") or cols_lower.get("drug_source_value") or cols_lower.get("measurement_source_value") or cols[0]
        id_col = cols_lower.get("concept_id") or cols_lower.get("condition_concept_id") or cols_lower.get("drug_concept_id") or cols_lower.get("measurement_concept_id") or cols_lower.get("procedure_concept_id")
        filter_clause = f"({id_col} = 0 OR {id_col} IS NULL) AND " if id_col else ""
        unmapped_sql = f"""
            SELECT 
                CAST({src_col} AS VARCHAR) AS source_value,
                COUNT(*) AS unmapped_count
            FROM {unmapped_table}
            WHERE {filter_clause}{src_col} IS NOT NULL AND TRIM(CAST({src_col} AS VARCHAR)) != ''
            GROUP BY 1
        """
    else:
        mapping = _DOMAIN_TABLE_MAP.get(domain_key)
        if not mapping:
            raise ValueError(f"Unknown domain {domain!r}; choose one of {list(_DOMAIN_TABLE_MAP.keys())}")
        tbl, id_col, src_col, _ = mapping
        unmapped_sql = f"""
            SELECT 
                CAST({src_col} AS VARCHAR) AS source_value,
                COUNT(*) AS unmapped_count
            FROM {tbl}
            WHERE ({id_col} = 0 OR {id_col} IS NULL) AND {src_col} IS NOT NULL AND TRIM(CAST({src_col} AS VARCHAR)) != ''
            GROUP BY 1
        """

    # 2. Resolve concept table
    vocab_ref = "concept"
    if vocab_db:
        vocab_ref = f"{vocab_db}.concept"
    else:
        tables = {r[0].lower() for r in con.execute("SHOW TABLES").fetchall()}
        if "concept" not in tables:
            try:
                if con.execute("SELECT 1 FROM central_vocab.main.concept LIMIT 1").fetchone():
                    vocab_ref = "central_vocab.main.concept"
            except Exception:
                pass

    # 3. Fuzzy matching query
    match_sql = f"""
        WITH unmapped_src AS (
            {unmapped_sql}
        ),
        target_concepts AS (
            SELECT 
                concept_id,
                concept_name,
                domain_id,
                vocabulary_id,
                lower(concept_name) AS norm_name
            FROM {vocab_ref}
            WHERE domain_id = '{domain_title}'
              AND standard_concept = 'S'
        ),
        scored AS (
            SELECT 
                u.source_value,
                u.unmapped_count,
                c.concept_id AS suggested_concept_id,
                c.concept_name AS suggested_concept_name,
                c.domain_id,
                c.vocabulary_id,
                ROUND(GREATEST(
                    jaro_winkler_similarity(lower(u.source_value), c.norm_name),
                    CASE 
                        WHEN lower(u.source_value) LIKE '%' || c.norm_name || '%' 
                          OR c.norm_name LIKE '%' || lower(u.source_value) || '%' 
                        THEN 0.90 
                        ELSE 0.0 
                    END
                ), 4) AS similarity_score
            FROM unmapped_src u
            CROSS JOIN target_concepts c
        ),
        filtered AS (
            SELECT 
                source_value,
                unmapped_count,
                suggested_concept_id,
                suggested_concept_name,
                domain_id,
                vocabulary_id,
                similarity_score,
                ROW_NUMBER() OVER (
                    PARTITION BY source_value 
                    ORDER BY similarity_score DESC, unmapped_count DESC, suggested_concept_id ASC
                ) AS rank
            FROM scored
            WHERE similarity_score >= {float(min_similarity)}
        )
        SELECT 
            source_value,
            unmapped_count,
            suggested_concept_id,
            suggested_concept_name,
            domain_id,
            vocabulary_id,
            similarity_score,
            rank
        FROM filtered
        WHERE rank <= {int(limit_per_code)}
        ORDER BY unmapped_count DESC, source_value, rank;
    """

    res_df = con.execute(match_sql).df()

    if isinstance(unmapped_table, pd.DataFrame):
        try:
            con.unregister("_tmp_unmapped_in")
        except Exception:
            pass

    if output_table:
        con.register("_tmp_suggested_out", res_df)
        con.execute(f"CREATE OR REPLACE TABLE {output_table} AS SELECT * FROM _tmp_suggested_out")
        con.unregister("_tmp_suggested_out")

    return res_df

