"""Standalone Command-Line Interface (CLI) for OMOP DuckDB ETL and Analytics.

Commands:
    omop-duckdb build --source <path> --target <db> [--vocab <path>] [--site-id <id>]
    omop-duckdb dqd --db <db> [--output <json>]
    omop-duckdb cohort define --db <db> --type <type> --out <tbl>
    omop-duckdb table1 export --db <db> --cohort <tbl> [--format <fmt>] [--out <path>]
    omop-duckdb export parquet --db <db> --table <tbl> --out <dir>
    omop-duckdb extract --url <sqlalchemy-url|env:VAR> --tables a,b --out <dir>
    omop-duckdb import-omop --url <url> --schema <cdm_schema> --out site.duckdb
    omop-duckdb check-source <dir> [--format csv|parquet]
    omop-duckdb evaluate --bundle bundle/ --db site.duckdb --site-id S01 --out telemetry_packet.json
"""

from __future__ import annotations

from omop_etl.jsonio import strict_dumps
from omop_etl.paths import sql_path_literal
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


def _add_manifest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--manifest", default=None, help="Write a run_manifest.json (versions, seeds, input hashes, counts) here.")
    p.add_argument("--verify-manifest", default=None, metavar="MANIFEST",
                   help="Re-check input hashes recorded in this manifest before running; fail if any changed.")


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
    build_parser.add_argument("--strict-source", action="store_true", help="Fail fast on missing required source tables/columns.")
    build_parser.add_argument("--key-check", choices=["warn", "raise", "ignore"], default="warn",
                              help="Source-key collision check; results are logged in the run manifest.")
    build_parser.add_argument("--use-enrollment", choices=["auto", "true", "false"], default="auto",
                              help="Build observation_period from the PCORnet ENROLLMENT file.")
    build_parser.add_argument("--enrollment-type-concept-id", type=int, default=0,
                              help="period_type_concept_id for enrolment-derived periods.")
    build_parser.add_argument("--data-cutoff", default=None, metavar="YYYY-MM-DD|auto",
                              help="Cap observation_period ends: an ISO date, or 'auto' for the latest event before today "
                                   "(default: today).")
    _add_manifest_args(build_parser)

    # 2. DQD
    dqd_parser = subparsers.add_parser("dqd", help="Run Duck-DQD data quality and integrity checks.")
    dqd_parser.add_argument("--db", "-d", required=True, help="Path to DuckDB database file.")
    dqd_parser.add_argument("--output", "-o", default=None, help="Path to output report file (JSON or MD).")
    dqd_parser.add_argument("--full", action="store_true", help="Also run the schema-driven check catalog over every CDM table.")

    ach = subparsers.add_parser("achilles", help="Run in-database Achilles analyses (a subset of the ~170 in the R package; see achilles_analysis_catalog()); writes achilles_results* tables into the DB.")
    ach.add_argument("--db", "-d", required=True, help="DuckDB CDM file (opened read-write: results tables are created in it).")
    ach.add_argument("--analysis", "-a", type=int, nargs="*", default=None, help="Analysis ids (default: all implemented).")
    ach.add_argument("--min-cell-count", type=int, default=0, help="Drop result rows with count_value below this.")

    # 3. Cohort
    cohort_parser = subparsers.add_parser("cohort", help="Define and materialize study cohorts.")
    cohort_sub = cohort_parser.add_subparsers(dest="cohort_action", help="Cohort actions")
    define_sub = cohort_sub.add_parser("define", help="Define a cohort")
    define_sub.add_argument("--db", "-d", required=True, help="Path to DuckDB database file.")
    define_sub.add_argument(
        "--type",
        "-t",
        required=True,
        choices=["inpatient", "ederri_diabetes", "diabetes", "readmission", "mortality", "circe"],
        help="Type of study cohort to construct.",
    )
    define_sub.add_argument("--out", "-o", default="cohort", help="Destination cohort table name.")
    define_sub.add_argument("--circe-json", default=None, help="Path to CIRCE JSON file (if type is circe).")
    define_sub.add_argument("--seed", type=int, default=42, help="Random seed for index-stay sampling (inpatient type).")
    define_sub.add_argument("--persist-keys", default=None, help="CSV of selected index stays; reused if it exists (inpatient type).")
    _add_manifest_args(define_sub)

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

    # 6. Source-database neutrality
    ex = subparsers.add_parser("extract", help="Stream source tables from a SQL server to Parquet.")
    ex.add_argument("--url", required=True, help="SQLAlchemy URL, or env:VARNAME to read it from the environment.")
    ex.add_argument("--tables", required=True, help="Comma-separated tables (name or name=SOURCE_TABLE).")
    ex.add_argument("--out", "-o", required=True, help="Output directory for <table>.parquet files.")
    ex.add_argument("--schema", default=None, help="Source schema.")
    ex.add_argument("--table-map", default=None, help="JSON or 'expected,source' CSV mapping expected -> actual table names.")
    ex.add_argument("--chunksize", type=int, default=500_000, help="Rows per chunk (default 500000).")

    imp = subparsers.add_parser("import-omop", help="Copy an existing OMOP CDM from a SQL server into a DuckDB file.")
    imp.add_argument("--url", required=True, help="SQLAlchemy URL, or env:VARNAME.")
    imp.add_argument("--schema", default=None, help="CDM schema on the server.")
    imp.add_argument("--out", "-o", required=True, help="Destination DuckDB file.")
    imp.add_argument("--tables", default=None, help="Comma-separated subset of CDM tables.")
    imp.add_argument("--include-vocab", action="store_true", help="Also copy vocabulary tables.")
    imp.add_argument("--table-map", default=None, help="JSON or CSV mapping CDM table -> server table name.")
    imp.add_argument("--chunksize", type=int, default=500_000)

    cs = subparsers.add_parser("check-source", help="Check a source directory against the ETL file/column contract.")
    cs.add_argument("source_dir", help="Directory of <table>.csv / <table>.parquet files.")
    cs.add_argument("--format", choices=["csv", "parquet"], default=None, help="Restrict to one format.")
    cs.add_argument("--json", action="store_true", help="Print the report as JSON.")

    sc = subparsers.add_parser("score", help="Score a feature table with a hash-verified model bundle.")
    sc.add_argument("--bundle", required=True, help="Model bundle directory.")
    sc.add_argument("--features", required=True, help="Feature table (.parquet or .csv).")
    sc.add_argument("--out", "-o", required=True, help="Output scores file (.parquet or .csv).")
    sc.add_argument("--on-violation", choices=["raise", "clip", "nullify"], default="raise")
    _add_manifest_args(sc)

    ev = subparsers.add_parser("evaluate", help="One-shot consortium evaluation: check-source, features, validate, score, telemetry.")
    ev.add_argument("--bundle", required=True, help="Model bundle directory.")
    ev.add_argument("--db", required=True, help="Site OMOP CDM DuckDB file (opened read-only).")
    ev.add_argument("--out", "-o", required=True, help="Telemetry packet JSON to write.")
    ev.add_argument("--site-id", required=True, help="Opaque consortium site code, [A-Za-z0-9_-]{1,32}.")
    ev.add_argument("--source-dir", default=None, help="Raw source directory to preflight with check-source first.")
    ev.add_argument("--source-format", choices=["csv", "parquet"], default=None)
    ev.add_argument("--cohort-table", default=None, help="Existing cohort table with an outcome_flag column (default: define the 30-day readmission cohort).")
    ev.add_argument("--extra-features", default=None, help="Parquet/CSV of extra feature columns keyed by subject_id + cohort_start_date (labs, elix_*, med_*).")
    ev.add_argument("--on-violation", choices=["raise", "clip", "nullify"], default="raise")
    ev.add_argument("--min-cell", type=int, default=11)
    ev.add_argument("--seed", type=int, default=42)
    ev.add_argument("--persist-keys", default=None, help="CSV of index stays to reuse/write for identical cohorts across runs.")
    ev.add_argument("--manifest", default=None, help="run_manifest.json path (default: next to --out).")

    se = subparsers.add_parser("score-encounter", help="Score one encounter from its latest pre-discharge labs/vitals (JSON to stdout).")
    se.add_argument("--bundle", required=True, help="Model bundle directory.")
    se.add_argument("--db", required=True, help="OMOP CDM DuckDB file (opened read-only).")
    se.add_argument("--visit-id", type=int, required=True, help="visit_occurrence_id to score.")
    se.add_argument("--as-of", default=None, help="Count measurements up to this date (default: the earlier of visit end and today).")
    se.add_argument("--extra-features", default=None, help="Parquet/CSV of extra feature columns keyed by subject_id + cohort_start_date.")
    se.add_argument("--risk-tiers", default=None, help="JSON file of [{name, min}] cut-points replacing the bundle's tiers (see risk-tiers fit).")
    se.add_argument("--on-violation", choices=["raise", "clip", "nullify"], default="raise")

    mo = subparsers.add_parser("monitor", help="Monthly drift (PSI) and calibration report for live scores; upserts an aggregate-only CSV log.")
    mo.add_argument("--scores", required=True, help="Live scores (.parquet or .csv) with a date, a probability and optionally an outcome column.")
    mo.add_argument("--baseline", required=True, help="Frozen baseline scores (.parquet or .csv) with a probability column.")
    mo.add_argument("--log", required=True, help="CSV log to upsert (keyed on model version + month).")
    mo.add_argument("--model-version", default="", help="Model version recorded in the log.")
    mo.add_argument("--date-col", default="score_date")
    mo.add_argument("--prob-col", default="prob")
    mo.add_argument("--outcome-col", default="outcome_flag")
    mo.add_argument("--as-of", default=None, help="Report date (default today); a month is assessed once month end + follow-up <= this.")
    mo.add_argument("--followup-days", type=int, default=30)
    mo.add_argument("--min-events", type=int, default=30)
    mo.add_argument("--fail-on-flag", action="store_true", help="Exit 2 if any month is 'drift' or 'miscalibrated'.")

    rt = subparsers.add_parser("risk-tiers", help="Capacity-calibrated risk tiers: fit cut-points, apply them to scores.")
    rt_sub = rt.add_subparsers(dest="risk_tiers_command", required=True)
    rtf = rt_sub.add_parser("fit", help="Cut-points so each tier holds its share of a reference score set.")
    rtf.add_argument("--reference", required=True, help="Reference scores (.parquet or .csv).")
    rtf.add_argument("--prob-col", default="prob")
    rtf.add_argument("--tier", action="append", default=[], metavar="NAME=FRACTION",
                     help="Tier and its CUMULATIVE top fraction, highest risk first, e.g. --tier high=0.10 --tier elevated=0.30.")
    rtf.add_argument("--lowest", default="standard", help="Name of the remainder tier (default: standard).")
    rtf.add_argument("--out", "-o", required=True, help="Thresholds JSON to write.")
    rta = rt_sub.add_parser("apply", help="Add a risk_tier column to a score table.")
    rta.add_argument("--scores", required=True, help="Scores (.parquet or .csv).")
    rta.add_argument("--thresholds", required=True, help="Thresholds JSON from 'risk-tiers fit'.")
    rta.add_argument("--prob-col", default="prob")
    rta.add_argument("--out", "-o", required=True, help="Output file (.parquet or .csv).")
    rta.add_argument("--capacity", action="append", default=[], metavar="TIER=PER_PERIOD", help="Cases per period a tier's team can handle.")
    rta.add_argument("--periods", type=int, default=1, help="Days/weeks covered by the scores (for the capacity check).")

    ck = subparsers.add_parser("checkpoint", help="CHECKPOINT DuckDB files so they can be attached READ_ONLY (no .wal left).")
    ck.add_argument("db", nargs="+", help="DuckDB file(s) to checkpoint; no other process may have them open.")

    vm = subparsers.add_parser("verify-manifest", help="Verify input hashes recorded in a run_manifest.json.")
    vm.add_argument("manifest", help="Path to run_manifest.json.")

    mv = subparsers.add_parser("materialize-vocab", help="Copy central_vocab views into real tables so HADES can open the file alone.")
    mv.add_argument("db", help="DuckDB CDM file.")
    mv.add_argument("--vocab", default=None, help="Vocabulary DuckDB file to ATTACH read-only as central_vocab first.")

    return parser


