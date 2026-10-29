"""Explanations, FHIR adapter, append ETL, drift report and unit guard."""
import sys
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema  # noqa: E402
from omop_etl.cli import main  # noqa: E402
from omop_etl.drift_report import write_drift_report  # noqa: E402
from omop_etl.encounter import score_encounter  # noqa: E402
from omop_etl.fhir import score_fhir_bundle  # noqa: E402
from omop_etl.model_bundle import write_model_bundle  # noqa: E402
from omop_etl.monitoring import monitor_scores  # noqa: E402
from omop_etl.unit_guard import check_unit_sanity  # noqa: E402

SPEC = {"features": [
    {"name": "lab_creatinine", "dtype": "float", "bounds": [0.1, 30.0], "impute": 1.0},
    {"name": "heart_rate", "dtype": "float", "bounds": [20, 250], "impute": 80.0},
    {"name": "prior_ip_count", "dtype": "int", "impute": 0},
]}


@pytest.fixture()
def bundle(tmp_path):
    return write_model_bundle(tmp_path / "b", name="demo", version="1", feature_spec=SPEC,
                              components=[{"kind": "logistic", "filename": "glm.json",
                                           "content": {"intercept": -3.5, "coef": [0.8, 0.02, 0.5]}}])


FHIR = {"resourceType": "Bundle", "entry": [
    {"resource": {"resourceType": "Patient", "id": "p1", "gender": "male", "birthDate": "1950-03-02"}},
    {"resource": {"resourceType": "Encounter", "id": "e1", "class": {"code": "IMP"},
                  "period": {"start": "2024-01-01", "end": "2024-01-05"}}},
    {"resource": {"resourceType": "Observation", "code": {"coding": [{"system": "http://loinc.org", "code": "2160-0"}]},
                  "effectiveDateTime": "2024-01-04", "valueQuantity": {"value": 265.2, "unit": "umol/L"}}},
    {"resource": {"resourceType": "Observation", "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                  "effectiveDateTime": "2024-01-04", "valueQuantity": {"value": 90, "unit": "/min"}}},
    {"resource": {"resourceType": "Condition", "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006"}]}}},
]}


def test_fhir_in_memory_score_with_unit_conversion_and_explain(bundle):
    r = score_fhir_bundle(FHIR, bundle, explain=True)
    assert r["features"]["lab_creatinine"] == pytest.approx(3.0, abs=0.01)  # 265.2 umol/L -> 3.0 mg/dL
    assert r["fhir"]["unmapped_codes"] == ["http://snomed.info/sct|44054006"]
    top = r["top_drivers"]
    assert top[0]["name"] == "lab_creatinine" and top[0]["impact"] > 0
    assert all(abs(a["impact"]) >= abs(b["impact"]) for a, b in zip(top, top[1:]))


def test_fhir_requires_bundle(bundle):
    with pytest.raises(ValueError):
        score_fhir_bundle({"resourceType": "Patient"}, bundle)


def test_score_encounter_explain(bundle):
    con = duckdb.connect(":memory:")
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1950, 0, 0)")
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) "
                "VALUES (1, 1, 9201, DATE '2020-06-01', DATE '2020-06-04', 0)")
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number, measurement_type_concept_id) "
                "VALUES (1, 1, 3016723, DATE '2020-06-03', 3.0, 0)")
    assert "top_drivers" not in score_encounter(con, 1, bundle)
    r = score_encounter(con, 1, bundle, explain=True, top_k=2)
    assert len(r["top_drivers"]) <= 2 and r["top_drivers"][0]["value"] == 3.0


