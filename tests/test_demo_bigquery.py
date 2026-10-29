"""BigQuery SynPUF demo: GoogleSQL generation and graceful claims handling, with a fake client (no GCP credentials)."""
import json

import pandas as pd
import pytest

sqlglot = pytest.importorskip("sqlglot")

from omop_etl.demo_bigquery import (DISCLAIMER, compile_cohort_bigquery, render_template, run_demo,  # noqa: E402
                                    summarize_features)

CDM = "bigquery-public-data.cms_synthetic_patient_data_omop"
SCRATCH = "my-proj.scratch"
VALUES = dict(cdm=f"`{CDM}`", scratch=f"`{SCRATCH}`", cohort_table="demo_cohort", features_table="demo_features", cohort_id=1)


def test_cohort_compiles_to_parseable_googlesql():
    sql = compile_cohort_bigquery("cohorts/diabetes_phenotype.circe.json", CDM, SCRATCH)
    sqlglot.parse_one(sql, read="bigquery")  # raises on a syntax error
    assert sql.startswith("CREATE OR REPLACE TABLE `my-proj.scratch`.demo_cohort AS")
    assert f"`{CDM}`.concept_ancestor" in sql and "cdm_ph" not in sql
    assert "INTERVAL '" not in sql  # DuckDB-style interval literals are not GoogleSQL
    tree = sqlglot.parse_one(sql, read="bigquery")
    for q in tree.find_all(sqlglot.exp.Qualify):  # GoogleSQL needs WHERE/GROUP BY/HAVING beside QUALIFY
        assert any(q.parent.args.get(k) for k in ("where", "group", "having"))


def test_gestational_exclusion_is_in_the_sql():
    sql = compile_cohort_bigquery("cohorts/diabetes_phenotype.circe.json", CDM, SCRATCH)
    assert "4024659" in sql  # gestational diabetes subtracted from the diabetes concept set


def test_vocabulary_dataset_is_used_for_concept_ancestor():
    sql = compile_cohort_bigquery("cohorts/diabetes_phenotype.circe.json", CDM, SCRATCH, vocabulary="my-proj.vocab")
    assert "`my-proj.vocab`.concept_ancestor" in sql and f"`{CDM}`.concept_ancestor" not in sql


@pytest.mark.parametrize("name", ["features", "availability"])
def test_template_sql_is_googlesql(name):
    from pathlib import Path
    raw = (Path(__file__).resolve().parents[1] / "inst" / "sql" / f"bigquery_demo_{name}.sql").read_text(encoding="utf-8")
    sqlglot.parse_one(render_template(raw, **VALUES), read="bigquery")
    with pytest.raises(KeyError):
        render_template(raw, cdm="x")


def test_bad_identifiers_rejected():
    with pytest.raises(ValueError):
        compile_cohort_bigquery("cohorts/diabetes_phenotype.circe.json", "nodataset", SCRATCH)
    with pytest.raises(ValueError):
        compile_cohort_bigquery("cohorts/diabetes_phenotype.circe.json", CDM, "a.b; DROP", )


def test_summary_survives_claims_data_without_labs():
    feats = pd.DataFrame({"age_at_index": [60, 70], "sex": ["Male", "Female"], "race": ["White", "White"],
                          "prior_ip_os_ed_count": [3, 0], "readmission_count_twelve_months": [1, 0]})
    avail = pd.DataFrame({"n_measurement_rows": [5], "n_numeric_values": [0], "n_creatinine": [0], "n_glucose": [0],
                          "n_albumin": [0], "n_systolic_bp": [0], "n_diastolic_bp": [0]})
    s = summarize_features(feats, avail)
    assert s["n_cohort"] == 2 and s["utilization"]["prior_ip_os_ed_count"]["mean"] == 1.5
    assert all("unavailable" in v["status"] for v in s["clinical_telemetry"].values())
    json.dumps(s)  # no NaN/numpy leakage
    empty = summarize_features(feats.iloc[0:0], None)  # empty cohort, no measurement table
    assert empty["n_cohort"] == 0 and empty["age_at_index"]["mean"] is None


class _Result:
    def __init__(self, df):
        self.df = df

    def result(self):
        return self

    def to_dataframe(self):
        return self.df


