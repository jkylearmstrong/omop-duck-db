"""v0.8.2: audit gaps (FR-A6, FR-C7, FR-C8, FR-C9, FR-D2)."""
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from omop_etl import check_key_collisions, KeyCollisionError, run_dqd, extract_standard_labs
from omop_etl.build_omop_cdm import build_schema, load_macros, materialize_vocabulary


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pcornet_sample"


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
    from omop_etl.cohort_sql import render_cohort_sql, translate_to_duckdb
    pytest.importorskip("sqlglot")
    return translate_to_duckdb(render_cohort_sql(_COHORTS / name, **params))


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
    common = dict(cdm_database_schema="main", target_database_schema="main", target_cohort_table="cohort")
    for stmt in _render("target_cohort.sql", target_cohort_id=1, followup_days=30, min_age=18, **common):
        con.execute(stmt)
    got = con.execute("SELECT subject_id, cohort_start_date, cohort_end_date FROM cohort WHERE cohort_definition_id = 1"
                      " ORDER BY subject_id").fetchall()
    assert [(r[0], str(r[1])) for r in got] == [(1, "2020-02-04"), (5, "2020-03-05")]
    assert all(r[1] == r[2] for r in got)
    for stmt in _render("outcome_cohort.sql", target_cohort_id=2, **common):
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


def test_portable_cohorts_run_with_cdm_attached_read_only(tmp_path):
    """N4 / FR-D3: the CDM is attached READ_ONLY and the cohort rows are written to a separate database."""
    cdm = tmp_path / "cdm.duckdb"
    c = duckdb.connect(str(cdm))
    build_schema(c)
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
              " VALUES (1, 8507, 1980, 0, 0)")
    c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,"
              " observation_period_end_date, period_type_concept_id) VALUES (1, 1, DATE '2015-01-01', DATE '2021-12-31', 0)")
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
              " visit_end_date, visit_type_concept_id) VALUES (1, 1, 9201, DATE '2020-02-01', DATE '2020-02-04', 0)")
    c.close()

    scratch = duckdb.connect(str(tmp_path / "scratch.duckdb"))
    scratch.execute(f"ATTACH '{cdm.as_posix()}' AS cdm (READ_ONLY)")
    scratch.execute("CREATE TABLE main.cohort (cohort_definition_id INTEGER, subject_id BIGINT,"
                    " cohort_start_date DATE, cohort_end_date DATE)")
    common = dict(cdm_database_schema="cdm.main", target_database_schema="main", target_cohort_table="cohort")
    for stmt in _render("target_cohort.sql", target_cohort_id=1, followup_days=30, min_age=18, **common):
        scratch.execute(stmt)
    for stmt in _render("outcome_cohort.sql", target_cohort_id=2, **common):
        scratch.execute(stmt)
    assert scratch.execute("SELECT COUNT(*) FROM main.cohort WHERE cohort_definition_id = 1").fetchone()[0] == 1
    assert scratch.execute("SELECT COUNT(*) FROM main.cohort WHERE cohort_definition_id = 2").fetchone()[0] == 1
    with pytest.raises(duckdb.Error):  # the CDM really is read-only
        scratch.execute("INSERT INTO cdm.main.cohort (cohort_definition_id, subject_id) VALUES (9, 9)")
    scratch.close()


# ---- v0.8.4 ------------------------------------------------------------------------------------------------
def test_observation_period_ignores_placeholder_and_future_dates():
    from omop_etl.build_omop_cdm import build_observation_period
    con = _con()
    for pid in (1, 2, 3):
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                    f" VALUES ({pid}, 8507, 1980, 0, 0)")
    visits = [(1, 1, "1899-12-30", "1899-12-30"), (2, 1, "2020-01-01", "2020-03-01"),   # person 1: epoch start dropped
              (3, 2, "2021-05-01", "2999-01-01"),                                       # person 2: far-future end dropped
              (4, 3, "1900-01-01", "1900-01-01")]                                       # person 3: only placeholders
    for vid, pid, s, e in visits:
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                    f" visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, 9201, DATE '{s}', DATE '{e}', 0)")
    build_observation_period(con)
    rows = con.execute("SELECT person_id, observation_period_start_date, observation_period_end_date"
                       " FROM observation_period ORDER BY person_id").fetchall()
    assert [(r[0], str(r[1]), str(r[2])) for r in rows] == [(1, "2020-01-01", "2020-03-01"), (2, "2021-05-01", "2021-05-01")]
    build_observation_period(con, max_date="2021-01-01")   # explicit data cutoff
    assert [r[0] for r in con.execute("SELECT person_id FROM observation_period").fetchall()] == [1]
    with pytest.raises(ValueError):
        build_observation_period(con, min_date="not a date")


