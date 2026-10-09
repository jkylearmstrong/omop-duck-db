"""Enterprise Multi-Target Export Engine for OMOP DuckDB CDM.

Supports:
1. Partitioned Parquet (Hive partitioned for S3/GCS/Azure enclaves).
2. Native DuckDB -> PostgreSQL streaming (zero intermediate CSVs via postgres extension).
3. Oracle enterprise bulk loading (generates data files + SQL*Loader .ctl control files + Oracle DDL).
4. Standard delimited CSV / TSV exports.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import duckdb


ALL_CDM_TABLES = [
    "person", "observation_period", "visit_occurrence", "condition_occurrence",
    "procedure_occurrence", "drug_exposure", "measurement", "death", "care_site",
    "provider", "location", "cdm_source", "condition_era", "drug_era"
]

ORACLE_TYPE_MAP = {
    "BIGINT": "NUMBER(19)",
    "INTEGER": "NUMBER(10)",
    "SMALLINT": "NUMBER(5)",
    "TINYINT": "NUMBER(3)",
    "VARCHAR": "VARCHAR2(255)",
    "DOUBLE": "NUMBER",
    "FLOAT": "NUMBER",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "BOOLEAN": "NUMBER(1)",
}


def _posix_path(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def export_cdm(
    con: duckdb.DuckDBPyConnection,
    target_type: str,
    output_path: str | Path,
    tables: Sequence[str] | None = None,
    partition_by: Mapping[str, str] | None = None,
    compression: str = "zstd",
) -> dict[str, Any]:
    """Exports OMOP CDM tables to cloud Parquet, PostgreSQL, Oracle, or CSV.
    
    Args:
        con: Active DuckDB connection.
        target_type: One of 'parquet', 'postgres', 'oracle', 'csv', 'tsv'.
        output_path: Destination directory path, or connection string (for postgres).
        tables: Sequence of CDM tables to export (None = all populated CDM tables).
        partition_by: Mapping of table_name -> SQL partition expression (for Parquet).
        compression: Parquet compression codec ('zstd', 'snappy', 'gzip', etc.).
        
    Returns:
        dict: Summary of exported tables, row counts, and output files.
    """
    tgt = target_type.lower().strip()
    out_dir = Path(output_path) if tgt != "postgres" else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    if tables is None:
        # Discover existing CDM tables
        existing_tables = []
        for tbl in ALL_CDM_TABLES:
            cnt = con.execute(f"SELECT COUNT(*) FROM information_schema.tables WHERE lower(table_name) = '{tbl.lower()}';").fetchone()[0]
            if cnt > 0:
                row_cnt = con.execute(f"SELECT COUNT(*) FROM {tbl};").fetchone()[0]
                if row_cnt > 0:
                    existing_tables.append(tbl)
        tables = existing_tables or ALL_CDM_TABLES

    results = {}

    if tgt == "parquet":
        partition_by = partition_by or {}
        for tbl in tables:
            part_expr = partition_by.get(tbl)
            tbl_path = out_dir / tbl
            if part_expr:
                tbl_path.mkdir(parents=True, exist_ok=True)
                p_str = _posix_path(tbl_path)
                sql = f"""
                    COPY (
                        SELECT *, {part_expr} AS _part_col 
                        FROM {tbl}
                    ) TO '{p_str}' 
                    (FORMAT PARQUET, PARTITION_BY (_part_col), COMPRESSION {compression});
                """
            else:
                out_file = out_dir / f"{tbl}.parquet"
                p_str = _posix_path(out_file)
                sql = f"COPY {tbl} TO '{p_str}' (FORMAT PARQUET, COMPRESSION {compression});"
            con.execute(sql)
            results[tbl] = {"status": "EXPORTED", "format": "parquet"}

    elif tgt == "postgres":
        # DuckDB native postgres extension streaming
        con.execute("INSTALL postgres; LOAD postgres;")
        pg_uri = str(output_path).replace("'", "''")
        con.execute(f"ATTACH '{pg_uri}' AS target_pg (TYPE POSTGRES);")
        for tbl in tables:
            con.execute(f"INSERT INTO target_pg.{tbl} SELECT * FROM {tbl};")
            results[tbl] = {"status": "STREAMED_TO_POSTGRES"}
        con.execute("DETACH target_pg;")

    elif tgt == "oracle":
        # Generates CSV data dumps + Oracle .ctl SQL*Loader control files + Oracle DDL
        ddl_statements = []
        ctl_files = []

        for tbl in tables:
            csv_path = out_dir / f"{tbl}.csv"
            p_str = _posix_path(csv_path)
            con.execute(f"COPY {tbl} TO '{p_str}' (FORMAT CSV, HEADER TRUE);")

            # Introspect schema for Oracle DDL and control file
            cols_info = con.execute(f"DESCRIBE SELECT * FROM {tbl} LIMIT 0").fetchall()
            oracle_cols = []
            ctl_col_defs = []

            for col_name, col_type, *rest in cols_info:
                clean_type = col_type.split("(")[0].upper()
                ora_type = ORACLE_TYPE_MAP.get(clean_type, "VARCHAR2(255)")
                if "VARCHAR" in clean_type and "(" in col_type:
                    ora_type = col_type.upper().replace("VARCHAR", "VARCHAR2")
                oracle_cols.append(f"    {col_name.upper()} {ora_type}")
                ctl_col_defs.append(f"    {col_name.upper()} CHAR(4000)")

            ddl_statements.append(f"CREATE TABLE {tbl.upper()} (\n" + ",\n".join(oracle_cols) + "\n);")

            ctl_cols_str = ",\n".join(ctl_col_defs)
            ctl_content = f"""LOAD DATA
