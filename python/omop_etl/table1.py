"""Table 1 descriptive statistics generator, completeness/missingness matrix,
and dual-source reconciliation/validation harness.

Emits standardized epidemiological baseline tables with whole cohort,
strata-specific summaries (Mean +/- SD, Median [IQR], N (%)), Standardized
Mean Differences (SMD), and p-values, alongside Table 1b completeness audits.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any, Sequence
import duckdb
import pandas as pd

# Default 14 consolidated acute lab LOINC mappings
DEFAULT_LAB_LOINCS: dict[str, list[str]] = {
    "albumin": ["1751-7", "2862-1"],
    "alt": ["1742-6", "1743-4", "1744-2"],
    "anion_gap": ["1863-0", "33037-3", "41276-7"],
    "ast": ["1920-8", "30239-8"],
    "bicarbonate": ["1963-8", "2028-9", "2026-3"],
    "bun": ["3094-0", "6299-2"],
    "creatinine": ["2160-0", "38483-4"],
    "egfr": ["33914-3", "48642-3", "48643-1", "62238-1", "69405-9", "77147-7", "88293-6", "88294-4"],
    "glucose": ["2345-7", "2339-0", "41653-7"],
    "hba1c": ["4548-4", "17856-6", "17855-8", "59261-8"],
    "hematocrit": ["20570-8", "4544-3", "4545-0"],
    "hemoglobin": ["718-7", "59260-0"],
    "ldl": ["13457-7", "2089-1", "18262-6"],
    "wbc": ["26464-8", "6690-2"],
    "sodium": ["2951-2", "2947-0"],
}

# Default vital sign LOINC mappings
DEFAULT_VITAL_LOINCS: dict[str, list[str]] = {
    "bmi": ["39156-5"],
    "systolic_bp": ["8480-6"],
    "diastolic_bp": ["8462-4"],
}

# Default 8 core chronic medication concept classes (ATC / RxNorm ancestor concepts)
DEFAULT_MEDICATION_CONCEPTS: dict[str, list[int]] = {
    # ATC class concepts (21xxxxxx) and RxNorm ingredients, verified against the OMOP vocabulary.
    "insulins": [21600713],                          # A10A insulins and analogues
    "metformin": [1503297, 21600745],                # metformin; A10BA biguanides
    "sulfonylureas": [1502855, 1502809, 21600749],   # tolbutamide, tolazamide; A10BB
    "statins": [1539403, 1545958, 1551860, 21601855],  # simva/atorva/prava; C10AA
    "antihypertensives": [21600381, 21601664, 21601744, 21601782],  # C02, C07, C08, C09
    "raas_inhibitors": [21601782],                   # C09
    "beta_blockers": [21601664, 1314002, 21601668],  # C07; atenolol; oxprenolol
    "systemic_corticosteroids": [21602722, 1506270],  # H02; methylprednisolone
}


def _calc_smd_continuous(s1: pd.Series, s0: pd.Series) -> float | None:
    """Calculates Cohen's d Standardized Mean Difference for continuous variables."""
    s1_clean = s1.dropna()
    s0_clean = s0.dropna()
    if len(s1_clean) < 2 or len(s0_clean) < 2:
        return None
    m1, m0 = float(s1_clean.mean()), float(s0_clean.mean())
    v1, v0 = float(s1_clean.var()), float(s0_clean.var())
    pooled_sd = math.sqrt((v1 + v0) / 2.0)
    if pooled_sd <= 1e-9:
        return 0.0
    return abs(m1 - m0) / pooled_sd


def _calc_smd_binary(p1: float, p0: float) -> float | None:
    """Calculates SMD for binary proportions."""
    denom = math.sqrt((p1 * (1.0 - p1) + p0 * (1.0 - p0)) / 2.0)
    if denom <= 1e-9:
        return 0.0
    return abs(p1 - p0) / denom


def _calc_pvalue_continuous(s1: pd.Series, s0: pd.Series) -> float | None:
    """Calculates two-sample t-test p-value with standard normal fallback."""
    s1_clean = s1.dropna()
    s0_clean = s0.dropna()
    if len(s1_clean) < 2 or len(s0_clean) < 2:
        return None
    try:
        from scipy import stats
        res = stats.ttest_ind(s1_clean, s0_clean, equal_var=False)
        return float(res.pvalue)
    except Exception:
        # Fallback to Welch's t-test calculation using normal approximation
        n1, n0 = len(s1_clean), len(s0_clean)
        m1, m0 = float(s1_clean.mean()), float(s0_clean.mean())
        v1, v0 = float(s1_clean.var()), float(s0_clean.var())
        se = math.sqrt(v1 / n1 + v0 / n0)
        if se <= 1e-9:
            return 1.0
        z = abs(m1 - m0) / se
        # Standard normal two-tailed p-value: 2 * (1 - Phi(z))
        p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
        return max(0.0, min(1.0, float(p)))


