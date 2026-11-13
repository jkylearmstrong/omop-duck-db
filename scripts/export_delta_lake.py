#!/usr/bin/env python3
"""CLI utility to export Parquet datasets or DuckDB tables into Delta Lake format.

Examples:
    # Export a directory of Parquet feature files to a Delta Lake table
    python scripts/export_delta_lake.py --input run_021_delivered_preset --output derived/delta_lake/features

    # Export a specific DuckDB table
    python scripts/export_delta_lake.py --input derived/omop_duckdb/omop_SiteA.duckdb --table person --output derived/delta_lake/person
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Ensure repository python package is imported
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl.lakehouse import export_to_delta_lake


def parse_args(args=None):
    p = _parser()
    a = p.parse_args(args)
    if a.input.suffix.lower() in (".duckdb", ".db") and not a.table:
        p.error("--table is required when --input is a DuckDB database file.")
    return a


def _parser():
    p = argparse.ArgumentParser(
        description="Export Parquet datasets or DuckDB tables to versioned Delta Lake tables.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--input", "-i", required=True, type=Path,
        help="Path to source Parquet file, directory of Parquets, or DuckDB database file.",
    )
    p.add_argument(
        "--output", "-o", required=True, type=str,
        help="Target Delta Lake directory or cloud object store URI (e.g. s3://bucket/path).",
    )
    p.add_argument(
        "--table", "-t", default=None,
        help="Table name if --input is a DuckDB database file.",
    )
    p.add_argument(
        "--mode", "-m", choices=["overwrite", "append", "error", "ignore"], default="overwrite",
        help="Delta write mode.",
    )
    p.add_argument(
        "--partition-by", "-p", nargs="*", default=None,
        help="Optional column name(s) to partition the Delta table by.",
    )
    return p


def main(argv=None):
    a = parse_args(argv)
    input_path = a.input.resolve()

    if input_path.suffix.lower() in (".duckdb", ".db"):
        import duckdb
        con = duckdb.connect(str(input_path), read_only=True)
        try:
            print(f"Exporting table '{a.table}' from {input_path} to Delta Lake at {a.output}...")
            out = export_to_delta_lake(
                con, a.output, table_name=a.table, mode=a.mode, partition_by=a.partition_by,
            )
        finally:
            con.close()
    else:
        print(f"Exporting Parquet data from {input_path} to Delta Lake at {a.output}...")
        out = export_to_delta_lake(
            input_path, a.output, mode=a.mode, partition_by=a.partition_by,
        )

    print(f"Delta Lake export complete: {out}")


if __name__ == "__main__":
    main()
