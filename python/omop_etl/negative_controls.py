"""Automated Negative Control & Empirical Calibration Outcome Generator (OHDSI HADES standard).

Identifies candidate negative control outcome concept sets with zero plausible mechanistic
links or hierarchical ancestry to index exposures/conditions, and fits empirical null
distributions to calibrate p-values and confidence intervals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

__all__ = [
    "generate_negative_controls",
    "calibrate_estimates",
    "EmpiricalCalibrationResult",
]


@dataclass
class EmpiricalCalibrationResult:
    """Container for HADES empirical null calibration diagnostics."""

    null_mean: float
    null_sd: float
    nominal_effect: float
    nominal_se: float
    nominal_p: float
    nominal_ci: tuple[float, float]
    calibrated_effect: float
    calibrated_se: float
    calibrated_p: float
    calibrated_ci: tuple[float, float]
    n_negative_controls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "null_mean": round(self.null_mean, 4),
            "null_sd": round(self.null_sd, 4),
            "nominal_effect": round(self.nominal_effect, 4),
            "nominal_se": round(self.nominal_se, 4),
            "nominal_p": round(self.nominal_p, 6),
            "nominal_ci": (round(self.nominal_ci[0], 4), round(self.nominal_ci[1], 4)),
            "calibrated_effect": round(self.calibrated_effect, 4),
            "calibrated_se": round(self.calibrated_se, 4),
            "calibrated_p": round(self.calibrated_p, 6),
            "calibrated_ci": (round(self.calibrated_ci[0], 4), round(self.calibrated_ci[1], 4)),
            "n_negative_controls": self.n_negative_controls,
        }


def generate_negative_controls(
    con: duckdb.DuckDBPyConnection | Any,
    exposure_concept_id: int,
    target_domain: str = "Condition",
    candidate_limit: int = 50,
    min_patient_count: int = 1,
) -> pd.DataFrame:
    """Identifies candidate negative control outcomes with zero plausible ancestral or relational links.

    Traverses the OMOP CDM concept hierarchy (`concept_ancestor`, `concept_relationship`, and `concept`)
    to identify standard concepts in `target_domain` that:
    1. Are neither ancestors nor descendants of `exposure_concept_id`.
    2. Share zero direct mechanistic relationships ('Has indicated drug', 'Has indication', 'Due to of').
    3. Have active clinical occurrences in the database if tables are populated.

    Args:
        con: Active DuckDB connection.
        exposure_concept_id: Concept ID of the primary exposure, treatment, or condition.
        target_domain: OMOP domain ID for negative controls ('Condition', 'Measurement', etc.).
        candidate_limit: Maximum number of candidate negative control concepts to return.
        min_patient_count: Minimum observed patient count in the domain occurrence table.

    Returns:
        pd.DataFrame: Candidate negative control concepts with columns:
            `concept_id`, `concept_name`, `domain_id`, `vocabulary_id`, `patient_count`, `rationale`.
    """
    domain = target_domain.capitalize() if target_domain.lower() == "condition" else target_domain

    # Check available CDM tables
    tables_res = con.execute("SHOW TABLES").fetchall()
    existing_tables = {r[0].lower() for r in tables_res}

    has_ancestor = "concept_ancestor" in existing_tables
    has_rel = "concept_relationship" in existing_tables
    has_concept = "concept" in existing_tables

    domain_table_map = {
        "Condition": "condition_occurrence",
        "Drug": "drug_exposure",
        "Measurement": "measurement",
        "Procedure": "procedure_occurrence",
    }
    occurrence_table = domain_table_map.get(domain, "condition_occurrence")
    has_occurrence = occurrence_table in existing_tables

    concept_id_col_map = {
        "condition_occurrence": "condition_concept_id",
        "drug_exposure": "drug_concept_id",
        "measurement": "measurement_concept_id",
        "procedure_occurrence": "procedure_concept_id",
    }
    concept_id_col = concept_id_col_map.get(occurrence_table, "condition_concept_id")

    if not has_concept:
        # Fallback when concept table is missing
        return pd.DataFrame(
            columns=[
                "concept_id",
                "concept_name",
                "domain_id",
                "vocabulary_id",
                "patient_count",
                "rationale",
            ]
        )

    # 1. Collect excluded concepts (ancestors, descendants, related)
    excluded_ids: set[int] = {int(exposure_concept_id)}

    if has_ancestor:
        anc_rows = con.execute(
            f"""
            SELECT ancestor_concept_id, descendant_concept_id
            FROM concept_ancestor
            WHERE ancestor_concept_id = {int(exposure_concept_id)}
               OR descendant_concept_id = {int(exposure_concept_id)}
            """
        ).fetchall()
        for a, d in anc_rows:
            if a is not None: excluded_ids.add(int(a))
            if d is not None: excluded_ids.add(int(d))

    if has_rel:
        rel_rows = con.execute(
            f"""
            SELECT concept_id_1, concept_id_2
            FROM concept_relationship
            WHERE concept_id_1 = {int(exposure_concept_id)}
               OR concept_id_2 = {int(exposure_concept_id)}
            """
        ).fetchall()
        for c1, c2 in rel_rows:
            if c1 is not None: excluded_ids.add(int(c1))
            if c2 is not None: excluded_ids.add(int(c2))

    excl_sql_list = ", ".join(str(cid) for cid in excluded_ids) if excluded_ids else "0"

    # 2. Query concepts in target domain not in excluded set
    if has_occurrence:
        sql = f"""
        SELECT 
            c.concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            COUNT(DISTINCT o.person_id) AS patient_count,
            'No plausible ancestral or relational link to index exposure' AS rationale
        FROM concept c
        JOIN {occurrence_table} o ON c.concept_id = o.{concept_id_col}
        WHERE LOWER(c.domain_id) = LOWER('{domain}')
          AND c.standard_concept = 'S'
          AND c.concept_id NOT IN ({excl_sql_list})
        GROUP BY c.concept_id, c.concept_name, c.domain_id, c.vocabulary_id
        HAVING COUNT(DISTINCT o.person_id) >= {int(min_patient_count)}
        ORDER BY patient_count DESC, c.concept_id
        LIMIT {int(candidate_limit)}
        """
    else:
        sql = f"""
        SELECT 
            c.concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            0 AS patient_count,
            'No plausible ancestral or relational link to index exposure' AS rationale
        FROM concept c
        WHERE LOWER(c.domain_id) = LOWER('{domain}')
          AND c.standard_concept = 'S'
          AND c.concept_id NOT IN ({excl_sql_list})
        ORDER BY c.concept_id
        LIMIT {int(candidate_limit)}
        """

    df = con.execute(sql).fetchdf()
    return df


def calibrate_estimates(
    null_estimates: pd.DataFrame | Sequence[tuple[float, float]] | np.ndarray,
    observed_effect: float,
    observed_se: float | None = None,
) -> EmpiricalCalibrationResult:
    """Calibrates nominal study effect estimates and p-values using an empirical null distribution.

    Implements the OHDSI HADES EmpiricalCalibration framework. Fits a Gaussian empirical null
    distribution N(mu, sigma^2) across negative control effect estimates (e.g., log Relative Risk)
    and adjusts the nominal test statistic for residual systematic bias.

    Args:
        null_estimates: DataFrame with columns `log_rr` (or `effect`) and `se_log_rr` (or `se`),
            or an array-like of (effect, se) tuples for negative controls.
        observed_effect: Observed point estimate (e.g., log hazard ratio or log relative risk).
        observed_se: Standard error of the observed point estimate (default 0.1 if None).

    Returns:
        EmpiricalCalibrationResult: Empirical null parameters, calibrated effect, CI, and p-value.
    """
    if isinstance(null_estimates, pd.DataFrame):
        df = null_estimates.copy()
        # Find effect column
        eff_col = None
        for col in ["log_rr", "effect", "estimate", "log_hr", "beta", "x"]:
            if col in df.columns:
                eff_col = col
                break
        if eff_col is None:
            eff_col = df.columns[0]

        # Find SE column
        se_col = None
        for col in ["se_log_rr", "se", "std_err", "stderr", "se_beta", "y"]:
            if col in df.columns:
                se_col = col
                break
        if se_col is None and len(df.columns) > 1:
            se_col = df.columns[1]

        effects = df[eff_col].dropna().values.astype(float)
        ses = df[se_col].dropna().values.astype(float) if se_col else np.full_like(effects, 0.1)
    elif isinstance(null_estimates, (list, tuple, np.ndarray)):
        arr = np.asarray(null_estimates, dtype=float)
        if arr.ndim == 2 and arr.shape[1] >= 2:
            effects = arr[:, 0]
            ses = arr[:, 1]
        else:
            effects = arr.ravel()
            ses = np.full_like(effects, 0.1)
    else:
        raise ValueError("null_estimates must be a DataFrame or 2D array of (effect, se).")

    n_ctrl = len(effects)
    if n_ctrl < 1:
        raise ValueError("At least one negative control estimate is required for calibration.")

    # 1. Fit empirical null distribution N(mu, sigma^2)
    # Using method of moments accounting for measurement error
    weights = 1.0 / np.maximum(ses**2, 1e-6)
    mu_null = float(np.sum(weights * effects) / np.sum(weights))

    sample_var = float(np.var(effects, ddof=1)) if n_ctrl > 1 else 0.0
    mean_se_sq = float(np.mean(ses**2))
    sigma_sq = max(0.0, sample_var - mean_se_sq)
    sigma_null = math.sqrt(sigma_sq) if sigma_sq > 0 else 0.0

    se_obs = observed_se if observed_se is not None and observed_se > 0 else 0.10

    # Nominal calculations
    z_nom = observed_effect / se_obs
    p_nom = float(2.0 * (1.0 - stats.norm.cdf(abs(z_nom))))
    ci_nom = (observed_effect - 1.96 * se_obs, observed_effect + 1.96 * se_obs)

    # Calibrated calculations
    cal_eff = observed_effect - mu_null
    cal_se = math.sqrt(se_obs**2 + sigma_null**2)
    z_cal = cal_eff / cal_se
    p_cal = float(2.0 * (1.0 - stats.norm.cdf(abs(z_cal))))
    ci_cal = (cal_eff - 1.96 * cal_se, cal_eff + 1.96 * cal_se)

    return EmpiricalCalibrationResult(
        null_mean=mu_null,
        null_sd=sigma_null,
        nominal_effect=observed_effect,
        nominal_se=se_obs,
        nominal_p=p_nom,
        nominal_ci=ci_nom,
        calibrated_effect=cal_eff,
        calibrated_se=cal_se,
        calibrated_p=p_cal,
        calibrated_ci=ci_cal,
        n_negative_controls=n_ctrl,
    )
