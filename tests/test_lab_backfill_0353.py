"""0.35.3 (run C21): backfill_lab_values fills lab rows without a number from the raw LAB_RESULT_CM file's RAW_RESULT, keeps the
operator of a censored result, and never guesses between two raw values. All data are made up."""
from __future__ import annotations

import duckdb
import pytest

from omop_etl.lab_text import backfill_lab_values

HBA1C = 3004410


def _db(path):
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (3004410, 'Hemoglobin A1c/Hemoglobin.total in Blood', 'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4', DATE '1970-01-01', DATE '2099-12-31', NULL))
        t(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason)""")
    con.execute("CREATE TABLE concept_ancestor AS SELECT concept_id AS ancestor_concept_id, concept_id AS descendant_concept_id, "
                "0 AS min_levels_of_separation, 0 AS max_levels_of_separation FROM concept")
    con.execute("CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR, "
                "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)")
    con.execute("CREATE TABLE person AS SELECT * FROM (VALUES (1, 'P1'), (2, 'P2'), (3, 'P3'), (4, 'P4'), (5, 'P5'), (6, 'P6')) t(person_id, person_source_value)")
    # (id, person, date, datetime, value, source LOINC); the builder lost every RAW_RESULT-only value
    rows = [(1, 1, "2020-06-01", "2020-06-01 00:00:00", None, "4548-4"),   # raw "<4.0" twice (duplicate raw rows): filled, censored
            (2, 2, "2020-06-02", "2020-06-02 00:00:00", None, "LOCAL"),    # source value not a LOINC: matched by the concept's code
            (3, 3, "2020-06-03", "2020-06-03 00:00:00", None, "4548-4"),   # raw 6.1 and 7.2 the same day: ambiguous
            (4, 4, "2020-06-04", "2020-06-04 08:10:00", None, "4548-4"),   # same day, the 08:10 raw row decides: 6.6
            (5, 5, "2020-06-05", "2020-06-05 00:00:00", None, "4548-4"),   # raw "POSITIVE": not parseable
            (6, 6, "2020-06-06", "2020-06-06 00:00:00", None, "4548-4"),   # no raw row
            (7, 1, "2020-06-07", "2020-06-07 00:00:00", 5.9, "4548-4"),    # already has a number: untouched
            (8, 2, "2021-06-01", "2021-06-01 00:00:00", None, "4548-4")]   # outside start/end
    con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 1, 9201, DATE '2020-05-31', DATE '2020-06-03'), "
                "(2, 2, 9202, DATE '2020-06-02', DATE '2020-06-02')) t(visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date)")
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id BIGINT, measurement_date DATE, "
                "measurement_datetime TIMESTAMP, value_as_number DOUBLE, measurement_source_value VARCHAR, operator_concept_id BIGINT)")
    con.executemany("INSERT INTO measurement VALUES (?, ?, 3004410, ?, ?, ?, ?, 0)", [list(r) for r in rows])
    con.close()


RAW = """LAB_RESULT_CM_ID,PATID,LAB_LOINC,RESULT_DATE,RESULT_TIME,SPECIMEN_DATE,RESULT_NUM,RAW_RESULT
1,P1,4548-4,2020-06-01,,,,<4.0
2,P1,4548-4,06/01/2020,,,,< 4.0
3,P2,4548-4,02JUN2020,,,,>14.0
4,P3,4548-4,2020-06-03,,,,6.1
5,P3,4548-4,2020-06-03,,,,7.2
6,P4,4548-4,2020-06-04,8:10,,,6.6
7,P4,4548-4,2020-06-04,14:00,,,7.0
8,P5,4548-4,,,2020-06-05,,POSITIVE
9,P1,4548-4,2020-06-07,,,5.9,
10,P2,4548-4,2021-06-01,,,,6.0
"""


@pytest.fixture()
def site(tmp_path):
    _db(tmp_path / "omop.duckdb")
    (tmp_path / "LAB_RESULT_CM.csv").write_text(RAW)
    return tmp_path


def test_dry_run_counts_and_read_only(site):
    con = duckdb.connect(str(site / "omop.duckdb"), read_only=True)
    r = backfill_lab_values(con, site / "LAB_RESULT_CM.csv", names=["hba1c"], start="2020-01-01", end="2020-12-31",
                            dry_run=True).set_index("name").loc["hba1c"]
    assert (r["null_rows"], r["filled"], r["filled_censored"], r["filled_in_stay"], r["ambiguous"], r["not_parseable"], r["no_match"]) == (6, 3, 2, 1, 1, 1, 1)
    assert r["pct_filled"] == 50.0
    assert con.execute("SELECT count(*) FROM measurement WHERE value_as_number IS NULL").fetchone()[0] == 7
    con.close()


def test_fill_writes_value_and_operator(site):
    con = duckdb.connect(str(site / "omop.duckdb"))
    backfill_lab_values(con, site / "LAB_RESULT_CM.csv", names=["hba1c"], start="2020-01-01", end="2020-12-31")
    got = {i: (v, op) for i, v, op in con.execute(
        "SELECT measurement_id, value_as_number, operator_concept_id FROM measurement ORDER BY 1").fetchall()}
    assert got[1] == (4.0, 4171756) and got[2] == (14.0, 4172704) and got[4] == (6.6, 0)
    assert got[3] == (None, 0) and got[5] == (None, 0) and got[6] == (None, 0)
    assert got[7] == (5.9, 0) and got[8] == (None, 0)
    # a second run finds nothing left to fill
    again = backfill_lab_values(con, site / "LAB_RESULT_CM.csv", names=["hba1c"], start="2020-01-01", end="2020-12-31")
    assert int(again["filled"].sum()) == 0
    assert not con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '_blv%'").fetchone()[0]
    con.close()
