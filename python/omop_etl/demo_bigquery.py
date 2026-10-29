"""Public BigQuery demonstration / smoke test on the CMS SynPUF OMOP dataset.

Builds ``cohorts/diabetes_phenotype.circe.json`` and the baseline features on
``bigquery-public-data.cms_synthetic_patient_data_omop`` and writes a summary report. The data are SYNTHETIC CLAIMS:
the run is a software check (the cohort compiles to GoogleSQL, executes, and the feature step survives a CDM without
labs or vital signs), not a clinical result.

The Circe definition is compiled by the package's native compiler and transpiled DuckDB -> GoogleSQL with sqlglot
(the R entry point uses CirceR + SqlRender instead). ``run_demo(..., dry_run=True)`` needs no credentials: it
compiles and writes the SQL only. Install the client with ``pip install omop-duck-db[bigquery]``.

    python -m omop_etl.demo_bigquery --billing-project my-gcp-project [--output-dir out] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from omop_etl.circe import compile_circe_to_duckdb

__all__ = ["DEFAULT_DATASET", "DISCLAIMER", "CLINICAL_TELEMETRY", "UTILIZATION_FEATURES",
           "compile_cohort_bigquery", "render_template", "run_demo", "summarize_features", "main"]

DEFAULT_DATASET = "bigquery-public-data.cms_synthetic_patient_data_omop"
REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent
REQUIRED_TABLES = ("person", "visit_occurrence", "observation_period", "concept_ancestor")
ENTRY_TABLES = ("condition_occurrence", "drug_exposure", "measurement")
#: the lab / vital-sign features a claims dataset cannot supply; reported as unavailable, never imputed
CLINICAL_TELEMETRY = ("serum_creatinine", "serum_glucose", "serum_albumin", "systolic_bp", "diastolic_bp")
#: the administrative sub-model
UTILIZATION_FEATURES = ("prior_ip_os_ed_count", "readmission_count_twelve_months")
DISCLAIMER = (
    "These results come from CMS DE-SynPUF, a SYNTHETIC claims dataset. They verify that the software runs "
    "(cohort SQL, dialect translation, feature extraction). They say nothing about real-world clinical performance "
    "or human epidemiology, and SynPUF has no laboratory values or vital signs, so only the administrative "
    "utilization sub-model can be exercised."
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _resource(*parts: str) -> Path:
    packaged = PACKAGE_DIR / "resources" / Path(*parts)
    return packaged if packaged.exists() else REPO_ROOT / "inst" / Path(*parts)


def _split_dataset(dataset: str, what: str = "dataset") -> tuple[str, str]:
    bits = dataset.split(".")
    if len(bits) != 2 or not _PROJECT.match(bits[0]) or not _IDENT.match(bits[1]):
        raise ValueError(f"{what} must be 'project.dataset', got {dataset!r}")
    return bits[0], bits[1]


def _qualified(dataset: str, what: str = "dataset") -> str:
    _split_dataset(dataset, what)
    return f"`{dataset}`"


def render_template(sql: str, **values: Any) -> str:
    """Substitute ``{{name}}`` placeholders; a placeholder without a value is an error."""
    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise KeyError(f"no value for SQL placeholder {{{{{m.group(1)}}}}}")
        return str(values[m.group(1)])
    return re.sub(r"\{\{(\w+)\}\}", sub, sql)


def _bigquery_ast_fixes(tree: Any) -> Any:
    """GoogleSQL differences sqlglot does not resolve: QUALIFY needs a WHERE/GROUP BY/HAVING, and date arithmetic
    must use DATE_ADD/DATE_SUB with an integer interval (``INTERVAL '1' DAY`` is DuckDB syntax)."""
    from sqlglot import exp

    for q in list(tree.find_all(exp.Qualify)):
        sel = q.parent
        if not (sel.args.get("where") or sel.args.get("group") or sel.args.get("having")):
            sel.set("where", exp.Where(this=exp.true()))

    def date_math(node):
        if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
            amount = node.expression.this
            if isinstance(amount, exp.Literal):
                amount = exp.Literal.number(amount.name)
            cls = exp.DateAdd if isinstance(node, exp.Add) else exp.DateSub
            return cls(this=node.this, expression=amount, unit=exp.var("DAY"))
        return node

    return tree.transform(date_math)


def compile_cohort_bigquery(circe_json: str | Path | dict, cdm: str, scratch: str, cohort_table: str = "demo_cohort",
                            cohort_id: int = 1, vocabulary: str | None = None) -> str:
    """Compile a Circe definition to one GoogleSQL ``CREATE OR REPLACE TABLE ... AS`` statement.

    ``cdm`` / ``scratch`` / ``vocabulary`` are ``project.dataset`` strings; ``vocabulary`` (default ``cdm``) is where
    ``concept_ancestor`` lives. Raises ``NotImplementedError`` for constructs the native compiler does not evaluate.
    """
    import sqlglot

    if not _IDENT.match(cohort_table):
        raise ValueError(f"cohort_table must be a plain identifier, got {cohort_table!r}")
    cdm_q, scratch_q, vocab_q = _qualified(cdm, "cdm"), _qualified(scratch, "scratch"), _qualified(vocabulary or cdm, "vocabulary")
    source = compile_circe_to_duckdb(circe_json, target_cohort_id=cohort_id, cdm_schema="cdm_ph")
    tree = _bigquery_ast_fixes(sqlglot.parse_one(source, read="duckdb"))
    sql = tree.sql(dialect="bigquery")
    sql = re.sub(r"\bcdm_ph\.concept_ancestor\b", f"{vocab_q}.concept_ancestor", sql)
    sql = re.sub(r"\bcdm_ph\.(\w+)", lambda m: f"{cdm_q}.{m.group(1)}", sql)
    return f"CREATE OR REPLACE TABLE {scratch_q}.{cohort_table} AS\n{sql}"


class _Runner:
    """Thin wrapper over ``google.cloud.bigquery.Client`` so tests can substitute a fake with the same two methods."""

    def __init__(self, billing_project: str, client: Any = None) -> None:
        if client is None:
            try:
                from google.cloud import bigquery
            except ImportError as exc:
                raise ImportError("this demo needs google-cloud-bigquery: pip install omop-duck-db[bigquery]") from exc
            client = bigquery.Client(project=billing_project)
        self.client = client

    def execute(self, sql: str) -> None:
        self.client.query(sql).result()

    def fetch(self, sql: str) -> pd.DataFrame:
        return self.client.query(sql).result().to_dataframe()

    def ensure_dataset(self, dataset: str) -> None:
        self.client.create_dataset(dataset, exists_ok=True)


def _timed(timings: dict, name: str, fn: Callable[[], Any]) -> Any:
    t0 = time.perf_counter()
    try:
        return fn()
    finally:
        timings[name] = round(time.perf_counter() - t0, 2)


def summarize_features(features: pd.DataFrame, availability: pd.DataFrame | None) -> dict:
    """Cohort size, demographics, utilization and lab/vital availability. NaN-safe on an empty cohort."""
    n = int(len(features))

    def pct(series: pd.Series) -> dict:
        counts = series.value_counts()
        return {str(k): {"n": int(v), "pct": round(100.0 * v / n, 1)} for k, v in counts.items()} if n else {}

    def num(col: str) -> dict:
        s = pd.to_numeric(features[col], errors="coerce") if col in features else pd.Series(dtype=float)
        s = s.dropna()
        if s.empty:
            return {"mean": None, "sd": None, "median": None}
        return {"mean": round(float(s.mean()), 2), "sd": round(float(s.std(ddof=1)), 2) if len(s) > 1 else None,
                "median": float(s.median())}

    avail = availability.iloc[0].to_dict() if availability is not None and len(availability) else {}
    avail = {k: int(0 if pd.isna(v) else v) for k, v in avail.items()}
    lab_cols = {"serum_creatinine": "n_creatinine", "serum_glucose": "n_glucose", "serum_albumin": "n_albumin",
                "systolic_bp": "n_systolic_bp", "diastolic_bp": "n_diastolic_bp"}
    telemetry = {}
    for feature, col in lab_cols.items():
        have = avail.get(col, 0)
        telemetry[feature] = {"n_numeric_values": have,
                              "status": "available" if have else "unavailable in this data (100% missing, not imputed)"}
    return {
        "n_cohort": n,
        "age_at_index": num("age_at_index"),
        "sex": pct(features["sex"]) if "sex" in features else {},
        "race": pct(features["race"]) if "race" in features else {},
        "utilization": {f: num(f) for f in UTILIZATION_FEATURES},
        "clinical_telemetry": telemetry,
        "numeric_measurement_values_in_cohort": avail.get("n_numeric_values", 0),
        "model_scope": ("administrative utilization sub-model: " + ", ".join(UTILIZATION_FEATURES)),
    }


def _report_markdown(summary: dict, run: dict) -> str:
    def row(label: str, d: dict) -> str:
        return f"| {label} | {d['n']} ({d['pct']}%) |"

    lines = ["# BigQuery SynPUF demonstration report", "", f"> **{DISCLAIMER}**", "",
             "## Run", "", f"- Billing project: `{run['billing_project']}`", f"- Source: `{run['dataset']}`",
             f"- Cohort: `{run['cohort_json']}` (cohort id {run['cohort_id']})", ""]
    if run.get("dry_run"):
        lines += ["Dry run: SQL was compiled and written; nothing was executed on BigQuery.", ""]
    else:
        lines += ["| Step | Seconds |", "|---|---|"] + [f"| {k} | {v} |" for k, v in run["timings_seconds"].items()] + [""]
        age, n = summary["age_at_index"], summary["n_cohort"]
        lines += ["## Cohort", "", f"- Patients: **{n}**", f"- Age at index: mean {age['mean']}, SD {age['sd']}, median {age['median']}",
                  "", "| Sex | n (%) |", "|---|---|"] + [row(k, v) for k, v in summary["sex"].items()]
        lines += ["", "| Race | n (%) |", "|---|---|"] + [row(k, v) for k, v in summary["race"].items()]
        lines += ["", "## Prior-year utilization (administrative sub-model)", "", "| Feature | Mean | SD | Median |", "|---|---|---|---|"]
        lines += [f"| {k} | {v['mean']} | {v['sd']} | {v['median']} |" for k, v in summary["utilization"].items()]
        lines += ["", "## Clinical telemetry", "", "| Feature | Numeric values in cohort | Status |", "|---|---|---|"]
        lines += [f"| {k} | {v['n_numeric_values']} | {v['status']} |" for k, v in summary["clinical_telemetry"].items()]
        lines += ["", f"Scope: {summary['model_scope']}."]
    return "\n".join(lines) + "\n"


def run_demo(billing_project: str, output_dir: str | Path = "bigquery_demo_out", dataset: str = DEFAULT_DATASET,
             scratch_dataset: str | None = None, vocabulary_dataset: str | None = None,
             cohort_json: str | Path | None = None, cohort_id: int = 1, dry_run: bool = False,
             client: Any = None) -> dict:
    """Run the demonstration; returns the summary dict (also written to ``output_dir``).

    ``scratch_dataset`` is a dataset (``"project.dataset"``) you may create tables in; default
    ``<billing_project>.omop_duckdb_demo``. Public data is only read; every table written lives there.
    """
    if not isinstance(billing_project, str) or not _PROJECT.match(billing_project or "") \
            or billing_project == "REPLACE_WITH_YOUR_GCP_PROJECT_ID":
        raise ValueError("billing_project must be your GCP project id (BigQuery bills the query to it)")
    cohort_json = Path(cohort_json) if cohort_json else REPO_ROOT / "cohorts" / "diabetes_phenotype.circe.json"
    if not cohort_json.exists():
        raise FileNotFoundError(f"cohort definition not found: {cohort_json} (pass cohort_json=)")
    scratch = scratch_dataset or f"{billing_project}.omop_duckdb_demo"
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cohort_table, features_table = "demo_cohort", "demo_features"
    timings: dict[str, float] = {}

    cohort_sql = _timed(timings, "compile_cohort_to_googlesql", lambda: compile_cohort_bigquery(
        cohort_json, dataset, scratch, cohort_table, cohort_id, vocabulary_dataset))
    values = dict(cdm=_qualified(dataset), scratch=_qualified(scratch, "scratch"), cohort_table=cohort_table,
                  features_table=features_table, cohort_id=int(cohort_id))
    features_sql = render_template(_resource("sql", "bigquery_demo_features.sql").read_text(encoding="utf-8"), **values)
    avail_sql = render_template(_resource("sql", "bigquery_demo_availability.sql").read_text(encoding="utf-8"), **values)
    (out / "cohort.googlesql.sql").write_text(cohort_sql + ";\n", encoding="utf-8")
    (out / "features.googlesql.sql").write_text(features_sql + ";\n", encoding="utf-8")
    (out / "availability.googlesql.sql").write_text(avail_sql + ";\n", encoding="utf-8")

    run = {"billing_project": billing_project, "dataset": dataset, "scratch_dataset": scratch,
           "cohort_json": cohort_json.name, "cohort_id": cohort_id, "dry_run": dry_run, "timings_seconds": timings}
    if dry_run:
        summary = {"disclaimer": DISCLAIMER, "run": run}
        (out / "demo_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (out / "demo_report.md").write_text(_report_markdown({}, run), encoding="utf-8")
        return summary

    runner = _Runner(billing_project, client)
    src_project, src_ds = _split_dataset(dataset)
    present = set(_timed(timings, "preflight", lambda: runner.fetch(
        f"SELECT table_name FROM `{src_project}.{src_ds}.INFORMATION_SCHEMA.TABLES`")["table_name"]))
    missing = [t for t in REQUIRED_TABLES + ENTRY_TABLES
               if t not in present and not (t == "concept_ancestor" and vocabulary_dataset)]
    if missing:
        raise RuntimeError(f"{dataset} is missing {', '.join(missing)} (found: {sorted(present)}); pass "
                           "vocabulary_dataset= if concept_ancestor lives elsewhere, or point dataset= at an OMOP CDM "
                           "with these tables (the definition reads condition_occurrence, drug_exposure, measurement)")
    _timed(timings, "create_scratch_dataset", lambda: runner.ensure_dataset(scratch))
    _timed(timings, "build_cohort", lambda: runner.execute(cohort_sql))
    _timed(timings, "extract_features", lambda: runner.execute(features_sql))
    features = _timed(timings, "download_features", lambda: runner.fetch(
        f"SELECT * FROM {_qualified(scratch, 'scratch')}.{features_table}"))
    try:
        availability = _timed(timings, "lab_availability", lambda: runner.fetch(avail_sql))
    except Exception as exc:  # noqa: BLE001 - no measurement table / no rows is the expected claims case
        run["availability_note"] = f"availability query failed ({type(exc).__name__}); treated as no numeric values"
        availability = None
    summary = summarize_features(features, availability)
    summary.update(disclaimer=DISCLAIMER, run=run)
    (out / "demo_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    features.to_csv(out / "demo_features.csv", index=False)
    (out / "demo_report.md").write_text(_report_markdown(summary, run), encoding="utf-8")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--billing-project", default=None, help="your GCP project id (default: billing_project in config/bigquery.yml)")
    ap.add_argument("--output-dir", default="bigquery_demo_out")
    ap.add_argument("--dataset", default=None, help=f"default {DEFAULT_DATASET}")
    ap.add_argument("--scratch-dataset", default=None, help="project.dataset you can write to (default <billing>.omop_duckdb_demo)")
    ap.add_argument("--vocabulary-dataset", default=None, help="project.dataset holding concept_ancestor, if not in --dataset")
    ap.add_argument("--cohort-json", default=None)
    ap.add_argument("--dry-run", action="store_true", help="compile and write the SQL only; no BigQuery call")
    a = ap.parse_args(argv)
    cfg: dict = {}
    cfg_path = Path("config") / "bigquery.yml"
    if cfg_path.exists():
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    summary = run_demo(a.billing_project or cfg.get("billing_project", ""), a.output_dir,
                       a.dataset or cfg.get("dataset") or DEFAULT_DATASET, a.scratch_dataset, a.vocabulary_dataset,
                       a.cohort_json, dry_run=a.dry_run)
    print(f"Wrote {Path(a.output_dir) / 'demo_report.md'}")
    if not a.dry_run:
        print(f"Cohort: {summary['n_cohort']} patients")
    print(DISCLAIMER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