def handle_materialize_vocab(args: argparse.Namespace) -> int:
    import duckdb
    from omop_etl.build_omop_cdm import attach_central_vocabulary, materialize_vocabulary

    con = duckdb.connect(args.db)
    try:
        if args.vocab:
            attach_central_vocabulary(con, args.vocab, temporary=True)
        done = materialize_vocabulary(con)
    finally:
        con.close()
    for tbl, n in done.items():
        print(f"{tbl}: {n} rows")
    if not done:
        print("No vocabulary views to materialize.")
    return 0


def _verify_first(args: argparse.Namespace) -> None:
    if getattr(args, "verify_manifest", None):
        from omop_etl.run_manifest import verify_manifest

        verify_manifest(args.verify_manifest)
        print(f"Manifest {args.verify_manifest} verified.")


def handle_score(args: argparse.Namespace) -> int:
    from omop_etl.run_manifest import RunManifest
    from omop_etl.scoring import load_model_bundle, score_cohort

    _verify_first(args)
    with RunManifest("score", inputs=[args.bundle + "/manifest.json", args.features],
                     feature_spec=Path(args.bundle) / "feature_spec.json") as m:
        scores = score_cohort(load_model_bundle(args.bundle), args.features, on_violation=args.on_violation)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".parquet":
            scores.to_parquet(out, index=False)
        else:
            scores.to_csv(out, index=False)
        m.add_step("scored", len(scores))
    if args.manifest:
        m.write(args.manifest)
    print(f"Scored {len(scores)} rows -> {args.out}")
    return 0


