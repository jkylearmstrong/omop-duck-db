"""v0.8.2: audit gaps (FR-A6, FR-C7, FR-C8, FR-C9, FR-D2)."""
import duckdb
import pandas as pd
import pytest

from omop_etl import check_key_collisions, KeyCollisionError, run_dqd, extract_standard_labs
from omop_etl.build_omop_cdm import build_schema, load_macros, materialize_vocabulary


def _con():
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    return con


def test_key_collision_check_ok_and_forced():
    con = _con()
    con.execute("CREATE TABLE src AS SELECT i::VARCHAR AS encid FROM range(1000) t(i)")
    assert check_key_collisions(con, "src", "encid")["ok"]
    con.execute("INSERT INTO src VALUES ('x'), ('y')")
    r = check_key_collisions(con, "src", "encid", key_expr="CAST(length({col}) AS BIGINT)", on_collision="ignore")
    assert not r["ok"] and r["lost"] > 0
    with pytest.raises(KeyCollisionError):
        check_key_collisions(con, "src", "encid", key_expr="CAST(length({col}) AS BIGINT)", on_collision="raise")


def test_pcornet_id53_fits_double():
    con = _con()
    mx = con.execute("SELECT max(pcornet_id53(i::VARCHAR)) FROM range(100000) t(i)").fetchone()[0]
    assert mx < 2**53


def test_materialize_vocabulary_makes_tables():
    con = _con()
    con.execute("DROP TABLE concept")
    con.execute("CREATE TABLE base AS SELECT 1 AS concept_id, 'a' AS concept_name")
    con.execute("CREATE VIEW concept AS SELECT * FROM base")
    assert materialize_vocabulary(con) == {"concept": 1}
    assert con.execute("SELECT table_type FROM information_schema.tables WHERE table_name='concept'").fetchone()[0] == "BASE TABLE"


def test_dqd_flags_duplicate_visit_ids():
    con = _con()
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1,8507,1950,0,0)")
    for vid in (1, 1, 2):
        con.execute(f"INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES ({vid},1,9201,DATE '2020-01-01',DATE '2020-01-02',0)")
    res = run_dqd(con)
    df = res if isinstance(res, pd.DataFrame) else pd.DataFrame(res["CheckResults"])
    row = df[df["check_id"] == "FIELD_PK_DUPLICATE_VISIT_OCCURRENCE"].iloc[0]
    assert row["num_violated_rows"] == 2 and bool(row["failed"])


def _labs_fixture():
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                " VALUES (1, 8532, 1960, 0, 0)")
    for cid, code in ((3002, "2160-0"), (3003, "77147-7"), (3004, "33037-3")):
        con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code,"
                    f" valid_start_date, valid_end_date) VALUES ({cid}, 'x', 'Measurement', 'LOINC', 'Lab Test', '{code}',"
                    " DATE '1970-01-01', DATE '2099-12-31')")
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                " visit_end_date, visit_type_concept_id) VALUES (1,1,9201,DATE '2020-01-01',DATE '2020-01-05',0)")
    for mid, cid, v in ((1, 3002, 1.0), (2, 3003, 99.0), (3, 3004, 12.0)):
        con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date,"
                    f" measurement_type_concept_id, value_as_number, visit_occurrence_id) VALUES ({mid},1,{cid},DATE '2020-01-02',0,{v},1)")
    con.execute("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)"
                " VALUES (1, 1, DATE '2020-01-01', DATE '2020-01-05')")
    return con


def test_standard_labs_ckd_epi_2021_and_anion_gap():
    from omop_etl.velocity import compute_ckd_epi_2021
    con = _labs_fixture()
    df = extract_standard_labs(con, "cohort", egfr="ckd_epi_2021", anion_gap=True, warn_empty=False)
    assert df["anion_gap"].iloc[0] == 12.0
    expected = float(compute_ckd_epi_2021(1.0, 60, True))
    assert df["egfr"].iloc[0] == pytest.approx(expected)  # computed, not the measured 99
    assert df["egfr"].iloc[0] != 99.0
    meas = extract_standard_labs(con, "cohort", egfr="measured", warn_empty=False)
    assert meas["egfr"].iloc[0] == 99.0


# ---- FR-D1: portable cohort SQL (OHDSI SQL, translated with sqlglot as a stand-in for SqlRender) ----
from pathlib import Path

_COHORTS = Path(__file__).resolve().parent.parent / "cohorts"


def _render(name, **params):
    sql = (_COHORTS / name).read_text(encoding="utf-8")
    for k, v in params.items():
        sql = sql.replace(f"@{k}", str(v))
    sqlglot = pytest.importorskip("sqlglot")
    return [s for s in sqlglot.transpile(sql, read="tsql", write="duckdb") if s.strip()]


