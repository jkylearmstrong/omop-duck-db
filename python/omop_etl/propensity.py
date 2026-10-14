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
                "pre_smd": round(pre_smd, 4),
                "post_smd_ate": round(post_smd_ate, 4),
                "post_smd_att": round(post_smd_att, 4),
                "post_smd": round(post_smd_ate, 4),
                "mean_target_pre": round(mean_tgt_pre, 4),
                "mean_comparator_pre": round(mean_cmp_pre, 4),
                "mean_target_post_ate": round(mean_tgt_ate, 4),
                "mean_comparator_post_ate": round(mean_cmp_ate, 4),
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


def match_nearest_neighbor(
    df: pd.DataFrame,
    propensity_score_col: str = "propensity_score",
    treatment_col: str = "treatment",
    caliper: float = 0.2,
    ratio: int = 1,
    replacement: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Matches treated and comparator units via 1:k nearest-neighbor caliper matching on logit(PS).

    Args:
        df: DataFrame containing features, treatment indicator, and propensity scores.
        propensity_score_col: Column containing propensity score values e_i in (0, 1).
        treatment_col: Binary treatment indicator column (1 = treated, 0 = control).
        caliper: Caliper width in standard deviations of logit(PS) (default 0.2).
        ratio: Matching ratio k (1:k matching, default 1).
        replacement: Whether to match controls with replacement (default False).

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: Matched dataset and pairs linkage table.
    """
    treated = df[df[treatment_col] == 1].copy()
    control = df[df[treatment_col] == 0].copy()

    if len(treated) == 0 or len(control) == 0:
        return pd.DataFrame(), pd.DataFrame()

    ps_t = np.clip(treated[propensity_score_col].values.astype(float), 1e-6, 1.0 - 1e-6)
    ps_c = np.clip(control[propensity_score_col].values.astype(float), 1e-6, 1.0 - 1e-6)

    logit_t = np.log(ps_t / (1.0 - ps_t))
    logit_c = np.log(ps_c / (1.0 - ps_c))

    all_logit = np.concatenate([logit_t, logit_c])
    sd_logit = float(np.std(all_logit, ddof=1)) if len(all_logit) > 1 else 1.0
    caliper_dist = caliper * sd_logit

    matched_pairs = []
    used_controls = set()

    c_indices = control.index.values
    t_indices = treated.index.values

    for i, t_idx in enumerate(t_indices):
        lt = logit_t[i]
        dists = np.abs(logit_c - lt)
        eligible = np.where(dists <= caliper_dist)[0]
        if len(eligible) == 0:
            continue

        if not replacement:
            eligible = [idx for idx in eligible if c_indices[idx] not in used_controls]
            if len(eligible) == 0:
                continue

        sorted_eligible = sorted(eligible, key=lambda idx: dists[idx])
        selected_c = sorted_eligible[:ratio]

        for sc in selected_c:
            c_idx = c_indices[sc]
            used_controls.add(c_idx)
            matched_pairs.append({
                "subclass_id": i + 1,
                "target_index": t_idx,
                "control_index": c_idx,
                "distance": float(dists[sc]),
            })

    pairs_df = pd.DataFrame(matched_pairs)
    if pairs_df.empty:
        return pd.DataFrame(columns=df.columns), pairs_df

    target_set = set(pairs_df["target_index"])
    ctrl_set = set(pairs_df["control_index"])
    matched_indices = list(target_set.union(ctrl_set))

    matched_df = df.loc[matched_indices].copy()
    matched_df["matched"] = 1
    return matched_df, pairs_df


def consortium_propensity_score(
    con: Any = None,
    cohort_table: str | pd.DataFrame = "cohort",
    exposure_col: str = "treatment",
    covariate_cols: Sequence[str] | None = None,
    method: str = "matching",
    caliper: float = 0.2,
    ratio: int = 1,
    trim: float | None = 0.01,
) -> PropensityResult:
    """Consortium causal inference engine supporting 1:k nearest-neighbor caliper matching and IPTW.

    Args:
        con: Active DuckDB connection (required if cohort_table is str).
        cohort_table: Cohort table name or DataFrame containing exposure and covariates.
        exposure_col: Column indicating binary treatment / exposure (1 = treated, 0 = comparator).
        covariate_cols: List of covariate column names to balance.
        method: Adjustment method: 'matching' (1:k caliper matching) or 'iptw' (inverse probability weighting).
        caliper: Caliper threshold in units of logit(PS) SD (default 0.2).
        ratio: Ratio for 1:k matching (default 1 = 1:1 matching).
        trim: Trimming threshold for extreme propensity scores (default 0.01).

    Returns:
        PropensityResult: Result container with matched/weighted DataFrame, balance ledger, and max post-SMD.
    """
    if isinstance(cohort_table, str):
        if con is None:
            raise ValueError("Must provide DuckDB connection when cohort_table is a table name string.")
        df = con.execute(f"SELECT * FROM {cohort_table}").fetchdf()
    else:
        df = cohort_table.copy()

    if exposure_col not in df.columns:
        raise ValueError(f"Exposure column '{exposure_col}' not found in cohort.")

    # 1. First estimate propensity scores via generate_propensity_weights
    iptw_res = generate_propensity_weights(
        con=None,
        cohort_table=df,
        target_id=1,
        comparator_id=0,
        covariate_cols=covariate_cols,
        treatment_col=exposure_col,
        trim=trim,
    )

    if method.lower() == "iptw":
        return iptw_res

    # 2. Method == 'matching': Execute 1:k nearest neighbor caliper matching
    scored_df = iptw_res.data
    covs = list(covariate_cols) if covariate_cols else list(iptw_res.balance["covariate"])

    matched_df, pairs_df = match_nearest_neighbor(
        scored_df,
        propensity_score_col="propensity_score",
        treatment_col=exposure_col,
        caliper=caliper,
        ratio=ratio,
    )

    if matched_df.empty:
        return PropensityResult(
            data=matched_df,
            balance=iptw_res.balance,
            coefficients=iptw_res.coefficients,
            max_post_smd=1.0,
            converged=False,
        )

    # Recompute SMD on matched cohort
    tgt_m = matched_df[matched_df[exposure_col] == 1]
    cmp_m = matched_df[matched_df[exposure_col] == 0]

    bal_rows = []
    for col in covs:
        x_tgt = tgt_m[col].values.astype(float)
        x_cmp = cmp_m[col].values.astype(float)

        pre_row = iptw_res.balance[iptw_res.balance["covariate"] == col]
        pre_smd = float(pre_row["pre_smd"].iloc[0]) if not pre_row.empty else 0.0

        post_smd, m_tgt, m_cmp, _ = _calculate_smd(x_tgt, x_cmp)

        bal_rows.append({
            "covariate": col,
            "pre_smd": round(pre_smd, 4),
            "post_smd": round(post_smd, 4),
            "balanced": bool(post_smd < 0.10),
            "mean_target_matched": round(m_tgt, 4),
            "mean_comparator_matched": round(m_cmp, 4),
        })

    bal_df = pd.DataFrame(bal_rows)
    max_post_smd = float(bal_df["post_smd"].max()) if not bal_df.empty else 0.0

    return PropensityResult(
        data=matched_df,
        balance=bal_df,
        coefficients=iptw_res.coefficients,
        max_post_smd=max_post_smd,
        converged=True,
    )


def plot_love_plot(
    balance_df: pd.DataFrame,
    threshold: float = 0.10,
    title: str = "Covariate Balance (Love Plot)",
    output_path: str | None = None,
) -> str:
    """Generates a camera-ready SVG Love Plot visualizing pre- and post-adjustment SMDs against the 0.10 threshold.

    Args:
        balance_df: Balance dataframe from consortium_propensity_score or generate_propensity_weights.
        threshold: Recommended balance threshold (default 0.10).
        title: Chart title.
        output_path: Optional file path to write SVG (.svg).

    Returns:
        str: Standalone SVG string.
    """
    df = balance_df.copy()
    if "post_smd" not in df.columns and "post_smd_ate" in df.columns:
        df["post_smd"] = df["post_smd_ate"]

    covariates = list(df["covariate"])
    n = len(covariates)
    height = max(300, 40 * n + 100)
    width = 650

    # Scales
    max_smd = max(0.40, float(df["pre_smd"].max()) * 1.1)
    margin_left = 180
    margin_right = 50
    margin_top = 60
    margin_bottom = 50
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    def x_scale(val: float) -> float:
        return margin_left + (min(val, max_smd) / max_smd) * plot_width

    def y_scale(idx: int) -> float:
        return margin_top + (idx + 0.5) * (plot_height / max(1, n))

    thresh_x = x_scale(threshold)

    svg_elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">',
        f'<text x="{width/2}" y="35" text-anchor="middle" font-size="16" font-weight="600" fill="#2d3748">{title}</text>',
        f'<rect x="{margin_left}" y="{margin_top}" width="{plot_width}" height="{plot_height}" fill="#f8fafc" stroke="#e2e8f0" />',
        f'<line x1="{thresh_x}" y1="{margin_top}" x2="{thresh_x}" y2="{margin_top + plot_height}" stroke="#e53e3e" stroke-width="1.5" stroke-dasharray="4,4" />',
        f'<text x="{thresh_x}" y="{margin_top - 8}" text-anchor="middle" font-size="11" fill="#e53e3e" font-weight="600">Threshold ({threshold})</text>',
    ]

    # Legend
    svg_elements.extend([
        f'<circle cx="{margin_left + 20}" cy="{margin_top - 20}" r="5" fill="#a0aec0" />',
        f'<text x="{margin_left + 30}" y="{margin_top - 16}" font-size="11" fill="#4a5568">Unadjusted (Pre-Match)</text>',
        f'<circle cx="{margin_left + 180}" cy="{margin_top - 20}" r="5" fill="#3182ce" />',
        f'<text x="{margin_left + 190}" y="{margin_top - 16}" font-size="11" fill="#4a5568">Adjusted (Post-Match)</text>',
    ])

    for i, row in df.iterrows():
        y = y_scale(i)
        cov_name = str(row["covariate"])
        pre_val = float(row["pre_smd"])
        post_val = float(row["post_smd"])

        x_pre = x_scale(pre_val)
        x_post = x_scale(post_val)

        # Label
        svg_elements.append(
            f'<text x="{margin_left - 10}" y="{y + 4}" text-anchor="end" font-size="12" fill="#2d3748">{cov_name}</text>'
        )
        # Connecting line
        svg_elements.append(
            f'<line x1="{x_pre}" y1="{y}" x2="{x_post}" y2="{y}" stroke="#cbd5e0" stroke-width="1.5" />'
        )
        # Pre dot
        svg_elements.append(
            f'<circle cx="{x_pre}" cy="{y}" r="5" fill="#a0aec0" stroke="#718096" stroke-width="1" />'
        )
        # Post dot
        svg_elements.append(
            f'<circle cx="{x_post}" cy="{y}" r="5.5" fill="#3182ce" stroke="#2b6cb0" stroke-width="1" />'
        )

    # Bottom axis
    svg_elements.append(
        f'<text x="{margin_left + plot_width/2}" y="{height - 15}" text-anchor="middle" font-size="12" fill="#4a5568">Absolute Standardized Mean Difference (SMD)</text>'
    )
    svg_elements.append('</svg>')

    svg_content = "\n".join(svg_elements)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(svg_content)
    return svg_content