def _read_table(path: str):
    import pandas as pd

    return pd.read_parquet(path) if str(path).lower().endswith(".parquet") else pd.read_csv(path)


def _write_table(df, path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False) if out.suffix.lower() == ".parquet" else df.to_csv(out, index=False)


def handle_score_encounter(args: argparse.Namespace) -> int:
    from omop_etl.encounter import score_encounter

    tiers = json.loads(Path(args.risk_tiers).read_text(encoding="utf-8")) if args.risk_tiers else None
    extra = _read_table(args.extra_features) if args.extra_features else None
    con = duckdb.connect(args.db, read_only=True)
    try:
        res = score_encounter(con, args.visit_id, args.bundle, as_of=args.as_of, extra_features=extra,
                              on_violation=args.on_violation, risk_tiers=tiers)
    except (ValueError, KeyError) as exc:
        print(f"score-encounter: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()
    print(strict_dumps(res, indent=2, default=str))
    return 0


def handle_monitor(args: argparse.Namespace) -> int:
    from omop_etl.monitoring import monitor_scores, update_monitoring_log

    baseline = _read_table(args.baseline)
    if args.prob_col not in baseline.columns:
        print(f"monitor: baseline has no {args.prob_col!r} column.", file=sys.stderr)
        return 1
    try:
        report = monitor_scores(_read_table(args.scores), baseline[args.prob_col].to_numpy(), date_col=args.date_col,
                                prob_col=args.prob_col, outcome_col=args.outcome_col, as_of=args.as_of,
                                followup_days=args.followup_days, min_events=args.min_events)
    except (KeyError, ValueError) as exc:
        print(f"monitor: {exc}", file=sys.stderr)
        return 1
    update_monitoring_log(report, args.log, model_version=args.model_version)
    print(report.to_string(index=False, float_format=lambda v: f"{v:.4g}"))
    print(f"Log -> {args.log}")
    if args.fail_on_flag and report["status"].isin(["drift", "miscalibrated"]).any():
        return 2
    return 0


def _pairs(items: list[str], what: str) -> list[tuple[str, float]]:
    out = []
    for it in items:
        name, sep, val = it.partition("=")
        try:
            out.append((name, float(val)))
        except ValueError:
            raise ValueError(f"{what} expects NAME=NUMBER, got {it!r}") from None
    return out


def handle_risk_tiers(args: argparse.Namespace) -> int:
    from omop_etl.risk_tiers import fit_capacity_thresholds, map_risk_tiers, tier_load

    try:
        if args.risk_tiers_command == "fit":
            tiers = [{"name": n, "top_fraction": f} for n, f in _pairs(args.tier, "--tier")] + [{"name": args.lowest}]
            ref = _read_table(args.reference)[args.prob_col].to_numpy()
            ts = fit_capacity_thresholds(ref, tiers)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(strict_dumps(ts, indent=2), encoding="utf-8")
            for t in ts[::-1]:
                print(f"{t['name']}: p >= {t['min']:.4g}  ({t['fraction']:.1%} of reference)")
            return 0
        ts = json.loads(Path(args.thresholds).read_text(encoding="utf-8"))
        df = _read_table(args.scores)
        df["risk_tier"] = map_risk_tiers(df[args.prob_col].to_numpy(), ts)
        _write_table(df, args.out)
        cap = dict(_pairs(args.capacity, "--capacity")) or None
        print(tier_load(df[args.prob_col].to_numpy(), ts, capacity=cap, periods=args.periods).to_string(index=False))
        return 0
    except (KeyError, ValueError) as exc:
        print(f"risk-tiers: {exc}", file=sys.stderr)
        return 1


def handle_evaluate(args: argparse.Namespace) -> int:
    from omop_etl.evaluate import EvaluationError, evaluate_site

    try:
        evaluate_site(args.bundle, args.db, args.out, site_id=args.site_id, source_dir=args.source_dir,
                      source_format=args.source_format, cohort_table=args.cohort_table, extra_features=args.extra_features,
                      on_violation=args.on_violation, min_cell=args.min_cell, seed=args.seed, persist_keys=args.persist_keys,
                      manifest=args.manifest)
    except EvaluationError as exc:
        print(f"evaluate failed: {exc}", file=sys.stderr)
        return 1
    return 0


def handle_verify_manifest(args: argparse.Namespace) -> int:
    from omop_etl.run_manifest import verify_manifest

    verify_manifest(args.manifest)
    print(f"Manifest {args.manifest} verified.")
    return 0


def handle_extract(args: argparse.Namespace) -> int:
    from omop_etl.extract import from_sql

    report = from_sql(args.url, [t for t in args.tables.split(",") if t.strip()], args.out, chunksize=args.chunksize,
                      schema=args.schema, table_map=args.table_map)
    for r in report:
        print(f"{r['table']}: {r['rows']} rows, {len(r['columns'])} columns -> {r['file']}")
    return 0


def handle_import_omop(args: argparse.Namespace) -> int:
    from omop_etl.extract import import_omop

    tables = [t for t in args.tables.split(",") if t.strip()] if args.tables else None
    for r in import_omop(args.url, args.out, schema=args.schema, tables=tables, include_vocab=args.include_vocab,
                         chunksize=args.chunksize, table_map=args.table_map):
        print(f"{r['table']}: " + (f"{r['rows']} rows" if r.get("rows") is not None else f"skipped ({r.get('skipped')})"))
    return 0


def handle_check_source(args: argparse.Namespace) -> int:
    from omop_etl.source_check import check_source, format_report

    report = check_source(args.source_dir, args.format)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0 if report["ok"] else 1


def handle_build(args: argparse.Namespace) -> int:
    """Executes the build command."""
    from omop_etl.run_manifest import RunManifest

    _verify_first(args)
    print(f"Building OMOP CDM from '{args.source}' -> '{args.target}'...")
    src_files = sorted(p for p in Path(args.source).glob("*") if p.is_file() and p.suffix.lower() in (".csv", ".parquet"))
    with RunManifest("build", inputs=src_files, extra={"site_id": args.site_id}) as m:
        etl_pcornet(
            args.source,
            args.target,
            build_schema_flag=True,
            central_vocab=args.vocab,
            site_id=args.site_id,
            site_name=args.site_name,
            strict_source=args.strict_source,
            key_check=args.key_check,
            use_enrollment={"auto": "auto", "true": True, "false": False}[args.use_enrollment],
            enrollment_type_concept_id=args.enrollment_type_concept_id,
            data_cutoff=args.data_cutoff,
            key_check_callback=lambda report: [
                m.add_step(f"key_check:{r['table']}.{r['id_column']}", r["distinct_ids"], distinct_keys=r["distinct_keys"],
                           lost=r["lost"], ok=r["ok"], cdm_table=r["cdm_table"], cdm_rows=r["cdm_rows"])
                for r in report],
        )
        con = duckdb.connect(args.target, read_only=True)
        try:
            for t in ("person", "visit_occurrence", "condition_occurrence", "measurement", "drug_exposure"):
                m.add_step(f"rows:{t}", con.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
            m.add_step("observation_period:total", con.execute("SELECT count(*) FROM observation_period").fetchone()[0])
            if args.use_enrollment != "false":
                m.add_step("observation_period:enrollment_spans", con.execute(
                    "SELECT count(*) FROM observation_period WHERE period_type_concept_id = ?",
                    [args.enrollment_type_concept_id]).fetchone()[0], use_enrollment=args.use_enrollment,
                    enrollment_type_concept_id=args.enrollment_type_concept_id)
        finally:
            con.close()
    if args.manifest:
        m.write(args.manifest)
    print("Build completed successfully.")
    return 0


def handle_dqd(args: argparse.Namespace) -> int:
    """Executes the DQD command."""
    con = duckdb.connect(args.db, read_only=True)
    try:
        if getattr(args, "full", False):
            from omop_etl.dqd_catalog import run_dqd_full

            results = run_dqd_full(con)
        else:
            results = run_dqd(con)
        if args.output:
            out_p = Path(args.output)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            if out_p.suffix.lower() == ".json":
                out_p.write_text(strict_dumps(results, indent=2, default=str), encoding="utf-8")
            else:
                md_lines = ["# Duck-DQD Data Quality Report\n"]
                for k, v in results.items():
                    md_lines.append(f"## {k}\n```\n{v}\n```\n")
                out_p.write_text("\n".join(md_lines), encoding="utf-8")
            print(f"DQD report written to {args.output}")
        else:
            print(strict_dumps(results, indent=2, default=str))
        return 0
    finally:
        con.close()


def handle_cohort(args: argparse.Namespace) -> int:
    """Executes cohort definition subcommands."""
    from omop_etl.run_manifest import RunManifest

    _verify_first(args)
    # the database is written by this command, so it is not hashed; its source row counts are recorded instead
    manifest = RunManifest("cohort define", seeds={"random_state": args.seed},
                           extra={"type": args.type, "out": args.out, "db": str(args.db)})
    con = duckdb.connect(args.db)
    try:
        if args.type == "inpatient":
            df = define_study_cohort(con, visit_type="inpatient", random_state=args.seed, persist_keys=args.persist_keys,
                                     table_name=args.out)
            manifest.add_attrition(df.attrs.get("attrition", []))
            if args.persist_keys:
                manifest.add_input(args.persist_keys)
        elif args.type in ("ederri_diabetes", "diabetes"):
            df = define_study_cohort(con, visit_type="inpatient", require_diabetes=True, random_state=args.seed,
                                     persist_keys=args.persist_keys, table_name=args.out)
            manifest.add_attrition(df.attrs.get("attrition", []))
            if args.persist_keys:
                manifest.add_input(args.persist_keys)
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
        for t in ("person", "visit_occurrence"):
            manifest.add_step(f"source_rows:{t}", con.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
        manifest.add_step("cohort_rows", count)
        print(f"Cohort '{args.out}' generated with {count} records.")
        con.close()
        if args.manifest:
            manifest.finish().write(args.manifest)
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
        sql = f"COPY {args.table} TO {sql_path_literal(dest_dir)} (FORMAT PARQUET{part_clause});"
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
    if args.command == "achilles":
        from omop_etl.achilles import run_achilles

        con = duckdb.connect(args.db)
        try:
            out = run_achilles(con, args.analysis, min_cell_count=args.min_cell_count)
        finally:
            con.close()
        print(strict_dumps(out, indent=2, default=str))
        return 0
    if args.command == "cohort":
        return handle_cohort(args)
    if args.command == "table1":
        return handle_table1(args)
    if args.command == "export":
        return handle_export(args)
    if args.command == "score":
        return handle_score(args)
    if args.command == "evaluate":
        return handle_evaluate(args)
    if args.command == "score-encounter":
        return handle_score_encounter(args)
    if args.command == "monitor":
        return handle_monitor(args)
    if args.command == "risk-tiers":
        return handle_risk_tiers(args)
    if args.command == "materialize-vocab":
        return handle_materialize_vocab(args)
    if args.command == "checkpoint":
        from omop_etl.build_omop_cdm import checkpoint_database

        for db in args.db:
            checkpoint_database(db)
            print(f"Checkpointed {db}")
        return 0
    if args.command == "verify-manifest":
        return handle_verify_manifest(args)
    if args.command == "extract":
        return handle_extract(args)
    if args.command == "import-omop":
        return handle_import_omop(args)
    if args.command == "check-source":
        return handle_check_source(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
