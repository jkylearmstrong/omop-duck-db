"""BigQuery vs DuckDB parity, offline: the GoogleSQL the BigQuery demo sends is run on DuckDB (transpiled back with
sqlglot) and compared with the native DuckDB path on the same CDM, so a dialect bug that changes who is in the cohort or
what a feature counts fails here without credentials. Ids above 2^31 and 2^53 check that nothing truncates them."""
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sqlglot = pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.backend_parity import (FEATURE_COLUMNS, check_id_precision, compare_cohorts, compare_column_types,  # noqa: E402
                                     compare_demo_outputs, compare_demo_summaries, compare_features,
                                     emulate_bigquery_demo, googlesql_to_duckdb, main, run_duckdb_demo)
from omop_etl.build_omop_cdm import build_schema  # noqa: E402
from omop_etl.demo_bigquery import summarize_features  # noqa: E402

BEYOND_DOUBLE = 2 ** 53 + 1      # a float64 rounds this to 2^53
BEYOND_INT32 = 2 ** 31 + 5
#: the tables the demo reads on BigQuery (SynPUF) and copies into DuckDB
DEMO_TABLES = ("person", "observation_period", "visit_occurrence", "condition_occurrence", "drug_exposure", "measurement",
               "concept_ancestor")


def _cdm():
    """A small CDM that exercises every step of the diabetes definition and of the features."""
    c = duckdb.connect()
    build_schema(c)

    def person(pid, sex, yob, race):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, {sex}, {yob}, {race}, 0)")
        c.execute("INSERT INTO observation_period VALUES "
                  f"({pid}, {pid}, DATE '2016-01-01', DATE '2022-12-31', 0)")

    n = {"c": 0, "d": 0, "m": 0, "v": BEYOND_DOUBLE}

    def cond(pid, cid, d):
        n["c"] += 1
        c.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
                  f"condition_start_date, condition_type_concept_id) VALUES ({n['c']}, {pid}, {cid}, DATE '{d}', 0)")

    def drug(pid, cid, d):
        n["d"] += 1
        c.execute("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
                  f"drug_exposure_end_date, drug_type_concept_id) VALUES ({n['d']}, {pid}, {cid}, DATE '{d}', DATE '{d}', 0)")

    def meas(pid, value, d):
        n["m"] += 1
        c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                  f"measurement_type_concept_id, value_as_number) VALUES ({n['m']}, {pid}, 3004410, DATE '{d}', 0, {value})")

    def visit(pid, cid, start, end):
        n["v"] += 1
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                  f"visit_end_date, visit_type_concept_id) VALUES ({n['v']}, {pid}, {cid}, DATE '{start}', DATE '{end}', 0)")

    c.execute("INSERT INTO concept_ancestor VALUES (201820, 201820, 0, 0), (201820, 443238, 1, 1), (201820, 4024659, 1, 1), "
              "(201820, 4058243, 2, 2), (4024659, 4024659, 0, 0), (4024659, 4058243, 1, 1), (21600712, 21600712, 0, 0), "
              "(21600712, 1503297, 3, 3)")
    # 1: diabetes (a descendant concept), with prior utilization, an id beyond float64
    person(BEYOND_DOUBLE, 8507, 1950, 8527)
    cond(BEYOND_DOUBLE, 443238, "2019-03-01")
    visit(BEYOND_DOUBLE, 9201, "2018-06-01", "2018-06-05")
    visit(BEYOND_DOUBLE, 9201, "2018-06-20", "2018-06-25")  # readmission 15 days after discharge
    visit(BEYOND_DOUBLE, 9203, "2018-09-01", "2018-09-01")
    visit(BEYOND_DOUBLE, 9202, "2018-12-01", "2018-12-01")
    visit(BEYOND_DOUBLE, 9202, "2017-01-01", "2017-01-01")  # outside the 365-day look-back
    visit(BEYOND_DOUBLE, 9201, "2019-03-01", "2019-03-04")  # on the index date: not prior
    # 2: gestational diabetes only (and a descendant of it): excluded concept set, an id beyond int32
    person(BEYOND_INT32, 8532, 1990, 8516)
    cond(BEYOND_INT32, 4024659, "2019-04-01")
    cond(BEYOND_INT32, 4058243, "2019-05-01")
    # 3: enters on a diabetes drug (descendant of ATC A10)
    person(3, 8532, 1960, 8516)
    drug(3, 1503297, "2020-01-10")
    # 4: HbA1c 6.0 does not qualify, 7.1 does
    person(4, 8507, 1970, 8515)
    meas(4, 6.0, "2019-01-01")
    meas(4, 7.1, "2019-05-05")
    visit(4, 262, "2019-04-01", "2019-04-02")
    # 5: a child: fails the adult inclusion rule
    person(5, 8507, 2012, 8527)
    cond(5, 201820, "2022-01-01")
    # 6: first event before the observation period; the cohort enters on the later one
    person(6, 0, 1980, 0)
    cond(6, 201820, "2015-06-01")
    cond(6, 201820, "2018-02-02")
    cond(6, 201820, "2018-03-03")
    return c


