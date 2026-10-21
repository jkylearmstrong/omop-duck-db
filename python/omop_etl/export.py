"""Enterprise Multi-Target Export Engine for OMOP DuckDB CDM.

Supports:
1. Partitioned Parquet (Hive partitioned for S3/GCS/Azure enclaves).
2. Native DuckDB -> PostgreSQL streaming (zero intermediate CSVs via postgres extension).
3. Oracle / SQL Server / Snowflake / BigQuery: data files, DDL and loader scripts generated from the one CDM DDL source
   (omop_etl.export_targets): SQL*Loader .ctl, BULK INSERT, PUT + COPY INTO, bq load.
4. Standard delimited CSV / TSV exports.
"""

from __future__ import annotations

from omop_etl.paths import sanitize_sql_path
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import duckdb

from omop_etl.export_targets import (
    EXTRA_TARGETS,
    bigquery_load_script,
    csv_select,
    generate_ddl,
    load_cdm_schema,
    oracle_ctl,
    snowflake_load_script,
    sqlserver_load_script,
)


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
    return sanitize_sql_path(p)


def export_cdm(
    con: duckdb.DuckDBPyConnection,
    target_type: str,
    output_path: str | Path,
    tables: Sequence[str] | None = None,
    partition_by: Mapping[str, str] | None = None,
    compression: str = "zstd",
    oracle_max_identifier: int = 30,
) -> dict[str, Any]:
    """Exports OMOP CDM tables to cloud Parquet, PostgreSQL, Oracle, SQL Server, Snowflake, BigQuery, or CSV.
    
    Args:
        con: Active DuckDB connection.
        target_type: One of 'parquet', 'postgres', 'oracle', 'sqlserver', 'snowflake', 'bigquery', 'csv', 'tsv'.
        output_path: Destination directory path, or connection string (for postgres).
        tables: Sequence of CDM tables to export (None = all populated CDM tables).
        partition_by: Mapping of table_name -> SQL partition expression (for Parquet).
        compression: Parquet compression codec ('zstd', 'snappy', 'gzip', etc.).
        oracle_max_identifier: 30 (Oracle < 12.2, the safe default) or 128; longer identifiers raise.

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

    elif tgt in EXTRA_TARGETS:
        # DDL, data files and loader scripts all derive from the single CDM DDL source (see export_targets)
        model = load_cdm_schema()
        unknown = [t for t in tables if t.lower() not in model]
        if unknown:
            raise LookupError(f"Tables not in the CDM v5.4 DDL: {unknown}")
        (out_dir / f"omop_{tgt}_ddl.sql").write_text(
            generate_ddl(tgt, tables, schema=model, oracle_max_identifier=oracle_max_identifier), encoding="utf-8")
        for tbl in tables:
            cols = model[tbl.lower()]
            if tgt in ("oracle", "sqlserver"):
                data = out_dir / f"{tbl}.csv"
                con.execute(f"COPY ({csv_select(tbl, cols)}) TO '{_posix_path(data)}' (FORMAT CSV, HEADER TRUE);")
                results[tbl] = {"status": "EXPORTED", "format": f"{tgt}_csv"}
                if tgt == "oracle":
                    ctl = out_dir / f"{tbl}.ctl"
                    ctl.write_text(oracle_ctl(tbl, cols, oracle_max_identifier), encoding="utf-8")
                    results[tbl].update(format="oracle_sqlldr", ctl_file=str(ctl))
            else:
                data = out_dir / f"{tbl}.parquet"
                con.execute(f"COPY {tbl} TO '{_posix_path(data)}' (FORMAT PARQUET, COMPRESSION {compression});")
                results[tbl] = {"status": "EXPORTED", "format": f"{tgt}_parquet"}
        if tgt == "oracle":
            (out_dir / "omop_oracle_ddl.sql").write_text(
                generate_ddl("oracle", tables, schema=model, oracle_max_identifier=oracle_max_identifier), encoding="utf-8")
            lines = [f"sqlldr userid=$ORA_CONN control={t}.ctl log={t}.log bad={t}.bad" for t in tables]
            nl = chr(10)
            (out_dir / "load_oracle.sh").write_text("#!/usr/bin/env bash" + nl + "set -euo pipefail" + nl + nl.join(lines) + nl, encoding="utf-8")
        elif tgt == "sqlserver":
            (out_dir / "load_sqlserver.sql").write_text(sqlserver_load_script(tables), encoding="utf-8")
        elif tgt == "snowflake":
            (out_dir / "load_snowflake.sql").write_text(snowflake_load_script(tables), encoding="utf-8")
        else:
            (out_dir / "load_bigquery.sh").write_text(bigquery_load_script(tables), encoding="utf-8")

    elif tgt in ("csv", "tsv"):
        delim = "\\t" if tgt == "tsv" else ","
        ext = tgt
        for tbl in tables:
            out_file = out_dir / f"{tbl}.{ext}"
            p_str = _posix_path(out_file)
            con.execute(f"COPY {tbl} TO '{p_str}' (FORMAT CSV, HEADER TRUE, DELIMITER '{delim}');")
            results[tbl] = {"status": "EXPORTED", "format": tgt}

    else:
        raise ValueError(f"Unsupported export target_type: '{target_type}'. Use 'parquet', 'postgres', 'oracle', 'sqlserver', 'snowflake', 'bigquery', 'csv', or 'tsv'.")

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

