"""v0.8.7: schema-driven DQD catalog catches planted defects and passes a clean CDM."""
import datetime

import duckdb
import pytest

from omop_etl.build_omop_cdm import build_schema
from omop_etl.dqd_catalog import run_dqd_catalog, run_dqd_full

TODAY = datetime.date(2024, 1, 1)


def _con():
    c = duckdb.connect()
    build_schema(c)
    for pid in (1, 2):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, 8507, 1980, 8527, 38003563)")
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
              "visit_type_concept_id) VALUES (1, 1, 9201, DATE '2020-01-05', DATE '2020-01-09', 0)")
    return c


def _by(res, name, tbl, fld=None):
    hits = [r for r in res["CheckResults"] if r["check_name"] == name and r["cdm_table_name"] == tbl
            and (fld is None or r["cdm_field_name"] == fld)]
    assert hits, (name, tbl, fld)
    return hits[0]


def test_clean_cdm_passes_structural_checks():
    res = run_dqd_catalog(_con(), today=TODAY)
    assert len(res["CheckResults"]) > 500
    for name in ("isPrimaryKey", "isForeignKey", "plausibleStartBeforeEnd", "plausibleAfterBirth", "plausibleFutureDate"):
        rows = [r for r in res["CheckResults"] if r["check_name"] == name]
        assert rows and not any(r["failed"] for r in rows), name
    assert res["Overview"]["count_total"] == len(res["CheckResults"])
    assert _by(res, "cdmTable", "visit_occurrence")["execution_status"] == "PASSED"
    assert _by(res, "isRequired", "visit_occurrence", "person_id")["execution_status"] == "PASSED"
    assert _by(res, "isPrimaryKey", "person", "person_id")["execution_status"] == "PASSED"
    assert _by(res, "measureValueCompleteness", "death", "death_date")["execution_status"] == "NOT_APPLICABLE"   # empty table


def test_concept_checks_domain_and_standard_validity():
    c = _con()
    c.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, "
              "valid_start_date, valid_end_date, invalid_reason) VALUES "
              "(9201, 'Inpatient', 'Visit', 'Visit', 'Visit', 'S', 'IP', DATE '1970-01-01', DATE '2099-12-31', NULL), "
              "(201820, 'Diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'x', DATE '1970-01-01', DATE '2099-12-31', NULL), "
              "(1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', 'y', DATE '1970-01-01', DATE '2099-12-31', NULL), "
              "(4216643, 'old death', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'z', DATE '1970-01-01', DATE '2000-01-01', 'D'), "
              "(8536, 'src visit', 'Visit', 'Visit', 'Visit', NULL, 'w', DATE '1970-01-01', DATE '2099-12-31', NULL)")
    c.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
              "condition_type_concept_id) VALUES (1, 1, 201820, DATE '2020-01-01', 0), (2, 1, 1503297, DATE '2020-01-01', 0), "      # 2: drug concept in a condition field
              "(3, 1, 4216643, DATE '2020-01-01', 0), (4, 1, 0, DATE '2020-01-01', 0)")                                              # 3: deprecated
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
              "visit_type_concept_id) VALUES (5, 1, 8536, DATE '2020-01-05', DATE '2020-01-09', 0)")                                # non-standard visit
    res = run_dqd_catalog(c, today=TODAY)
    assert _by(res, "fkDomain", "condition_occurrence", "condition_concept_id")["num_violated_rows"] == 1
    assert _by(res, "isStandardValidConcept", "condition_occurrence", "condition_concept_id")["num_violated_rows"] == 1
    assert _by(res, "isStandardValidConcept", "visit_occurrence", "visit_concept_id")["num_violated_rows"] == 1   # 8536 is not standard
    assert _by(res, "fkDomain", "condition_occurrence", "condition_concept_id")["check_origin"] == "DQD"
    assert not any(r["check_name"] in ("fkDomain", "isStandardValidConcept") for r in run_dqd_catalog(_con(), today=TODAY)["CheckResults"])


