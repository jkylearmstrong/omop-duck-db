"""0.34.2: drug rows on a non-standard or deprecated concept take its single valid 'Maps to' target (C15 close-out). Synthetic data only."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl.build_omop_cdm import backfill_drug_concepts  # noqa: E402


def test_non_standard_drug_concepts_follow_maps_to():
    con = duckdb.connect()
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (45000001, '00002771501', 'NDC', '11-digit NDC', NULL, '00002771501', NULL),
        (40000002, 'old insulin product', 'RxNorm', 'Branded Drug', NULL, '1242215', 'U'),
        (45000003, 'combo NDC', 'NDC', '11-digit NDC', NULL, '00000000003', NULL),
        (45000004, 'NDC to a deprecated target', 'NDC', '11-digit NDC', NULL, '00000000004', NULL),
        (19078461, 'metformin 500 MG Oral Tablet', 'RxNorm', 'Clinical Drug', 'S', '861007', NULL),
        (1567198, 'insulin lispro', 'RxNorm', 'Ingredient', 'S', '86009', NULL),
        (1503297, 'metformin', 'RxNorm', 'Ingredient', 'S', '6809', NULL),
        (1567000, 'retired drug', 'RxNorm', 'Clinical Drug', 'S', '1', 'D'))
        t(concept_id, concept_name, vocabulary_id, concept_class_id, standard_concept, concept_code, invalid_reason)""")
    con.execute("""CREATE TABLE concept_relationship AS SELECT * FROM (VALUES
        (45000001, 19078461, 'Maps to', NULL), (40000002, 1567198, 'Maps to', NULL),
        (45000003, 1567198, 'Maps to', NULL), (45000003, 1503297, 'Maps to', NULL),   -- two targets: left as is
        (45000004, 1567000, 'Maps to', NULL),                                          -- target not valid: left as is
        (45000001, 1503297, 'Maps to', 'D'))                                           -- a retired mapping is ignored
        t(concept_id_1, concept_id_2, relationship_id, invalid_reason)""")
    con.execute("""CREATE TABLE drug_exposure AS SELECT * FROM (VALUES
        (1, 45000001, 0, 'x'), (2, 40000002, NULL, 'y'), (3, 45000003, 0, 'z'), (4, 45000004, 0, 'w'), (5, 19078461, 0, 'v'),
        (6, 45000001, 555, 'u'))
        t(drug_exposure_id, drug_concept_id, drug_source_concept_id, drug_source_value)""")
    got = backfill_drug_concepts(con)
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT drug_exposure_id, drug_concept_id, drug_source_concept_id FROM drug_exposure").fetchall()}
    assert rows == {1: (19078461, 45000001), 2: (1567198, 40000002), 3: (45000003, 0), 4: (45000004, 0), 5: (19078461, 0),
                    6: (19078461, 555)}   # an existing source concept is kept
    assert got["non_standard_before"] == 5 and got["by_maps_to"] == 3 and got["unmapped_before"] == 0
    assert got["non_standard_after"] == 2   # the two rows with no single valid target


def test_drug_remap_refuses_a_vocabulary_without_maps_to():
    """C16: with an empty (site-local) concept_relationship the remap matched nothing and said nothing; it now stops."""
    import pytest

    con = duckdb.connect()
    con.execute("CREATE TABLE concept AS SELECT 40000002 AS concept_id, NULL AS standard_concept, 'U' AS invalid_reason")
    con.execute("CREATE TABLE concept_relationship (concept_id_1 INTEGER, concept_id_2 INTEGER, relationship_id VARCHAR, invalid_reason VARCHAR)")
    con.execute("CREATE TABLE drug_exposure AS SELECT 1 AS drug_exposure_id, 40000002 AS drug_concept_id, 'y' AS drug_source_value")
    with pytest.raises(ValueError, match="no 'Maps to' rows"):
        backfill_drug_concepts(con)
    assert con.execute("SELECT drug_concept_id FROM drug_exposure").fetchone()[0] == 40000002


def test_n_secondary_dx_counts_the_index_visit_only():
    import pandas as pd

    from omop_etl.benchmark_features import extract_protocol_history_features

    c = duckdb.connect()
    c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,"
              " visit_end_date DATE)")
    c.execute("INSERT INTO visit_occurrence VALUES (100, 1, 9201, DATE '2020-01-10', DATE '2020-01-15'), "
              "(101, 1, 9202, DATE '2020-01-12', DATE '2020-01-12')")
    c.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,"
              " condition_source_value VARCHAR, visit_occurrence_id BIGINT, condition_status_source_value VARCHAR)")
    c.execute("""INSERT INTO condition_occurrence VALUES
        (1, 1, DATE '2020-01-10', 'I50.9', 100, 'P'),   -- principal
        (1, 2, DATE '2020-01-10', 'E11.9', 100, NULL),  -- secondary on the index visit
        (1, 3, DATE '2020-01-12', 'M54.5', 101, NULL),  -- outpatient visit inside the stay: left out since 0.34.2
        (1, 4, DATE '2020-01-13', 'N18.3', NULL, NULL)  -- no visit id: by date""")
    c.execute("CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE)")
    cohort = pd.DataFrame({"row_id": [0], "subject_id": [1], "cohort_start_date": pd.to_datetime(["2020-01-10"]),
                           "cohort_end_date": pd.to_datetime(["2020-01-15"])})
    assert extract_protocol_history_features(c, cohort).loc[0, "n_secondary_dx"] == 3            # no visit id in the cohort: by date
    assert extract_protocol_history_features(c, cohort.assign(visit_occurrence_id=[100])).loc[0, "n_secondary_dx"] == 2