def test_unit_guard_flags_umol_creatinine():
    df = pd.DataFrame({"lab_creatinine": np.full(50, 88.0), "heart_rate": np.full(50, 82.0)})
    with pytest.warns(UserWarning, match="lab_creatinine"):
        res = check_unit_sanity(df)
    by = res.set_index("feature")
    assert by.loc["lab_creatinine", "flag"] and "umol" in by.loc["lab_creatinine", "hint"]
    assert not by.loc["heart_rate", "flag"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        check_unit_sanity(pd.DataFrame({"lab_creatinine": np.full(50, 1.1)}))


def test_drift_report_html_and_cli(tmp_path):
    rng = np.random.default_rng(0)
    n = 600
    sc = pd.DataFrame({"score_date": pd.Timestamp("2023-01-01") + pd.to_timedelta(rng.integers(0, 360, n), unit="D"),
                       "prob": rng.beta(2, 8, n)})
    sc["outcome_flag"] = (rng.uniform(size=n) < sc["prob"]).astype(int)
    log = monitor_scores(sc, rng.beta(2, 8, 1000), as_of="2024-06-01", min_events=5)
    log.insert(0, "model_version", "1")
    out = write_drift_report(log, tmp_path / "r.html", scores=sc)
    page = out.read_text(encoding="utf-8")
    assert "<svg" in page and "Population stability index" in page and "Calibration" in page and "<script" not in page
    log.to_csv(tmp_path / "log.csv", index=False)
    assert main(["report-drift", "--log", str(tmp_path / "log.csv"), "--out", str(tmp_path / "c.html")]) == 0


def test_append_mode_adds_only_new_rows(tmp_path):
    from omop_etl.build_omop_cdm import etl_pcornet
    src = tmp_path / "src"
    src.mkdir()
    (src / "demographic.csv").write_text("PATID,SEX,BIRTH_DATE,RACE,HISPANIC\nP1,F,1960-01-01,05,N\n", encoding="utf-8")
    (src / "encounter.csv").write_text("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,DISCHARGE_DATE\nE1,P1,IP,2022-08-01,2022-08-05\n", encoding="utf-8")
    db = str(tmp_path / "cdm.duckdb")
    etl_pcornet(src, db, build_schema_flag=True, key_check="ignore")
    (src / "demographic.csv").write_text("PATID,SEX,BIRTH_DATE,RACE,HISPANIC\nP1,F,1960-01-01,05,N\nP2,M,1970-01-01,03,N\n", encoding="utf-8")
    (src / "encounter.csv").write_text("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,DISCHARGE_DATE\nE1,P1,IP,2022-08-01,2022-08-05\n"
                                       "E2,P2,IP,2022-11-01,2022-11-04\nE3,P1,ED,2022-10-02,2022-10-02\n", encoding="utf-8")
    etl_pcornet(src, db, mode="append", since="2022-10-01", key_check="ignore")
    etl_pcornet(src, db, mode="append", since="2022-10-01", key_check="ignore")  # idempotent
    con = duckdb.connect(db, read_only=True)
    assert con.execute("SELECT count(*) FROM person").fetchone()[0] == 2
    ids = con.execute("SELECT visit_occurrence_id, visit_source_value FROM visit_occurrence ORDER BY 1").fetchall()
    assert [v for _, v in ids] == ["E1", "E2", "E3"] and [i for i, _ in ids] == [1, 2, 3]
    con.close()
    with pytest.raises(ValueError, match="since"):
        etl_pcornet(src, db, mode="append")


def test_cli_score_fhir_unit_check_and_explain(tmp_path, bundle, capsys):
    import json
    fp = tmp_path / "b.json"
    fp.write_text(json.dumps(FHIR), encoding="utf-8")
    cm = tmp_path / "cm.json"
    cm.write_text("{}", encoding="utf-8")
    assert main(["score-fhir", "--bundle", str(bundle), "--fhir", str(fp), "--concept-map", str(cm), "--explain"]) == 0
    txt = capsys.readouterr().out
    out = json.loads(txt[txt.index("{"):])
    assert out["top_drivers"] and out["fhir"]["encounter_id"] == "e1"
    bad = tmp_path / "bad.json"
    bad.write_text("{\"resourceType\": \"Patient\"}", encoding="utf-8")
    assert main(["score-fhir", "--bundle", str(bundle), "--fhir", str(bad)]) == 1

    feats = tmp_path / "f.csv"
    pd.DataFrame({"lab_creatinine": np.full(40, 88.0), "heart_rate": np.full(40, 80.0)}).to_csv(feats, index=False)
    ref = tmp_path / "ref.json"
    ref.write_text("{\"heart_rate\": 80}", encoding="utf-8")
    assert main(["unit-check", "--features", str(feats), "--reference", str(ref)]) == 0
    assert main(["unit-check", "--features", str(feats), "--fail-on-flag"]) == 2
    empty = tmp_path / "e.csv"
    pd.DataFrame({"x": [1, 2]}).to_csv(empty, index=False)
    assert main(["unit-check", "--features", str(empty)]) == 0


def test_cli_score_encounter_explain_and_report_errors(tmp_path, bundle, capsys):
    import json
    db = tmp_path / "c.duckdb"
    con = duckdb.connect(str(db))
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1950, 0, 0)")
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) "
                "VALUES (1, 1, 9201, DATE '2020-06-01', DATE '2020-06-04', 0)")
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number, measurement_type_concept_id) "
                "VALUES (1, 1, 3016723, DATE '2020-06-03', 3.0, 0)")
    con.close()
    assert main(["score-encounter", "--bundle", str(bundle), "--db", str(db), "--visit-id", "1", "--explain", "--top-k", "1"]) == 0
    txt = capsys.readouterr().out
    assert len(json.loads(txt[txt.index("{"):])["top_drivers"]) == 1
    (tmp_path / "nolog.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    assert main(["report-drift", "--log", str(tmp_path / "nolog.csv"), "--out", str(tmp_path / "x.html")]) == 1


def test_score_cohort_unit_check_warns_and_jsonl_log(tmp_path, bundle):
    from omop_etl.model_bundle import score_cohort
    df = pd.DataFrame({"visit_occurrence_id": range(30), "lab_creatinine": 88.0, "heart_rate": 80.0, "prior_ip_count": 0})
    with pytest.warns(UserWarning, match="unit sanity"):
        score_cohort(bundle, df, on_violation="clip", unit_check=True)
    log = pd.DataFrame({"model_version": ["1", "1"], "month": ["2024-01", "2024-02"], "psi": [0.05, 0.3],
                        "brier": [0.1, 0.12], "status": ["ok", "drift"]})
    lp = tmp_path / "monitoring.jsonl"
    log.to_json(lp, orient="records", lines=True)
    page = write_drift_report(lp, tmp_path / "j.html").read_text(encoding="utf-8")
    assert "2024-02" in page and "drift" in page