def test_dqd_plausible_observation_period_flags_epoch_dates():
    con = _con()
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                " VALUES (1, 8507, 1980, 0, 0), (2, 8507, 1980, 0, 0)")
    con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,"
                " observation_period_end_date, period_type_concept_id) VALUES"
                " (1, 1, DATE '1899-12-30', DATE '2020-01-01', 0), (2, 2, DATE '2015-01-01', DATE '2020-01-01', 0)")
    df = pd.DataFrame(run_dqd(con)["CheckResults"])
    row = df[df["check_id"] == "FIELD_PLAUSIBLE_OBSERVATION_PERIOD"].iloc[0]
    assert row["num_violated_rows"] == 1 and row["num_before_lower_bound"] == 1 and bool(row["failed"])
    con.execute("UPDATE observation_period SET observation_period_start_date = DATE '2000-01-01' WHERE person_id = 1")
    df = pd.DataFrame(run_dqd(con)["CheckResults"])
    assert not bool(df[df["check_id"] == "FIELD_PLAUSIBLE_OBSERVATION_PERIOD"].iloc[0]["failed"])


def test_etl_reports_key_check_and_visit_rows_match_distinct_encounters(tmp_path):
    from omop_etl.build_omop_cdm import etl_pcornet
    seen = []
    etl_pcornet(str(FIXTURE), str(tmp_path / "cdm.duckdb"), build_schema_flag=True,
                key_check_callback=seen.extend)
    visit = next(r for r in seen if r["cdm_table"] == "visit_occurrence")
    assert visit["ok"] and visit["lost"] == 0 and visit["id_column"] in ("ENCOUNTERID", "encounterid")
    assert visit["distinct_ids"] == visit["cdm_rows"]          # raw distinct encounters == visit_occurrence rows


def test_etl_key_check_ignore_and_person_check_when_disambiguated(tmp_path):
    from omop_etl.build_omop_cdm import etl_pcornet
    seen = []
    etl_pcornet(str(FIXTURE), str(tmp_path / "a.duckdb"), build_schema_flag=True,
                key_check="ignore", key_check_callback=seen.extend)
    assert seen == []
    etl_pcornet(str(FIXTURE), str(tmp_path / "b.duckdb"), build_schema_flag=True,
                site_id=7, disambiguate_patids=True, key_check_callback=seen.extend)
    person = next(r for r in seen if r["cdm_table"] == "person")
    assert person["ok"] and person["distinct_ids"] == person["cdm_rows"]


def test_cli_build_writes_key_check_steps_to_manifest(tmp_path):
    import json
    from omop_etl.cli import main
    manifest = tmp_path / "run_manifest.json"
    rc = main(["build", "--source", str(FIXTURE), "--target", str(tmp_path / "site.duckdb"),
               "--manifest", str(manifest)])
    assert rc == 0
    steps = [s["step"] for s in json.loads(manifest.read_text())["attrition"]]
    assert any(s.startswith("key_check:encounter") for s in steps)


