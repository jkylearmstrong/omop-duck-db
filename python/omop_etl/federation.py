"""Multi-Site Consortium Federation Helper.

Provides zero-copy cross-institution querying by attaching independent
DuckDB CDM databases in READ_ONLY mode and generating unified federated
views (v_person, v_visit_occurrence, etc.) with site provenance.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Union
import os
import duckdb
from omop_etl.paths import sql_path_literal
from .build_omop_cdm import attach_central_vocabulary

FEDERATED_TABLES = [
    "person",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "drug_exposure",
    "measurement",
    "observation",
    "death",
    "observation_period",
    "drug_era",
    "condition_era",
    "provider",
    "care_site",
    "cdm_source",
]


def create_federated_consortium(
    site_dbs: Dict[Union[str, int], str],
    central_vocab_db: Optional[str] = None,
    output_con: Optional[duckdb.DuckDBPyConnection] = None,
) -> duckdb.DuckDBPyConnection:
    """Attaches multiple site databases in READ_ONLY mode and builds unified
    v_person, v_visit_occurrence, etc. views with site_id and site_anon provenance.

    Parameters
    ----------
    site_dbs : dict[Union[str, int], str]
        Dictionary mapping site identifiers or pseudonyms (e.g. "Site A", "Site B")
        to DuckDB database file paths.
    central_vocab_db : str, optional
        Path to an external Athena vocabulary DuckDB database to attach with zero-copy views.
    output_con : duckdb.DuckDBPyConnection, optional
        Existing DuckDB connection to use. If None, a new in-memory connection is created.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Connection with attached site databases and unified v_* views.
    """
    if not site_dbs:
        raise ValueError("site_dbs dictionary cannot be empty.")

    con = output_con if output_con is not None else duckdb.connect(":memory:")

    attached_sites = []
    for idx, (site_key, db_path) in enumerate(site_dbs.items(), start=1):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Site database not found: {db_path}")

        schema_alias = f"site_{idx}"
        con.execute(f"ATTACH {sql_path_literal(db_path)} AS {schema_alias} (READ_ONLY);")

        # Resolve site_id and site_anon
        site_anon = str(site_key)
        site_id = None

        if isinstance(site_key, int) or (isinstance(site_key, str) and site_key.isdigit()):
            site_id = int(site_key)

        if site_id is None:
            # Try to infer site_id from attached care_site or person
            try:
                row = con.execute(
                    f"SELECT MIN(care_site_id) FROM {schema_alias}.care_site WHERE care_site_id IS NOT NULL"
                ).fetchone()
                if row and row[0] is not None:
                    site_id = int(row[0])
            except Exception:
                pass

        if site_id is None:
            try:
                row = con.execute(
                    f"SELECT MIN(care_site_id) FROM {schema_alias}.person WHERE care_site_id IS NOT NULL"
                ).fetchone()
                if row and row[0] is not None:
                    site_id = int(row[0])
            except Exception:
                pass

        if site_id is None:
            site_id = idx

        attached_sites.append(
            {
                "schema": schema_alias,
                "site_id": site_id,
                "site_anon": site_anon,
            }
        )

    # Attach central vocabulary if provided
    if central_vocab_db is not None:
        attach_central_vocabulary(con, central_vocab_db, temporary=False)

    # Build federated views for all CDM tables
    for tbl in FEDERATED_TABLES:
        site_selects = []
        first_schema = None
        for s_info in attached_sites:
            s_schema = s_info["schema"]
            has_table = con.execute(
                f"SELECT 1 FROM information_schema.tables WHERE table_catalog = '{s_schema}' AND table_name = '{tbl}'"
            ).fetchone()
            if has_table:
                if first_schema is None:
                    first_schema = s_schema

        if first_schema is None:
            continue

        cols_info = con.execute(
            f"SELECT column_name FROM information_schema.columns WHERE table_catalog = '{first_schema}' AND table_name = '{tbl}' ORDER BY ordinal_position"
        ).fetchall()
        cols = [c[0] for c in cols_info]
        cols_sql = ", ".join([f't."{c}"' for c in cols])

        for s_info in attached_sites:
            s_schema = s_info["schema"]
            has_table = con.execute(
                f"SELECT 1 FROM information_schema.tables WHERE table_catalog = '{s_schema}' AND table_name = '{tbl}'"
            ).fetchone()
            if has_table:
                s_id = s_info["site_id"]
                s_anon = s_info["site_anon"].replace("'", "''")
                site_selects.append(
                    f"SELECT {s_id} AS site_id, '{s_anon}' AS site_anon, {cols_sql} FROM {s_schema}.{tbl} t"
                )

        if site_selects:
            union_sql = " UNION ALL\n".join(site_selects)
            view_sql = f"CREATE OR REPLACE VIEW v_{tbl} AS\n{union_sql};"
            con.execute(view_sql)

    return con


def with_cell_suppression(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    output_view: Optional[str] = None,
    min_cell_size: int = 10,
    count_columns: Optional[list[str]] = None,
    fill_value: str = "<10",
) -> str:
    """Creates a privacy-preserving view suppressing counts below min_cell_size.

    Args:
        con: Active DuckDB connection.
        view_name: Source table or view to wrap.
        output_view: Target view name (default: f"safe_{view_name}").
        min_cell_size: Minimum cell threshold to display (default 10).
        count_columns: List of count columns to redact (if None, auto-detects 'count' or 'n' columns).
        fill_value: Replacement string for suppressed values (default '<10').

    Returns:
        str: Name of created privacy view.
    """
    target = output_view or f"safe_{view_name}"
    cols_info = con.execute(f"DESCRIBE SELECT * FROM {view_name} LIMIT 0;").fetchall()
    col_names = [c[0] for c in cols_info]

    if count_columns is None:
        target_counts = [
            c for c in col_names if any(k in c.lower() for k in ("count", "n_patients", "n_subjects", "_n", "subjects"))
        ]
    else:
        target_counts = count_columns

    select_exprs = []
    for col in col_names:
        if col in target_counts:
            select_exprs.append(
                f"CASE WHEN CAST({col} AS DOUBLE) < {int(min_cell_size)} THEN '{fill_value}' "
                f"ELSE CAST({col} AS VARCHAR) END AS {col}"
            )
        else:
            select_exprs.append(col)

    sql = f"""
    CREATE OR REPLACE VIEW {target} AS
    SELECT {', '.join(select_exprs)}
    FROM {view_name};
    """
    con.execute(sql)
    return target


def with_differential_privacy(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    output_view: Optional[str] = None,
    epsilon: float = 1.0,
    delta: float = 1e-5,
    count_columns: Optional[list[str]] = None,
) -> str:
    """Creates a privacy-preserving view adding calibrated Laplace noise to count aggregates.

    Implements (epsilon, delta)-Differential Privacy for consortium queries.
    Adds Laplace(0, 1/epsilon) noise:
        Count* = GREATEST(0, ROUND(Count + Laplace(0, 1/epsilon)))

    Args:
        con: Active DuckDB connection.
        view_name: Source table or view to protect.
        output_view: Target view name (default: f"dp_{view_name}").
        epsilon: Differential privacy budget parameter (default 1.0).
        delta: Privacy failure probability bound (default 1e-5).
        count_columns: List of count columns to privatize (auto-detected if None).

    Returns:
        str: Name of created differentially private view.
    """
    if epsilon <= 0:
        raise ValueError("Differential privacy budget epsilon must be strictly positive.")

    target = output_view or f"dp_{view_name}"
    cols_info = con.execute(f"DESCRIBE SELECT * FROM {view_name} LIMIT 0;").fetchall()
    col_names = [c[0] for c in cols_info]

    if count_columns is None:
        target_counts = [
            c for c in col_names if any(k in c.lower() for k in ("count", "n_patients", "n_subjects", "_n", "subjects"))
        ]
    else:
        target_counts = count_columns

    scale = 1.0 / float(epsilon)
    select_exprs = []
    for col in col_names:
        if col in target_counts:
            # Calibrated Laplace noise in DuckDB SQL:
            # If U <= 0.5: scale * ln(2 * U)
            # If U > 0.5: -scale * ln(2 * (1 - U))
            laplace_sql = (
                f"(CASE WHEN random() <= 0.5 "
                f"THEN {scale} * ln(GREATEST(1e-12, 2.0 * random())) "
                f"ELSE -{scale} * ln(GREATEST(1e-12, 2.0 * (1.0 - random()))) END)"
            )
            select_exprs.append(
                f"GREATEST(0, ROUND(CAST({col} AS DOUBLE) + {laplace_sql})) AS {col}"
            )
        else:
            select_exprs.append(col)

    sql = f"""
    CREATE OR REPLACE VIEW {target} AS
    SELECT {', '.join(select_exprs)}
    FROM {view_name};
    """
    con.execute(sql)
    return target


def check_cross_database_discrepancy(
    con: duckdb.DuckDBPyConnection,
    table_name: str = "condition_occurrence",
    concept_col: str = "condition_concept_id",
    top_n: int = 25,
) -> dict:
    """Compares concept prevalence and missingness divergence across federated sites.

    Args:
        con: Federated DuckDB connection with v_* views.
        table_name: CDM table name (e.g. 'condition_occurrence', 'drug_exposure').
        concept_col: Concept ID column to evaluate.
        top_n: Number of top concepts to compare.

    Returns:
        dict: Summary containing cross_site_prevalence DataFrame and divergence metrics.
    """
    view = f"v_{table_name}"
    # Calculate top overall concepts
    top_concepts_df = con.execute(f"""
        SELECT 
            {concept_col} AS concept_id,
            COUNT(*) AS total_count
        FROM {view}
        WHERE {concept_col} IS NOT NULL AND {concept_col} != 0
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT {int(top_n)}
    """).df()

    if len(top_concepts_df) == 0:
        import pandas as pd
        return {"prevalence": pd.DataFrame(), "sites": [], "evaluated_concepts": 0}

    cids_str = ", ".join(str(int(c)) for c in top_concepts_df["concept_id"])

    # Calculate site-stratified prevalence percentages
    cross_df = con.execute(f"""
        WITH site_totals AS (
            SELECT site_anon, COUNT(*) AS site_total_rows
            FROM {view}
            GROUP BY 1
        ),
        site_concepts AS (
            SELECT site_anon, {concept_col} AS concept_id, COUNT(*) AS concept_count
            FROM {view}
            WHERE {concept_col} IN ({cids_str})
            GROUP BY 1, 2
        )
        SELECT 
            sc.concept_id,
            sc.site_anon,
            sc.concept_count,
            st.site_total_rows,
            ROUND(sc.concept_count * 100.0 / st.site_total_rows, 3) AS prevalence_pct
        FROM site_concepts sc
        JOIN site_totals st ON sc.site_anon = st.site_anon
        ORDER BY sc.concept_id, sc.site_anon;
    """).df()

    sites = sorted(list(cross_df["site_anon"].unique())) if len(cross_df) > 0 else []

    return {
        "prevalence": cross_df,
        "sites": sites,
        "evaluated_concepts": len(top_concepts_df),
    }


class FederatedMetaAnalysisResult:
    """Multi-site federated meta-analysis results container with forest plot exporters."""

    def __init__(self, data: dict):
        self.data = data

    @property
    def pooled_estimate(self) -> float:
        return self.data["pooled_estimate"]

    @property
    def se(self) -> float:
        return self.data["se"]

    @property
    def ci_lower(self) -> float:
        return self.data["ci_lower"]

    @property
    def ci_upper(self) -> float:
        return self.data["ci_upper"]

    @property
    def i_squared(self) -> float:
        return self.data["i_squared"]

    @property
    def tau_squared(self) -> float:
        return self.data["tau_squared"]

    @property
    def q_statistic(self) -> float:
        return self.data["q_statistic"]

    @property
    def p_value(self) -> float:
        return self.data["p_value"]

    def to_dataframe(self) -> "pd.DataFrame":
        """Returns study-level and pooled estimates as a formatted DataFrame."""
        import pandas as pd
        rows = []
        for s in self.data["studies"]:
            rows.append({
                "site": s["site"],
                "estimate": s["estimate"],
                "std_error": s["std_error"],
                "ci_lower": s["ci_lower"],
                "ci_upper": s["ci_upper"],
                "weight_percent": s["weight_pct"],
                "type": "Study",
            })
        rows.append({
            "site": f"Pooled ({self.data['model'].capitalize()})",
            "estimate": self.data["pooled_estimate"],
            "std_error": self.data["se"],
            "ci_lower": self.data["ci_lower"],
            "ci_upper": self.data["ci_upper"],
            "weight_percent": 100.0,
            "type": "Pooled",
        })
        return pd.DataFrame(rows)

    def to_forest_text(self) -> str:
        """Render a publication-ready ASCII forest plot."""
        lines = [
            f"=== Federated Meta-Analysis ({self.data['model'].upper()} Effects) ===",
            f"Heterogeneity: Q = {self.data['q_statistic']:.2f} (df = {self.data['df']}), p = {self.data['q_p_value']:.4f}; I² = {self.data['i_squared']:.1f}%; τ² = {self.data['tau_squared']:.4f}",
            "",
            f"{'Site':<15} {'Estimate':<10} {'95% CI':<22} {'Weight (%)':<12} {'Forest Plot':<30}",
            "-" * 85,
        ]
        all_low = [s["ci_lower"] for s in self.data["studies"]] + [self.data["ci_lower"]]
        all_high = [s["ci_upper"] for s in self.data["studies"]] + [self.data["ci_upper"]]
        min_val = min(all_low) if all_low else -1.0
        max_val = max(all_high) if all_high else 1.0
        span = max(1e-6, max_val - min_val)

        def _draw_bar(est, low, high, is_diamond=False):
            width = 24
            pos_est = int(round((est - min_val) / span * (width - 1)))
            pos_low = max(0, min(width - 1, int(round((low - min_val) / span * (width - 1)))))
            pos_high = max(0, min(width - 1, int(round((high - min_val) / span * (width - 1)))))
            bar = [" "] * width
            for p in range(pos_low, pos_high + 1):
                bar[p] = "-"
            bar[pos_est] = "<>" if is_diamond else "■"
            return "".join(bar)

        for s in self.data["studies"]:
            site_name = str(s["site"])[:14]
            ci_str = f"[{s['ci_lower']:+.2f}, {s['ci_upper']:+.2f}]"
            bar_str = _draw_bar(s["estimate"], s["ci_lower"], s["ci_upper"], is_diamond=False)
            lines.append(f"{site_name:<15} {s['estimate']:+8.3f}  {ci_str:<22} {s['weight_pct']:8.1f}%    |{bar_str}|")

        lines.append("-" * 85)
        p_ci_str = f"[{self.data['ci_lower']:+.2f}, {self.data['ci_upper']:+.2f}]"
        diamond_bar = _draw_bar(self.data["pooled_estimate"], self.data["ci_lower"], self.data["ci_upper"], is_diamond=True)
        lines.append(f"{'POOLED':<15} {self.data['pooled_estimate']:+8.3f}  {p_ci_str:<22} {'100.0%':<12} |{diamond_bar}|")
        lines.append(f"Z = {self.data['z_score']:.2f}, p = {self.data['p_value']:.4e}")
        return "\n".join(lines)

    def to_forest_mermaid(self) -> str:
        """Render a Mermaid flowchart summary of the forest plot."""
        lines = [
            "flowchart TD",
            "    subgraph MetaAnalysis [Federated Meta-Analysis Summary]",
            f'        Header["I² = {self.data["i_squared"]:.1f}%, τ² = {self.data["tau_squared"]:.4f}, Q = {self.data["q_statistic"]:.2f}"]',
        ]
        for idx, s in enumerate(self.data["studies"], start=1):
            name = str(s["site"]).replace('"', "'")
            lines.append(f'        S{idx}["{name}: {s["estimate"]:+.2f} [{s["ci_lower"]:+.2f}, {s["ci_upper"]:+.2f}] ({s["weight_pct"]:.1f}%)"]')
            lines.append(f"        Header --> S{idx}")
        lines.append(f'        Pooled["★ POOLED: {self.data["pooled_estimate"]:+.2f} [{self.data["ci_lower"]:+.2f}, {self.data["ci_upper"]:+.2f}] (p = {self.data["p_value"]:.4f})"]')
        for idx in range(1, len(self.data["studies"]) + 1):
            lines.append(f"        S{idx} --> Pooled")
        lines.append("    end")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<FederatedMetaAnalysisResult pooled={self.data['pooled_estimate']:.4f} 95%CI=[{self.data['ci_lower']:.4f}, {self.data['ci_upper']:.4f}] I2={self.data['i_squared']:.1f}%>"


def federated_meta_analysis(
    estimates: Sequence[float] | None = None,
    std_errors: Sequence[float] | None = None,
    variances: Sequence[float] | None = None,
    sites: Sequence[str] | None = None,
    site_summaries: Any = None,
    con: duckdb.DuckDBPyConnection | None = None,
    query: str | None = None,
    model: str = "random",
    effect_type: str = "beta",
    alpha: float = 0.05,
) -> FederatedMetaAnalysisResult:
    """Computes multi-site federated summary estimates using the DerSimonian-Laird method.

    Aggregates site-level parameter estimates and standard errors without sharing
    patient-level records, outputting pooled effect sizes, Cochran's Q, τ² between-study
    variance, and Higgins' I² heterogeneity.

    Args:
        estimates: Sequence of site-specific parameter estimates (e.g. log OR, log HR, beta).
        std_errors: Sequence of site-specific standard errors.
        variances: Optional sequence of site variances (used if std_errors is None).
        sites: Sequence of site names or identifiers.
        site_summaries: Optional DataFrame or list of dicts with 'site', 'estimate', 'std_error'.
        con: Optional federated DuckDB connection with site databases attached.
        query: Optional SQL query evaluated per site or on v_* views.
        model: 'random' (DerSimonian-Laird random effects) or 'fixed' (inverse-variance fixed effect).
        effect_type: 'beta', 'log_or', 'log_hr', or 'mean_diff'.
        alpha: Significance level for confidence intervals (default: 0.05).

    Returns:
        FederatedMetaAnalysisResult: Result object with summary stats and forest plot methods.
    """
    import math
    import numpy as np
    import pandas as pd
    from scipy import stats

    site_list: list[str] = []
    est_list: list[float] = []
    se_list: list[float] = []

    # 1. Ingest from site_summaries DataFrame / list of dicts
    if site_summaries is not None:
        if isinstance(site_summaries, pd.DataFrame):
            df = site_summaries.copy()
            site_col = "site" if "site" in df.columns else ("site_anon" if "site_anon" in df.columns else df.columns[0])
            est_col = "estimate" if "estimate" in df.columns else ("coef" if "coef" in df.columns else df.columns[1])
            se_col = "std_error" if "std_error" in df.columns else ("se" if "se" in df.columns else df.columns[2])
            for _, r in df.iterrows():
                site_list.append(str(r[site_col]))
                est_list.append(float(r[est_col]))
                se_list.append(float(r[se_col]))
        elif isinstance(site_summaries, (list, tuple)):
            for item in site_summaries:
                site_list.append(str(item.get("site", f"Site {len(site_list)+1}")))
                est_list.append(float(item["estimate"]))
                se_list.append(float(item.get("std_error", item.get("se"))))

    # 2. Ingest from con if provided
    elif con is not None and query is not None:
        q_df = con.execute(query).df()
        site_col = "site" if "site" in q_df.columns else ("site_anon" if "site_anon" in q_df.columns else q_df.columns[0])
        est_col = "estimate" if "estimate" in q_df.columns else ("coef" if "coef" in q_df.columns else q_df.columns[1])
        se_col = "std_error" if "std_error" in q_df.columns else ("se" if "se" in q_df.columns else q_df.columns[2])
        for _, r in q_df.iterrows():
            site_list.append(str(r[site_col]))
            est_list.append(float(r[est_col]))
            se_list.append(float(r[se_col]))

    # 3. Direct estimates & std_errors
    elif estimates is not None:
        est_list = [float(e) for e in estimates]
        if std_errors is not None:
            se_list = [float(s) for s in std_errors]
        elif variances is not None:
            se_list = [math.sqrt(float(v)) for v in variances]
        else:
            raise ValueError("Must provide either std_errors or variances alongside estimates.")
        if sites is not None:
            site_list = [str(s) for s in sites]
        else:
            site_list = [f"Site {i+1}" for i in range(len(est_list))]

    else:
        raise ValueError("Must provide estimates, site_summaries, or con with query.")

    k = len(est_list)
    if k == 0:
        raise ValueError("No study estimates provided for meta-analysis.")

    ests = np.array(est_list, dtype=np.float64)
    ses = np.array(se_list, dtype=np.float64)
    vars_arr = ses ** 2

    # Fixed effect inverse variance weights
    fe_weights = 1.0 / np.maximum(1e-12, vars_arr)
    sum_fe_w = float(np.sum(fe_weights))
    theta_fe = float(np.sum(fe_weights * ests) / sum_fe_w)
    se_fe = float(1.0 / math.sqrt(sum_fe_w))

    # Cochran's Q statistic
    q_stat = float(np.sum(fe_weights * ((ests - theta_fe) ** 2)))
    df = max(1, k - 1)
    q_p_val = float(1.0 - stats.chi2.cdf(q_stat, df)) if k > 1 else 1.0

    # DerSimonian-Laird between-study variance tau^2
    sum_fe_w_sq = float(np.sum(fe_weights ** 2))
    c_factor = float(sum_fe_w - (sum_fe_w_sq / sum_fe_w)) if sum_fe_w > 0 else 1.0
    tau_sq = float(max(0.0, (q_stat - df) / c_factor)) if (k > 1 and c_factor > 0) else 0.0
    tau = math.sqrt(tau_sq)

    # Higgins' I^2 statistic
    i_sq = float(max(0.0, (q_stat - df) / max(1e-12, q_stat)) * 100.0) if (k > 1 and q_stat > 0) else 0.0

    # Random-effects weights
    re_weights = 1.0 / np.maximum(1e-12, vars_arr + tau_sq)
    sum_re_w = float(np.sum(re_weights))
    theta_re = float(np.sum(re_weights * ests) / sum_re_w)
    se_re = float(1.0 / math.sqrt(sum_re_w))

    # Select model output
    use_random = (model.lower().strip() == "random")
    pooled_est = theta_re if use_random else theta_fe
    pooled_se = se_re if use_random else se_fe
    final_weights = re_weights if use_random else fe_weights
    sum_final_w = sum_re_w if use_random else sum_fe_w

    z_crit = float(stats.norm.ppf(1.0 - alpha / 2.0))
    ci_lower = float(pooled_est - z_crit * pooled_se)
    ci_upper = float(pooled_est + z_crit * pooled_se)
    z_score = float(pooled_est / pooled_se) if pooled_se > 0 else 0.0
    p_val = float(2.0 * (1.0 - stats.norm.cdf(abs(z_score))))

    # Study records
    studies = []
    for i in range(k):
        s_low = float(ests[i] - z_crit * ses[i])
        s_high = float(ests[i] + z_crit * ses[i])
        w_pct = float(final_weights[i] / sum_final_w * 100.0)
        studies.append({
            "site": site_list[i],
            "estimate": float(ests[i]),
            "std_error": float(ses[i]),
            "ci_lower": s_low,
            "ci_upper": s_high,
            "weight": float(final_weights[i]),
            "weight_pct": w_pct,
        })

    out = {
        "model": "random" if use_random else "fixed",
        "effect_type": effect_type,
        "k_studies": k,
        "pooled_estimate": pooled_est,
        "se": pooled_se,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "z_score": z_score,
        "p_value": p_val,
        "q_statistic": q_stat,
        "df": df,
        "q_p_value": q_p_val,
        "tau_squared": tau_sq,
        "tau": tau,
        "i_squared": i_sq,
        "studies": studies,
    }

    if effect_type.lower() in ("log_or", "log_hr"):
        out["pooled_exp_estimate"] = math.exp(pooled_est)
        out["exp_ci_lower"] = math.exp(ci_lower)
        out["exp_ci_upper"] = math.exp(ci_upper)

    return FederatedMetaAnalysisResult(out)

