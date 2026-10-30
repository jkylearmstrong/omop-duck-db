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


def test_top_level_reexports_and_glucose_loinc():
    import omop_etl
    from omop_etl.fhir import LOINC_TO_FEATURE
    from omop_etl.velocity import VELOCITY_CONCEPTS

    for name in ("score_fhir_bundle", "write_drift_report", "check_unit_sanity"):
        assert getattr(omop_etl, name) and name in omop_etl.__all__
    assert LOINC_TO_FEATURE["2345-7"] == LOINC_TO_FEATURE["2339-0"] == "lab_glucose"
    assert VELOCITY_CONCEPTS["lab_glucose"] == (3004501, 3000483)


def test_fhir_history_encounters_feed_utilization_features(bundle):
    import copy
    b = copy.deepcopy(FHIR)
    b["entry"] += [{"resource": {"resourceType": "Encounter", "id": f"h{i}", "class": {"code": "IMP"},
                                 "period": {"start": s, "end": e}}}
                   for i, (s, e) in enumerate([("2023-12-01", "2023-12-04"), ("2023-10-01", "2023-10-03")])]
    b["entry"].append({"resource": {"resourceType": "Encounter", "id": "nostart", "class": {"code": "IMP"}}})  # skipped
    r = score_fhir_bundle(b, bundle)
    assert r["fhir"]["encounter_id"] == "e1"
    assert r["features"]["prior_ip_count"] == 2


def test_fhir_bp_panel_components_are_extracted(bundle):
    import copy
    b = copy.deepcopy(FHIR)
    comp = lambda code, v: {"code": {"coding": [{"system": "http://loinc.org", "code": code}]},
                            "valueQuantity": {"value": v, "unit": "mmHg"}}
    b["entry"].append({"resource": {"resourceType": "Observation", "effectiveDateTime": "2024-01-04",
                                    "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
                                    "component": [comp("8480-6", 130), comp("8462-4", 80)]}})
    assert score_fhir_bundle(b, bundle)["fhir"]["observations_skipped"] == 0


def _vocab():
    import duckdb
    v = duckdb.connect(":memory:")
    v.execute("CREATE TABLE concept (concept_id INT, concept_name VARCHAR, domain_id VARCHAR, vocabulary_id VARCHAR, "
              "concept_class_id VARCHAR, standard_concept VARCHAR, concept_code VARCHAR, invalid_reason VARCHAR)")
    v.execute("CREATE TABLE concept_relationship (concept_id_1 INT, concept_id_2 INT, relationship_id VARCHAR, invalid_reason VARCHAR)")
    v.execute("INSERT INTO concept VALUES (201826, 'T2DM', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', NULL), "
              "(1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', NULL), "
              "(900, 'icd', 'Condition', 'ICD10CM', 'ICD10 code', NULL, 'E11.9', NULL)")
    v.execute("INSERT INTO concept_relationship VALUES (900, 201826, 'Maps to', NULL), (900, 999, 'Maps to', 'D')")
    return v


def test_resolve_codes_standard_and_mapped():
    from omop_etl.fhir import resolve_codes
    got = resolve_codes(_vocab(), ["http://snomed.info/sct|44054006", "http://hl7.org/fhir/sid/icd-10-cm|E11.9",
                                   "http://www.nlm.nih.gov/research/umls/rxnorm|6809", "http://snomed.info/sct|0",
                                   "http://example.org|x"])
    assert got == {"http://snomed.info/sct|44054006": 201826, "http://hl7.org/fhir/sid/icd-10-cm|E11.9": 201826,
                   "http://www.nlm.nih.gov/research/umls/rxnorm|6809": 1503297}