def test_planted_defects_are_flagged():
    c = _con()
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
              "visit_type_concept_id) VALUES (1, 99, 9201, DATE '2020-03-05', DATE '2020-03-01', 0), "          # dup PK, orphan person, end < start
              "(2, 1, 0, DATE '1970-01-01', DATE '1970-01-02', 0), (3, 1, 9201, DATE '2030-01-01', DATE '2030-01-02', 0)")  # before birth, future
    c.execute("INSERT INTO death (person_id, death_date, death_type_concept_id) VALUES (1, DATE '2020-02-01', 0)")
    res = run_dqd_catalog(c, today=TODAY)
    assert _by(res, "isPrimaryKey", "visit_occurrence", "visit_occurrence_id")["num_violated_rows"] == 2   # both rows of the duplicated key, as the official DQD counts
    assert _by(res, "isForeignKey", "visit_occurrence", "person_id")["failed"]
    assert _by(res, "plausibleStartBeforeEnd", "visit_occurrence", "visit_start_date")["num_violated_rows"] == 1
    assert _by(res, "plausibleAfterBirth", "visit_occurrence", "visit_start_date")["num_violated_rows"] == 1
    assert _by(res, "plausibleFutureDate", "visit_occurrence", "visit_start_date")["num_violated_rows"] == 1
    assert _by(res, "plausibleBeforeDeath", "visit_occurrence", "visit_start_date")["failed"]   # 2020-03-05 and 2030 > death + 60 d
    assert _by(res, "standardConceptRecordCompleteness", "visit_occurrence", "visit_concept_id")["failed"]   # 1 of 4 rows is 0
    assert res["Overview"]["count_failed"] >= 6


def test_missing_table_missing_column_and_full(tmp_path):
    c = _con()
    c.execute("DROP TABLE care_site")
    res = run_dqd_catalog(c, tables=["care_site", "person"], today=TODAY)
    assert _by(res, "cdmTable", "care_site")["failed"]
    assert {r["cdm_table_name"] for r in res["CheckResults"]} == {"care_site", "person"}
    out = tmp_path / "dqd.json"
    full = run_dqd_full(_con(), out, today=TODAY)
    assert out.exists() and full["Overview"]["count_total"] > 500
    assert any(r["check_id"].startswith("CAT_") for r in full["CheckResults"])


def test_agrees_with_the_official_dqd_on_a_planted_defect_fixture():
    """Golden values from the official DataQualityDashboard (R, DuckDB) run on exactly this data."""
    c = _con()
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
              "visit_type_concept_id) VALUES (1, 99, 9201, DATE '2020-03-05', DATE '2020-03-01', 0), "
              "(2, 1, 0, DATE '1970-01-01', DATE '1970-01-02', 0), (3, 1, 9201, DATE '2030-01-01', DATE '2030-01-02', 0)")
    res = run_dqd_catalog(c, tables=["person", "visit_occurrence"], today=TODAY)
    failed = {(r["check_name"], r["cdm_field_name"]): r["num_violated_rows"] for r in res["CheckResults"]
              if r["cdm_table_name"] == "visit_occurrence" and r["failed"]
              and r["check_name"] in ("isPrimaryKey", "isForeignKey", "plausibleAfterBirth", "plausibleStartBeforeEnd")}
    assert failed == {("isPrimaryKey", "visit_occurrence_id"): 2, ("isForeignKey", "person_id"): 1,
                      ("plausibleAfterBirth", "visit_start_date"): 1, ("plausibleAfterBirth", "visit_end_date"): 1,
                      ("plausibleStartBeforeEnd", "visit_start_date"): 1}
    origin = {r["check_name"]: r["check_origin"] for r in res["CheckResults"]}
    assert origin["isPrimaryKey"] == "DQD" and origin["plausibleFutureDate"] == "omop-duck-db"