def test_attach_read_only_resolves_central_vocab_views(tmp_path):
    from omop_etl import attach_read_only
    vocab = tmp_path / "vocab.duckdb"
    v = duckdb.connect(str(vocab))
    v.execute("CREATE TABLE concept AS SELECT 1 AS concept_id, 'a' AS concept_name")
    v.close()
    site = tmp_path / "site.duckdb"
    c = duckdb.connect(str(site))
    c.execute(f"ATTACH '{vocab.as_posix()}' AS central_vocab (READ_ONLY)")
    c.execute("CREATE VIEW concept AS SELECT * FROM central_vocab.concept")
    c.close()

    s = duckdb.connect(str(tmp_path / "scratch.duckdb"))
    attach_read_only(s, str(site))                                    # CDM only: the view cannot resolve
    with pytest.raises(duckdb.Error, match="central_vocab"):
        s.execute("SELECT count(*) FROM cdm.main.concept").fetchone()
    s.close()
    s = duckdb.connect(str(tmp_path / "scratch2.duckdb"))
    aliases = attach_read_only(s, str(site), str(vocab))              # CDM + vocabulary, both read-only
    assert aliases == {"cdm": "cdm", "vocab": "central_vocab"}
    assert s.execute("SELECT count(*) FROM cdm.main.concept").fetchone()[0] == 1
    with pytest.raises(duckdb.Error):
        s.execute("INSERT INTO central_vocab.main.concept VALUES (2, 'b')")
    with pytest.raises(ValueError):
        attach_read_only(s, str(site), cdm_alias="bad alias")
    with pytest.raises(FileNotFoundError):
        attach_read_only(s, str(tmp_path / "nope.duckdb"), cdm_alias="other")
    s.close()


def test_circe_json_is_consistent_with_the_authoritative_sql():
    import json, re
    for name in ("target_cohort", "outcome_cohort"):
        expr = json.loads((_COHORTS / f"{name}.circe.json").read_text(encoding="utf-8"))
        for key in ("ConceptSets", "PrimaryCriteria", "InclusionRules", "EndStrategy", "CollapseSettings"):
            assert key in expr, (name, key)
        ids = sorted(i["concept"]["CONCEPT_ID"] for i in expr["ConceptSets"][0]["expression"]["items"])
        sql = (_COHORTS / f"{name}.sql").read_text(encoding="utf-8")
        sql_ids = sorted(int(x) for x in re.search(r"visit_concept_id IN \(([\d, ]+)\)", sql).group(1).split(","))
        assert ids == sql_ids == [262, 9201]
        assert "AUTHORITATIVE" in sql
    target = json.loads((_COHORTS / "target_cohort.circe.json").read_text(encoding="utf-8"))
    visit = target["PrimaryCriteria"]["CriteriaList"][0]["VisitOccurrence"]
    assert visit["Age"]["Value"] == 18 and target["PrimaryCriteria"]["ObservationWindow"]["PostDays"] == 30
    assert "discharged_to_concept_id" in target["InclusionRules"][0]["description"]   # the documented rule-3 gap


def test_followup_evidence_option_counts_later_events(tmp_path):
    """FR-D7: observation_period must reach discharge + 30 days by default; the option also accepts later events."""
    pytest.importorskip("sqlglot")
    from omop_etl.cohort_sql import run_cohort_sql
    con = duckdb.connect()
    build_schema(con)
    for pid in (1, 2):
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                    f" VALUES ({pid}, 8507, 1980, 0, 0)")
        # observation ends the day after discharge for both: not enough follow-up by the default rule
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,"
                    f" observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '2020-02-05', 0)")
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                    f" visit_end_date, visit_type_concept_id) VALUES ({pid}, {pid}, 9201, DATE '2020-02-01', DATE '2020-02-04', 0)")
    # person 2 has a later outpatient visit 60 days after discharge
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                " visit_end_date, visit_type_concept_id) VALUES (3, 2, 9202, DATE '2020-04-05', DATE '2020-04-05', 0)")
    common = dict(cdm_database_schema="main", target_database_schema="main", target_cohort_table="cohort",
                  followup_days=30, min_age=18)
    run_cohort_sql(con, _COHORTS / "target_cohort.sql", target_cohort_id=1, **common)
    assert con.execute("SELECT COUNT(*) FROM cohort WHERE cohort_definition_id = 1").fetchone()[0] == 0
    run_cohort_sql(con, _COHORTS / "target_cohort.sql", target_cohort_id=2, followup_evidence="observation_period_or_event", **common)
    assert [r[0] for r in con.execute("SELECT subject_id FROM cohort WHERE cohort_definition_id = 2").fetchall()] == [2]


