"""Request 2: LOINC fix, bounds/unit harmonization, medication windows, domain profiles, CohortFlow, Table2, PLP arrow."""

import duckdb
import pandas as pd
import pytest

from omop_etl import (
    CohortFlow, MedicationExtractor, Table1Generator, Table2Generator, as_plp_arrow, build_schema,
    filter_bounds, harmonize_measurement_units,
)
from omop_etl.measurement_filter import DEFAULT_PHYSIOLOGIC_BOUNDS
from omop_etl.table1 import DEFAULT_LAB_LOINCS


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    build_schema(c)
    for pid in (1, 2, 3):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, "
                  f"ethnicity_concept_id) VALUES ({pid}, 8507, {1950 if pid < 3 else 2015}, 8527, 38003563)")
    c.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, "
              "valid_start_date, valid_end_date) VALUES (3001, 'WBC', 'Measurement', 'LOINC', 'Lab Test', '6690-2', "
              "DATE '1970-01-01', DATE '2099-12-31'), (3002, 'Creat', 'Measurement', 'LOINC', 'Lab Test', '2160-0', "
              "DATE '1970-01-01', DATE '2099-12-31')")
    yield c
    c.close()


def _visit(c, vid, pid, concept, s, e):
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
              f"visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, {concept}, DATE '{s}', DATE '{e}', 32817)")


def _meas(c, mid, pid, cid, date, val, unit):
    c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
              "measurement_type_concept_id, value_as_number, unit_source_value) VALUES "
              f"({mid}, {pid}, {cid}, DATE '{date}', 32817, {val}, '{unit}')")


def test_loinc_48642_is_egfr_not_anion_gap():
    assert "48642-3" not in DEFAULT_LAB_LOINCS["anion_gap"]
    assert set(DEFAULT_LAB_LOINCS["anion_gap"]) == {"1863-0", "33037-3"}
    assert "48642-3" in DEFAULT_LAB_LOINCS["egfr"]


def test_filter_bounds_wide_long_override_disable():
    df = pd.DataFrame({"wbc": [5.0, 226750000.0, 0.01, 160.0], "sodium": [140.0, 90.0, 150.0, 181.0]})
    out = filter_bounds(df)
    assert out["wbc"].isna().tolist() == [False, True, True, False] and out["sodium"].isna().sum() == 2
    assert df["wbc"].max() == 226750000.0  # input untouched
    assert filter_bounds(df, {"wbc": [0.5, 150.0]})["wbc"].isna().sum() == 3
    assert filter_bounds(df, False).equals(df)
    assert filter_bounds(df, action="drop").shape[0] == 1
    clip = filter_bounds(df, action="clip")
    assert clip["wbc"].max() == DEFAULT_PHYSIOLOGIC_BOUNDS["wbc"][1]
    long = pd.DataFrame({"feature": ["wbc", "wbc", "bmi"], "value_as_number": [5e6, 7.0, 30.0]})
    lo = filter_bounds(long, feature_col="feature")
    assert lo["value_as_number"].isna().tolist() == [True, False, False]
    assert lo.attrs["bounds_report"].set_index("feature").loc["wbc", "n_out_of_range"] == 1


def test_harmonize_units_scales_and_flags(con):
    _meas(con, 1, 1, 3001, "2020-01-02", 7.5, "10*3/uL")
    _meas(con, 2, 1, 3001, "2020-01-03", 7500, "/uL")        # cells/uL -> 7.5
    _meas(con, 3, 1, 3002, "2020-01-02", 88.42, "umol/L")     # -> 1.0 mg/dL
    _meas(con, 4, 1, 3002, "2020-01-03", 2.0, "furlongs")     # unrecognized: untouched + flagged
    rep = harmonize_measurement_units(con, {"wbc": ["6690-2"], "creatinine": ["2160-0"]})
    st = rep.set_index(["feature", "unit"])["status"].to_dict()
    assert st[("wbc", "/uL")] == "scaled" and st[("wbc", "10*3/uL")] == "canonical"
    assert st[("creatinine", "furlongs")] == "unrecognized"
    v = con.execute("SELECT measurement_id, value_as_number FROM measurement_harmonized ORDER BY 1").fetchall()
    assert [round(x[1], 3) for x in v] == [7.5, 7.5, 1.0, 2.0]
    assert con.execute("SELECT value_as_number FROM measurement WHERE measurement_id=2").fetchone()[0] == 7500
    assert len(v) == 4  # no row duplication


