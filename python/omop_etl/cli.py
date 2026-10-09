"""Standalone Command-Line Interface (CLI) for OMOP DuckDB ETL and Analytics.

Commands:
    omop-duckdb build --source <path> --target <db> [--vocab <path>] [--site-id <id>]
    omop-duckdb dqd --db <db> [--output <json>]
    omop-duckdb cohort define --db <db> --type <type> --out <tbl>
    omop-duckdb table1 export --db <db> --cohort <tbl> [--format <fmt>] [--out <path>]
    omop-duckdb export parquet --db <db> --table <tbl> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import duckdb

from omop_etl import (
    __version__,
    etl_pcornet,
    run_dqd,
    generate_table1,
    export_table1,
    define_study_cohort,
    build_readmission_cohort,
    build_mortality_cohort,
    execute_circe_cohort,
)


def create_parser() -> argparse.ArgumentParser:
    """Builds the main command line parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="omop-duckdb",
        description="High-performance OMOP CDM v5.4 ETL, DQD, Cohorts, and ML Analytics on DuckDB.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # 1. Build
    build_parser = subparsers.add_parser("build", help="Run PCORnet to OMOP CDM ETL pipeline.")
    build_parser.add_argument("--source", "-s", required=True, help="Directory containing source CSV extracts.")
    build_parser.add_argument("--target", "-t", required=True, help="Target DuckDB database file path.")
    build_parser.add_argument("--vocab", "-v", default=None, help="Path to central Athena vocabulary DuckDB database.")
    build_parser.add_argument("--site-id", default=None, help="Unique site identifier for namespacing.")
    build_parser.add_argument("--site-name", default=None, help="Descriptive site facility name.")

    # 2. DQD
    dqd_parser = subparsers.add_parser("dqd", help="Run Duck-DQD data quality and integrity checks.")
    dqd_parser.add_argument("--db", "-d", required=True, help="Path to DuckDB database file.")
    dqd_parser.add_argument("--output", "-o", default=None, help="Path to output report file (JSON or MD).")

    # 3. Cohort
    cohort_parser = subparsers.add_parser("cohort", help="Define and materialize study cohorts.")
    cohort_sub = cohort_parser.add_subparsers(dest="cohort_action", help="Cohort actions")
    define_sub = cohort_sub.add_parser("define", help="Define a cohort")
    define_sub.add_argument("--db", "-d", required=True, help="Path to DuckDB database file.")
    define_sub.add_argument(
        "--type",
        "-t",
        required=True,
        choices=["inpatient", "readmission", "mortality", "circe"],
        help="Type of study cohort to construct.",
    )
    define_sub.add_argument("--out", "-o", default="cohort", help="Destination cohort table name.")
    define_sub.add_argument("--circe-json", default=None, help="Path to CIRCE JSON file (if type is circe).")

    # 4. Table 1
    t1_parser = subparsers.add_parser("table1", help="Generate and export Table 1 demographic reconciliation.")
    t1_sub = t1_parser.add_subparsers(dest="table1_action", help="Table 1 actions")
    t1_exp = t1_sub.add_parser("export", help="Export Table 1")
    t1_exp.add_argument("--db", "-d", required=True, help="Path to DuckDB database file.")
    t1_exp.add_argument("--cohort", "-c", default="cohort", help="Source cohort table name.")
    t1_exp.add_argument(
        "--format",
        "-f",
        default="markdown",
        choices=["markdown", "csv", "html", "latex"],
        help="Output export format.",
    )
    t1_exp.add_argument("--out", "-o", default=None, help="Optional output file path.")

    # 5. Export
    exp_parser = subparsers.add_parser("export", help="Export CDM tables to external formats.")
    exp_sub = exp_parser.add_subparsers(dest="export_action", help="Export actions")
    pq_exp = exp_sub.add_parser("parquet", help="Export table to partitioned Parquet")
    pq_exp.add_argument("--db", "-d", required=True, help="Path to DuckDB database file.")
    pq_exp.add_argument("--table", required=True, help="CDM table name to export.")
    pq_exp.add_argument("--out", "-o", required=True, help="Destination directory.")
    pq_exp.add_argument("--partition-by", default=None, help="Column to partition by.")

    return parser


def handle_build(args: argparse.Namespace) -> int:
    """Executes the build command."""
    print(f"Building OMOP CDM from '{args.source}' -> '{args.target}'...")
    con = duckdb.connect(args.target)
    try:
        etl_pcornet(
            con=con,
            source_dir=args.source,
            vocab_db=args.vocab,
            site_id=args.site_id,
            site_name=args.site_name,
        )
        print("Build completed successfully.")
        return 0
    finally:
        con.close()


def handle_dqd(args: argparse.Namespace) -> int:
    """Executes the DQD command."""
    con = duckdb.connect(args.db, read_only=True)
    try:
        results = run_dqd(con)
        if args.output:
            out_p = Path(args.output)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            if out_p.suffix.lower() == ".json":
                out_p.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            else:
                md_lines = ["# Duck-DQD Data Quality Report\n"]
                for k, v in results.items():
                    md_lines.append(f"## {k}\n```\n{v}\n```\n")
                out_p.write_text("\n".join(md_lines), encoding="utf-8")
            print(f"DQD report written to {args.output}")
        else:
            print(json.dumps(results, indent=2, default=str))
        return 0
    finally:
        con.close()


def handle_cohort(args: argparse.Namespace) -> int:
    """Executes cohort definition subcommands."""
    con = duckdb.connect(args.db)
    try:
        if args.type == "inpatient":
            define_study_cohort(con, cohort_table=args.out, visit_concept_ids=[9201])
        elif args.type == "readmission":
            build_readmission_cohort(con, target_table=args.out)
        elif args.type == "mortality":
            build_mortality_cohort(con, target_table=args.out)
        elif args.type == "circe":
            if not args.circe_json:
                print("Error: --circe-json is required when --type is circe", file=sys.stderr)
                return 1
            circe_def = json.loads(Path(args.circe_json).read_text(encoding="utf-8"))
            execute_circe_cohort(con, circe_def, target_cohort_table=args.out)
        count = con.execute(f"SELECT count(*) FROM {args.out}").fetchone()[0]
        print(f"Cohort '{args.out}' generated with {count} records.")
        return 0
    finally:
        con.close()


def handle_table1(args: argparse.Namespace) -> int:
    """Executes Table 1 export subcommands."""
    con = duckdb.connect(args.db, read_only=True)
    try:
        t1_df = generate_table1(con, cohort_table=args.cohort)
        exported = export_table1(t1_df, format=args.format)
        if args.out:
            Path(args.out).write_text(exported, encoding="utf-8")
            print(f"Table 1 exported to {args.out}")
        else:
            print(exported)
        return 0
    finally:
        con.close()


def handle_export(args: argparse.Namespace) -> int:
    """Executes Parquet export subcommands."""
    con = duckdb.connect(args.db, read_only=True)
    try:
        dest_dir = Path(args.out)
        dest_dir.mkdir(parents=True, exist_ok=True)
        part_clause = f", PARTITION_BY ({args.partition_by})" if args.partition_by else ""
        sql = f"COPY {args.table} TO '{dest_dir.as_posix()}' (FORMAT PARQUET{part_clause});"
        con.execute(sql)
        print(f"Table '{args.table}' exported to {args.out}")
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = create_parser()
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)

    if args.command == "build":
        return handle_build(args)
    if args.command == "dqd":
        return handle_dqd(args)
    if args.command == "cohort":
        return handle_cohort(args)
    if args.command == "table1":
        return handle_table1(args)
    if args.command == "export":
        return handle_export(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