def test_portable_cohorts_run_and_pick_earliest_stay():
    con = duckdb.connect()
    build_schema(con)
    people = {1: 1980, 2: 1980, 3: 2015, 4: 1980, 5: 1980}
    for pid, yob in people.items():
        con.execute(f"INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                    f" VALUES ({pid}, 8507, {yob}, 0, 0)")
        end = "2020-12-31" if pid != 4 else "2020-02-01"  # person 4: observation ends before discharge + 30
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,"
                    f" observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '{end}', 0)")
    visits = [  # vid, pid, concept, start, end, discharged_to
        (1, 1, 9201, "2020-03-01", "2020-03-05", None),
        (2, 1, 9201, "2020-02-01", "2020-02-04", None),  # earlier stay -> index
        (3, 2, 9201, "2020-03-01", "2020-03-05", 4216643),
        (4, 3, 9201, "2020-03-01", "2020-03-05", None),
        (5, 4, 9201, "2020-01-10", "2020-01-20", None),
        (6, 5, 262, "2020-03-01", "2020-03-05", None),
        (7, 1, 9203, "2020-01-01", "2020-01-01", None),  # ER-only: not an index or outcome
    ]
    for vid, pid, c, s, e, d in visits:
        dv = "NULL" if d is None else d
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                    " visit_end_date, visit_type_concept_id, discharged_to_concept_id)"
                    f" VALUES ({vid}, {pid}, {c}, DATE '{s}', DATE '{e}', 0, {dv})")
    common = dict(cdm_database_schema="main", cohort_database_schema="main", cohort_table="cohort")
    for stmt in _render("target_cohort.sql", target_cohort_id=1, followup_days=30, min_age=18, **common):
        con.execute(stmt)
    got = con.execute("SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = 1"
                      " ORDER BY subject_id").fetchall()
    assert [(r[0], str(r[1])) for r in got] == [(1, "2020-02-04"), (5, "2020-03-05")]
    assert all(r[1] == r[2] for r in got)
    for stmt in _render("outcome_cohort.sql", outcome_cohort_id=2, **common):
        con.execute(stmt)
    assert con.execute("SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = 2").fetchone()[0] == 6
    # re-running is idempotent
    for stmt in _render("target_cohort.sql", target_cohort_id=1, followup_days=30, min_age=18, **common):
        con.execute(stmt)
    assert con.execute("SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = 1").fetchone()[0] == 2


def test_materialize_vocab_cli(tmp_path):
    from omop_etl.cli import main
    db = tmp_path / "site.duckdb"
    vocab = tmp_path / "vocab.duckdb"
    v = duckdb.connect(str(vocab))
    v.execute("CREATE TABLE concept AS SELECT 1 AS concept_id, 'a' AS concept_name")
    v.close()
    c = duckdb.connect(str(db))
    c.close()
    assert main(["materialize-vocab", str(db), "--vocab", str(vocab)]) == 0
    c = duckdb.connect(str(db))
    # the file opened alone (nothing attached) must resolve concept as a real table
    assert c.execute("SELECT table_type FROM information_schema.tables WHERE table_name = 'concept'").fetchone()[0] == "BASE TABLE"
    c.close()


def test_standard_labs_rejects_bad_egfr_option():
    with pytest.raises(ValueError):
        extract_standard_labs(_labs_fixture(), "cohort", egfr="bogus")


def test_key_check_validates_inputs():
    con = _con()
    with pytest.raises(ValueError):
        check_key_collisions(con, "src", "bad col")
    with pytest.raises(ValueError):
        check_key_collisions(con, "src; DROP TABLE x", "id")
    with pytest.raises(ValueError):
        check_key_collisions(con, "src", "id", on_collision="explode")


def test_stable_random_and_persist_index_visits():
    from omop_etl import define_study_cohort, persist_index_visits
    con = _labs_fixture()
    for vid, s, e in ((2, "2020-02-01", "2020-02-04"), (3, "2020-03-01", "2020-03-05")):
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                    f" visit_end_date, visit_type_concept_id) VALUES ({vid},1,9201,DATE '{s}',DATE '{e}',0)")
    con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,"
                " observation_period_end_date, period_type_concept_id) VALUES (1,1,DATE '2015-01-01',DATE '2021-12-31',0)")
    first = define_study_cohort(con, sampling_rule="earliest", materialise=False, washin_days=None,
                                min_los_days=0, require_verified_followup=False)
    a = define_study_cohort(con, sampling_rule="stable_random", materialise=False, washin_days=None,
                            min_los_days=0, require_verified_followup=False)
    b = define_study_cohort(con, sampling_rule="stable_random", materialise=False, washin_days=None,
                            min_los_days=0, require_verified_followup=False)
    assert a.equals(b) and len(first) == 1 and len(a) == 1
    con.execute("DELETE FROM cohort")
    con.execute("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)"
                " VALUES (1, 1, DATE '2020-02-04', DATE '2020-02-04')")
    assert persist_index_visits(con) == 1
    assert con.execute("SELECT visit_occurrence_id FROM index_visit").fetchone()[0] == 2
