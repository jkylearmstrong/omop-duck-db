"""Multi-Site Consortium Federation Helper.

Provides zero-copy cross-institution querying by attaching independent
DuckDB CDM databases in READ_ONLY mode and generating unified federated
views (v_person, v_visit_occurrence, etc.) with site provenance.
"""

from typing import Dict, Optional, Union
import os
import duckdb
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

        norm_path = os.path.abspath(db_path).replace("\\", "/")
        schema_alias = f"site_{idx}"
        con.execute(f"ATTACH '{norm_path}' AS {schema_alias} (READ_ONLY);")

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

