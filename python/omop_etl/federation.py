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