@pytest.fixture(scope="module")
def runs():
    con = _cdm()
    native = run_duckdb_demo(con)
    emulated = emulate_bigquery_demo(con)
    yield con, native, emulated
    con.close()


def test_googlesql_cohort_matches_native_duckdb_cohort(runs):
    _, (cohort, _), (bq_cohort, _) = runs
    res = compare_cohorts(cohort, bq_cohort)
    assert res["match"], res
    assert set(cohort["subject_id"]) == {BEYOND_DOUBLE, 3, 4, 6}
    starts = dict(zip(cohort["subject_id"], cohort["cohort_start_date"].astype(str).str[:10]))
    assert starts[4] == "2019-05-05" and starts[6] == "2018-02-02"


def test_features_match_across_dialects_and_are_right(runs):
    _, (_, feats), (_, bq_feats) = runs
    res = compare_features(feats, bq_feats)
    assert res["match"], res
    assert set(FEATURE_COLUMNS) <= set(feats.columns)
    p1 = feats.set_index("subject_id").loc[BEYOND_DOUBLE]
    assert (p1.age_at_index, p1.sex, p1.race) == (69, "Male", "White")
    assert (p1.prior_ip_count, p1.prior_ed_count, p1.prior_os_count, p1.prior_ip_os_ed_count) == (2, 1, 1, 4)
    assert p1.readmission_count_twelve_months == 1


def test_ids_survive_both_paths_exactly(runs):
    con, (cohort, feats), (bq_cohort, bq_feats) = runs
    for df in (cohort, feats, bq_cohort, bq_feats):
        assert BEYOND_DOUBLE in set(int(x) for x in df["subject_id"]) and not check_id_precision(df)
    types = con.execute("SELECT table_schema, table_name, data_type FROM information_schema.columns "
                        "WHERE column_name = 'subject_id' AND table_name IN ('demo_cohort', 'demo_features')").fetchall()
    assert len(types) == 4 and {t[2] for t in types} == {"BIGINT"}


def test_column_types_of_the_demo_tables_hold_bigquery_types():
    """The shipped DuckDB DDL against the INT64/STRING/DATE/FLOAT64 types BigQuery serves for the same columns."""
    con = duckdb.connect()
    build_schema(con)
    tables = ", ".join(f"'{t}'" for t in DEMO_TABLES)
    duck = con.execute(f"SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_name IN ({tables})").df()
    to_bq = {"BIGINT": "INT64", "INTEGER": "INT64", "VARCHAR": "STRING", "DATE": "DATE", "TIMESTAMP": "DATETIME",
             "DOUBLE": "FLOAT64"}
    bq = duck.assign(data_type=duck["data_type"].map(lambda t: to_bq[t.split("(")[0]]))
    res = compare_column_types(bq, duck)
    assert (res["status"] == "ok").all(), res[res["status"] != "ok"]
    keys = res[res["column_name"].isin(["person_id", "visit_occurrence_id", "condition_occurrence_id", "drug_exposure_id",
                                        "measurement_id", "observation_period_id"])]
    assert set(keys["duckdb_type"]) == {"BIGINT"}


def test_column_type_check_flags_narrowed_ids_and_mismatches():
    bq = pd.DataFrame({"table_name": ["person"] * 5 + ["visit_occurrence"],
                       "column_name": ["person_id", "gender_concept_id", "year_of_birth", "person_source_value",
                                       "birth_datetime", "visit_occurrence_id"],
                       "data_type": ["INT64", "INT64", "INT64", "STRING", "DATETIME", "INT64"]})
    duck = pd.DataFrame({"table_name": ["PERSON"] * 5 + ["visit_occurrence"],
                         "column_name": ["person_id", "gender_concept_id", "year_of_birth", "person_source_value",
                                         "birth_datetime", "admitted_from_concept_id"],
                         "data_type": ["INTEGER", "INTEGER", "INTEGER", "DOUBLE", "TIMESTAMP", "INTEGER"]})
    st = compare_column_types(bq, duck).set_index(["table_name", "column_name"])["status"].to_dict()
    assert st[("person", "person_id")] == "id_narrowed"            # 32-bit key id
    assert st[("person", "gender_concept_id")] == "ok"             # concept ids may be 32-bit
    assert st[("person", "person_source_value")] == "type_mismatch"
    assert st[("person", "birth_datetime")] == "ok"
    assert st[("visit_occurrence", "visit_occurrence_id")] == "only_bigquery"
    assert st[("visit_occurrence", "admitted_from_concept_id")] == "only_duckdb"
    dbl = compare_column_types(bq.iloc[[0]], duck.iloc[[0]].assign(data_type="DOUBLE"))
    assert dbl["status"].tolist() == ["id_narrowed"]