def test_gap_class_and_value_checks():
    c = _con()
    c.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, "
              "valid_start_date, valid_end_date) VALUES "
              "(1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', 'a', DATE '1970-01-01', DATE '2099-12-31'), "
              "(40000001, 'metformin 500 mg', 'Drug', 'RxNorm', 'Clinical Drug', 'S', 'b', DATE '1970-01-01', DATE '2099-12-31')")
    # a drug era whose concept is not an ingredient
    c.execute("INSERT INTO drug_era (drug_era_id, person_id, drug_concept_id, drug_era_start_date, drug_era_end_date) VALUES "
              "(1, 1, 1503297, DATE '2020-01-01', DATE '2020-02-01'), (2, 1, 40000001, DATE '2020-01-01', DATE '2020-02-01')")
    # person 2 has no condition_era; both people have conditions? only person 1 has a condition and a condition_era
    c.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
              "condition_type_concept_id) VALUES (1, 1, 201820, DATE '2020-01-01', 0), (2, 2, 201820, DATE '2020-01-01', 0)")
    c.execute("INSERT INTO condition_era (condition_era_id, person_id, condition_concept_id, condition_era_start_date, condition_era_end_date) "
              "VALUES (1, 1, 201820, DATE '2020-01-01', DATE '2020-01-01')")
    # two overlapping observation periods of person 1, one clean period of person 2
    c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, "
              "period_type_concept_id) VALUES (1, 1, DATE '2019-01-01', DATE '2020-06-30', 0), (2, 1, DATE '2020-06-01', DATE '2021-01-01', 0), "
              "(3, 2, DATE '2019-01-01', DATE '2021-01-01', 0)")
    c.execute("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, "
              "drug_type_concept_id, days_supply, quantity, drug_source_concept_id) VALUES "
              "(1, 1, 1503297, DATE '2020-01-01', DATE '2020-02-01', 0, 30, 60, 0), (2, 1, 1503297, DATE '2020-01-01', DATE '2020-02-01', 0, 900, -1, 0)")
    c.execute("UPDATE person SET year_of_birth = 1700 WHERE person_id = 2")
    # a systolic blood pressure of 900 mmHg and one of 120 (concept 3004249, unit 8876), and one with an unmatched unit
    c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, "
              "value_as_number, unit_concept_id) VALUES (1, 1, 3004249, DATE '2020-01-01', 0, 120, 8876), "
              "(2, 1, 3004249, DATE '2020-01-02', 0, 900, 8876), (3, 1, 3004249, DATE '2020-01-03', 0, 900, 0)")
    res = run_dqd_catalog(c, today=TODAY)
    # golden values from the official DataQualityDashboard 2.9.1 (R, DuckDB) run on this data: violated rows and denominator
    gold = {('measurePersonCompleteness', 'visit_occurrence', ''): (1, 2), ('measurePersonCompleteness', 'condition_occurrence', ''): (0, 2), ('measurePersonCompleteness', 'drug_exposure', ''): (1, 2), ('measureConditionEraCompleteness', 'condition_era', ''): (1, 2), ('fkClass', 'drug_era', 'drug_concept_id'): (1, 2), ('sourceConceptRecordCompleteness', 'drug_exposure', 'drug_source_concept_id'): (2, 2), ('plausibleValueLow', 'drug_exposure', 'days_supply'): (0, 2), ('plausibleValueLow', 'drug_exposure', 'quantity'): (1, 2), ('plausibleValueLow', 'person', 'year_of_birth'): (1, 2), ('plausibleValueHigh', 'drug_exposure', 'days_supply'): (1, 2), ('plausibleValueHigh', 'person', 'year_of_birth'): (0, 2)}
    for (name, tbl, fld), (viol, den) in gold.items():
        row = next(r for r in res["CheckResults"] if r["check_name"] == name and r["cdm_table_name"] == tbl and (r["cdm_field_name"] or "") == fld)
        assert (row["num_violated_rows"], row["num_denominator_rows"]) == (viol, den), (name, tbl, fld)
    assert _by(res, "fkClass", "drug_era", "drug_concept_id")["num_violated_rows"] == 1
    assert _by(res, "measureConditionEraCompleteness", "condition_era")["num_violated_rows"] == 1
    assert _by(res, "measureObservationPeriodOverlap", "observation_period")["num_violated_rows"] == 2
    assert _by(res, "plausibleValueLow", "person", "year_of_birth")["num_violated_rows"] == 1
    assert _by(res, "plausibleValueHigh", "drug_exposure", "days_supply")["num_violated_rows"] == 1
    assert _by(res, "plausibleValueLow", "drug_exposure", "quantity")["num_violated_rows"] == 1
    hi = _by(res, "plausibleValueHigh", "measurement", "value_as_number")
    assert (hi["num_violated_rows"], hi["num_denominator_rows"]) == (1, 2)          # the unit-0 row is outside the limit table
    # every drug_exposure_source_concept_id is 0, so it is flagged; all persons have a visit-less table so completeness is per table
    assert _by(res, "sourceConceptRecordCompleteness", "drug_exposure", "drug_source_concept_id")["failed"]
    assert _by(res, "measurePersonCompleteness", "condition_occurrence")["num_violated_rows"] == 0
    assert _by(res, "measurePersonCompleteness", "drug_exposure")["num_violated_rows"] == 1      # person 2 has no drug
    assert all(r["check_origin"] == "DQD" for r in res["CheckResults"] if r["check_name"] in (
        "fkClass", "plausibleValueLow", "plausibleValueHigh", "sourceConceptRecordCompleteness", "measurePersonCompleteness"))