def _cohort_setup(con):
    _visit(con, 10, 1, 9201, "2020-06-01", "2020-06-05")
    _visit(con, 11, 2, 9201, "2020-06-01", "2020-06-05")
    _visit(con, 12, 3, 9201, "2020-06-01", "2020-06-05")   # child
    _visit(con, 13, 1, 9203, "2020-07-01", "2020-07-01")   # ER-only
    for pid in (1, 2, 3):
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                    f"observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2010-01-01', "
                    "DATE '2021-01-01', 32817)")
    con.execute("CREATE TABLE hf_cohort (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, "
                "cohort_end_date DATE, readmitted BIGINT)")
    con.execute("INSERT INTO hf_cohort VALUES (1,1,DATE '2020-06-01',DATE '2020-06-05',1),"
                "(1,2,DATE '2020-06-01',DATE '2020-06-05',0)")


def test_cohort_flow_attrition_and_preflight(con):
    _cohort_setup(con)
    f = (CohortFlow(con, anchor="admission", follow_up_days=30)
         .add_step("Adults", "EXTRACT(year FROM v.visit_start_date) - p.year_of_birth >= 18").run())
    a = f.attrition.set_index("step_name")["subjects_retained"]
    assert a["All visits"] == 4 and a["Adults"] == 2 and len(f.cohort) == 2
    assert (f.checks["status"] == "PASS").all()
    assert "digraph" in f.to_graphviz() and "flowchart" in f.to_mermaid() and f.to_markdown()
    # ER-only visits in the allowed set trip the inpatient check
    f2 = CohortFlow(con, visit_concept_ids=(9201, 9203)).run()
    assert (f2.checks.set_index("check").loc["visit types are inpatient (9201) or ER-to-inpatient (262)", "status"]
            == "FAIL")


def test_medication_windows(con):
    _cohort_setup(con)
    con.execute("INSERT INTO concept_ancestor VALUES (500, 600, 1, 1)")
    for did, day in ((1, "2020-03-01"), (2, "2020-06-03"), (3, "2020-06-05"), (4, "2020-06-10")):
        con.execute("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
                    f"drug_exposure_end_date, drug_type_concept_id) VALUES ({did}, 1, 600, DATE '{day}', DATE '{day}', 32817)")
    me = MedicationExtractor(con, {"bb": {"ancestor_concept_ids": [500]}})
    f = lambda w: int(me.extract("hf_cohort", window=w).query("person_id == 1")["bb"].iloc[0])
    assert (f("pre_admission"), f("inpatient"), f("discharge")) == (1, 1, 1)
    con.execute("DELETE FROM drug_exposure WHERE drug_exposure_id IN (1)")
    assert f("pre_admission") == 0
    con.execute("DELETE FROM drug_exposure WHERE drug_exposure_id IN (3)")
    assert f("discharge") == 0 and f("inpatient") == 1
    with pytest.raises(ValueError):
        me.extract("hf_cohort", window="bogus")
    wide = me.extract_all_windows("hf_cohort")
    assert {"bb__pre_admission", "bb__inpatient", "bb__discharge"} <= set(wide.columns)


def test_table1_generator_domain_profile(con):
    _cohort_setup(con)
    _meas(con, 1, 1, 3002, "2020-06-02", 1.2, "mg/dL")
    _meas(con, 2, 2, 3002, "2020-06-02", 884.2, "umol/L")      # 10 mg/dL after harmonization
    _meas(con, 3, 2, 3002, "2020-06-03", 900000, "mg/dL")      # artifact, dropped by bounds
    con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
                "condition_start_date, condition_type_concept_id, condition_source_value) VALUES "
                "(1, 1, 0, DATE '2019-01-01', 32817, 'I50.9')")
    t1 = Table1Generator(con=con)
    t1.add_domain_profile("nephrology")
    t1.medications = {}  # no ATC vocabulary in the fixture
    t1.add_custom_lab("wbc_x", ["6690-2"], bounds=[0.0, 50.0])
    out = t1.generate("hf_cohort", stratify_by="readmitted")
    feats = t1.features.set_index("subject_id")
    assert feats.loc[1, "creatinine"] == pytest.approx(1.2)
    assert feats.loc[2, "creatinine"] == pytest.approx(10.0)  # last value was an artifact -> earlier valid one
    assert "table1" in out and "creatinine" in set(out["table1"]["Variable"])
    hf = Table1Generator(con=con).add_domain_profile("heart_failure")
    hf.medications = {}
    hf.generate("hf_cohort")
    assert hf.features.set_index("subject_id").loc[1, "dx_heart_failure"] == 1
    with pytest.raises(KeyError):
        Table1Generator(con=con).add_domain_profile("nonexistent")


