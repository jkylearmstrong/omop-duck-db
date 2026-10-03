"""Tests for End of Life / Mortality Cohort Builder.
Verifies all 4 mortality paradigms (in_hospital, post_discharge, fixed_window, composite_readmit_or_death),
dual death source ascertainment (death table & discharged_to_concept_id), right-censoring follow-up filters,
cohort table materialization, and alias parity.
"""

from pathlib import Path
import sys
import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import (
    build_end_of_life_cohort,
    build_mortality_cohort,
    build_schema,
    ensure_cohort_tables,
)
from omop_etl.build_omop_cdm import load_macros


@pytest.fixture
def test_cdm():
    con = duckdb.connect(":memory:")
    build_schema(con)
    load_macros(con)
    ensure_cohort_tables(con)
    return con


def test_in_hospital_mortality(test_cdm):
    con = test_cdm

    # Patients:
    # 101: Adult, died in hospital via discharged_to_concept_id = 4216643
    # 102: Adult, died in hospital via death table during stay
    # 103: Adult, survived stay (discharged home 8536)
    # 104: Pediatric, died in hospital (should be excluded due to min_age=18)
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (101, 8507, 1970, 1, 1, 8527, 38003564),
            (102, 8532, 1965, 5, 10, 8516, 38003564),
            (103, 8507, 1980, 3, 15, 8527, 38003564),
            (104, 8532, 2015, 6, 20, 8527, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            (1001, 101, 9201, DATE '2021-05-01', DATE '2021-05-05', 4216643, 32827),
            (2001, 102, 9201, DATE '2021-05-01', DATE '2021-05-06', 8536, 32827),
            (3001, 103, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
            (4001, 104, 9201, DATE '2021-05-01', DATE '2021-05-03', 4216643, 32827);

        INSERT INTO death (person_id, death_date, death_type_concept_id)
        VALUES (102, DATE '2021-05-06', 32815);
    """)

    df = build_end_of_life_cohort(
        con,
        cohort_id=1,
        outcome_cohort_id=2,
        mortality_type="in_hospital",
        washin_days=0,
    )

    # 104 excluded by age
    assert set(df["subject_id"]) == {101, 102, 103}

    p101 = df[df["subject_id"] == 101].iloc[0]
    assert p101["outcome_flag"] == 1
    assert str(p101["outcome_date"])[:10] == "2021-05-05"

    p102 = df[df["subject_id"] == 102].iloc[0]
    assert p102["outcome_flag"] == 1
    assert str(p102["outcome_date"])[:10] == "2021-05-06"

    p103 = df[df["subject_id"] == 103].iloc[0]
    assert p103["outcome_flag"] == 0
    assert p103["outcome_date"] is None or str(p103["outcome_date"]) == "None" or p103["outcome_date"] != p103["outcome_date"]

    # Check outcome cohort definition & cohort table entries
    c_out = con.execute("SELECT subject_id FROM cohort WHERE cohort_definition_id = 2 ORDER BY subject_id;").fetchall()
    assert [r[0] for r in c_out] == [101, 102]


def test_post_discharge_mortality_and_censoring(test_cdm):
    con = test_cdm

    # Patients:
    # 201: Adult, discharged alive day 5, dies at day 20 (within 30d) -> outcome_flag = 1
    # 202: Adult, discharged alive day 5, confirmed OP encounter at day 40, dies at day 50 -> outcome_flag = 0
    # 203: Adult, discharged alive day 5, lost to follow-up (no records >= 30d) -> dropped by censoring
    # 204: Adult, dies in hospital day 5 -> excluded from post-discharge index stays
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (201, 8507, 1975, 1, 1, 8527, 38003564),
            (202, 8532, 1980, 2, 2, 8516, 38003564),
            (203, 8507, 1985, 3, 3, 8527, 38003564),
            (204, 8532, 1970, 4, 4, 8516, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            (2001, 201, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (2002, 202, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (2003, 202, 9202, DATE '2021-06-15', DATE '2021-06-15', 8536, 32827), -- verified follow-up at day 41
            (2004, 203, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827), -- lost to follow-up
            (2005, 204, 9201, DATE '2021-05-01', DATE '2021-05-05', 4216643, 32827); -- died in hospital

        INSERT INTO death (person_id, death_date, death_type_concept_id)
        VALUES 
            (201, DATE '2021-05-20', 32815),
            (202, DATE '2021-06-25', 32815);
    """)

    df = build_end_of_life_cohort(
        con,
        cohort_id=1,
        outcome_cohort_id=2,
        mortality_type="post_discharge",
        mortality_window_days=30,
        washin_days=0,
        require_verified_followup=True,
    )

    # 204 excluded (died in hospital); 203 dropped by right-censoring
    assert set(df["subject_id"]) == {201, 202}

    p201 = df[df["subject_id"] == 201].iloc[0]
    assert p201["outcome_flag"] == 1
    assert str(p201["outcome_date"])[:10] == "2021-05-20"

    p202 = df[df["subject_id"] == 202].iloc[0]
    assert p202["outcome_flag"] == 0


def test_fixed_window_eol(test_cdm):
    con = test_cdm

    # SARD style: 180-day window post-index with gap_days=30
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (301, 8507, 1960, 1, 1, 8527, 38003564),
            (302, 8532, 1962, 2, 2, 8516, 38003564),
            (303, 8507, 1964, 3, 3, 8527, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            (3001, 301, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827),
            (3002, 302, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827),
            (3003, 303, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827);

        -- Observation periods to verify follow-up
        INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
        VALUES 
            (1, 301, DATE '2020-01-01', DATE '2021-12-31', 32817),
            (2, 302, DATE '2020-01-01', DATE '2021-12-31', 32817),
            (3, 303, DATE '2020-01-01', DATE '2021-12-31', 32817);

        INSERT INTO death (person_id, death_date, death_type_concept_id)
        VALUES 
            -- 301: Death at day 15 (inside gap_days=30, so outside window)
            (301, DATE '2021-01-16', 32815),
            -- 302: Death at day 90 (inside gap_days=30 to window=180)
            (302, DATE '2021-04-01', 32815);
            -- 303: Alive
    """)

    df = build_end_of_life_cohort(
        con,
        mortality_type="fixed_window",
        mortality_window_days=180,
        gap_days=30,
        washin_days=0,
    )

    assert set(df["subject_id"]) == {301, 302, 303}

    p301 = df[df["subject_id"] == 301].iloc[0]
    assert p301["outcome_flag"] == 0  # died during gap period, not in outcome window

    p302 = df[df["subject_id"] == 302].iloc[0]
    assert p302["outcome_flag"] == 1
    assert str(p302["outcome_date"])[:10] == "2021-04-01"

    p303 = df[df["subject_id"] == 303].iloc[0]
    assert p303["outcome_flag"] == 0


def test_composite_readmission_or_death(test_cdm):
    con = test_cdm

    # 401: Readmitted at day 10, no death -> outcome_flag = 1, outcome_date = 2021-05-15
    # 402: Dies at day 12, no readmission -> outcome_flag = 1, outcome_date = 2021-05-17
    # 403: Readmitted at day 10, dies at day 20 -> outcome_flag = 1, outcome_date = 2021-05-15 (first event)
    # 404: Survives 30d with confirmed encounter at day 45 -> outcome_flag = 0
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (401, 8507, 1970, 1, 1, 8527, 38003564),
            (402, 8532, 1972, 2, 2, 8516, 38003564),
            (403, 8507, 1974, 3, 3, 8527, 38003564),
            (404, 8532, 1976, 4, 4, 8516, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            -- Index stays: 2021-05-01 to 2021-05-05
            (4001, 401, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (4002, 401, 9201, DATE '2021-05-15', DATE '2021-05-18', 8536, 32827), -- readmit
            (4003, 402, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (4004, 403, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (4005, 403, 9201, DATE '2021-05-15', DATE '2021-05-18', 8536, 32827), -- readmit
            (4006, 404, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (4007, 404, 9202, DATE '2021-06-20', DATE '2021-06-20', 8536, 32827); -- follow-up

        INSERT INTO death (person_id, death_date, death_type_concept_id)
        VALUES 
            (402, DATE '2021-05-17', 32815),
            (403, DATE '2021-05-25', 32815);
    """)

    df = build_end_of_life_cohort(
        con,
        mortality_type="composite_readmit_or_death",
        mortality_window_days=30,
        washin_days=0,
    )

    assert set(df["subject_id"]) == {401, 402, 403, 404}

    p401 = df[df["subject_id"] == 401].iloc[0]
    assert p401["outcome_flag"] == 1
    assert str(p401["outcome_date"])[:10] == "2021-05-15"

    p402 = df[df["subject_id"] == 402].iloc[0]
    assert p402["outcome_flag"] == 1
    assert str(p402["outcome_date"])[:10] == "2021-05-17"

    p403 = df[df["subject_id"] == 403].iloc[0]
    assert p403["outcome_flag"] == 1
    assert str(p403["outcome_date"])[:10] == "2021-05-15"

    p404 = df[df["subject_id"] == 404].iloc[0]
    assert p404["outcome_flag"] == 0


def test_alias_parity_and_sampling(test_cdm):
    con = test_cdm

    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES (501, 8507, 1970, 1, 1, 8527, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            (5001, 501, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827),
            (5002, 501, 9201, DATE '2021-06-01', DATE '2021-06-05', 8536, 32827);

        INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
        VALUES (1, 501, DATE '2020-01-01', DATE '2022-01-01', 32817);
    """)

    df_eol = build_end_of_life_cohort(con, mortality_type="in_hospital", index_selection_rule="first", washin_days=0)
    df_mort = build_mortality_cohort(con, mortality_type="in_hospital", index_selection_rule="first", washin_days=0)

    assert len(df_eol) == 1
    assert df_eol.iloc[0]["visit_occurrence_id"] == 5001
    assert df_eol.equals(df_mort)

    df_last = build_end_of_life_cohort(con, mortality_type="in_hospital", index_selection_rule="last", washin_days=0)
    assert df_last.iloc[0]["visit_occurrence_id"] == 5002