def test_stays_still_in_hospital_are_excluded():
    from omop_etl import build_schema, cohort_attrition, define_study_cohort

    def cdm():
        c = duckdb.connect()
        build_schema(c)
        for pid, src in ((1, "HO"), (2, " sh "), (3, None)):
            c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                      f"VALUES ({pid}, 8507, 1960, 0, 0)")
            c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                      f"observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '2022-01-01', 0)")
            c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                      "visit_type_concept_id, discharged_to_concept_id, discharged_to_source_value) "
                      f"VALUES ({pid}, {pid}, 9201, DATE '2020-03-01', DATE '2020-03-05', 0, 0, {'NULL' if src is None else repr(src)})")
        return c

    kw = dict(visit_type="inpatient", target_outcome="none", washin_days=0, require_verified_followup=False)
    assert sorted(define_study_cohort(cdm(), **kw)["subject_id"]) == [1, 2, 3]           # off unless asked
    out = define_study_cohort(cdm(), still_in_hospital_status="SH", **kw)
    assert sorted(out["subject_id"]) == [1, 3]                                           # a NULL status is kept
    assert "Discharged (not still in hospital)" in cohort_attrition(out).step_name.tolist()
    import pytest

    bare = cdm()
    bare.execute("ALTER TABLE visit_occurrence DROP COLUMN discharged_to_source_value")
    with pytest.warns(UserWarning, match="no discharged_to_source_value"):
        assert sorted(define_study_cohort(bare, still_in_hospital_status=["SH"], **kw)["subject_id"]) == [1, 2, 3]


def test_protocol_and_ederri_cohort_exclude_still_in_hospital():
    import inspect

    from omop_etl.cohort import build_ederri_diabetes_cohort
    from omop_etl.study_run import PROTOCOL_COHORT_PARAMS

    assert PROTOCOL_COHORT_PARAMS["still_in_hospital_status"] == ["SH"]
    assert inspect.signature(build_ederri_diabetes_cohort).parameters["still_in_hospital_status"].default == ("SH",)


def test_zip_readers_restore_leading_zeros():
    from omop_etl.geography import zip5_sql

    c = duckdb.connect()
    got = [c.execute(f"SELECT {zip5_sql('z')} FROM (SELECT CAST(? AS VARCHAR) AS z)", [z]).fetchone()[0]
           for z in ("02108", "02108-1234", "021081234", "8101", "21081234", "081", "", None, "ABCDE", " 08608 ")]
    assert got == ["02108", "02108", "02108", "08101", "02108", None, None, None, None, "08608"]


def test_location_zip_is_five_digits(tmp_path):
    from test_pr104_review import _sample

    from omop_etl import build_schema, load_location, load_person
    from omop_etl.build_omop_cdm import load_macros

    src = _sample(tmp_path, ["1,{pat},HO,PO,Y,Camden,NJ,8101,,01JAN2010,31DEC2012\n",
                             "2,{pat},HO,PO,N,Boston,MA,02108,021081234,01JAN2013,31DEC2014\n"])
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(src))
    load_location(con, str(src))
    assert sorted(r[0] for r in con.execute("SELECT zip FROM location").fetchall()) == ["02108", "08101"]


def test_tobacco_is_the_latest_status_up_to_discharge():
    import pandas as pd

    from omop_etl.benchmark_features import extract_protocol_history_features

    c = duckdb.connect()
    c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,"
              " visit_end_date DATE)")
    c.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,"
              " condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
    c.execute("""INSERT INTO condition_occurrence VALUES
        (1, 1, DATE '2015-01-01', 'F17.210', NULL), (1, 2, DATE '2019-06-01', 'Z87.891', NULL),   -- quit: Former
        (2, 1, DATE '2015-01-01', 'Z87.891', NULL), (2, 2, DATE '2019-06-01', 'F17.210', NULL),   -- relapsed: Current
        (3, 1, DATE '2019-06-01', 'Z87.891', NULL), (3, 2, DATE '2019-06-01', 'F17.210', NULL),   -- same day: the higher level
        (5, 1, DATE '2020-02-01', 'F17.210', NULL)                                                -- after discharge: ignored""")
    c.execute("CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE, value_as_concept_id INT,"
              " value_source_value VARCHAR)")
    c.execute("""INSERT INTO observation VALUES (4, 43054909, DATE '2016-01-01', 45881517, '01'),
                 (4, 43054909, DATE '2019-12-01', 45879404, '04')   -- current, then a later never-smoker status""")
    cohort = pd.DataFrame({"row_id": range(5), "subject_id": range(1, 6), "cohort_start_date": pd.to_datetime(["2020-01-10"] * 5),
                           "cohort_end_date": pd.to_datetime(["2020-01-15"] * 5)})
    assert extract_protocol_history_features(c, cohort)["tobacco"].tolist() == [
        "Former user", "Current user", "Current user", "Never user", "Missing / Unknown"]