def test_table2_flags():
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0, 4.0], "b": [5.0, 5.0, 5.0, 5.0], "c": [None, None, None, 1.0],
                       "d": [None] * 4})
    r = Table2Generator().generate(df).set_index("variable")
    assert r.loc["b", "flags"] == "ZERO_SD_SENTINEL" and r.loc["c", "flags"] == "HIGH_MISSINGNESS"
    assert "ALL_MISSING" in r.loc["d", "flags"] and r.loc["a", "flags"] == "" and r.loc["a", "median"] == 2.5


def test_as_plp_arrow_stable_rowids(con):
    pytest.importorskip("pyarrow")
    df = pd.DataFrame({"subject_id": [10, 11, 12], "cohort_start_date": pd.to_datetime(["2020-01-01"] * 3),
                       "age": [60.0, 0.0, 70.0], "diabetes": [1, 0, 1], "y": [1, 0, 0]})
    r = as_plp_arrow(con, df, outcome_col="y")
    cov = r.covariates.to_pandas()
    assert len(cov) == 4  # zeros dropped
    ref = r.covariate_ref.to_pandas().set_index("covariateName")["covariateId"].to_dict()
    coh = r.cohorts.to_pandas().set_index("rowId")
    row_of_12 = coh.index[coh.subjectId == 12][0]
    assert cov[(cov.rowId == row_of_12) & (cov.covariateId == ref["age"])].covariateValue.iloc[0] == 70.0
    assert set(r.outcomes.to_pandas().columns) == {"rowId", "outcomeCount"}


# ---------------------------------------------------------------- mapping integrity (audit of the 48642-3 bug class)
def _all_code_maps():
    from omop_etl.features import ACUTE_LAB_PANEL, CORE_14_LAB_PANEL
    from omop_etl.table1 import DEFAULT_MEDICATION_CONCEPTS, DEFAULT_VITAL_LOINCS
    return {"labs": DEFAULT_LAB_LOINCS, "vitals": DEFAULT_VITAL_LOINCS, "core14": CORE_14_LAB_PANEL,
            "acute": ACUTE_LAB_PANEL, "meds": DEFAULT_MEDICATION_CONCEPTS}


def test_no_code_assigned_to_two_features():
    for name, m in _all_code_maps().items():
        seen = {}
        for feat, codes in m.items():
            for c in ([codes] if isinstance(codes, (str, int)) else codes):
                assert c not in seen or name == "meds" and "antihypertensives" in {seen[c], feat}, \
                    f"{name}: {c} mapped to both {seen.get(c)} and {feat}"
                seen[c] = feat
    from omop_etl.table1_profiles import DOMAIN_PROFILES
    for prof, p in DOMAIN_PROFILES.items():
        seen = {}
        for feat, codes in p["labs"].items():
            for c in codes:
                assert seen.get(c, feat) == feat, f"{prof}: {c} in both {seen[c]} and {feat}"
                seen[c] = feat


