"""Test suite for v0.8.0 enhancements and defect fixes:
1. 63-bit signed BIGINT surrogate keys (FR-C8)
2. Staged vitals & regex-guarded datetime parsing (FR-C5/FR-C6)
3. eDERRI diabetes inclusion cohort phenotype (FR-A6)
4. Race-free CKD-EPI 2021 dynamic eGFR and stoichiometric Anion Gap
5. Longitudinal visit tensor extraction for deep sequence models
6. Native DQD person-visit cross-table consistency check (FR-C7)
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl.build_omop_cdm import build_schema
from omop_etl.cohort import define_study_cohort, build_ederri_diabetes_cohort
from omop_etl.tensor import extract_longitudinal_tensors, MAX_LONGITUDINAL_VISITS, MAX_TOKENS_PER_VISIT
from omop_etl.velocity import compute_ckd_epi_2021

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_63bit_surrogate_key_generation():
    con = duckdb.connect()
    build_schema(con)
    with open(REPO_ROOT / "inst" / "sql" / "mapping_macros.sql", "r", encoding="utf-8") as f:
        con.execute(f.read())
    
    # Test hashing with various encounters
    res = con.execute("""
        SELECT 
            pcornet_id('ENC_SITE_D_1234567890') AS k1,
            pcornet_id('ENC_SITE_E_9876543210') AS k2,
            pcornet_id('ENC_SITE_D_1234567891') AS k3
    """).fetchone()
    
    k1, k2, k3 = res
    assert all(isinstance(k, int) for k in (k1, k2, k3))
    assert len({k1, k2, k3}) == 3
    assert all(k >= 0 for k in (k1, k2, k3))

    # Key must be MD5-derived (DuckDB hash() collides on real encounter IDs)
    md5_k = con.execute(
        "SELECT (md5_number_lower('ENC_SITE_D_1234567890') & 9223372036854775807)::BIGINT"
    ).fetchone()[0]
    assert k1 == md5_k

    # No collisions over a large synthetic ID set
    n, d = con.execute(
        "SELECT count(*), count(DISTINCT pcornet_id(i::VARCHAR)) FROM range(5000000) t(i)"
    ).fetchone()
    assert n == d


def test_parse_omop_datetime_single_digit_hour():
    con = duckdb.connect()
    build_schema(con)
    with open(REPO_ROOT / "inst" / "sql" / "mapping_macros.sql", "r", encoding="utf-8") as f:
        con.execute(f.read())
    
    # Single digit hour '1:01'
    dt = con.execute("SELECT parse_omop_datetime('01AUG2002', '1:01')").fetchone()[0]
    assert str(dt) == "2002-08-01 01:01:00"
    
    # Standard format '14:30:00'
    dt2 = con.execute("SELECT parse_omop_datetime('2020-05-15', '14:30:00')").fetchone()[0]
    assert str(dt2) == "2020-05-15 14:30:00"


def test_ckd_epi_2021_calculation():
    # Test clinical reference points
    # 65yo female, Scr 1.0 -> ~62.3 mL/min
    egfr_f = compute_ckd_epi_2021(1.0, 65.0, True)
    assert 60.0 < float(egfr_f) < 65.0
    
    # 65yo male, Scr 1.0 -> ~83.5 mL/min
    egfr_m = compute_ckd_epi_2021(1.0, 65.0, False)
    assert 80.0 < float(egfr_m) < 86.0
    
    # Renal failure Scr 4.0
    egfr_rf = compute_ckd_epi_2021(4.0, 70.0, False)
    assert float(egfr_rf) < 20.0


def test_ederri_diabetes_cohort_phenotyping():
    con = duckdb.connect()
    build_schema(con)
    
    # Patient 1: Diabetic by ICD-10 (E11.9)
    # Patient 2: Diabetic by Metformin order
    # Patient 3: Diabetic by HbA1c 8.2%
    # Patient 4: Non-diabetic (hypertension only)
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (1, 8507, 1960, 8527, 38003564),
            (2, 8532, 1970, 8527, 38003564),
            (3, 8507, 1955, 8527, 38003564),
            (4, 8532, 1980, 8527, 38003564);
            
        -- Inpatient index visits for all 4
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES 
            (101, 1, 9201, DATE '2021-06-01', DATE '2021-06-05', 32827),
            (102, 2, 9201, DATE '2021-06-01', DATE '2021-06-04', 32827),
            (103, 3, 9201, DATE '2021-06-01', DATE '2021-06-06', 32827),
            (104, 4, 9201, DATE '2021-06-01', DATE '2021-06-03', 32827);

        -- Wash-in observation periods
        INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
        VALUES 
            (1, 1, DATE '2020-01-01', DATE '2022-01-01', 32827),
            (2, 2, DATE '2020-01-01', DATE '2022-01-01', 32827),
            (3, 3, DATE '2020-01-01', DATE '2022-01-01', 32827),
            (4, 4, DATE '2020-01-01', DATE '2022-01-01', 32827);

        -- Evidence:
        -- Patient 1: condition E11.9
        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value)
        VALUES (1, 1, 201820, DATE '2021-01-15', 32827, 'E11.9');

        -- Patient 2: drug Metformin
        INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value)
        VALUES (2, 2, 21600712, DATE '2021-02-10', DATE '2021-02-28', 32827, 'Metformin 500mg');

        -- Patient 3: lab HbA1c 8.2%
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, measurement_source_value)
        VALUES (3, 3, 3004410, DATE '2021-03-20', 32827, 8.2, 'HbA1c');

        -- Patient 4: Essential hypertension only
        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value)
        VALUES (4, 4, 320128, DATE '2021-01-10', 32827, 'I10');
    """)

    # Standard generic inpatient cohort should include all 4 patients
    generic_coh = define_study_cohort(con, visit_type="inpatient", require_diabetes=False, target_outcome="none",
                                      followup_days=None, require_verified_followup=False, materialise=False)
    assert len(generic_coh) == 4

    # eDERRI diabetes cohort must include exactly patients 1, 2, and 3 (dropping patient 4)
    dm_coh = build_ederri_diabetes_cohort(con, materialise=False)
    assert len(dm_coh) == 3
    assert set(dm_coh["subject_id"].tolist()) == {1, 2, 3}


def test_longitudinal_sequence_tensor_extraction():
    con = duckdb.connect()
    build_schema(con)
    
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES (1, 8507, 1960, 8527, 38003564);
        
        -- Cohort index visit
        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        VALUES (1, 1, DATE '2022-01-01', DATE '2022-01-05');
        
        -- 2 prior visits
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES 
            (10, 1, 9201, DATE '2021-10-01', DATE '2021-10-05', 32827),
            (20, 1, 9201, DATE '2021-11-01', DATE '2021-11-04', 32827);

        -- Concept events in prior visits
        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, visit_occurrence_id, condition_concept_id, condition_start_date, condition_type_concept_id)
        VALUES 
            (1, 1, 20, 201820, DATE '2021-11-01', 32827),
            (2, 1, 10, 316866, DATE '2021-10-01', 32827);
    """)

    tensor = extract_longitudinal_tensors(con, "cohort")
    assert tensor.shape == (1, MAX_LONGITUDINAL_VISITS, MAX_TOKENS_PER_VISIT)
    # Most recent visit (visit_rank 1 = visit 20) should have token at [0, 0, 0] > 0
    assert tensor[0, 0, 0] > 0
    # Second most recent visit (visit_rank 2 = visit 10) should have token at [0, 1, 0] > 0
    assert tensor[0, 1, 0] > 0