def test_id_precision_of_downloaded_frames():
    ok = pd.DataFrame({"person_id": pd.Series([BEYOND_DOUBLE], dtype="int64"), "visit_occurrence_id": pd.array([1], dtype="Int64"),
                       "care_site_id": ["7"]})
    assert check_id_precision(ok) == []
    bad = pd.DataFrame({"person_id": [float(BEYOND_DOUBLE)], "visit_occurrence_id": pd.Series([1], dtype="int32"), "x": [1.5]})
    assert [p["column"] for p in check_id_precision(bad)] == ["person_id", "visit_occurrence_id"]


def test_comparisons_catch_differences(runs):
    _, (cohort, feats), _ = runs
    shifted = cohort.copy()
    shifted.loc[0, "cohort_end_date"] = pd.Timestamp("2030-01-01")
    res = compare_cohorts(cohort, shifted)
    assert res["same_subjects"] and not res["match"] and res["n_rows_only_left"] == 1
    res = compare_cohorts(cohort, cohort.iloc[1:], examples=3)
    assert not res["same_subjects"] and res["n_subjects_only_left"] == 1 and len(res["examples_only_left"]) == 1

    other = feats.copy()
    other.loc[0, "prior_ip_count"] += 1
    other.loc[1, "sex"] = "Other/Unknown"
    res = compare_features(feats, other)
    assert not res["match"] and res["columns"]["prior_ip_count"]["n_mismatch"] == 1 and res["columns"]["sex"]["n_mismatch"] == 1
    assert compare_features(feats, other.assign(prior_ip_count=feats["prior_ip_count"] + 0.4), columns=["prior_ip_count"],
                            tolerance=0.5)["match"]
    assert not compare_features(feats, feats.iloc[1:])["match"]


def test_float_ids_beyond_2_53_are_refused_not_compared():
    a = pd.DataFrame({"subject_id": [float(BEYOND_DOUBLE)], "cohort_start_date": ["2020-01-01"], "cohort_end_date": ["2020-01-01"]})
    with pytest.raises(ValueError, match="2\\^53"):
        compare_cohorts(a, a)
    small = a.assign(subject_id=[3.0])
    assert compare_cohorts(small, small.assign(subject_id=[3]))["match"]


def test_googlesql_to_duckdb_maps_every_dataset():
    sql = "SELECT p.person_id FROM `bigquery-public-data.cms_synthetic_patient_data_omop.person` AS p"
    out = googlesql_to_duckdb(sql, {"bigquery-public-data.cms_synthetic_patient_data_omop": "main"})
    assert "main.person" in out.replace('"', "") and "bigquery-public-data" not in out
    with pytest.raises(KeyError, match="no DuckDB schema"):
        googlesql_to_duckdb(sql, {"other.ds": "main"})


def _summary(feats):
    return json.loads(json.dumps(summarize_features(feats, None)))


def test_demo_outputs_compare_r_style_and_python_style(tmp_path, runs):
    """What compare-outputs sees from an R run (ids as quoted text, integers without decimals) and a Python run."""
    _, (_, feats), _ = runs
    py, r = tmp_path / "py", tmp_path / "r"
    py.mkdir(), r.mkdir()
    s = _summary(feats)
    (py / "demo_summary.json").write_text(json.dumps({**s, "run": {"billing_project": "a"}}), encoding="utf-8")
    r_s = json.loads(json.dumps(s))
    r_s["age_at_index"]["mean"] = round(r_s["age_at_index"]["mean"] + 0.01, 2)  # rounding noise
    (r / "demo_summary.json").write_text(json.dumps({**r_s, "run": {"billing_project": "b"}}), encoding="utf-8")
    feats.to_csv(py / "demo_features.csv", index=False)
    feats.assign(subject_id=feats["subject_id"].astype(str)).to_csv(r / "demo_features.csv", index=False, quoting=1)
    res = compare_demo_outputs(r, py)
    assert res["match"], res
    assert main(["compare-outputs", str(r), str(py)]) == 0

    r_s["n_cohort"] += 1
    r_s["sex"]["Male"]["n"] += 1
    (r / "demo_summary.json").write_text(json.dumps(r_s), encoding="utf-8")
    diffs = compare_demo_summaries(r / "demo_summary.json", py / "demo_summary.json")
    assert "n_cohort: 5 vs 4" in diffs and any(d.startswith("sex.Male.n") for d in diffs)
    assert main(["compare-outputs", str(r), str(py)]) == 1