def test_codes_match_vocabulary_names():
    """Opt-in: OMOP_VOCAB_DB=<duckdb with concept> checks every LOINC / ATC / RxNorm id exists and is on-topic."""
    import os
    path = os.environ.get("OMOP_VOCAB_DB")
    if not path:
        pytest.skip("set OMOP_VOCAB_DB to a DuckDB file containing the OMOP concept table")
    v = duckdb.connect(path, read_only=True)
    expect = {  # concept_id -> substring that must appear in concept_name (lower case)
        21600713: "insulin", 21600745: "biguanide", 1503297: "metformin", 21600749: "sulfonylurea",
        1502855: "tolbutamide", 1502809: "tolazamide", 21601855: "hmg coa", 21601782: "renin-angiotensin",
        21600381: "antihypertensive", 21601664: "beta blocking", 21601744: "calcium channel",
        1314002: "atenolol", 21602722: "corticosteroids", 1506270: "methylprednisolone",
        1539403: "simvastatin", 1545958: "atorvastatin", 1551860: "pravastatin", 21601668: "oxprenolol"}
    for cid, word in expect.items():
        row = v.execute("SELECT concept_name FROM concept WHERE concept_id = ?", [cid]).fetchone()
        assert row and word in row[0].lower(), f"concept {cid}: expected '{word}', found {row}"
    from omop_etl.table1 import DEFAULT_MEDICATION_CONCEPTS
    unguarded = {i for ids in DEFAULT_MEDICATION_CONCEPTS.values() for i in ids} - set(expect)
    assert not unguarded, f"default medication concept ids without a name guard: {sorted(unguarded)}"
    from omop_etl.table1_profiles import DOMAIN_PROFILES
    codes = {c for m in (DEFAULT_LAB_LOINCS,) for cs in m.values() for c in cs}
    codes |= {c for p in DOMAIN_PROFILES.values() for cs in p["labs"].values() for c in cs}
    missing = [c for c in codes if not v.execute(
        "SELECT 1 FROM concept WHERE vocabulary_id='LOINC' AND concept_code=?", [c]).fetchone()]
    assert not missing, f"LOINC codes not in vocabulary: {missing}"


def test_domain_profiles_yaml_matches_python():
    yaml = pytest.importorskip("yaml")
    from pathlib import Path
    from omop_etl.table1_profiles import DOMAIN_PROFILES
    path = Path(__file__).resolve().parent.parent / "inst" / "extdata" / "domain_profiles.yml"
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == DOMAIN_PROFILES, \
        "inst/extdata/domain_profiles.yml (used by R) is out of sync with omop_etl.table1_profiles.DOMAIN_PROFILES"


def test_every_mapped_code_matches_its_feature_name_in_vocabulary():
    """Opt-in (OMOP_VOCAB_DB): every LOINC / ATC code's concept_name must contain a keyword for its feature.

    This is the guard that would have caught LOINC 48642-3 (eGFR) filed under ``anion_gap``.
    Keywords live in tests/fixtures/vocab_feature_keywords.csv (shared with the R tests).
    """
    import csv
    import os
    from pathlib import Path
    from omop_etl.features import ACUTE_LAB_PANEL, CORE_14_LAB_PANEL
    from omop_etl.table1 import DEFAULT_VITAL_LOINCS
    from omop_etl.table1_profiles import DOMAIN_PROFILES
    path = os.environ.get("OMOP_VOCAB_DB")
    if not path:
        pytest.skip("set OMOP_VOCAB_DB to a DuckDB file containing the OMOP concept table")
    fx = Path(__file__).resolve().parent / "fixtures" / "vocab_feature_keywords.csv"
    kw = {(r["kind"], r["name"]): r["keywords"].lower().split("|") for r in csv.DictReader(fx.open(encoding="utf-8"))}
    v = duckdb.connect(path, read_only=True)
    wanted: dict[tuple, set] = {}
    for m in (DEFAULT_LAB_LOINCS, DEFAULT_VITAL_LOINCS, CORE_14_LAB_PANEL, ACUTE_LAB_PANEL):
        for k, cs in m.items():
            wanted.setdefault(("lab", k), set()).update([cs] if isinstance(cs, str) else cs)
    for p in DOMAIN_PROFILES.values():
        for k, cs in p["labs"].items():
            wanted.setdefault(("lab", k), set()).update(cs)
        for k, s in p["medications"].items():
            wanted.setdefault(("med", k), set()).update(s["atc_codes"])
    problems = []
    for (kind, name), codes in wanted.items():
        if (kind, name) not in kw:
            problems.append(f"{kind} '{name}': add it to vocab_feature_keywords.csv")
            continue
        for code in sorted(codes):
            row = v.execute("SELECT concept_name FROM concept WHERE vocabulary_id = ? AND concept_code = ?",
                            ["ATC" if kind == "med" else "LOINC", code]).fetchone()
            if row is None:
                problems.append(f"{kind} '{name}': {code} not in vocabulary")
            elif not any(w in row[0].lower() for w in kw[(kind, name)]):
                problems.append(f"{kind} '{name}': {code} is '{row[0]}'")
    assert not problems, "\n".join(problems)
