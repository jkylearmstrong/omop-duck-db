"""Tests for 30-Day Readmission Cohort, Temporal Feature Extractor,
Concept Rollup & Measurement Aggregator, and Table 1 Generator/Reconciliation.
"""

from pathlib import Path
import sys
import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import (
    aggregate_concept_sets,
    build_readmission_cohort,
    build_schema,
    ensure_cohort_tables,
    extract_measurements,
    extract_temporal_features,
    generate_table1,
    prepare_competing_risks_data,
    validate_table1_reconciliation,
)
from omop_etl.build_omop_cdm import load_macros


@pytest.fixture
def test_cdm():
    con = duckdb.connect(":memory:")
    build_schema(con)
    load_macros(con)
    return con


def test_readmission_cohort_eligibility_and_censoring(test_cdm):
    con = test_cdm
    ensure_cohort_tables(con)

    # 1. Seed Patients
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (101, 8507, 1980, 1, 1, 8527, 38003564),  -- Adult, Readmitted within 30d
            (102, 8532, 1985, 5, 10, 8516, 38003564), -- Adult, Follow-up encounter >= 30d, No readmission
            (103, 8507, 1990, 3, 15, 8527, 38003564), -- Adult, Censored (no events >= 30d)
            (104, 8532, 2015, 6, 20, 8527, 38003564), -- Pediatric (Age < 18)
            (105, 8507, 1970, 8, 12, 8527, 38003564), -- Adult, Same-day stay (LOS = 0)
            (106, 8532, 1975, 4, 5, 8516, 38003564),  -- Adult, Died in hospital (4216643)
            (107, 8507, 1965, 9, 30, 8527, 38003564); -- Adult, In death table during stay
    """)

    # 2. Seed Visits
    con.execute("""
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            -- 101: Eligible index stay + 30d readmission
            (1001, 101, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
            (1002, 101, 9201, DATE '2021-05-15', DATE '2021-05-18', 8536, 32827),
            -- 102: Eligible index stay + OP visit at day 45 (verified follow-up >= 30d)
            (2001, 102, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
            (2002, 102, 9202, DATE '2021-06-18', DATE '2021-06-18', 8536, 32827),
            -- 103: Eligible index stay but lost to follow-up (no encounters >= 30d post-discharge)
            (3001, 103, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
            -- 104: Pediatric stay (Age < 18)
            (4001, 104, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
            -- 105: Same-day stay (LOS = 0)
            (5001, 105, 9201, DATE '2021-05-01', DATE '2021-05-01', 8536, 32827),
            -- 106: Died in hospital
            (6001, 106, 9201, DATE '2021-05-01', DATE '2021-05-04', 4216643, 32827),
            -- 107: Died during stay
            (7001, 107, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827);

        INSERT INTO death (person_id, death_date, death_type_concept_id)
        VALUES (107, DATE '2021-05-03', 32815);
    """)

    # Run cohort generator with require_verified_followup=True
    cohort_df = build_readmission_cohort(
        con,
        cohort_id=1,
        outcome_cohort_id=2,
        washin_days=0,
        require_verified_followup=True,
    )

    # 101 (readmitted) and 102 (subsequent visit at day 45) must be present
    # 103 (lost to follow-up), 104 (pediatric), 105 (LOS=0), 106 (died), 107 (died) must be excluded
    assert len(cohort_df) == 2
    assert set(cohort_df["subject_id"]) == {101, 102}

    p101 = cohort_df[cohort_df["subject_id"] == 101].iloc[0]
    assert p101["outcome_flag"] == 1
    assert p101["los_days"] == 4

    p102 = cohort_df[cohort_df["subject_id"] == 102].iloc[0]
    assert p102["outcome_flag"] == 0
    assert p102["los_days"] == 3

    # Check cohort table population
    c_rows = con.execute("SELECT cohort_definition_id, subject_id FROM cohort ORDER BY cohort_definition_id, subject_id").fetchall()
    assert (1, 101) in c_rows
    assert (1, 102) in c_rows
    # Outcome cohort has subject 101
    assert (2, 101) in c_rows
    assert (2, 102) not in c_rows

    # If require_verified_followup=False, 103 should be included with outcome_flag = 0
    cohort_all_df = build_readmission_cohort(
        con,
        cohort_id=1,
        washin_days=0,
        require_verified_followup=False,
    )
    assert len(cohort_all_df) == 3
    assert set(cohort_all_df["subject_id"]) == {101, 102, 103}


def test_index_selection_sampling_rules(test_cdm):
    con = test_cdm
    ensure_cohort_tables(con)

    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES (201, 8507, 1980, 1, 1, 8527, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            (2001, 201, 9201, DATE '2021-01-10', DATE '2021-01-15', 8536, 32827),
            (2002, 201, 9201, DATE '2021-04-10', DATE '2021-04-14', 8536, 32827),
            (2003, 201, 9201, DATE '2021-08-10', DATE '2021-08-16', 8536, 32827),
            -- Subsequent encounter ensuring verified follow-up for all 3
            (2004, 201, 9202, DATE '2021-10-01', DATE '2021-10-01', 8536, 32827);
    """)

    # Rule: 'first'
    first_df = build_readmission_cohort(con, cohort_id=1, index_selection_rule="first", washin_days=0)
    assert len(first_df) == 1
    assert first_df["visit_occurrence_id"].iloc[0] == 2001

    # Rule: 'last'
    last_df = build_readmission_cohort(con, cohort_id=1, index_selection_rule="last", washin_days=0)
    assert len(last_df) == 1
    assert last_df["visit_occurrence_id"].iloc[0] == 2003

    # Rule: 'random' with seed
    rand_df = build_readmission_cohort(con, cohort_id=1, index_selection_rule="random", random_state=42, washin_days=0)
    assert len(rand_df) == 1
    # Check reproducibility
    rand_df2 = build_readmission_cohort(con, cohort_id=1, index_selection_rule="random", random_state=42, washin_days=0)
    assert rand_df["visit_occurrence_id"].iloc[0] == rand_df2["visit_occurrence_id"].iloc[0]


def test_extract_temporal_features(test_cdm):
    con = test_cdm
    ensure_cohort_tables(con)

    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (301, 8507, 1970, 6, 15, 8527, 38003564),
            (302, 8532, 1950, 2, 20, 8516, 38003563);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
        VALUES 
            -- Prior visits for 301 (Index is 2021-07-01)
            (3001, 301, 9201, DATE '2020-10-01', DATE '2020-10-05', 8536, 32827), -- Prior IP
            (3002, 301, 9201, DATE '2020-10-20', DATE '2020-10-23', 8536, 32827), -- Prior Readmission within 30d of 3001!
            (3003, 301, 9203, DATE '2021-06-01', DATE '2021-06-01', 8536, 32827), -- Prior ED 30 days before index
            (3004, 301, 9202, DATE '2021-06-15', DATE '2021-06-15', 8536, 32827), -- Prior OP 16 days before index
            (3005, 301, 9201, DATE '2021-07-01', DATE '2021-07-06', 8536, 32827), -- INDEX STAY (LOS=5)
            
            -- 302: Index stay with SNF discharge, no prior visits
            (3006, 302, 9201, DATE '2021-08-01', DATE '2021-08-11', 38004284, 32827); -- INDEX STAY (LOS=10, SNF)
    """)

    # Seed index cohort
    con.execute("""
        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        VALUES 
            (10, 301, DATE '2021-07-01', DATE '2021-07-06'),
            (10, 302, DATE '2021-08-01', DATE '2021-08-11');
    """)

    t_df = extract_temporal_features(con, cohort_table="cohort", cohort_id=10, lookback_days=365)
    assert len(t_df) == 2

    p301 = t_df[t_df["subject_id"] == 301].iloc[0]
    assert p301["index_los"] == 5
    assert p301["discharge_category"] == "Home"
    assert p301["prior_ip_count"] == 2
    assert p301["prior_ed_count"] == 1
    assert p301["prior_op_count"] == 1
    assert p301["prior_visit_count"] == 4
    assert p301["prior_readmissions_30d"] == 1
    assert p301["days_since_prior_encounter"] == 16  # 2021-06-15 to 2021-07-01
    assert p301["days_since_prior_ip_ed"] == 30      # 2021-06-01 to 2021-07-01
    assert p301["gender_name"] == "Male"

    p302 = t_df[t_df["subject_id"] == 302].iloc[0]
    assert p302["index_los"] == 10
    assert p302["discharge_category"] == "SNF/Rehab"
    assert p302["prior_visit_count"] == 0
    assert p302["age_at_admission"] == 71
    assert p302["age_group"] == "65-74"
    assert p302["gender_name"] == "Female"
    assert p302["ethnicity_name"] == "Hispanic"


def test_aggregate_concept_sets_and_measurements(test_cdm):
    con = test_cdm
    ensure_cohort_tables(con)

    # Concept hierarchy
    con.execute("""
        INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
        VALUES 
            (1503297, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '2000-01-01', DATE '2099-12-31'),
            (19001409, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860975', DATE '2000-01-01', DATE '2099-12-31'),
            (3094, 'Blood urea nitrogen', 'Measurement', 'LOINC', 'Lab Test', 'S', '3094-0', DATE '2000-01-01', DATE '2099-12-31'),
            (6299, 'BUN alternate', 'Measurement', 'LOINC', 'Lab Test', 'S', '6299-2', DATE '2000-01-01', DATE '2099-12-31');

        INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
        VALUES 
            (1503297, 1503297, 0, 0),
            (1503297, 19001409, 1, 1);

        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        VALUES 
            (10, 401, DATE '2021-05-10', DATE '2021-05-15'),
            (10, 402, DATE '2021-06-01', DATE '2021-06-05');

        -- Drug exposures (Metformin for 401)
        INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
        VALUES (1, 401, 19001409, DATE '2021-04-01', DATE '2021-04-30', 32838);

        -- Measurements for 401 during stay (two BUN values)
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, value_as_number, measurement_type_concept_id)
        VALUES 
            (10, 401, 3094, DATE '2021-05-10', TIMESTAMP '2021-05-10 08:00:00', 25.0, 32817),
            (11, 401, 6299, DATE '2021-05-14', TIMESTAMP '2021-05-14 16:00:00', 18.0, 32817);
    """)

    # 1. Concept rollup
    drug_df = aggregate_concept_sets(
        con,
        cohort_table="cohort",
        cohort_id=10,
        concept_sets={"metformin": [1503297]},
        domain="drug",
        lookback_days=365,
        as_flag=True,
    )
    assert len(drug_df) == 2
    assert drug_df[drug_df["subject_id"] == 401]["metformin"].iloc[0] == 1
    assert drug_df[drug_df["subject_id"] == 402]["metformin"].iloc[0] == 0

    # 2. Measurement extraction with last_before_discharge
    meas_df = extract_measurements(
        con,
        cohort_table="cohort",
        cohort_id=10,
        loinc_map={"bun": ["3094-0", "6299-2"]},
        strategy="last_before_discharge",
        window="stay",
    )
    assert len(meas_df) == 2
    # For 401, last measurement before discharge was 18.0 on 2021-05-14
    val_401 = meas_df[meas_df["subject_id"] == 401]["bun"].iloc[0]
    assert val_401 == 18.0

    # For 402, no measurement -> NaN
    val_402 = meas_df[meas_df["subject_id"] == 402]["bun"].iloc[0]
    assert pd.isna(val_402)

    # Strategy: first_on_admission
    first_df = extract_measurements(
        con,
        cohort_table="cohort",
        cohort_id=10,
        loinc_map={"bun": ["3094-0", "6299-2"]},
        strategy="first_on_admission",
        window="stay",
    )
    assert first_df[first_df["subject_id"] == 401]["bun"].iloc[0] == 25.0


def test_generate_table1_and_reconciliation():
    # Synthetic patient feature data
    data = pd.DataFrame({
        "subject_id": [1, 2, 3, 4, 5, 6, 7, 8],
        "outcome_flag": [0, 0, 0, 0, 1, 1, 1, 1],
        "age": [50.0, 55.0, 60.0, 65.0, 70.0, 72.0, 75.0, 80.0],
        "los": [2.0, 3.0, 4.0, 2.0, 5.0, 6.0, 7.0, 8.0],
        "gender": ["Female", "Male", "Female", "Male", "Female", "Female", "Male", "Male"],
        "bun": [15.0, 18.0, 16.0, None, 28.0, 32.0, None, 35.0],
    })

    res = generate_table1(
        data,
        strata_col="outcome_flag",
        continuous_vars=["age", "los", "bun"],
        categorical_vars=["gender"],
        strata_labels={0: "Non-readmitted", 1: "Readmitted"},
        compute_smd=True,
    )

    t1 = res["table1"]
    t1b = res["table1b"]

    assert "Overall (N=8)" in t1.columns
    assert "Non-readmitted (N=4)" in t1.columns
    assert "Readmitted (N=4)" in t1.columns
    assert "SMD" in t1.columns
    assert "p_value" in t1.columns

    # Verify Table 1b completeness
    bun_miss = t1b[t1b["Variable"] == "bun"].iloc[0]
    assert bun_miss["overall_missing_n"] == 2
    assert bun_miss["overall_missing_pct"] == 25.0

    # Reconciliation validation against itself -> 100% concordant
    rec_pass = validate_table1_reconciliation(t1, t1, tolerance=0.05)
    assert rec_pass["is_concordant"] is True
    assert rec_pass["status"] == "PASS"

    # Reconciliation with simulated drift
    drifted_data = pd.DataFrame({
        "Variable": ["age", "los"],
        "Overall": ["85.0 (5.0)", "12.0 (3.0)"],  # Much higher than original
    })
    rec_drift = validate_table1_reconciliation(t1, drifted_data, tolerance=0.05)
    assert rec_drift["is_concordant"] is False
    assert rec_drift["status"] == "DRIFT_DETECTED"


def test_prepare_competing_risks_data(test_cdm):
    con = test_cdm
    ensure_cohort_tables(con)

    # Seed 3 patients:
    # Patient 1: Readmitted within 10 days post-discharge (Status 1)
    # Patient 2: Dies 15 days post-discharge without readmission (Status 2 - Competing Risk)
    # Patient 3: Alive and event-free through 30 days (Status 0 - Censored)
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (201, 8507, 1970, 1, 1, 8527, 38003564),
            (202, 8532, 1965, 1, 1, 8516, 38003564),
            (203, 8507, 1980, 1, 1, 8527, 38003564);

        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES 
            -- Index stays (discharge on 2024-01-10)
            (2001, 201, 9201, '2024-01-01', '2024-01-10', 44818518),
            (2002, 202, 9201, '2024-01-01', '2024-01-10', 44818518),
            (2003, 203, 9201, '2024-01-01', '2024-01-10', 44818518),
            -- Patient 201 readmission on 2024-01-20 (10 days post-discharge)
            (2004, 201, 9201, '2024-01-20', '2024-01-25', 44818518);

        -- Patient 202 dies post-discharge on 2024-01-25 (15 days post-discharge)
        INSERT INTO death (person_id, death_date, death_type_concept_id)
        VALUES (202, '2024-01-25', 38003565);

        -- Materialize index cohort
        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        VALUES 
            (50, 201, '2024-01-01', '2024-01-10'),
            (50, 202, '2024-01-01', '2024-01-10'),
            (50, 203, '2024-01-01', '2024-01-10');
    """)

    df, summary = prepare_competing_risks_data(con, cohort_table="cohort", cohort_id=50, followup_window_days=30)

    assert len(df) == 3
    assert summary["total_patients"] == 3
    assert summary["n_readmissions"] == 1
    assert summary["n_competing_deaths"] == 1
    assert summary["n_censored"] == 1
    assert summary["n_composite_events"] == 2
    assert pytest.approx(summary["composite_rate"], 0.01) == 2 / 3

    # Check row-level details
    row_201 = df[df["subject_id"] == 201].iloc[0]
    assert row_201["status"] == 1
    assert row_201["time_days"] == 10
    assert row_201["event_type"] == "readmission"
    assert row_201["composite_event"] == 1

    row_202 = df[df["subject_id"] == 202].iloc[0]
    assert row_202["status"] == 2
    assert row_202["time_days"] == 15
    assert row_202["event_type"] == "death_competing"
    assert row_202["composite_event"] == 1

    row_203 = df[df["subject_id"] == 203].iloc[0]
    assert row_203["status"] == 0
    assert row_203["time_days"] == 30
    assert row_203["event_type"] == "censored"
    assert row_203["composite_event"] == 0