INFILE '{tbl}.csv'
INTO TABLE {tbl.upper()}
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
TRAILING NULLCOLS
(
{ctl_cols_str}
)
"""
            ctl_path = out_dir / f"{tbl}.ctl"
            ctl_path.write_text(ctl_content, encoding="utf-8")
            ctl_files.append(str(ctl_path))

            results[tbl] = {"status": "EXPORTED", "format": "oracle_sqlldr", "ctl_file": str(ctl_path)}

        ddl_path = out_dir / "omop_oracle_ddl.sql"
        ddl_path.write_text("\n\n".join(ddl_statements), encoding="utf-8")

        sh_script = out_dir / "load_oracle.sh"
        sh_lines = [f"sqlldr control={tbl}.ctl log={tbl}.log bad={tbl}.bad direct=true" for tbl in tables]
        sh_script.write_text("#!/usr/bin/env bash\n" + "\n".join(sh_lines) + "\n", encoding="utf-8")

    elif tgt in ("csv", "tsv"):
        delim = "\\t" if tgt == "tsv" else ","
        ext = tgt
        for tbl in tables:
            out_file = out_dir / f"{tbl}.{ext}"
            p_str = _posix_path(out_file)
            con.execute(f"COPY {tbl} TO '{p_str}' (FORMAT CSV, HEADER TRUE, DELIMITER '{delim}');")
            results[tbl] = {"status": "EXPORTED", "format": tgt}

    else:
        raise ValueError(f"Unsupported export target_type: '{target_type}'. Use 'parquet', 'postgres', 'oracle', 'csv', or 'tsv'.")

    return {
        "target_type": tgt,
        "output_path": str(output_path),
        "exported_tables": results,
    }


def export_to_parquet(
    db_path: str | Path | duckdb.DuckDBPyConnection,
    table_name: str,
    output_dir: str | Path,
    partition_by: Sequence[str] | None = None,
    compression: str = "zstd",
) -> str:
    """Exports an OMOP CDM table directly from DuckDB to partitioned or unpartitioned Parquet.

    Args:
        db_path: Path to DuckDB database file or an active connection.
        table_name: Table or view name in DuckDB to export.
        output_dir: Destination directory path.
        partition_by: Optional sequence of column names to partition by.
        compression: Parquet compression codec (default 'zstd').

    Returns:
        str: Absolute destination path.
    """
    should_close = False
    if isinstance(db_path, (str, Path)):
        con = duckdb.connect(str(db_path), read_only=True)
        should_close = True
    else:
        con = db_path

    try:
        out_p = Path(output_dir)
        out_p.mkdir(parents=True, exist_ok=True)

        if partition_by:
            p_str = _posix_path(out_p)
            part_cols = ", ".join(partition_by)
            part_clause = f", PARTITION_BY ({part_cols})"
            sql = f"COPY {table_name} TO '{p_str}' (FORMAT PARQUET, COMPRESSION '{compression}'{part_clause});"
        else:
            out_file = out_p / f"{table_name}.parquet"
            p_str = _posix_path(out_file)
            sql = f"COPY {table_name} TO '{p_str}' (FORMAT PARQUET, COMPRESSION '{compression}');"

        con.execute(sql)
        return str(out_p.resolve())
    finally:
        if should_close:
            con.close()

