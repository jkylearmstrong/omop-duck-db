"""In-Engine Propensity Score and Inverse Probability of Treatment Weighting (IPTW) Module.

Provides comparative effectiveness propensity score estimation, IPTW weighting
(ATE, ATT/SMR, stabilized weights), and standardized mean difference (SMD) balance auditing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
import numpy as np
import pandas as pd


def _fit_logistic_irls(
    X: np.ndarray,
    y: np.ndarray,
    max_iter: int = 50,
    tol: float = 1e-6,
    l2_reg: float = 1e-4,
) -> tuple[np.ndarray, bool]:
    """Fits logistic regression using Iteratively Reweighted Least Squares (IRLS).

    Args:
        X: Feature matrix of shape (n_samples, n_features) without intercept.
        y: Binary target array of shape (n_samples,) with values in {0, 1}.
        max_iter: Maximum number of Newton-Raphson iterations.
        tol: Convergence tolerance on step norm.
        l2_reg: Small L2 regularization on non-intercept coefficients for numerical stability.

    Returns:
        tuple[np.ndarray, bool]: Coefficients vector (intercept first) and convergence flag.
    """
    n_samples, n_features = X.shape
    # Add intercept column
    X_aug = np.column_stack([np.ones(n_samples, dtype=np.float64), X.astype(np.float64)])
    p_dim = n_features + 1

    # Initialize coefficients (intercept at log-odds of y, others 0)
    beta = np.zeros(p_dim, dtype=np.float64)
    y_mean = np.clip(np.mean(y), 1e-4, 1.0 - 1e-4)
    beta[0] = np.log(y_mean / (1.0 - y_mean))

    reg_diag = np.full(p_dim, l2_reg, dtype=np.float64)
    reg_diag[0] = 0.0  # Do not regularize intercept

    converged = False
    for _ in range(max_iter):
        eta = np.clip(X_aug @ beta, -30.0, 30.0)
        p = 1.0 / (1.0 + np.exp(-eta))
        p = np.clip(p, 1e-15, 1.0 - 1e-15)

        w = p * (1.0 - p)
        grad = X_aug.T @ (y - p) - reg_diag * beta
        Hess = (X_aug.T * w) @ X_aug + np.diag(reg_diag)

        try:
            delta = np.linalg.solve(Hess, grad)
        except np.linalg.LinAlgError:
            # Fallback to pseudo-inverse if singular
            delta = np.linalg.pinv(Hess) @ grad

        beta += delta
        if np.max(np.abs(delta)) < tol:
            converged = True
            break

    return beta, converged


def _calculate_smd(
    x_target: np.ndarray,
    x_comp: np.ndarray,
    w_target: np.ndarray | None = None,
    w_comp: np.ndarray | None = None,
    pooled_sd: float | None = None,
) -> tuple[float, float, float, float]:
    """Calculates Standardized Mean Difference (SMD) between target and comparator groups.

    Args:
        x_target: Values in target (treated) group.
        x_comp: Values in comparator (control) group.
        w_target: Optional weights for target group.
        w_comp: Optional weights for comparator group.
        pooled_sd: Optional pre-calculated unweighted pooled standard deviation.

    Returns:
        tuple[float, float, float, float]: (smd, mean_target, mean_comp, pooled_sd).
    """
    if w_target is None:
        mean_tgt = float(np.mean(x_target))
        var_tgt = float(np.var(x_target, ddof=1)) if len(x_target) > 1 else 0.0
    else:
        sum_wt = float(np.sum(w_target))
        mean_tgt = float(np.sum(w_target * x_target) / sum_wt) if sum_wt > 0 else 0.0
        var_tgt = 0.0

    if w_comp is None:
        mean_cmp = float(np.mean(x_comp))
        var_cmp = float(np.var(x_comp, ddof=1)) if len(x_comp) > 1 else 0.0
    else:
        sum_wc = float(np.sum(w_comp))
        mean_cmp = float(np.sum(w_comp * x_comp) / sum_wc) if sum_wc > 0 else 0.0
        var_cmp = 0.0

    if pooled_sd is None:
        pooled_var = (var_tgt + var_cmp) / 2.0
        pooled_sd = float(np.sqrt(pooled_var)) if pooled_var > 0 else 0.0

    if pooled_sd <= 1e-12:
        smd = 0.0
    else:
        smd = float(np.abs(mean_tgt - mean_cmp) / pooled_sd)

    return smd, mean_tgt, mean_cmp, pooled_sd


@dataclass
class PropensityResult:
    """Results of propensity score estimation and IPTW weighting."""

    data: pd.DataFrame
    balance: pd.DataFrame
    coefficients: dict[str, float]
    max_post_smd: float
    converged: bool = True

    def __getitem__(self, key: str) -> Any:
        if key == "data":
            return self.data
        if key == "balance":
            return self.balance
        if key == "coefficients":
            return self.coefficients
        if key == "max_post_smd":
            return self.max_post_smd
        raise KeyError(key)

    def to_duckdb(self, con: Any, table_name: str) -> None:
        """Registers the weighted cohort dataset into DuckDB.

        Args:
            con: Active DuckDB connection.
            table_name: Destination table or view name.
        """
        con.register(f"_tmp_{table_name}", self.data)
        con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM _tmp_{table_name};")
        con.unregister(f"_tmp_{table_name}")


def generate_propensity_weights(
    con: Any = None,
    cohort_table: str | pd.DataFrame = "cohort",
    target_id: int = 1,
    comparator_id: int = 2,
    covariate_cols: Sequence[str] | None = None,
    treatment_col: str = "cohort_definition_id",
    trim: float | None = 0.01,
) -> PropensityResult:
    r"""Calculates propensity scores, IPTW weights, and SMD balance diagnostics.

    Computes logistic regression propensity scores :math:`e_i = P(Z_i = 1 \mid X_i)`
    and Inverse Probability of Treatment Weighting (IPTW):

    .. math::
        \text{ATE} = \frac{Z_i}{e_i} + \frac{1 - Z_i}{1 - e_i}

    .. math::
        \text{ATT (SMR)} = Z_i + (1 - Z_i) \frac{e_i}{1 - e_i}

    .. math::
        \text{Stabilized ATE} = Z_i \frac{\bar{Z}}{e_i} + (1 - Z_i) \frac{1 - \bar{Z}}{1 - e_i}

    Args:
        con: Active DuckDB connection (required if cohort_table is a table name string).
        cohort_table: Table name string or pandas DataFrame containing cohort subjects and covariates.
        target_id: Identifier value representing target (treated) group (Z = 1).
        comparator_id: Identifier value representing comparator group (Z = 0).
        covariate_cols: List of covariate column names to balance. If None, auto-detects numeric/bool columns.
        treatment_col: Column containing group indicators (default 'cohort_definition_id').
        trim: Optional clipping threshold for extreme propensity scores (e.g. 0.01 for [0.01, 0.99]).

    Returns:
        PropensityResult: Result container with weighted DataFrame, SMD balance table,
            model coefficients, and max post-weighting SMD.
    """
    if isinstance(cohort_table, str):
        if con is None:
            raise ValueError("Must provide active DuckDB connection `con` when cohort_table is a string table name.")
        query = f"SELECT * FROM {cohort_table} WHERE {treatment_col} IN ({int(target_id)}, {int(comparator_id)})"
        df = con.execute(query).df()
    elif isinstance(cohort_table, pd.DataFrame):
        df = cohort_table[cohort_table[treatment_col].isin([target_id, comparator_id])].copy()
    else:
        raise TypeError(f"cohort_table must be str or pd.DataFrame, got {type(cohort_table)}")

    if len(df) == 0:
        raise ValueError(
            f"No rows found in cohort_table matching {treatment_col} in ({target_id}, {comparator_id})."
        )

    # Treatment binary indicator: 1 = target, 0 = comparator
    df["_treatment_z"] = (df[treatment_col] == target_id).astype(int)
    z = df["_treatment_z"].to_numpy(dtype=np.float64)

    if covariate_cols is None:
        exclude_cols = {
            treatment_col,
            "_treatment_z",
            "cohort_definition_id",
            "subject_id",
            "person_id",
            "cohort_start_date",
            "cohort_end_date",
            "visit_occurrence_id",
            "propensity_score",
            "iptw_ate",
            "iptw_att",
        }
        covariate_cols = [
            c
            for c in df.columns
            if c not in exclude_cols and pd.api.types.is_numeric_dtype(df[c])
        ]

    if not covariate_cols:
        raise ValueError("No valid covariate columns found or specified for propensity modeling.")

    # Prepare feature matrix X (impute missing with column median)
    X_mat = np.zeros((len(df), len(covariate_cols)), dtype=np.float64)
    for idx, col in enumerate(covariate_cols):
        col_vals = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=np.float64)
        median_val = np.nanmedian(col_vals) if not np.all(np.isnan(col_vals)) else 0.0
        X_mat[:, idx] = np.where(np.isnan(col_vals), median_val, col_vals)

    # Standardize X for numerical stability in logistic regression
    means = np.mean(X_mat, axis=0)
    stds = np.std(X_mat, axis=0)
    stds = np.where(stds == 0, 1.0, stds)
    X_scaled = (X_mat - means) / stds

    # Fit logistic regression
    beta, converged = _fit_logistic_irls(X_scaled, z)

    # Compute raw propensity scores
    eta = beta[0] + X_scaled @ beta[1:]
    e_raw = 1.0 / (1.0 + np.exp(-np.clip(eta, -30.0, 30.0)))

    if trim is not None and trim > 0:
        e = np.clip(e_raw, trim, 1.0 - trim)
    else:
        e = np.clip(e_raw, 1e-6, 1.0 - 1e-6)

    # Compute IPTW weights
    ate = (z / e) + ((1.0 - z) / (1.0 - e))
    att = z + (1.0 - z) * (e / (1.0 - e))

    z_mean = float(np.mean(z))
    stab_ate = (z * z_mean / e) + ((1.0 - z) * (1.0 - z_mean) / (1.0 - e))

    df["propensity_score"] = e
    df["iptw_ate"] = ate
    df["iptw_att"] = att
    df["iptw_stabilized_ate"] = stab_ate

    # Calculate balance diagnostics (pre- and post-weighting SMD)
    balance_rows = []
    target_mask = z == 1
    comp_mask = z == 0

    for idx, col in enumerate(covariate_cols):
        x_tgt = X_mat[target_mask, idx]
        x_cmp = X_mat[comp_mask, idx]
        w_tgt_ate = ate[target_mask]
        w_cmp_ate = ate[comp_mask]
        w_tgt_att = att[target_mask]
        w_cmp_att = att[comp_mask]

        # Pre-weighting unweighted SMD
        pre_smd, mean_tgt_pre, mean_cmp_pre, pooled_sd = _calculate_smd(x_tgt, x_cmp)

        # Post-weighting ATE SMD (using baseline pooled_sd)
        post_smd_ate, mean_tgt_ate, mean_cmp_ate, _ = _calculate_smd(
            x_tgt, x_cmp, w_target=w_tgt_ate, w_comp=w_cmp_ate, pooled_sd=pooled_sd
        )

        # Post-weighting ATT SMD
        post_smd_att, mean_tgt_att, mean_cmp_att, _ = _calculate_smd(
            x_tgt, x_cmp, w_target=w_tgt_att, w_comp=w_cmp_att, pooled_sd=pooled_sd
        )

        balance_rows.append(
            {
                "covariate": col,
                "pre_smd": pre_smd,
                "post_smd_ate": post_smd_ate,
                "post_smd_att": post_smd_att,
                "mean_target_pre": mean_tgt_pre,
                "mean_comparator_pre": mean_cmp_pre,
                "mean_target_post_ate": mean_tgt_ate,
                "mean_comparator_post_ate": mean_cmp_ate,
            }
        )

    balance_df = pd.DataFrame(balance_rows)
    max_post_smd = float(balance_df["post_smd_ate"].max()) if len(balance_df) > 0 else 0.0

    coef_dict = {"(Intercept)": float(beta[0])}
    for idx, col in enumerate(covariate_cols):
        coef_dict[col] = float(beta[idx + 1] / stds[idx])

    df = df.drop(columns=["_treatment_z"])

    return PropensityResult(
        data=df,
        balance=balance_df,
        coefficients=coef_dict,
        max_post_smd=max_post_smd,
        converged=converged,
    )