def test_compare_cohort_definitions_reports_counts_overlap_and_outcome_rates():
    pytest.importorskip("sqlglot")
    from omop_etl.cohort_sql import compare_cohort_definitions
    con = duckdb.connect()
    build_schema(con)
    for pid in (1, 2, 3):
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                    f" VALUES ({pid}, 8507, 1980, 0, 0)")
    # p1, p2: observation covers follow-up; p3: only a later visit evidences it (counts under definition B only)
    for pid, end in ((1, "2020-12-31"), (2, "2020-12-31"), (3, "2020-02-05")):
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,"
                    f" observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '{end}', 0)")
    visits = [(1, 1, 9201, "2020-02-01", "2020-02-04"), (2, 2, 9201, "2020-02-01", "2020-02-04"),
              (3, 3, 9201, "2020-02-01", "2020-02-04"), (4, 3, 9202, "2020-04-05", "2020-04-05"),
              (5, 1, 9201, "2020-02-20", "2020-02-22")]            # p1 readmitted 16 days after discharge
    for vid, pid, c, s, e in visits:
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                    f" visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, {c}, DATE '{s}', DATE '{e}', 0)")
    res = compare_cohort_definitions(
        con, _COHORTS / "target_cohort.sql", _COHORTS / "target_cohort.sql", outcome_sql=_COHORTS / "outcome_cohort.sql",
        params_b={"followup_evidence": "observation_period_or_event"})
    s = res["summary"].set_index("definition")
    assert (s.loc["A", "n"], s.loc["B", "n"]) == (2, 3)
    assert s.loc["A", "events"] == 1 and s.loc["A", "outcome_rate"] == 0.5
    o = res["overlap"].iloc[0]
    assert (o["both_count"], o["a_only_count"], o["b_only_count"]) == (2, 0, 1)
    assert o["jaccard_index"] == pytest.approx(2 / 3, abs=1e-3)
    no_outcome = compare_cohort_definitions(con, _COHORTS / "target_cohort.sql", _COHORTS / "target_cohort.sql")
    assert no_outcome["summary"]["events"].isna().all()


def test_observation_period_from_enrollment_merges_spans_and_falls_back_to_events(tmp_path):
    import shutil
    from omop_etl.build_omop_cdm import etl_pcornet
    src = tmp_path / "src"
    shutil.copytree(str(FIXTURE), src)
    (src / "enrollment.csv").write_text(
        "PATID,ENR_START_DATE,ENR_END_DATE,CHART,ENR_BASIS\n"
        "FAKE-P001,2023-01-01,2023-06-30,Y,I\n"
        "FAKE-P001,2023-07-01,2024-12-31,Y,I\n"      # touches the first span: merged into one
        "FAKE-P001,2025-02-01,2025-06-30,Y,I\n"      # after a gap: a second period
        "FAKE-P001,1850-01-01,1850-02-01,Y,I\n", encoding="utf-8")   # placeholder span before min_date: dropped
    db = tmp_path / "cdm.duckdb"
    etl_pcornet(str(src), str(db), build_schema_flag=True, key_check="ignore", enrollment_type_concept_id=32880)
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute("SELECT p.person_source_value, o.observation_period_start_date, o.observation_period_end_date, "
                       "o.period_type_concept_id FROM observation_period o JOIN person p USING (person_id) "
                       "ORDER BY 1, 2").fetchall()
    p1 = [(str(r[1]), str(r[2]), r[3]) for r in rows if r[0] == "FAKE-P001"]
    assert p1 == [("2023-01-01", "2024-12-31", 32880), ("2025-02-01", "2025-06-30", 32880)]
    others = [r for r in rows if r[0] != "FAKE-P001"]
    assert others and all(r[3] == 32827 for r in others)                # event-span fallback for people not enrolled
    assert con.execute("SELECT COUNT(DISTINCT observation_period_id) = COUNT(*) FROM observation_period").fetchone()[0]
    con.close()
    # use_enrollment=True without an ENROLLMENT file fails loudly
    with pytest.raises(FileNotFoundError):
        etl_pcornet(str(FIXTURE), str(tmp_path / "x.duckdb"), build_schema_flag=True,
                    key_check="ignore", use_enrollment=True)