class _FakeClient:
    def __init__(self, tables, fail_measurement=False):
        self.sql, self.datasets, self.tables, self.fail_measurement = [], [], tables, fail_measurement

    def create_dataset(self, ds, exists_ok=False):
        self.datasets.append(ds)

    def query(self, sql):
        self.sql.append(sql)
        if "INFORMATION_SCHEMA" in sql:
            return _Result(pd.DataFrame({"table_name": self.tables}))
        if "demo_features" in sql and sql.lstrip().upper().startswith("SELECT * FROM"):
            return _Result(pd.DataFrame({"subject_id": [1, 2, 3], "age_at_index": [55, 61, 72],
                                         "sex": ["Male", "Female", "Female"], "race": ["White", "Black or African American", "White"],
                                         "prior_ip_os_ed_count": [0, 2, 5], "readmission_count_twelve_months": [0, 0, 1]}))
        if "n_numeric_values" in sql:
            if self.fail_measurement:
                raise RuntimeError("Not found: Table measurement")
            return _Result(pd.DataFrame({"n_measurement_rows": [9], "n_numeric_values": [0], "n_creatinine": [0], "n_glucose": [0],
                                         "n_albumin": [0], "n_systolic_bp": [0], "n_diastolic_bp": [0]}))
        return _Result(None)


ALL = ["person", "visit_occurrence", "observation_period", "concept_ancestor", "condition_occurrence", "drug_exposure", "measurement"]


@pytest.mark.parametrize("fail_measurement", [False, True])
def test_run_demo_end_to_end_with_fake_client(tmp_path, fail_measurement):
    client = _FakeClient(ALL, fail_measurement)
    s = run_demo("my-proj", tmp_path, client=client, cohort_json="cohorts/diabetes_phenotype.circe.json")
    assert s["n_cohort"] == 3 and client.datasets == ["my-proj.omop_duckdb_demo"]
    assert all("unavailable" in v["status"] for v in s["clinical_telemetry"].values())
    report = (tmp_path / "demo_report.md").read_text(encoding="utf-8")
    assert "SYNTHETIC" in report and DISCLAIMER in report
    assert {p.name for p in tmp_path.iterdir()} >= {"cohort.googlesql.sql", "demo_summary.json", "demo_features.csv"}


def test_run_demo_dry_run_makes_no_client_and_missing_tables_fail_clearly(tmp_path):
    s = run_demo("my-proj", tmp_path / "d", dry_run=True)
    assert s["run"]["dry_run"] and (tmp_path / "d" / "cohort.googlesql.sql").exists()
    with pytest.raises(RuntimeError, match="missing concept_ancestor"):
        run_demo("my-proj", tmp_path / "m", client=_FakeClient([t for t in ALL if t != "concept_ancestor"]))
    with pytest.raises(ValueError, match="billing_project"):
        run_demo("REPLACE_WITH_YOUR_GCP_PROJECT_ID", tmp_path, dry_run=True)


@pytest.mark.skipif(not __import__("os").environ.get("OMOP_BQ_BILLING_PROJECT"),
                    reason="live BigQuery smoke test: set OMOP_BQ_BILLING_PROJECT (bills your project)")
def test_live_bigquery_synpuf_smoke(tmp_path):
    import os
    s = run_demo(os.environ["OMOP_BQ_BILLING_PROJECT"], tmp_path)
    assert s["n_cohort"] > 0
    assert all("unavailable" in v["status"] for v in s["clinical_telemetry"].values())


def test_cli_dry_run_reads_config_and_prints_disclaimer(tmp_path, monkeypatch, capsys):
    pytest.importorskip("yaml")
    from omop_etl.demo_bigquery import main
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bigquery.yml").write_text("billing_project: cfg-proj\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cohort = __import__("pathlib").Path(__file__).resolve().parents[1] / "cohorts" / "diabetes_phenotype.circe.json"
    assert main(["--dry-run", "--output-dir", "o", "--cohort-json", str(cohort)]) == 0
    assert "SYNTHETIC" in capsys.readouterr().out
    assert "cfg-proj" in (tmp_path / "o" / "demo_summary.json").read_text(encoding="utf-8")


def test_runner_wraps_client_calls():
    from omop_etl.demo_bigquery import _Runner
    client = _FakeClient(ALL)
    r = _Runner("my-proj", client)
    r.execute("SELECT 1")
    r.ensure_dataset("my-proj.d")
    assert list(r.fetch("SELECT x FROM INFORMATION_SCHEMA.TABLES")["table_name"]) == ALL
    assert client.sql[0] == "SELECT 1" and client.datasets == ["my-proj.d"]
