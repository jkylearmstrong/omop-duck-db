"""Direct Andromeda Zero-Copy View Adapter for OHDSI HADES.

Provides zero-copy and high-performance streaming between DuckDB and OHDSI Andromeda
SQLite databases, eliminating the intermediate disk serialization bottleneck.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Sequence
import duckdb
import pandas as pd


def _posix_path(p: str | Path) -> str:
    return str(p).replace("\\", "/")


def export_to_andromeda(
    con: duckdb.DuckDBPyConnection,
    sqlite_path: str | Path,
    table_names: Sequence[str] | None = None,
    chunk_size: int = 100_000,
) -> Path:
    """Exports DuckDB tables directly to an OHDSI Andromeda SQLite database.

    Attempts native DuckDB SQLite zero-copy attachment; falls back to chunked
    streaming into SQLite without monolithic memory bloat.

    Args:
        con: Active DuckDB connection holding the tables/views.
        sqlite_path: Path to target SQLite file (created if does not exist).
        table_names: Names of tables to export (e.g. ['covariates', 'covariateRef', 'analysisRef']).
            If None, automatically exports 'covariates', 'covariateRef', 'analysisRef' if present.
        chunk_size: Row chunk size for streaming fallback.

    Returns:
        Path: Absolute path to the populated Andromeda SQLite file.
    """
    target = Path(sqlite_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    posix_target = _posix_path(target)

    if table_names is None:
        # Default OHDSI Andromeda tables
        existing = [t[0].lower() for t in con.execute("SHOW TABLES;").fetchall()]
        table_names = [t for t in ["covariates", "covariateref", "analysisref"] if t in existing]
        if not table_names:
            # Fall back to all tables
            table_names = existing

    # Try native DuckDB SQLite ATTACH zero-copy write
    native_ok = False
    try:
        # Test if sqlite extension is present/loadable
        con.execute(f"ATTACH '{posix_target}' AS andr_sq (TYPE SQLITE);")
        for tbl in table_names:
            con.execute(f"CREATE OR REPLACE TABLE andr_sq.{tbl} AS SELECT * FROM {tbl};")
        con.execute("DETACH andr_sq;")
        native_ok = True
    except Exception:
        native_ok = False

    if not native_ok:
        # High-performance chunked fallback via sqlite3
        conn = sqlite3.connect(str(target))
        try:
            for tbl in table_names:
                # Get column names and types from DuckDB
                desc = con.execute(f"DESCRIBE SELECT * FROM {tbl} LIMIT 0;").fetchall()
                col_names = [d[0] for d in desc]
                
                # Fetch row count
                total_rows = con.execute(f"SELECT COUNT(*) FROM {tbl};").fetchone()[0]
                
                # Create table in SQLite
                col_defs = ", ".join(f'"{col}"' for col in col_names)
                conn.execute(f'DROP TABLE IF EXISTS "{tbl}";')
                conn.execute(f'CREATE TABLE "{tbl}" ({col_defs});')

                # Chunked cursor streaming
                placeholders = ", ".join(["?"] * len(col_names))
                insert_sql = f'INSERT INTO "{tbl}" VALUES ({placeholders})'

                offset = 0
                while offset < total_rows:
                    chunk = con.execute(
                        f"SELECT * FROM {tbl} LIMIT {chunk_size} OFFSET {offset};"
                    ).fetchall()
                    if not chunk:
                        break
                    conn.executemany(insert_sql, chunk)
                    offset += len(chunk)

                conn.commit()

            # Index optimization for covariates
            if "covariates" in [t.lower() for t in table_names]:
                try:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_covariates_row_id ON covariates(row_id);")
                    conn.commit()
                except Exception:
                    pass
        finally:
            conn.close()

    return target


class AndromedaAdapter:
    """Adapter for interacting with OHDSI Andromeda SQLite storage from DuckDB."""

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path).resolve()
        if not self.sqlite_path.exists():
            raise FileNotFoundError(f"Andromeda SQLite database not found: {self.sqlite_path}")

    @property
    def tables(self) -> list[str]:
        """List tables contained in the Andromeda SQLite database."""
        conn = sqlite3.connect(str(self.sqlite_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_table(self, table_name: str) -> pd.DataFrame:
        """Read a table from the Andromeda file into a DataFrame."""
        conn = sqlite3.connect(str(self.sqlite_path))
        try:
            return pd.read_sql_query(f'SELECT * FROM "{table_name}";', conn)
        finally:
            conn.close()

    def mount_in_duckdb(
        self,
        con: duckdb.DuckDBPyConnection,
        schema: str = "andromeda",
        read_only: bool = True,
    ) -> bool:
        """Mount the Andromeda database into an active DuckDB connection.

        Returns True if mounted via native SQLite attach, False if loaded as views.
        """
        posix = _posix_path(self.sqlite_path)
        ro_flag = " (READ_ONLY)" if read_only else ""
        try:
            con.execute(f"ATTACH '{posix}' AS {schema}{ro_flag} (TYPE SQLITE);")
            return True
        except Exception:
            # Fallback: create in-memory views over SQLite via Arrow/pandas
            conn = sqlite3.connect(str(self.sqlite_path))
            try:
                for tbl in self.tables:
                    df = pd.read_sql_query(f'SELECT * FROM "{tbl}";', conn)
                    con.register(f"_{tbl}_andr_raw", df)
                    con.execute(f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM _{tbl}_andr_raw;")
                return False
            finally:
                conn.close()

    def summary(self) -> dict[str, int]:
        """Summary of table row counts."""
        res = {}
        conn = sqlite3.connect(str(self.sqlite_path))
        try:
            for tbl in self.tables:
                cnt = conn.execute(f'SELECT COUNT(*) FROM "{tbl}";').fetchone()[0]
                res[tbl] = int(cnt)
            return res
        finally:
            conn.close()