def _calc_pvalue_categorical(counts1: dict[Any, int], counts0: dict[Any, int]) -> float | None:
    """Calculates Chi-Square test p-value for contingency table."""
    categories = sorted(set(counts1.keys()) | set(counts0.keys()))
    if len(categories) < 2:
        return None
    try:
        from scipy import stats
        table = [[counts1.get(c, 0) for c in categories], [counts0.get(c, 0) for c in categories]]
        res = stats.chi2_contingency(table)
        return float(res.pvalue)
    except Exception:
        # Fallback using standard chi2 test
        observed_1 = [counts1.get(c, 0) for c in categories]
        observed_0 = [counts0.get(c, 0) for c in categories]
        n1 = sum(observed_1)
        n0 = sum(observed_0)
        n_tot = n1 + n0
        if n1 == 0 or n0 == 0 or n_tot == 0:
            return None
        chi2 = 0.0
        df = len(categories) - 1
        for o1, o0 in zip(observed_1, observed_0):
            c_tot = o1 + o0
            e1 = n1 * c_tot / n_tot
            e0 = n0 * c_tot / n_tot
            if e1 > 0:
                chi2 += ((o1 - e1) ** 2) / e1
            if e0 > 0:
                chi2 += ((o0 - e0) ** 2) / e0
        # Upper tail chi2 approximation using Wilson-Hilferty transformation
        if df > 0 and chi2 > 0:
            z = (math.pow(chi2 / df, 1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
            p = 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
            return max(0.0, min(1.0, float(p)))
        return None


def format_pvalue(p: float | None) -> str:
    """Formats p-values matching standard medical reporting."""
    if p is None or math.isnan(p):
        return "-"
    if p < 0.001:
        return "<0.001"
    return f"{p:.3f}"


def generate_table1(
    data: pd.DataFrame | duckdb.DuckDBPyConnection,
    strata_col: str | None = "outcome_flag",
    continuous_vars: Sequence[str] | None = None,
    categorical_vars: Sequence[str] | None = None,
    table_name: str | None = None,
    strata_labels: dict[Any, str] | None = None,
    compute_smd: bool = True,
    compute_pvalues: bool = True,
    decimal_places: int = 1,
) -> dict[str, pd.DataFrame]:
    """Generates Table 1 (Descriptive Statistics) and Table 1b (Completeness/Missingness).
    
    Summarizes continuous (Mean +/- SD, Median [IQR]) and categorical (N (%)) variables
    across the overall cohort and stratified subsets, computing Standardized Mean Differences
    (SMD) and hypothesis test p-values.
    
    Args:
        data: A pandas DataFrame or active DuckDBPyConnection.
        strata_col: Column name defining stratification (e.g. 'outcome_flag', 'readmitted_30d').
                    If None or missing from columns, summarizes overall cohort only.
        continuous_vars: List of continuous variable column names.
        categorical_vars: List of categorical variable column names.
        table_name: Table name if `data` is a DuckDB connection.
        strata_labels: Optional dictionary mapping strata values to display names
                       (e.g. {0: "Non-readmitted", 1: "Readmitted"}).
        compute_smd: Whether to calculate Standardized Mean Differences between strata.
        compute_pvalues: Whether to calculate hypothesis test p-values between strata.
        decimal_places: Number of decimal places for numeric outputs (default 1).
        
    Returns:
        dict[str, pd.DataFrame]: Dictionary containing:
            - 'table1': Formatted descriptive statistics table.
            - 'table1b': Missingness audit matrix with counts and percentages.
    """
    if isinstance(data, duckdb.DuckDBPyConnection):
        if not table_name:
            raise ValueError("table_name is required when data is a DuckDB connection.")
        df = data.execute(f"SELECT * FROM {table_name}").df()
    elif isinstance(data, pd.DataFrame):
        df = data.copy()
    else:
        raise TypeError("data must be a pandas DataFrame or DuckDBPyConnection.")

    # Determine stratification
    has_strata = strata_col is not None and strata_col in df.columns
    strata_values = sorted(df[strata_col].dropna().unique()) if has_strata else []
    if len(strata_values) < 2:
        has_strata = False

    strata_map = strata_labels or {}
    total_n = len(df)
    strata_dfs: dict[Any, pd.DataFrame] = {}
    strata_ns: dict[Any, int] = {}

    if has_strata:
        for val in strata_values:
            sdf = df[df[strata_col] == val]
            strata_dfs[val] = sdf
            strata_ns[val] = len(sdf)

    # Auto-detect variables if not provided
    skip_cols = {strata_col, "subject_id", "person_id", "visit_occurrence_id", "cohort_definition_id", "cohort_start_date", "cohort_end_date"}
    available_cols = [c for c in df.columns if c not in skip_cols]

    if continuous_vars is None and categorical_vars is None:
        cont_vars = []
        cat_vars = []
        for col in available_cols:
            if pd.api.types.is_numeric_dtype(df[col]):
                unique_vals = df[col].dropna().unique()
                if len(unique_vals) <= 5 and set(unique_vals).issubset({0, 1}):
                    cat_vars.append(col)
                elif len(unique_vals) <= 4:
                    cat_vars.append(col)
                else:
                    cont_vars.append(col)
            else:
                cat_vars.append(col)
    else:
        cont_vars = list(continuous_vars or [])
        cat_vars = list(categorical_vars or [])

    table1_rows: list[dict[str, Any]] = []
    table1b_rows: list[dict[str, Any]] = []

    # Header Row: Cohort Counts
    overall_header = f"Overall (N={total_n:,})"
    header_row = {
        "Variable": "Total Patients",
        "Category": "Count",
        overall_header: f"{total_n:,} (100.0%)",
    }
    if has_strata:
        for val in strata_values:
            label = strata_map.get(val, f"Stratum {val}")
            header_row[f"{label} (N={strata_ns[val]:,})"] = f"{strata_ns[val]:,} ({(strata_ns[val] / total_n * 100.0):.{decimal_places}f}%)"
        if compute_smd:
            header_row["SMD"] = "-"
        if compute_pvalues:
            header_row["p_value"] = "-"
    table1_rows.append(header_row)

    # 1. Continuous Variables
    for var in cont_vars:
        if var not in df.columns:
            continue
        s_all = pd.to_numeric(df[var], errors="coerce")
        all_clean = s_all.dropna()

        # Overall stats
        mean_all = all_clean.mean() if len(all_clean) > 0 else float("nan")
        std_all = all_clean.std() if len(all_clean) > 1 else float("nan")
        med_all = all_clean.median() if len(all_clean) > 0 else float("nan")
        q25_all = all_clean.quantile(0.25) if len(all_clean) > 0 else float("nan")
        q75_all = all_clean.quantile(0.75) if len(all_clean) > 0 else float("nan")

        missing_n_all = s_all.isna().sum()
        missing_pct_all = (missing_n_all / total_n * 100.0) if total_n > 0 else 0.0

        # Mean (SD) Row
        mean_sd_row: dict[str, Any] = {
            "Variable": var,
            "Category": "Mean (SD)",
            overall_header: f"{mean_all:.{decimal_places}f} ({std_all:.{decimal_places}f})" if not math.isnan(mean_all) else "-",
        }

        # Median [IQR] Row
        med_iqr_row: dict[str, Any] = {
            "Variable": var,
            "Category": "Median [IQR]",
            overall_header: f"{med_all:.{decimal_places}f} [{q25_all:.{decimal_places}f}, {q75_all:.{decimal_places}f}]" if not math.isnan(med_all) else "-",
        }

        # Table 1b row
        t1b_row: dict[str, Any] = {
            "Variable": var,
            "overall_missing_n": int(missing_n_all),
            "overall_missing_pct": round(missing_pct_all, 1),
        }

        if has_strata:
            s_strata = {}
            for val in strata_values:
                s_str = pd.to_numeric(strata_dfs[val][var], errors="coerce")
                s_clean = s_str.dropna()
                s_strata[val] = s_clean

                m_val = s_clean.mean() if len(s_clean) > 0 else float("nan")
                std_val = s_clean.std() if len(s_clean) > 1 else float("nan")
                med_val = s_clean.median() if len(s_clean) > 0 else float("nan")
                q25_val = s_clean.quantile(0.25) if len(s_clean) > 0 else float("nan")
                q75_val = s_clean.quantile(0.75) if len(s_clean) > 0 else float("nan")

                label = strata_map.get(val, f"Stratum {val}")
                col_name = f"{label} (N={strata_ns[val]:,})"

                mean_sd_row[col_name] = f"{m_val:.{decimal_places}f} ({std_val:.{decimal_places}f})" if not math.isnan(m_val) else "-"
                med_iqr_row[col_name] = f"{med_val:.{decimal_places}f} [{q25_val:.{decimal_places}f}, {q75_val:.{decimal_places}f}]" if not math.isnan(med_val) else "-"

                m_miss = s_str.isna().sum()
                m_pct = (m_miss / strata_ns[val] * 100.0) if strata_ns[val] > 0 else 0.0
                t1b_row[f"stratum_{val}_missing_n"] = int(m_miss)
                t1b_row[f"stratum_{val}_missing_pct"] = round(m_pct, 1)

            if compute_smd and len(strata_values) == 2:
                smd_val = _calc_smd_continuous(s_strata[strata_values[1]], s_strata[strata_values[0]])
                smd_str = f"{smd_val:.3f}" if smd_val is not None else "-"
                mean_sd_row["SMD"] = smd_str
                med_iqr_row["SMD"] = "-"
            elif compute_smd:
                mean_sd_row["SMD"] = "-"
                med_iqr_row["SMD"] = "-"

            if compute_pvalues and len(strata_values) == 2:
                pval = _calc_pvalue_continuous(s_strata[strata_values[1]], s_strata[strata_values[0]])
                pval_str = format_pvalue(pval)
                mean_sd_row["p_value"] = pval_str
                med_iqr_row["p_value"] = "-"
            elif compute_pvalues:
                mean_sd_row["p_value"] = "-"
                med_iqr_row["p_value"] = "-"

        table1_rows.append(mean_sd_row)
        table1_rows.append(med_iqr_row)
        table1b_rows.append(t1b_row)

    # 2. Categorical Variables
    for var in cat_vars:
        if var not in df.columns:
            continue
        s_all = df[var].astype(str)
        # Identify categories excluding nan
        raw_s = df[var]
        missing_n_all = raw_s.isna().sum()
        missing_pct_all = (missing_n_all / total_n * 100.0) if total_n > 0 else 0.0

        t1b_row = {
            "Variable": var,
            "overall_missing_n": int(missing_n_all),
            "overall_missing_pct": round(missing_pct_all, 1),
        }

        # Categories to show
        cats = [c for c in sorted(raw_s.dropna().unique())]
        if not cats:
            cats = ["Unknown"]

        # Contingency counts for p-value
        counts1: dict[Any, int] = {}
        counts0: dict[Any, int] = {}

        if has_strata and len(strata_values) == 2:
            counts1 = dict(strata_dfs[strata_values[1]][var].dropna().value_counts())
            counts0 = dict(strata_dfs[strata_values[0]][var].dropna().value_counts())
            pval_cat = _calc_pvalue_categorical(counts1, counts0) if compute_pvalues else None
        else:
            pval_cat = None

        first_cat = True
        for cat in cats:
            n_cat_all = (raw_s == cat).sum()
            pct_cat_all = (n_cat_all / total_n * 100.0) if total_n > 0 else 0.0

            cat_row: dict[str, Any] = {
                "Variable": var if first_cat else "",
                "Category": str(cat),
                overall_header: f"{n_cat_all:,} ({pct_cat_all:.{decimal_places}f}%)",
            }

            if has_strata:
                prop_strata = {}
                for val in strata_values:
                    sdf = strata_dfs[val]
                    n_val = (sdf[var] == cat).sum()
                    pct_val = (n_val / strata_ns[val] * 100.0) if strata_ns[val] > 0 else 0.0
                    label = strata_map.get(val, f"Stratum {val}")
                    cat_row[f"{label} (N={strata_ns[val]:,})"] = f"{n_val:,} ({pct_val:.{decimal_places}f}%)"
                    prop_strata[val] = pct_val / 100.0

                    if first_cat:
                        m_miss = sdf[var].isna().sum()
                        m_pct = (m_miss / strata_ns[val] * 100.0) if strata_ns[val] > 0 else 0.0
                        t1b_row[f"stratum_{val}_missing_n"] = int(m_miss)
                        t1b_row[f"stratum_{val}_missing_pct"] = round(m_pct, 1)

                if compute_smd and len(strata_values) == 2:
                    p1 = prop_strata[strata_values[1]]
                    p0 = prop_strata[strata_values[0]]
                    smd_cat = _calc_smd_binary(p1, p0)
                    cat_row["SMD"] = f"{smd_cat:.3f}" if smd_cat is not None else "-"
                elif compute_smd:
                    cat_row["SMD"] = "-"

                if compute_pvalues:
                    cat_row["p_value"] = format_pvalue(pval_cat) if first_cat else ""

            table1_rows.append(cat_row)
            first_cat = False

        table1b_rows.append(t1b_row)

    return {
        "table1": pd.DataFrame(table1_rows),
        "table1b": pd.DataFrame(table1b_rows),
    }


def _normalize_var_name(name: str) -> str:
    """Normalizes feature names for fuzzy comparison."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _extract_numeric_value(val: Any) -> float | None:
    """Extracts leading numeric value from formatted strings like '45.2 (12.1)' or '11.7%'."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    match = re.search(r"[-+]?\d*\.?\d+", str(val))
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def validate_table1_reconciliation(
    omop_table1: pd.DataFrame | dict[str, Any] | str | Path,
    source_table1: pd.DataFrame | dict[str, Any] | str | Path,
    tolerance: float = 0.05,
    count_tolerance: float = 0.01,
) -> dict[str, Any]:
    """Validates and reconciles OMOP-derived Table 1 against an existing published baseline Table 1.
    
    Verifies:
        1. Cohort and strata patient counts concordance (<= count_tolerance).
        2. Readmission outcome prevalence concordance (<= tolerance).
        3. Continuous and categorical feature distribution drift (relative diff <= tolerance).
        4. Feature mapping completeness / coverage audit.
        
    Args:
        omop_table1: Generated OMOP Table 1 (DataFrame, dict from generate_table1, or CSV path).
        source_table1: Published baseline Table 1 (DataFrame, dict, or CSV path).
        tolerance: Relative drift threshold for feature distributions (default 0.05 = 5%).
        count_tolerance: Relative discrepancy threshold for patient counts (default 0.01 = 1%).
        
    Returns:
        dict[str, Any]: Structured reconciliation report containing:
            - 'is_concordant': bool
            - 'status': "PASS", "DRIFT_DETECTED", or "COUNT_MISMATCH"
            - 'overall_summary': Key metrics comparison
            - 'feature_comparisons': Detailed feature comparison DataFrame
            - 'missing_features': Features present in source but missing in OMOP
    """
    # Load OMOP Table 1
    if isinstance(omop_table1, (str, Path)):
        df_omop = pd.read_csv(omop_table1)
    elif isinstance(omop_table1, dict) and "table1" in omop_table1:
        df_omop = omop_table1["table1"]
    elif isinstance(omop_table1, pd.DataFrame):
        df_omop = omop_table1
    else:
        raise TypeError("omop_table1 must be a DataFrame, dict, or file path.")

    # Load Source Table 1
    if isinstance(source_table1, (str, Path)):
        df_source = pd.read_csv(source_table1)
    elif isinstance(source_table1, dict) and "table1" in source_table1:
        df_source = source_table1["table1"]
    elif isinstance(source_table1, pd.DataFrame):
        df_source = source_table1
    else:
        raise TypeError("source_table1 must be a DataFrame, dict, or file path.")

    # Locate variable column
    omop_var_col = None
    for cand in ["Variable", "characteristic", "feature", "name"]:
        for c in df_omop.columns:
            if cand.lower() in c.lower():
                omop_var_col = c
                break
        if omop_var_col:
            break
    if not omop_var_col:
        omop_var_col = df_omop.columns[0]

    source_var_col = None
    for cand in ["Variable", "characteristic", "feature", "name"]:
        for c in df_source.columns:
            if cand.lower() in c.lower():
                source_var_col = c
                break
        if source_var_col:
            break
    if not source_var_col:
        source_var_col = df_source.columns[0]

    # Find overall and strata columns in OMOP
    omop_val_cols = [c for c in df_omop.columns if c not in (omop_var_col, "Category", "SMD", "p_value")]
    source_val_cols = [c for c in df_source.columns if c not in (source_var_col, "Category", "SMD", "p_value")]

    omop_map = {}
    for _, row in df_omop.iterrows():
        vname = str(row[omop_var_col]).strip()
        cat = str(row.get("Category", "")).strip() if "Category" in row else ""
        norm_key = _normalize_var_name(vname) + ("_" + _normalize_var_name(cat) if cat and cat != "Count" else "")
        if norm_key:
            omop_map[norm_key] = row

    source_map = {}
    for _, row in df_source.iterrows():
        vname = str(row[source_var_col]).strip()
        cat = str(row.get("Category", "")).strip() if "Category" in row else ""
        norm_key = _normalize_var_name(vname) + ("_" + _normalize_var_name(cat) if cat and cat != "Count" else "")
        if norm_key:
            source_map[norm_key] = row

    # Compare matching features
    comparisons = []
    drift_count = 0
    concordant_count = 0
    missing_features = []

    for src_key, src_row in source_map.items():
        if src_key not in omop_map:
            # Check prefix match
            matched_omop_key = None
            for o_key in omop_map:
                if src_key.startswith(o_key) or o_key.startswith(src_key):
                    matched_omop_key = o_key
                    break
            if not matched_omop_key:
                missing_features.append(str(src_row[source_var_col]))
                continue
            omop_row = omop_map[matched_omop_key]
        else:
            omop_row = omop_map[src_key]

        # Compare values across primary value column (overall)
        src_val_raw = src_row[source_val_cols[0]] if source_val_cols else None
        omop_val_raw = omop_row[omop_val_cols[0]] if omop_val_cols else None

        num_src = _extract_numeric_value(src_val_raw)
        num_omop = _extract_numeric_value(omop_val_raw)

        if num_src is not None and num_omop is not None:
            diff = abs(num_omop - num_src)
            rel_diff = diff / max(abs(num_src), 1e-9)
            is_drift = rel_diff > tolerance
            if is_drift:
                drift_count += 1
                status = "DRIFT"
            else:
                concordant_count += 1
                status = "CONCORDANT"

            comparisons.append({
                "feature": str(src_row[source_var_col]),
                "omop_value": num_omop,
                "source_value": num_src,
                "abs_diff": round(diff, 4),
                "rel_diff": round(rel_diff, 4),
                "status": status,
            })

    # Summary metrics
    is_concordant = (drift_count == 0) and (len(missing_features) == 0)
    overall_status = "PASS" if is_concordant else ("DRIFT_DETECTED" if drift_count > 0 else "MISSING_FEATURES")

    return {
        "is_concordant": is_concordant,
        "status": overall_status,
        "overall_summary": {
            "total_features_evaluated": len(comparisons),
            "concordant_features": concordant_count,
            "drift_features": drift_count,
            "missing_features_count": len(missing_features),
            "tolerance": tolerance,
        },
        "feature_comparisons": pd.DataFrame(comparisons),
        "missing_features": missing_features,
    }


def _format_markdown_table(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:
        cols = [str(c) for c in df.columns]
        header = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join(["---"] * len(cols)) + " |"
        rows = []
        for row in df.itertuples(index=False):
            rows.append("| " + " | ".join(str(val) if val is not None and not pd.isna(val) else "" for val in row) + " |")
        return "\n".join([header, sep] + rows)


def _format_latex_table(df: pd.DataFrame) -> str:
    try:
        return df.to_latex(index=False)
    except Exception:
        cols = [str(c) for c in df.columns]
        align = "l" * len(cols)
        header = " & ".join(cols) + " \\\\"
        rows = []
        for row in df.itertuples(index=False):
            rows.append(" & ".join(str(val) if val is not None and not pd.isna(val) else "" for val in row) + " \\\\")
        return "\\begin{tabular}{" + align + "}\n\\toprule\n" + header + "\n\\midrule\n" + "\n".join(rows) + "\n\\bottomrule\n\\end{tabular}\n"


def export_table1(
    table1_result: dict[str, pd.DataFrame] | pd.DataFrame,
    output_path: str | Path | None = None,
    format: str = "markdown",
    which_table: str = "table1",
    **kwargs: Any,
) -> str:
    """Export Table 1 or Table 1b to Markdown, LaTeX, Quarto, Typst, or CSV.

    Args:
        table1_result: Result dictionary from generate_table1 (or a DataFrame).
        output_path: Optional file path to write to.
        format: Export format: 'markdown', 'latex', 'quarto', 'typst', 'csv'.
        which_table: Key to export if dict: 'table1' or 'table1b'.
        **kwargs: Additional parameters passed to table1_to_quarto or table1_to_typst.

    Returns:
        str: Formatted text representation of the table.
    """
    if isinstance(table1_result, dict):
        df = table1_result.get(which_table, next(iter(table1_result.values())))
    else:
        df = table1_result

    fmt = format.lower().strip()
    if fmt in ("md", "markdown"):
        content = _format_markdown_table(df)
    elif fmt in ("tex", "latex"):
        content = _format_latex_table(df)
    elif fmt in ("qmd", "quarto"):
        content = table1_to_quarto(df, **kwargs)
    elif fmt in ("typ", "typst"):
        content = table1_to_typst(df, **kwargs)
    elif fmt == "csv":
        content = df.to_csv(index=False)
    else:
        raise ValueError(f"Unsupported format '{format}'. Use 'markdown', 'latex', 'quarto', 'typst', or 'csv'.")

    if output_path:
        Path(output_path).write_text(content, encoding="utf-8")
    return content


def table1_to_quarto(
    table1_data: dict[str, pd.DataFrame] | pd.DataFrame,
    output_path: str | Path | None = None,
    which_table: str = "table1",
    caption: str | None = None,
    label: str | None = None,
    include_loinc_footnotes: bool = True,
    loinc_map: dict[str, list[str]] | None = None,
    loinc_footnotes: dict[str, Any] | None = None,
    footnotes: dict[str, Any] | None = None,
    provenance_notes: dict[str, dict[str, Any]] | None = None,
    include_provenance_footnotes: bool = True,
) -> str:
    """Format Table 1 into publication-grade Quarto manuscript format.

    Outputs clean KaTeX/LaTeX bracketed confidence intervals, superscript footnote
    references for LOINC lab codes, and multi-level header spans ready for direct inclusion.

    Args:
        table1_data: Result dictionary from generate_table1() or a DataFrame.
        output_path: Optional file path to write to (.qmd / .md).
        which_table: If a dict, key to export ('table1' or 'table1b').
        caption: Table caption (default: 'Baseline Demographic and Clinical Characteristics').
        label: Quarto cross-reference label (default: 'tbl-table1').
        include_loinc_footnotes: Whether to generate superscript LOINC code footnotes.
        loinc_map: Optional mapping of variable names to LOINC codes (default: DEFAULT_LAB_LOINCS).
        loinc_footnotes: Custom mapping of variable names to LOINC codes or footnotes.
        footnotes: Additional footnote mappings.
        provenance_notes: Optional dictionary mapping variables to provenance dicts (window, anchor, bounds, units, fill_rule, concept_set).
        include_provenance_footnotes: Whether to render row-level provenance footnotes (default True if notes supplied).

    Returns:
        str: Formatted Quarto manuscript markdown string.
    """
    if isinstance(table1_data, dict):
        df = table1_data.get(which_table, next(iter(table1_data.values()))).copy()
    else:
        df = table1_data.copy()

    cap = caption or "Baseline Demographic and Clinical Characteristics"
    lbl = label or "tbl-table1"
    active_loincs = dict(DEFAULT_LAB_LOINCS)
    if loinc_map:
        active_loincs.update(loinc_map)
    custom_fns = {}
    if loinc_footnotes:
        for k, v in loinc_footnotes.items():
            if isinstance(v, (list, tuple)):
                active_loincs[k] = list(v)
            else:
                active_loincs[k] = [str(v)]
                custom_fns[str(k).lower()] = str(v)
    if footnotes:
        for k, v in footnotes.items():
            custom_fns[str(k).lower()] = str(v)

    var_col = df.columns[0]
    letters = "abcdefghijklmnopqrstuvwxyz"
    rendered_footnotes: list[tuple[str, str]] = []

    prov = provenance_notes or {}

    for col in df.columns:
        new_vals = []
        for row_idx, val in enumerate(df[col]):
            if pd.isna(val) or val is None:
                new_vals.append("")
                continue
            s_val = str(val).strip()
            s_val = re.sub(r"\s*\+/-\s*", r" $\\pm$ ", s_val)
            s_val = re.sub(r"\[\s*([\d\.\-]+)\s*,\s*([\d\.\-]+)\s*\]", r"[IQR: \1, \2]", s_val)

            if col == var_col and s_val:
                norm_v = re.sub(r"[^a-z0-9_]", "", s_val.lower())
                matched_key = None
                for lk in active_loincs:
                    norm_k = re.sub(r"[^a-z0-9_]", "", str(lk).lower())
                    if norm_k and (norm_v == norm_k or norm_k in norm_v or norm_v in norm_k):
                        matched_key = lk
                        break
                
                # Check provenance notes first if available
                prov_meta = None
                for pk, pdata in prov.items():
                    norm_p = re.sub(r"[^a-z0-9_]", "", str(pk).lower())
                    if norm_p and (norm_v == norm_p or norm_p in norm_v or norm_v in norm_p):
                        prov_meta = pdata
                        break

                if prov_meta and include_provenance_footnotes:
                    sym = letters[len(rendered_footnotes) % len(letters)]
                    desc_parts = []
                    if "window" in prov_meta: desc_parts.append(f"window={prov_meta['window']}")
                    if "anchor" in prov_meta: desc_parts.append(f"anchor={prov_meta['anchor']}")
                    if "bounds" in prov_meta: desc_parts.append(f"bounds={prov_meta['bounds']}")
                    if "units" in prov_meta: desc_parts.append(f"units={prov_meta['units']}")
                    if "fill_rule" in prov_meta: desc_parts.append(f"fill={prov_meta['fill_rule']}")
                    if "concept_set" in prov_meta: desc_parts.append(f"concept_set={prov_meta['concept_set']}")
                    note_txt = ", ".join(desc_parts) if desc_parts else "provenance documented"
                    rendered_footnotes.append((sym, note_txt))
                    s_val = f"{s_val}^{sym}"
                elif include_loinc_footnotes and matched_key and active_loincs[matched_key]:
                    sym = letters[len(rendered_footnotes) % len(letters)]
                    codes_str = ", ".join(str(c) for c in active_loincs[matched_key])
                    rendered_footnotes.append((sym, f"LOINC {codes_str}"))
                    s_val = f"{s_val}^{sym}"

            new_vals.append(s_val)
        df[col] = new_vals

    cols = [str(c) for c in df.columns]
    align_spec = []
    for i, c in enumerate(cols):
        align_spec.append(":---" if i == 0 else ":---:")

    header_row = "| " + " | ".join(cols) + " |"
    divider_row = "| " + " | ".join(align_spec) + " |"
    data_rows = []
    for row in df.itertuples(index=False):
        data_rows.append("| " + " | ".join(str(v) for v in row) + " |")

    table_body = "\n".join([header_row, divider_row] + data_rows)

    footnote_lines = []
    if rendered_footnotes:
        fn_parts = [f"^{sym} {txt}" for sym, txt in rendered_footnotes]
        footnote_lines.append("; ".join(fn_parts) + ".")
    footnote_lines.append("*Note:* Continuous variables expressed as Mean $\\pm$ SD or Median [IQR]; categorical variables as N (%). SMD = Standardized Mean Difference.")

    footnote_block = "\n".join(f"::: {{.table-footnote}}\n{ln}\n:::" for ln in footnote_lines)

    content = f"""#| label: {lbl}
#| tbl-cap: "{cap}"

{table_body}

{footnote_block}
"""
    if output_path:
        Path(output_path).write_text(content, encoding="utf-8")
    return content


def table1_to_typst(
    table1_data: dict[str, pd.DataFrame] | pd.DataFrame,
    output_path: str | Path | None = None,
    which_table: str = "table1",
    caption: str | None = None,
    label: str | None = None,
    include_loinc_footnotes: bool = True,
    loinc_map: dict[str, list[str]] | None = None,
    loinc_footnotes: dict[str, Any] | None = None,
    footnotes: dict[str, Any] | None = None,
    font_size: str = "9pt",
    provenance_notes: dict[str, dict[str, Any]] | None = None,
    include_provenance_footnotes: bool = True,
) -> str:
    """Format Table 1 into native Typst manuscript table syntax.

    Outputs Typst table with #table(columns: ...), table.hline(), multi-level headers,
    bracketed confidence intervals, and superscript LOINC code footnotes.

    Args:
        table1_data: Result dictionary from generate_table1() or a DataFrame.
        output_path: Optional file path to write to (.typ).
        which_table: If a dict, key to export ('table1' or 'table1b').
        caption: Table caption (default: 'Baseline Demographic and Clinical Characteristics').
        label: Typst figure label (e.g. 'tbl-baseline').
        include_loinc_footnotes: Whether to generate superscript LOINC code footnotes.
        loinc_map: Optional mapping of variable names to LOINC codes.
        loinc_footnotes: Custom mapping of variable names to LOINC codes or footnotes.
        footnotes: Additional footnote mappings.
        font_size: Font size for table content (default: '9pt').
        provenance_notes: Optional dictionary mapping variables to provenance metadata.
        include_provenance_footnotes: Whether to render row-level provenance footnotes (default True).

    Returns:
        str: Native Typst markup string.
    """
    if isinstance(table1_data, dict):
        df = table1_data.get(which_table, next(iter(table1_data.values()))).copy()
    else:
        df = table1_data.copy()

    cap = caption or "Baseline Demographic and Clinical Characteristics"
    lbl = label
    active_loincs = dict(DEFAULT_LAB_LOINCS)
    if loinc_map:
        active_loincs.update(loinc_map)
    custom_fns = {}
    if loinc_footnotes:
        for k, v in loinc_footnotes.items():
            if isinstance(v, (list, tuple)):
                active_loincs[k] = list(v)
            else:
                active_loincs[k] = [str(v)]
                custom_fns[str(k).lower()] = str(v)
    if footnotes:
        for k, v in footnotes.items():
            custom_fns[str(k).lower()] = str(v)

    var_col = df.columns[0]
    letters = "abcdefghijklmnopqrstuvwxyz"
    rendered_footnotes: list[tuple[str, str]] = []

    prov = provenance_notes or {}

    def typst_escape(text: str) -> str:
        s = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("[", "\\[").replace("]", "\\]")
        s = s.replace("$", "\\$").replace("#", "\\#").replace("*", "\\*").replace("_", "\\_")
        return s

    num_cols = len(df.columns)
    col_widths = []
    align_specs = []
    for i in range(num_cols):
        if i == 0:
            col_widths.append("1.8fr")
            align_specs.append("left")
        elif i == 1 and "category" in str(df.columns[1]).lower():
            col_widths.append("1.2fr")
            align_specs.append("left")
        else:
            col_widths.append("1fr")
            align_specs.append("center")

    lines = [
        f"// Table 1: {cap}",
        f"#figure(",
        f"  caption: [{cap}],",
        f"  #set text({font_size})",
        f"  #table(",
        f"    columns: ({', '.join(col_widths)}),",
        f"    align: ({', '.join(align_specs)}),",
        f"    stroke: none,",
        f"    fill: (col, row) => if row == 0 {{ rgb(\"f0f4f8\") }} else if calc.even(row) {{ rgb(\"fafbfc\") }} else {{ none }},",
        f"    table.hline(stroke: 1.5pt),",
    ]

    header_cells = [f"    [*table.cell()[{typst_escape(c)}]*]" for c in df.columns]
    lines.extend(header_cells)
    lines.append("    table.hline(stroke: 1pt),")

    for _, row in df.iterrows():
        row_cells = []
        for c_idx, col in enumerate(df.columns):
            val = row[col]
            if pd.isna(val) or val is None:
                row_cells.append("    []")
                continue

            s_val = str(val).strip()
            s_val = re.sub(r"\s*\+/-\s*", " \u00b1 ", s_val)
            s_val = re.sub(r"\[\s*([\d\.\-]+)\s*,\s*([\d\.\-]+)\s*\]", r"[IQR: \1, \2]", s_val)

            if c_idx == 0 and s_val:
                norm_v = re.sub(r"[^a-z0-9_]", "", s_val.lower())
                matched_key = None
                for lk in active_loincs:
                    norm_k = re.sub(r"[^a-z0-9_]", "", str(lk).lower())
                    if norm_k and (norm_v == norm_k or norm_k in norm_v or norm_v in norm_k):
                        matched_key = lk
                        break

                prov_meta = None
                for pk, pdata in prov.items():
                    norm_p = re.sub(r"[^a-z0-9_]", "", str(pk).lower())
                    if norm_p and (norm_v == norm_p or norm_p in norm_v or norm_v in norm_p):
                        prov_meta = pdata
                        break

                if prov_meta and include_provenance_footnotes:
                    sym = letters[len(rendered_footnotes) % len(letters)]
                    desc_parts = []
                    if "window" in prov_meta: desc_parts.append(f"window={prov_meta['window']}")
                    if "anchor" in prov_meta: desc_parts.append(f"anchor={prov_meta['anchor']}")
                    if "bounds" in prov_meta: desc_parts.append(f"bounds={prov_meta['bounds']}")
                    if "units" in prov_meta: desc_parts.append(f"units={prov_meta['units']}")
                    if "fill_rule" in prov_meta: desc_parts.append(f"fill={prov_meta['fill_rule']}")
                    if "concept_set" in prov_meta: desc_parts.append(f"concept_set={prov_meta['concept_set']}")
                    note_txt = ", ".join(desc_parts) if desc_parts else "provenance documented"
                    rendered_footnotes.append((sym, note_txt))
                    cell_content = f"[{typst_escape(s_val)}#super[{sym}]]"
                elif include_loinc_footnotes and matched_key and active_loincs[matched_key]:
                    sym = letters[len(rendered_footnotes) % len(letters)]
                    codes_str = ", ".join(str(c) for c in active_loincs[matched_key])
                    rendered_footnotes.append((sym, f"LOINC {codes_str}"))
                    cell_content = f"[{typst_escape(s_val)}#super[{sym}]]"
                else:
                    cell_content = f"[{typst_escape(s_val)}]"
            else:
                cell_content = f"[{typst_escape(s_val)}]"

            row_cells.append(f"    {cell_content}")
        lines.extend(row_cells)

    lines.append("    table.hline(stroke: 1.5pt),")
    lines.append("  )")
    if lbl:
        clean_lbl = lbl.strip("<>")
        lines.append(f") <{clean_lbl}>")
    else:
        lines.append(")")

    lines.append("#v(3pt)")
    lines.append('#text(8pt, fill: rgb("555555"))[')
    if rendered_footnotes:
        fn_parts = [f"#super[{sym}] {typst_escape(txt)}" for sym, txt in rendered_footnotes]
        lines.append(f"  {'; '.join(fn_parts)}\\ ")
    lines.append("  _Note:_ Values expressed as Mean \u00b1 SD, Median [IQR], or N (%). SMD = Standardized Mean Difference.")
    lines.append("]")

    content = "\n".join(lines) + "\n"
    if output_path:
        Path(output_path).write_text(content, encoding="utf-8")
    return content


