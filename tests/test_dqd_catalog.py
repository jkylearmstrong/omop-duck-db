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


def test_planted_defects_are_flagged():
    c = _con()
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
              "visit_type_concept_id) VALUES (1, 99, 9201, DATE '2020-03-05', DATE '2020-03-01', 0), "          # dup PK, orphan person, end < start
              "(2, 1, 0, DATE '1970-01-01', DATE '1970-01-02', 0), (3, 1, 9201, DATE '2030-01-01', DATE '2030-01-02', 0)")  # before birth, future
    c.execute("INSERT INTO death (person_id, death_date, death_type_concept_id) VALUES (1, DATE '2020-02-01', 0)")
    res = run_dqd_catalog(c, today=TODAY)
    assert _by(res, "isPrimaryKey", "visit_occurrence", "visit_occurrence_id")["num_violated_rows"] == 1
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
