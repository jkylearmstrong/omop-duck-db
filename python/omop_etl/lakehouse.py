"""Delta Lake and Apache Iceberg Lakehouse Connector for OMOP DuckDB.

Enables zero-copy querying of remote cloud object stores (S3, MinIO, Azure Blob, GCS)
or local Lakehouses stored in Delta Lake or Apache Iceberg format.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import duckdb
import pandas as pd

DEFAULT_LAKEHOUSE_TABLES = [
    "person",
    "observation_period",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "drug_exposure",
    "measurement",
    "observation",
    "death",
    "care_site",
    "provider",
    "location",
    "cdm_source",
    "condition_era",
    "drug_era",
]


def _configure_s3(con: duckdb.DuckDBPyConnection, s3_options: Mapping[str, Any]) -> None:
    """Configures DuckDB S3 / MinIO / Ceph credentials and endpoint settings."""
    mapping = {
        "endpoint": "s3_endpoint",
        "s3_endpoint": "s3_endpoint",
        "access_key_id": "s3_access_key_id",
        "s3_access_key_id": "s3_access_key_id",
        "secret_access_key": "s3_secret_access_key",
        "s3_secret_access_key": "s3_secret_access_key",
        "region": "s3_region",
        "s3_region": "s3_region",
        "session_token": "s3_session_token",
        "s3_session_token": "s3_session_token",
        "use_ssl": "s3_use_ssl",
        "s3_use_ssl": "s3_use_ssl",
        "url_style": "s3_url_style",
        "s3_url_style": "s3_url_style",
    }

    for opt_key, opt_val in s3_options.items():
        var_name = mapping.get(opt_key.lower())
        if var_name:
            if isinstance(opt_val, bool):
                val_str = "true" if opt_val else "false"
            else:
                val_str = f"'{opt_val}'"
            try:
                con.execute(f"SET {var_name} = {val_str};")
            except Exception:
                pass


def omop_connect_lakehouse(
    lake_uri: str | Path,
    format: str = "delta",
    s3_options: Mapping[str, Any] | None = None,
    tables: Sequence[str] | None = None,
    output_con: duckdb.DuckDBPyConnection | None = None,
    create_bare_aliases: bool = True,
) -> duckdb.DuckDBPyConnection:
    """Connects to a Delta Lake or Apache Iceberg OMOP repository and registers views.

    Dynamically attaches tables located under `lake_uri/{table}` as OMOP views
    `v_person`, `v_condition_occurrence`, etc.

    Args:
        lake_uri: Root URI or filesystem path to the lakehouse storage directory.
        format: Lakehouse table format ('delta', 'iceberg', or 'parquet').
        s3_options: S3/MinIO configuration dictionary (endpoint, access keys, region, SSL).
        tables: Sequence of CDM table names to scan (defaults to standard CDM tables).
        output_con: Optional existing DuckDB connection (creates new in-memory connection if None).
        create_bare_aliases: Whether to also create views without 'v_' prefix (e.g. `person`).

    Returns:
        duckdb.DuckDBPyConnection: Active connection with registered lakehouse views.
    """
    con = output_con if output_con is not None else duckdb.connect(":memory:")
    uri_str = str(lake_uri).rstrip("/\\").replace("\\", "/")
    fmt = format.lower().strip()
    target_tables = list(tables) if tables is not None else DEFAULT_LAKEHOUSE_TABLES

    # Try loading httpfs extension for remote URI access
    if uri_str.startswith(("s3://", "s3a://", "http://", "https://", "azure://", "abfs://", "gcs://")):
        try:
            con.execute("INSTALL httpfs; LOAD httpfs;")
        except Exception:
            pass

    if s3_options:
        _configure_s3(con, s3_options)

    # Load the delta / iceberg extension only for tables that are one (a `_delta_log` or `metadata` directory, or a remote URI that
    # cannot be inspected). A plain parquet directory never needs it, and a failed INSTALL / LOAD crashes the R binding of DuckDB
    # (intermittent segfault, reproduced on Windows), so both bindings behave the same way.
    delta_available = False
    iceberg_available = False
    is_remote = uri_str.startswith(("s3://", "s3a://", "http://", "https://", "azure://", "abfs://", "gcs://"))
    marker = {"delta": "_delta_log", "iceberg": "metadata"}.get(fmt)

    def is_lake_table(tbl: str) -> bool:
        return is_remote or (marker is not None and (Path(uri_str) / tbl / marker).is_dir())

    needs_extension = marker is not None and any(is_lake_table(t) for t in target_tables)

    if fmt == "delta" and needs_extension:
        try:
            con.execute("INSTALL delta; LOAD delta;")
            delta_available = True
        except Exception:
            try:
                con.execute("LOAD delta;")
                delta_available = True
            except Exception:
                delta_available = False
    elif fmt == "iceberg" and needs_extension:
        try:
            con.execute("INSTALL iceberg; LOAD iceberg;")
            iceberg_available = True
        except Exception:
            try:
                con.execute("LOAD iceberg;")
                iceberg_available = True
            except Exception:
                iceberg_available = False

    is_local = not uri_str.startswith(("s3://", "s3a://", "http://", "https://", "azure://", "abfs://", "gcs://"))

    for tbl in target_tables:
        table_path = f"{uri_str}/{tbl}"
        if is_local:
            local_path = Path(table_path)
            if not local_path.exists():
                continue

        scan_expr = None
        if fmt == "delta" and delta_available and is_lake_table(tbl):
            scan_expr = f"delta_scan('{table_path}')"
        elif fmt == "iceberg" and iceberg_available and is_lake_table(tbl):
            # Check for metadata folder or scan directly
            scan_expr = f"iceberg_scan('{table_path}')"
        else:
            # Fallback to parquet reading
            if is_local:
                parquet_files = list(Path(table_path).glob("**/*.parquet"))
                if parquet_files:
                    scan_expr = f"read_parquet('{table_path}/**/*.parquet')"
            else:
                scan_expr = f"read_parquet('{table_path}/**/*.parquet')"

        if scan_expr is None and is_local:
            # Check if direct parquet file exists (e.g. {table}.parquet)
            single_parquet = Path(f"{uri_str}/{tbl}.parquet")
            if single_parquet.exists():
                scan_expr = f"read_parquet('{uri_str}/{tbl}.parquet')"

        if scan_expr:
            try:
                con.execute(f"CREATE OR REPLACE VIEW v_{tbl} AS SELECT * FROM {scan_expr};")
                if create_bare_aliases:
                    con.execute(f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM v_{tbl};")
            except Exception:
                # If delta_scan fails (e.g. mock delta without transaction log in test), try read_parquet fallback
                try:
                    fallback_expr = f"read_parquet('{table_path}/**/*.parquet')"
                    con.execute(f"CREATE OR REPLACE VIEW v_{tbl} AS SELECT * FROM {fallback_expr};")
                    if create_bare_aliases:
                        con.execute(f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM v_{tbl};")
                except Exception:
                    pass

    return con


def export_to_delta_lake(
    data: Any,
    table_uri: str | Path,
    *,
    table_name: str | None = None,
    mode: str = "overwrite",
    partition_by: Sequence[str] | None = None,
    storage_options: Mapping[str, Any] | None = None,
) -> Path | str:
    """Exports a pandas DataFrame, DuckDB table, or Parquet dataset to Delta Lake.

    Requires the ``deltalake`` package. Writes ACID-compliant, versioned Delta Lake tables
    with support for partition pruning and schema validation.

    Args:
        data: A pandas DataFrame, DuckDB connection, or Path to a parquet file/dir.
        table_uri: Target directory path or cloud URI (s3://, gcs://, abfs://) for the Delta table.
        table_name: Table name if ``data`` is a DuckDB connection.
        mode: Write mode ('overwrite', 'append', 'error', 'ignore'). Default 'overwrite'.
        partition_by: Optional sequence of column names to partition the Delta table by.
        storage_options: Optional storage options mapping for cloud object stores.

    Returns:
        Path | str: Path or URI to the target Delta Lake directory.
    """
    try:
        import deltalake
    except ImportError as exc:
        raise ImportError(
            "The 'deltalake' package is required for export_to_delta_lake(). "
            "Install it with 'pip install deltalake'."
        ) from exc

    is_remote = str(table_uri).startswith(("s3://", "s3a://", "http://", "https://", "azure://", "abfs://", "gcs://"))
    target_str = str(table_uri).rstrip("/\\")

    if not is_remote:
        target_path = Path(table_uri).resolve()
        target_path.mkdir(parents=True, exist_ok=True)
        target_str = str(target_path)

    if isinstance(data, pd.DataFrame):
        df = data
    elif hasattr(data, "execute") and hasattr(data, "df"):
        if not table_name:
            raise ValueError("table_name must be specified when exporting from a DuckDB connection")
        df = data.execute(f"SELECT * FROM {table_name}").df()
    elif isinstance(data, (str, Path)):
        p = Path(data)
        if p.is_file():
            df = pd.read_parquet(p)
        elif p.is_dir():
            pq_files = list(p.glob("*.parquet")) + list(p.glob("**/*.parquet"))
            if not pq_files:
                raise FileNotFoundError(f"No parquet files found under directory {p}")
            df = pd.concat([pd.read_parquet(f) for f in pq_files], ignore_index=True)
        else:
            raise FileNotFoundError(f"Source path does not exist: {p}")
    else:
        raise TypeError(f"Unsupported data source type: {type(data)}")

    deltalake.write_deltalake(
        target_str,
        df,
        mode=mode,
        partition_by=list(partition_by) if partition_by else None,
        storage_options=dict(storage_options) if storage_options else None,
    )

    return Path(target_str) if not is_remote else target_str