def test_sequence_tensor_can_include_index_stay_and_lab_tokens():
    import numpy as np
    from omop_etl.tensor import extract_longitudinal_tensors
    con = duckdb.connect()
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)"
                " VALUES (1, 8507, 1960, 0, 0)")
    # cohort anchored at ADMISSION: the index stay (visit 30) starts on cohort_start_date
    con.execute("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)"
                " VALUES (1, 1, DATE '2021-12-28', DATE '2022-01-01')")
    for vid, s_, e in ((20, "2021-11-01", "2021-11-04"), (30, "2021-12-28", "2022-01-01")):
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,"
                    f" visit_end_date, visit_type_concept_id) VALUES ({vid}, 1, 9201, DATE '{s_}', DATE '{e}', 0)")
    con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, visit_occurrence_id, condition_concept_id,"
                " condition_start_date, condition_type_concept_id) VALUES (1, 1, 20, 201820, DATE '2021-11-01', 0),"
                " (2, 1, 30, 316866, DATE '2021-12-28', 0)")
    # same lab concept, above and below the reference range during the index stay: two different tokens
    con.execute("INSERT INTO measurement (measurement_id, person_id, visit_occurrence_id, measurement_concept_id, measurement_date,"
                " measurement_type_concept_id, value_as_number, range_low, range_high) VALUES"
                " (1, 1, 30, 3004249, DATE '2021-12-29', 0, 9.0, 1.0, 5.0), (2, 1, 30, 3004249, DATE '2021-12-30', 0, 0.2, 1.0, 5.0)")

    base = extract_longitudinal_tensors(con, "cohort")
    assert base[0, 0, 0] > 0 and (base[0, 1] == 0).all()                    # admission-anchored: index stay dropped
    idx = extract_longitudinal_tensors(con, "cohort", include_index_stay=True)
    assert idx[0, 0, 0] > 0 and idx[0, 1, 0] > 0                            # slot 0 = index stay, slot 1 = earlier visit
    assert idx[0, 1, 0] == base[0, 0, 0] and idx[0, 0, 0] != idx[0, 1, 0]
    no_labs = int((idx[0, 0] > 0).sum())
    labs = extract_longitudinal_tensors(con, "cohort", include_index_stay=True, include_labs=True)
    assert int((labs[0, 0] > 0).sum()) == no_labs + 2                       # above- and below-range are distinct tokens
    assert np.array_equal(extract_longitudinal_tensors(con, "cohort"), base)   # defaults unchanged

    # discharge-anchored cohort: the index stay started earlier, so the default window already has it
    con.execute("UPDATE cohort SET cohort_start_date = DATE '2022-01-01'")
    assert (extract_longitudinal_tensors(con, "cohort")[0, 1] > 0).any()


def test_cli_passes_key_check_and_enrollment_flags(monkeypatch):
    from omop_etl import build_omop_cdm as b

    seen = {}
    monkeypatch.setattr(b, "etl_pcornet", lambda **kw: seen.update(kw))
    monkeypatch.setattr("sys.argv", ["build_omop_cdm.py", "--source-dir", "x", "--key-check", "raise",
                                     "--use-enrollment", "false", "--enrollment-type-concept-id", "32817"])
    b.main()
    assert seen["key_check"] == "raise"
    assert seen["use_enrollment"] is False
    assert seen["enrollment_type_concept_id"] == 32817

    monkeypatch.setattr("sys.argv", ["build_omop_cdm.py", "--source-dir", "x"])
    b.main()
    assert (seen["key_check"], seen["use_enrollment"], seen["enrollment_type_concept_id"]) == ("warn", "auto", 0)
