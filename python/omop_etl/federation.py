"""Multi-Site Consortium Federation Helper.

Provides zero-copy cross-institution querying by attaching independent
DuckDB CDM databases in READ_ONLY mode and generating unified federated
views (v_person, v_visit_occurrence, etc.) with site provenance.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Union
import os
import re
import duckdb
import numpy as np
import pandas as pd
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
    "cohort",
    "cohort_definition",
    "provider",
    "care_site",
    "cdm_source",
]

__all__ = [
    "create_federated_consortium",
    "FederatedConsortium",
    "with_cell_suppression",
    "with_differential_privacy",
    "check_cross_database_discrepancy",
    "federated_meta_analysis",
    "FederatedMetaAnalysisResult",
    "FEDERATED_TABLES",
    "PrivacyAuditResult",
    "audit_cell_suppression",
    "consortium_privacy_audit",
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
            # Create base table view alias so functions expecting base CDM table names work seamlessly
            has_tbl = con.execute(
                f"SELECT 1 FROM information_schema.tables WHERE table_catalog = current_database() AND table_schema = 'main' AND table_name = '{tbl}' AND table_type = 'BASE TABLE'"
            ).fetchone()
            if not has_tbl:
                con.execute(f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM v_{tbl};")

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


class FederatedConsortium:
    """High-level Multi-Site Federated Consortium Connector.

    Orchestrates multi-site observational analytics across independent site databases
    (e.g., in eDERRI consortium studies) attached to a central Athena vocabulary.
    Provides unified querying and one-call generation of federated Table 1 (demographics & clinical profiles)
    and Table 2 (missingness audits).

    Example:
        >>> consortium = FederatedConsortium(
        ...     sites={"Site A": "omop_Temple.duckdb", "Site B": "omop_PSU.duckdb"},
        ...     central_vocabulary="central_vocabulary.duckdb"
        ... )
        >>> t1 = consortium.generate_table1(domain="heart_failure")
        >>> t2 = consortium.generate_table2()
    """

    def __init__(
        self,
        sites: Mapping[Union[str, int], str],
        central_vocabulary: str | None = None,
        auto_resources: bool = True,
    ):
        if not sites:
            raise ValueError("sites dictionary cannot be empty.")
        self.sites = dict(sites)
        self.central_vocabulary = central_vocabulary
        self.con = create_federated_consortium(self.sites, central_vocab_db=central_vocabulary)
        if auto_resources:
            from omop_etl.resources import configure_resources
            configure_resources(self.con)

    def query(self, sql: str) -> pd.DataFrame:
        """Executes a query across the unified federated views (v_person, v_visit_occurrence, etc.)."""
        return self.con.execute(sql).df()

    def execute(self, sql: str):
        return self.con.execute(sql)

    def site_summaries(self) -> pd.DataFrame:
        """Audits cohort record counts across all participating consortium sites."""
        rows = []
        for site_name in self.sites:
            s_esc = str(site_name).replace("'", "''")
            cnts = self.con.execute(f"""
                SELECT
                    '{s_esc}' AS site,
                    (SELECT COUNT(DISTINCT person_id) FROM v_person WHERE site_anon = '{s_esc}') AS n_persons,
                    (SELECT COUNT(*) FROM v_visit_occurrence WHERE site_anon = '{s_esc}') AS n_visits,
                    (SELECT COUNT(*) FROM v_condition_occurrence WHERE site_anon = '{s_esc}') AS n_conditions,
                    (SELECT COUNT(*) FROM v_measurement WHERE site_anon = '{s_esc}') AS n_measurements,
                    (SELECT COUNT(*) FROM v_drug_exposure WHERE site_anon = '{s_esc}') AS n_drugs
            """).fetchone()
            rows.append({
                "site": cnts[0],
                "n_persons": cnts[1],
                "n_visits": cnts[2],
                "n_conditions": cnts[3],
                "n_measurements": cnts[4],
                "n_drugs": cnts[5],
            })
        return pd.DataFrame(rows)

    def generate_table1(
        self,
        cohort_table: str = "v_visit_occurrence",
        domain: str | None = None,
        stratify_by: str | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Generates federated Table 1 baseline characteristics across all participating sites."""
        from omop_etl.table1_profiles import Table1Generator
        t1 = Table1Generator(con=self.con)
        if domain:
            t1.add_domain_profile(domain)
        return t1.generate(cohort_table=cohort_table, stratify_by=stratify_by, **kwargs)

    def generate_table2(
        self,
        cohort_table: str = "v_visit_occurrence",
        strata_col: str | None = None,
        columns: Sequence[str] | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Generates federated Table 2 missingness and sentinel audit across all sites."""
        from omop_etl.cohort_flow import Table2Generator
        t2 = Table2Generator(**kwargs)
        df = self.con.execute(f"SELECT * FROM {cohort_table}").df()
        return t2.generate(df, columns=columns, strata_col=strata_col)

    def calculate_scores(
        self,
        cohort_table: str = "cohort",
        scores: Sequence[str] = ("lace", "hospital"),
        stratify_by_site: bool = True,
    ) -> ConsortiumScoreResult:
        """Calculates consortium-wide and site-stratified LACE Index and HOSPITAL scores.

        In a multi-center study (e.g. eDERRI), computes patient-level risk predictions and
        site-stratified bedside score distributions across all sites in a single pass.

        Args:
            cohort_table: Name of cohort table or view. Must contain subject_id / person_id,
                cohort_start_date, and cohort_end_date.
            scores: Sequence of scores to calculate: 'lace', 'hospital' (or both).
            stratify_by_site: Whether to generate site-stratified audit statistics.

        Returns:
            ConsortiumScoreResult: Object holding patient-level scores (.scores),
            site-stratified audit (.site_summary), and pooled consortium metrics (.consortium_summary).
        """
        from omop_etl.risk_scores import calculate_bedside_scores

        scores_lower = [s.lower() for s in scores]
        actual_table = cohort_table
        has_table = self.con.execute(
            f"SELECT 1 FROM information_schema.tables WHERE table_name = '{actual_table}'"
        ).fetchone()
        if not has_table:
            has_v = self.con.execute(
                f"SELECT 1 FROM information_schema.tables WHERE table_name = 'v_{actual_table}'"
            ).fetchone()
            if has_v:
                actual_table = f"v_{actual_table}"

        scores_df = calculate_bedside_scores(self.con, cohort_table=actual_table, scores=scores)

        # Attach site provenance if available
        cols = {c.lower() for c in scores_df.columns}
        if "site" not in cols and "site_anon" in cols:
            scores_df["site"] = scores_df["site_anon"]
        elif "site" not in cols:
            try:
                cohort_cols = {c[0].lower() for c in self.con.execute(f"DESCRIBE SELECT * FROM {cohort_table} LIMIT 0;").fetchall()}
                if "site" in cohort_cols or "site_anon" in cohort_cols:
                    s_col = "site" if "site" in cohort_cols else "site_anon"
                    person_col = "subject_id" if "subject_id" in cohort_cols else "person_id"
                    start_col = "cohort_start_date" if "cohort_start_date" in cohort_cols else "visit_start_date"
                    site_map = self.con.execute(f"SELECT DISTINCT {person_col} AS person_id, {start_col} AS cohort_start_date, {s_col} AS site FROM {cohort_table}").df()
                    scores_df = scores_df.merge(site_map, on=["person_id", "cohort_start_date"], how="left")
                else:
                    site_map = self.con.execute("SELECT DISTINCT person_id, site_anon AS site FROM v_person").df()
                    scores_df = scores_df.merge(site_map, on="person_id", how="left")
            except Exception:
                scores_df["site"] = "Consortium"

        if "site" not in scores_df.columns or scores_df["site"].isna().all():
            scores_df["site"] = "Site 1"
        else:
            scores_df["site"] = scores_df["site"].fillna("Unknown")

        # Summary statistics per site and overall
        def _calc_stats(df_sub: pd.DataFrame, site_name: str) -> dict[str, Any]:
            n_pts = len(df_sub)
            row = {
                "site": site_name,
                "n_patients": n_pts,
                "los_mean": float(df_sub["los_days"].mean()) if n_pts > 0 and "los_days" in df_sub else 0.0,
                "los_median": float(df_sub["los_days"].median()) if n_pts > 0 and "los_days" in df_sub else 0.0,
            }
            if "lace" in scores_lower and "lace_score" in df_sub:
                row["mean_lace_score"] = float(df_sub["lace_score"].mean()) if n_pts > 0 else 0.0
                row["median_lace_score"] = float(df_sub["lace_score"].median()) if n_pts > 0 else 0.0
                high_lace = (df_sub["lace_score"] >= 10).sum() if n_pts > 0 else 0
                mod_lace = ((df_sub["lace_score"] >= 5) & (df_sub["lace_score"] <= 9)).sum() if n_pts > 0 else 0
                row["high_lace_count"] = int(high_lace)
                row["high_lace_pct"] = float(high_lace / n_pts * 100.0) if n_pts > 0 else 0.0
                row["moderate_lace_pct"] = float(mod_lace / n_pts * 100.0) if n_pts > 0 else 0.0
            if "hospital" in scores_lower and "hospital_score" in df_sub:
                row["mean_hospital_score"] = float(df_sub["hospital_score"].mean()) if n_pts > 0 else 0.0
                row["median_hospital_score"] = float(df_sub["hospital_score"].median()) if n_pts > 0 else 0.0
                high_hosp = (df_sub["hospital_score"] >= 7).sum() if n_pts > 0 else 0
                int_hosp = ((df_sub["hospital_score"] >= 5) & (df_sub["hospital_score"] <= 6)).sum() if n_pts > 0 else 0
                row["high_hospital_count"] = int(high_hosp)
                row["high_hospital_pct"] = float(high_hosp / n_pts * 100.0) if n_pts > 0 else 0.0
                row["intermediate_hospital_pct"] = float(int_hosp / n_pts * 100.0) if n_pts > 0 else 0.0
            return row

        site_rows = []
        for s_val, group in scores_df.groupby("site"):
            site_rows.append(_calc_stats(group, str(s_val)))

        site_summary_df = pd.DataFrame(site_rows)
        consortium_summary_df = pd.DataFrame([_calc_stats(scores_df, "Consortium (All Sites)")])

        return ConsortiumScoreResult(
            scores=scores_df,
            site_summary=site_summary_df,
            consortium_summary=consortium_summary_df,
        )

    def audit_privacy(
        self,
        tables: dict[str, pd.DataFrame] | pd.DataFrame,
        min_cell_count: int = 11,
        mask_value: str | None = None,
        count_cols: Sequence[str] | None = None,
        suppress_zero: bool = False,
        preserve_pct: bool = False,
        secondary_suppression: bool = False,
    ) -> "PrivacyAuditResult":
        """Audits exported consortium tables against small cell count privacy thresholds (HIPAA/GDPR)."""
        return audit_cell_suppression(
            tables=tables,
            min_cell_count=min_cell_count,
            mask_value=mask_value,
            count_cols=count_cols,
            suppress_zero=suppress_zero,
            preserve_pct=preserve_pct,
            secondary_suppression=secondary_suppression,
        )

    def audit_cell_suppression(
        self,
        tables: dict[str, pd.DataFrame] | pd.DataFrame,
        min_cell_count: int = 11,
        mask_value: str | None = None,
        count_cols: Sequence[str] | None = None,
        suppress_zero: bool = False,
        preserve_pct: bool = False,
        secondary_suppression: bool = False,
    ) -> "PrivacyAuditResult":
        """Alias for audit_privacy."""
        return self.audit_privacy(
            tables=tables,
            min_cell_count=min_cell_count,
            mask_value=mask_value,
            count_cols=count_cols,
            suppress_zero=suppress_zero,
            preserve_pct=preserve_pct,
            secondary_suppression=secondary_suppression,
        )

    def cross_site_validation(
        self,
        site_data: dict[str, pd.DataFrame] | pd.DataFrame,
        feature_cols: Sequence[str],
        target_col: str,
        site_col: str = "site",
        regularize: bool = True,
        alpha: float = 1.0,
    ) -> Any:
        """Runs K x K Internal-External Cross-Validation (IECV) across participating sites."""
        from .ml import consortium_cross_site_validation
        return consortium_cross_site_validation(
            site_data=site_data,
            feature_cols=feature_cols,
            target_col=target_col,
            site_col=site_col,
            regularize=regularize,
            alpha=alpha,
        )

    def cohort_diagnostics(
        self,
        cohort_table: str = "cohort",
        cohort_id: int = 1,
        baseline_window_days: int = 365,
        top_n_concepts: int = 10,
        secondary_suppression: bool = True,
        min_cell_count: int = 11,
    ) -> Any:
        """Generates pure DuckDB OHDSI Cohort Diagnostics across the consortium database."""
        from .cohort_diagnostics import consortium_cohort_diagnostics
        return consortium_cohort_diagnostics(
            con=self.con,
            cohort_table=cohort_table,
            cohort_id=cohort_id,
            baseline_window_days=baseline_window_days,
            top_n_concepts=top_n_concepts,
            secondary_suppression=secondary_suppression,
            min_cell_count=min_cell_count,
        )

    def build_manuscript(self, **kwargs) -> str:
        """Builds an end-to-end Quarto or Typst manuscript scaffold for the consortium."""
        from .manuscript import build_manuscript_scaffold
        kwargs.setdefault("consortium_name", self.consortium_name)
        return build_manuscript_scaffold(**kwargs)

    def close(self):
        """Closes the DuckDB connection handle."""
        if self.con is not None:
            try:
                self.con.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class ConsortiumScoreResult:
    """Consortium-level bedside scores and site-stratified summaries."""

    def __init__(
        self,
        scores: pd.DataFrame,
        site_summary: pd.DataFrame,
        consortium_summary: pd.DataFrame,
    ):
        self._scores = scores
        self._site_summary = site_summary
        self._consortium_summary = consortium_summary

    @property
    def scores(self) -> pd.DataFrame:
        """Patient/visit-level scores across all participating sites."""
        return self._scores

    @property
    def site_summary(self) -> pd.DataFrame:
        """Site-stratified risk scores, acuity, and predictor distribution."""
        return self._site_summary

    @property
    def consortium_summary(self) -> pd.DataFrame:
        """Consortium-wide pooled score summary."""
        return self._consortium_summary

    def summary(self) -> pd.DataFrame:
        """Combined summary table with per-site rows and a pooled Consortium row."""
        return pd.concat([self._site_summary, self._consortium_summary], ignore_index=True)

    def to_dataframe(self) -> pd.DataFrame:
        return self._scores.copy()

    def __getitem__(self, key: str) -> Any:
        if key == "scores":
            return self._scores
        elif key == "site_summary":
            return self._site_summary
        elif key == "consortium_summary":
            return self._consortium_summary
        elif key in self._scores.columns:
            return self._scores[key]
        raise KeyError(key)

    def __repr__(self) -> str:
        return (
            f"<ConsortiumScoreResult: {len(self._scores)} records across "
            f"{len(self._site_summary)} site(s)>\n"
            f"{self.summary().to_string()}"
        )


class PrivacyAuditResult(dict):
    """Container for privacy-audited consortium tables with cell suppression logs.

    Subclasses dict so that clean_tables['table1'] or clean_tables.get('table1')
    returns the masked, privacy-compliant DataFrame directly.
    """

    def __init__(
        self,
        tables: dict[str, pd.DataFrame],
        violations: list[dict[str, Any]],
        min_cell_count: int,
        mask_value: str,
        is_single_df: bool = False,
    ):
        super().__init__(tables)
        self.tables = tables
        self.violations = (
            pd.DataFrame(violations)
            if violations
            else pd.DataFrame(
                columns=["table", "row_index", "column", "original_value", "masked_value"]
            )
        )
        self.min_cell_count = min_cell_count
        self.mask_value = mask_value
        self._is_single_df = is_single_df

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    @property
    def is_clean(self) -> bool:
        return len(self.violations) == 0

    @property
    def primary_count(self) -> int:
        if self.violations.empty:
            return 0
        if "suppression_type" in self.violations.columns:
            return int((self.violations["suppression_type"] == "primary").sum())
        return len(self.violations)

    @property
    def secondary_count(self) -> int:
        if self.violations.empty or "suppression_type" not in self.violations.columns:
            return 0
        return int((self.violations["suppression_type"] == "secondary").sum())

    def summary(self) -> pd.DataFrame:
        """Emits a per-table summary of audited rows, columns, and small cell violations."""
        rows = []
        for tbl_name, df in self.tables.items():
            tbl_v = (
                self.violations[self.violations["table"] == tbl_name]
                if not self.violations.empty
                else pd.DataFrame()
            )
            prim_c = (
                int((tbl_v["suppression_type"] == "primary").sum())
                if not tbl_v.empty and "suppression_type" in tbl_v.columns
                else len(tbl_v)
            )
            sec_c = (
                int((tbl_v["suppression_type"] == "secondary").sum())
                if not tbl_v.empty and "suppression_type" in tbl_v.columns
                else 0
            )
            rows.append({
                "table": tbl_name,
                "table_name": tbl_name,
                "n_rows": len(df),
                "n_columns": len(df.columns),
                "suppressed_cells": len(tbl_v),
                "primary_suppressed": prim_c,
                "secondary_suppressed": sec_c,
                "violations": len(tbl_v),
                "min_cell_count": self.min_cell_count,
                "status": "CLEAN" if len(tbl_v) == 0 else f"SUPPRESSED_{len(tbl_v)}",
            })
        return pd.DataFrame(rows)

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the primary clean DataFrame if a single table was audited, or the violations table."""
        if self._is_single_df and len(self.tables) == 1:
            return next(iter(self.tables.values())).copy()
        return self.violations.copy()

    def __repr__(self) -> str:
        sec_info = f" ({self.primary_count} primary, {self.secondary_count} secondary)" if self.secondary_count > 0 else ""
        return (
            f"<PrivacyAuditResult: {len(self.tables)} table(s) audited, "
            f"{len(self.violations)} small cell(s) suppressed{sec_info} (N < {self.min_cell_count})>\n"
            f"{self.summary().to_string(index=False)}"
        )


def audit_cell_suppression(
    tables: dict[str, pd.DataFrame] | pd.DataFrame,
    min_cell_count: int = 11,
    mask_value: str | None = None,
    count_cols: Sequence[str] | None = None,
    suppress_zero: bool = False,
    preserve_pct: bool = False,
    secondary_suppression: bool = False,
    return_audit: bool = False,
) -> Any:
    """Scans and redacts small cell counts in consortium export tables for HIPAA/GDPR compliance.

    Consortium governance and de-identification standards (HIPAA Safe Harbor, GDPR,
    and CMS cell suppression policy) require that multi-site summary tables do not
    display small cell counts (e.g. N < 11 or N < 20) that could re-identify individual patients.
    When secondary_suppression=True, complementary masking is performed: if exactly one cell
    in a row or column is suppressed, the second-smallest non-zero cell is masked to prevent
    deductive algebraic disclosure.

    Args:
        tables: Single pandas DataFrame or dictionary of DataFrames (e.g. from generate_table1(),
            generate_table2(), calculate_scores(), risk factor tables).
        min_cell_count: Threshold below which patient counts are masked (default 11).
        mask_value: String to replace small counts with (default: f"<{min_cell_count}").
        count_cols: Explicit list of column names to scan. If None, automatically detects count
            columns and formatted count cells (e.g. '5 (2.3%)', integer counts).
        suppress_zero: Whether to suppress zero counts (default False, as 0 does not identify individuals).
        preserve_pct: For cells formatted as 'N (pct%)', whether to preserve the percentage (e.g. '<11 (2.3%)').
        secondary_suppression: Whether to apply complementary masking to prevent marginal reconstruction.
        return_audit: Whether to always return PrivacyAuditResult even for a single DataFrame.

    Returns:
        PrivacyAuditResult or pd.DataFrame: Container holding sanitized DataFrames and violation audit logs.
    """
    if isinstance(tables, pd.DataFrame):
        tables_dict = {"table": tables.copy()}
        is_single_df = True
    elif isinstance(tables, dict):
        tables_dict = {k: (v.copy() if isinstance(v, pd.DataFrame) else pd.DataFrame(v)) for k, v in tables.items()}
        is_single_df = False
    else:
        raise TypeError(f"tables must be a DataFrame or dict of DataFrames, got {type(tables).__name__}")

    mask_str = mask_value if mask_value is not None else f"<{int(min_cell_count)}"
    violations: list[dict[str, Any]] = []

    skip_col_names = {
        "p_value", "p_val", "smd", "mean", "sd", "median", "iqr", "hr", "shr",
        "auc", "c_index", "brier", "ece", "mce", "threshold", "step", "variable",
        "feature", "category", "site", "site_anon", "metric", "unit", "concept_id", "loinc",
    }

    count_keywords = ("count", "n_patients", "n_subjects", "_n", "subjects", "patients", "cases", "events", "num", "freq", "total", "high_risk", "readmitted", "deaths")

    pct_cell_pattern = re.compile(r"^(\d+)\s*\(([\d\.]+%?)\)$")
    int_cell_pattern = re.compile(r"^\s*(\d+)\s*$")
    ratio_cell_pattern = re.compile(r"^(\d+)\s*/\s*(\d+)")

    def should_suppress(n: int) -> bool:
        if suppress_zero:
            return 0 <= n < min_cell_count
        return 1 <= n < min_cell_count

    clean_tables: dict[str, pd.DataFrame] = {}

    for tbl_name, df in tables_dict.items():
        df_clean = df.copy()
        target_count_cols: list[str] = []

        for col in df_clean.columns:
            col_lower = str(col).lower().strip()
            if count_cols is not None:
                is_target_col = col in count_cols
            else:
                if col_lower in skip_col_names or col_lower.endswith("_pct") or col_lower.endswith("_percent") or col_lower.endswith("_smd") or col_lower.endswith("_rate"):
                    is_target_col = False
                elif any(kw in col_lower for kw in count_keywords):
                    is_target_col = True
                else:
                    # Generic column (e.g. Overall, Strata, No, Yes): inspect values
                    is_target_col = True

            if not is_target_col:
                continue

            target_count_cols.append(col)

            # Iterate through column values
            new_col_vals = []
            col_modified = False
            for row_idx, val in enumerate(df_clean[col]):
                if pd.isna(val) or val is None:
                    new_col_vals.append(val)
                    continue

                masked = False
                new_val = val

                # 1. Numeric types (int or float integer)
                if isinstance(val, (int, np.integer)):
                    n_val = int(val)
                    if should_suppress(n_val):
                        new_val = mask_str
                        masked = True
                elif isinstance(val, (float, np.floating)) and val.is_integer() and (count_cols is not None or any(kw in col_lower for kw in count_keywords)):
                    n_val = int(val)
                    if should_suppress(n_val):
                        new_val = mask_str
                        masked = True
                # 2. String representations
                elif isinstance(val, str):
                    val_str = val.strip()
                    m_pct = pct_cell_pattern.match(val_str)
                    m_int = int_cell_pattern.match(val_str)
                    m_ratio = ratio_cell_pattern.match(val_str)

                    if m_pct:
                        n_val = int(m_pct.group(1))
                        if should_suppress(n_val):
                            pct_str = m_pct.group(2)
                            new_val = f"{mask_str} ({pct_str})" if preserve_pct else mask_str
                            masked = True
                    elif m_int and (count_cols is not None or any(kw in col_lower for kw in count_keywords) or (row_idx == 0 and "total" in str(df_clean.iloc[row_idx, 0]).lower())):
                        n_val = int(m_int.group(1))
                        if should_suppress(n_val):
                            new_val = mask_str
                            masked = True
                    elif m_ratio:
                        n_val = int(m_ratio.group(1))
                        if should_suppress(n_val):
                            new_val = mask_str
                            masked = True

                if masked:
                    col_modified = True
                    violations.append({
                        "table": tbl_name,
                        "row_index": row_idx,
                        "column": col,
                        "original_value": val,
                        "masked_value": new_val,
                        "suppression_type": "primary",
                    })

                new_col_vals.append(new_val)

            if col_modified:
                df_clean[col] = new_col_vals

        # Secondary / complementary cell suppression
        if secondary_suppression and target_count_cols:
            for c in target_count_cols:
                if c in df_clean.columns and df_clean[c].dtype != object:
                    df_clean[c] = df_clean[c].astype(object)

            def parse_cell_count(v: Any) -> tuple[int | None, str | None]:
                if pd.isna(v) or v is None:
                    return None, None
                if isinstance(v, (int, np.integer)):
                    return int(v), None
                if isinstance(v, (float, np.floating)) and v.is_integer():
                    return int(v), None
                if isinstance(v, str):
                    v_s = v.strip()
                    m_p = pct_cell_pattern.match(v_s)
                    if m_p:
                        return int(m_p.group(1)), m_p.group(2)
                    m_i = int_cell_pattern.match(v_s)
                    if m_i:
                        return int(m_i.group(1)), None
                    m_r = ratio_cell_pattern.match(v_s)
                    if m_r:
                        return int(m_r.group(1)), None
                return None, None

            # a) Row-level secondary suppression across count columns
            for r_idx in range(len(df_clean)):
                suppressed_in_row = [
                    v for v in violations
                    if v["table"] == tbl_name and v["row_index"] == r_idx and v["column"] in target_count_cols
                ]
                if len(suppressed_in_row) == 1:
                    # Exactly one cell suppressed in this row: find the second-smallest non-zero cell
                    suppressed_col = suppressed_in_row[0]["column"]
                    candidates = []
                    for c_col in target_count_cols:
                        if c_col == suppressed_col:
                            continue
                        val = df_clean.at[r_idx, c_col]
                        cnt, pct = parse_cell_count(val)
                        if cnt is not None and cnt > 0:
                            candidates.append((cnt, pct, c_col, val))
                    if candidates:
                        candidates.sort(key=lambda x: x[0])
                        sec_cnt, sec_pct, sec_col, sec_orig = candidates[0]
                        sec_masked_val = (
                            f"{mask_str} ({sec_pct})"
                            if preserve_pct and sec_pct is not None
                            else mask_str
                        )
                        df_clean.at[r_idx, sec_col] = sec_masked_val
                        violations.append({
                            "table": tbl_name,
                            "row_index": r_idx,
                            "column": sec_col,
                            "original_value": sec_orig,
                            "masked_value": sec_masked_val,
                            "suppression_type": "secondary",
                        })

            # b) Column-level secondary suppression
            for c_col in target_count_cols:
                suppressed_in_col = [
                    v for v in violations
                    if v["table"] == tbl_name and v["column"] == c_col
                ]
                if len(suppressed_in_col) == 1:
                    suppressed_row = suppressed_in_col[0]["row_index"]
                    candidates = []
                    for r_idx in range(len(df_clean)):
                        if r_idx == suppressed_row:
                            continue
                        val = df_clean.at[r_idx, c_col]
                        cnt, pct = parse_cell_count(val)
                        if cnt is not None and cnt > 0:
                            candidates.append((cnt, pct, r_idx, val))
                    if candidates:
                        candidates.sort(key=lambda x: x[0])
                        sec_cnt, sec_pct, sec_row, sec_orig = candidates[0]
                        sec_masked_val = (
                            f"{mask_str} ({sec_pct})"
                            if preserve_pct and sec_pct is not None
                            else mask_str
                        )
                        df_clean.at[sec_row, c_col] = sec_masked_val
                        violations.append({
                            "table": tbl_name,
                            "row_index": sec_row,
                            "column": c_col,
                            "original_value": sec_orig,
                            "masked_value": sec_masked_val,
                            "suppression_type": "secondary",
                        })

        clean_tables[tbl_name] = df_clean

    result = PrivacyAuditResult(
        tables=clean_tables,
        violations=violations,
        min_cell_count=min_cell_count,
        mask_value=mask_str,
        is_single_df=is_single_df,
    )

    if is_single_df and not return_audit:
        single_df = clean_tables["table"]
        single_df.attrs["suppressed_cell_count"] = len(violations)
        single_df.attrs["primary_suppressed_count"] = result.primary_count
        single_df.attrs["secondary_suppressed_count"] = result.secondary_count
        single_df.attrs["min_cell_count"] = min_cell_count
        single_df.attrs["is_clean"] = len(violations) == 0
        return single_df

    return result


consortium_privacy_audit = audit_cell_suppression


# Aggregate-only site telemetry and the consortium aggregator (FR-A2).
from omop_etl.telemetry import (  # noqa: E402,F401
    aggregate_telemetry,
    site_telemetry,
    validate_packet,
)

__all__ += ["site_telemetry", "aggregate_telemetry", "validate_packet"]