def test_features_template_is_packaged_for_the_wheel():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"inst/sql/duckdb_demo_features.sql"' in text


R_SCRIPT = r"""
args <- commandArgs(trailingOnly = TRUE)
suppressMessages(if (requireNamespace("pkgload", quietly = TRUE) && file.exists("DESCRIPTION")) pkgload::load_all(".", quiet = TRUE) else library(omopduckdb))
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = args[[1]])
res <- run_duckdb_demo(con, cohort_table = "r_demo_cohort", features_table = "r_demo_features")
DBI::dbDisconnect(con, shutdown = TRUE)
dir.create(args[[2]], showWarnings = FALSE)
jsonlite::write_json(summarize_bigquery_demo(res$features, NULL), file.path(args[[2]], "demo_summary.json"),
                     auto_unbox = TRUE, null = "null", digits = NA)
utils::write.csv(res$features, file.path(args[[2]], "demo_features.csv"), row.names = FALSE)
utils::write.csv(res$cohort, file.path(args[[2]], "demo_cohort.csv"), row.names = FALSE)
"""


@pytest.mark.skipif(__import__("shutil").which("Rscript") is None, reason="Rscript is not available")
def test_r_and_python_duckdb_demo_agree(tmp_path):
    """R vs Python on the same CDM file: same cohort, same features, same summary (ids above 2^53 included)."""
    import os
    import subprocess
    db = tmp_path / "cdm.duckdb"
    con = _cdm()
    con.execute(f"ATTACH '{db}' AS out")
    con.execute("COPY FROM DATABASE memory TO out")
    con.execute("DETACH out")
    cohort, feats = run_duckdb_demo(con)
    con.close()
    py = tmp_path / "py"
    py.mkdir()
    (py / "demo_summary.json").write_text(json.dumps(summarize_features(feats, None)), encoding="utf-8")
    feats.to_csv(py / "demo_features.csv", index=False)
    script = tmp_path / "run.R"
    script.write_text(R_SCRIPT, encoding="utf-8")
    r = subprocess.run(["Rscript", "--vanilla", str(script), str(db), str(tmp_path / "r")], cwd=REPO_ROOT,
                       capture_output=True, text=True, env={**os.environ, "RENV_CONFIG_AUTOLOADER_ENABLED": "FALSE"})
    if r.returncode != 0 and "there is no package called" in r.stderr:
        pytest.skip("the R package or its dependencies are not installed: " + r.stderr[-300:])
    assert r.returncode == 0, r.stderr[-2000:]
    r_cohort = pd.read_csv(tmp_path / "r" / "demo_cohort.csv", dtype={"subject_id": str})
    assert compare_cohorts(r_cohort, cohort)["match"]
    res = compare_demo_outputs(tmp_path / "r", py)
    assert res["match"], res


def test_synpuf_shaped_cdm_without_visits_or_labs():
    """The public SynPUF copy has no visit_occurrence or measurement: the GoogleSQL path runs on empty stand-ins and still
    matches DuckDB run on the same CDM with those tables empty (which is what the sample copy gives)."""
    con = _cdm()
    con.execute("DROP TABLE visit_occurrence")
    con.execute("DROP TABLE measurement")
    con.execute("CREATE SCHEMA synpuf")
    for t in ("person", "observation_period", "condition_occurrence", "drug_exposure", "concept_ancestor"):
        con.execute(f"CREATE TABLE synpuf.{t} AS SELECT * FROM main.{t}")
    bq_cohort, bq_feats = emulate_bigquery_demo(con, cdm_schema="synpuf")
    con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM bq_scratch.stub_visit_occurrence")
    con.execute("CREATE TABLE measurement AS SELECT * FROM bq_scratch.stub_measurement")
    cohort, feats = run_duckdb_demo(con)
    assert compare_cohorts(cohort, bq_cohort)["match"]
    assert set(cohort["subject_id"]) == {BEYOND_DOUBLE, 3, 6}  # person 4 entered only on an HbA1c value
    assert compare_features(feats, bq_feats)["match"]
    assert (bq_feats["prior_ip_os_ed_count"] == 0).all()
    con.close()