def test_score_fhir_bundle_resolves_codes_from_vocabulary(bundle, tmp_path):
    import duckdb
    r = score_fhir_bundle(FHIR, bundle, vocab_con=_vocab())
    assert r["fhir"]["unmapped_codes"] == []
    path = tmp_path / "v.duckdb"
    v = duckdb.connect(str(path))
    v.execute("CREATE TABLE concept AS SELECT * FROM (VALUES (1, 'x', 'Condition', 'SNOMED', 'c', 'S', '44054006', NULL::VARCHAR)) "
              "t(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, invalid_reason)")
    v.close()
    assert score_fhir_bundle(FHIR, bundle, vocab_db_path=path)["fhir"]["unmapped_codes"] == []
    # an explicit concept_map entry wins over the vocabulary
    assert score_fhir_bundle(FHIR, bundle, vocab_con=_vocab(),
                             concept_map={"http://snomed.info/sct|44054006": 5})["fhir"]["unmapped_codes"] == []


def test_harmonize_contract_units_rescales_named_suspects_only():
    from omop_etl.unit_guard import harmonize_contract_units
    df = pd.DataFrame({"lab_creatinine": np.full(50, 88.4), "lab_glucose": np.full(50, 6.1),
                       "heart_rate": np.full(50, 400.0), "lab_sodium": np.full(50, 139.0)})
    fixed, applied = harmonize_contract_units(df)
    assert fixed["lab_creatinine"].iloc[0] == pytest.approx(1.0, rel=0.02)
    assert fixed["lab_glucose"].iloc[0] == pytest.approx(110, rel=0.02)
    assert (fixed["heart_rate"] == 400.0).all() and (fixed["lab_sodium"] == 139.0).all()  # unexplained / fine: untouched
    assert df["lab_creatinine"].iloc[0] == 88.4  # input not modified
    by = applied.set_index("feature")
    assert by.loc["lab_creatinine", "rescaled"] and by.loc["lab_glucose", "rescaled"] and not by.loc["heart_rate", "rescaled"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert not check_unit_sanity(fixed.drop(columns="heart_rate"))["flag"].any()


def test_export_consort_flowchart_writes_images(tmp_path):
    pytest.importorskip("matplotlib")
    from omop_etl import export_consort_flowchart
    from omop_etl.cohort import ConsortAttrition
    att = ConsortAttrition(pd.DataFrame({"step_number": [1, 2, 3], "step_name": ["Adults", "Has labs", "Index admission"],
                                         "subjects_retained": [1000, 800, 650], "subjects_dropped": [0, 200, 150],
                                         "percent_retained": [100.0, 80.0, 65.0]}))
    png = export_consort_flowchart(att, tmp_path / "out" / "figure1.png", title="Figure 1")
    svg = export_consort_flowchart(att.to_dataframe(), tmp_path / "f.svg")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "650" in svg.read_text(encoding="utf-8") or svg.stat().st_size > 1000
    with pytest.raises(ValueError):
        export_consort_flowchart(ConsortAttrition(att.to_dataframe().iloc[0:0]), tmp_path / "e.png")


def _bundle_with(extra_obs):
    import copy
    b = copy.deepcopy(FHIR)
    for code, vq in extra_obs:
        b["entry"].append({"resource": {"resourceType": "Observation", "effectiveDateTime": "2024-01-04",
                                        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
                                        "valueQuantity": vq}})
    return b


def test_fhir_censored_and_qualitative_values_do_not_abort_scoring(bundle):
    b = _bundle_with([("62238-1", {"value": "> 60", "unit": "mL/min"}),          # eGFR ceiling: censored, kept
                      ("2160-0", {"value": "<0.2", "unit": "mg/dL"}),            # string below the limit: censored
                      ("3094-0", {"value": 12, "comparator": ">", "unit": "mg/dL"}),  # FHIR comparator field
                      ("2951-2", {"value": "negative"}),                          # qualitative: skipped
                      ("2823-3", {"value": "trace"}),                             # qualitative: skipped
                      ("2075-0", {"value": True}), ("2028-9", {"value": "nan"})])  # malformed: skipped
    r = score_fhir_bundle(b, bundle)
    assert r["fhir"]["observations_censored"] == 3
    assert r["fhir"]["observations_skipped"] == 4
    assert score_fhir_bundle(FHIR, bundle)["fhir"]["observations_censored"] == 0


def test_fhir_quantity_value_parsing():
    from omop_etl.fhir import _quantity_value
    assert _quantity_value({"value": 5}) == (5.0, False)
    assert _quantity_value({"value": " >= 90 "}) == (90.0, True)
    assert _quantity_value({"value": "-1.5e-2"}) == (-0.015, False)  # sign and exponent survive (a digit-strip would not)
    assert _quantity_value({"value": "< 0.02"}) == (0.02, True)
    for bad in ("negative", "", "1.2.3", "inf", None, False):
        assert _quantity_value({"value": bad}) is None


def test_fhir_unparseable_dates_are_ignored_not_fatal(bundle):
    import copy
    from omop_etl.fhir import _date
    assert _date("unknown") is None and _date("TBD") is None and _date("null") is None and _date(None) is None
    assert _date("2024-01-04T10:00:00Z") == pd.Timestamp("2024-01-04")
    b = copy.deepcopy(FHIR)
    b["entry"].append({"resource": {"resourceType": "Observation", "effectiveDateTime": "TBD",
                                    "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
                                    "valueQuantity": {"value": 90, "unit": "/min"}}})
    b["entry"].append({"resource": {"resourceType": "Encounter", "id": "bad", "class": {"code": "IMP"},
                                    "period": {"start": "unknown"}}})
    r = score_fhir_bundle(b, bundle)
    assert r["fhir"]["encounter_id"] == "e1" and r["fhir"]["observations_skipped"] >= 1
    b["entry"][0]["resource"]["birthDate"] = "unknown"
    with pytest.raises(ValueError, match="birthDate"):
        score_fhir_bundle(b, bundle)


def test_harmonize_recomputes_derived_labs():
    from omop_etl.unit_guard import harmonize_contract_units
    n = 50
    df = pd.DataFrame({"lab_creatinine": np.full(n, 88.4), "age": np.full(n, 60.0), "gender_concept_id": np.full(n, 8507),
                       "lab_egfr": np.full(n, 1.0),  # stale: computed from the umol/L value, so far below 60
                       "lab_sodium": np.full(n, 139.0), "lab_chloride": np.full(n, 103.0), "lab_bicarbonate": np.full(n, 24.0),
                       "lab_anion_gap": np.full(n, 12.0)})
    fixed, applied = harmonize_contract_units(df)
    assert fixed["lab_creatinine"].iloc[0] == pytest.approx(1.0, rel=0.02)
    assert fixed["lab_egfr"].iloc[0] == pytest.approx(85, abs=8)  # CKD-EPI 2021, 60-year-old man, creatinine 1.0
    assert fixed["lab_anion_gap"].iloc[0] == 12.0  # inputs untouched: reported value kept
    by = applied.set_index("feature")
    assert not by.loc["lab_egfr", "rescaled"] and "recomputed" in by.loc["lab_egfr", "hint"]
    kept, applied_off = harmonize_contract_units(df, recompute_derived=False)
    assert (kept["lab_egfr"] == 1.0).all() and not applied_off["hint"].str.contains("recomputed").any()
    assert (df["lab_egfr"] == 1.0).all()  # input not modified


def test_harmonize_recomputes_anion_gap_from_rescaled_electrolytes():
    from omop_etl.unit_guard import harmonize_contract_units
    df = pd.DataFrame({"lab_sodium": [13.9, 13.9, np.nan], "lab_chloride": [103.0, 103.0, 103.0],
                       "lab_bicarbonate": [24.0, 24.0, 24.0], "lab_anion_gap": [-113.1, -113.1, 7.0]})
    sanity = pd.DataFrame({"feature": ["lab_sodium"], "flag": [True], "hint": ["sodium scaled 1/10"], "suspect_factor": [10.0]})
    fixed, applied = harmonize_contract_units(df, sanity)
    assert fixed["lab_anion_gap"].tolist()[:2] == [12.0, 12.0] and fixed["lab_anion_gap"].iloc[2] == 7.0  # NaN input row kept
    assert "recomputed" in applied.set_index("feature").loc["lab_anion_gap", "hint"]
