"""Unit tests for Statistical Leakage, Informative Missingness, Provenance, and Overlap."""

import os
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl.integrity import (
    leakage_scan,
    add_missing_indicators,
    missingness_by_outcome,
    provenance,
    check_vocabulary_provenance,
    numbers_of_record,
    scan_document_numbers,
    vocabulary_preflight,
    generate_site_patient_tokens,
    consortium_patient_overlap,
    planned_readmission_filter,
    observation_period_diagnostics,
)


@pytest.fixture
def mock_cdm_integrity():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE concept (
            concept_id INTEGER PRIMARY KEY,
            concept_name VARCHAR,
            domain_id VARCHAR,
            vocabulary_id VARCHAR,
            concept_class_id VARCHAR,
            standard_concept VARCHAR,
            concept_code VARCHAR,
            valid_start_date DATE,
            valid_end_date DATE,
            invalid_reason VARCHAR
        );
        CREATE TABLE concept_ancestor (
            ancestor_concept_id INTEGER,
            descendant_concept_id INTEGER,
            min_levels_of_separation INTEGER,
            max_levels_of_separation INTEGER
        );
        CREATE TABLE vocabulary (
            vocabulary_id VARCHAR PRIMARY KEY,
            vocabulary_name VARCHAR,
            vocabulary_reference VARCHAR,
            vocabulary_version VARCHAR,
            vocabulary_concept_id INTEGER
        );
        CREATE TABLE person (
            person_id BIGINT PRIMARY KEY,
            gender_concept_id INTEGER,
            year_of_birth INTEGER,
            race_concept_id INTEGER,
            ethnicity_concept_id INTEGER
        );
        CREATE TABLE visit_occurrence (
            visit_occurrence_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            visit_concept_id INTEGER,
            visit_start_date DATE,
            visit_end_date DATE,
            visit_type_concept_id INTEGER
        );
        CREATE TABLE condition_occurrence (
            condition_occurrence_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            condition_concept_id INTEGER,
            condition_start_date DATE,
            condition_type_concept_id INTEGER
        );
        CREATE TABLE drug_exposure (
            drug_exposure_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            drug_concept_id INTEGER,
            drug_exposure_start_date DATE,
            drug_type_concept_id INTEGER
        );
        CREATE TABLE procedure_occurrence (
            procedure_occurrence_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            procedure_concept_id INTEGER,
            procedure_date DATE,
            procedure_type_concept_id INTEGER
        );
        CREATE TABLE measurement (
            measurement_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            measurement_concept_id INTEGER,
            measurement_date DATE,
            measurement_type_concept_id INTEGER
        );
        CREATE TABLE observation_period (
            observation_period_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            observation_period_start_date DATE,
            observation_period_end_date DATE,
            period_type_concept_id INTEGER
        );
    """)

    # Populate vocabulary releases
    con.execute("""
        INSERT INTO vocabulary VALUES
        ('None', 'OMOP Standard', '', 'v5.0 2023-10-01', 0),
        ('RxNorm', 'RxNorm', '', 'RxNorm 2023-11-06', 0),
        ('SNOMED', 'SNOMED CT', '', 'SNOMED 2023-09-01', 0),
        ('LOINC', 'LOINC', '', 'LOINC 2.76', 0),
        ('ATC', 'WHO ATC', '', 'ATC 2023-01-01', 0);
    """)

    # Populate concepts & ATC ancestors
    con.execute("""
        INSERT INTO concept VALUES
        (21602722, 'Metformin ATC', 'Drug', 'ATC', 'ATC 4th', 'C', 'A10BA', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (6809, 'Metformin Ingredient', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (4048386, 'Chemotherapy procedure', 'Procedure', 'SNOMED', 'Procedure', 'S', '12345', DATE '1970-01-01', DATE '2099-12-31', NULL),
        (3004410, 'Glucose measurement', 'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7', DATE '1970-01-01', DATE '2099-12-31', NULL);

        INSERT INTO concept_ancestor VALUES
        (21602722, 6809, 1, 1);
    """)

    # 10 test patients
    for pid in range(1, 11):
        con.execute(f"INSERT INTO person VALUES ({pid}, 8507, 1980, 8527, 38003563);")
        # Observation period 2 years prior
        con.execute(f"INSERT INTO observation_period VALUES ({pid}, {pid}, DATE '2021-01-01', DATE '2024-01-01', 44814724);")
        # Visit & events
        con.execute(f"INSERT INTO visit_occurrence VALUES ({100 + pid}, {pid}, 9201, DATE '2023-05-01', DATE '2023-05-05', 44818518);")
        con.execute(f"INSERT INTO condition_occurrence VALUES ({200 + pid}, {pid}, 201826, DATE '2021-06-01', 32020);")
        con.execute(f"INSERT INTO drug_exposure VALUES ({300 + pid}, {pid}, 6809, DATE '2022-01-01', 38000177);")

    # Patient 1 has a planned chemotherapy procedure within 30 days of 2023-05-05
    con.execute("INSERT INTO procedure_occurrence VALUES (501, 1, 4048386, DATE '2023-05-15', 38000180);")

    yield con
    con.close()


def test_leakage_scan():
    n = 100
    y = np.array([1] * 50 + [0] * 50)

    # Feature 1: Perfect leakage (AUC = 1.0)
    f_leak = np.array([10.0] * 50 + [0.0] * 50)

    # Feature 2: High AUC (0.99)
    f_high = np.concatenate([np.random.normal(5, 0.1, 50), np.random.normal(0, 0.1, 50)])

    # Feature 3: Normal feature (AUC ~ 0.5)
    np.random.seed(42)
    f_norm = np.random.normal(0, 1, n)

    # Feature 4: Zero variance
    f_zero = np.zeros(n)

    X = pd.DataFrame({
        "f_leak": f_leak,
        "f_high": f_high,
        "f_norm": f_norm,
        "f_zero": f_zero,
    })

    audit = leakage_scan(X, y, max_auc=0.98)
    assert not audit.empty
    assert "pure_stratum_separation" in audit["check"].values
    assert "high_auc" in audit["check"].values
    assert "zero_variance" in audit["check"].values

    # f_norm should not be flagged
    norm_rows = audit[audit["feature"] == "f_norm"]
    assert not norm_rows["flagged"].any()


def test_add_missing_indicators_and_missingness_by_outcome():
    df = pd.DataFrame({
        "feat_a": [1.0, np.nan, 3.0, np.nan, 5.0, 6.0, np.nan, 8.0, 9.0, 10.0],
        "feat_b": [np.nan] * 5 + [2.0] * 5,
        "outcome": [0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
        "site": ["SiteA"] * 5 + ["SiteB"] * 5,
    })

    # Add missing indicators
    ind_df = add_missing_indicators(df, cols=["feat_a"])
    assert "miss_feat_a" in ind_df.columns
    assert ind_df["miss_feat_a"].sum() == 3

    # Missingness by outcome
    miss_table = missingness_by_outcome(
        df,
        outcome_col="outcome",
        cols=["feat_a", "feat_b"],
        by="site",
        fill_rule_notes={"feat_b": "imputed 0"},
    )
    assert not miss_table.empty
    assert "feat_b" in miss_table["feature"].values
    # In feat_b, outcome 0 is 100% missing, outcome 1 is 0% missing -> large gap
    b_all = miss_table[(miss_table["site"] == "ALL") & (miss_table["feature"] == "feat_b")].iloc[0]
    assert b_all["flagged"]
    assert b_all["fill_rule"] == "imputed 0"


def test_provenance_and_preflight(mock_cdm_integrity):
    prov = provenance(mock_cdm_integrity, seed=123)
    assert prov["package_version"] in ("0.6.4", "0.6.5")
    assert "RxNorm" in prov["vocabularies"]

    # Preflight check
    report = vocabulary_preflight(mock_cdm_integrity, required=["RxNorm", "ATC", "LOINC", "SNOMED"], fail_fast=True)
    assert report["passed"]
    assert report["vocabularies"]["RxNorm"] > 0
    assert report["atc_counts"]["total_descendants"] > 0


def test_numbers_of_record_and_scan(tmp_path):
    c1 = pd.DataFrame({"outcome": [1, 0, 1, 0, 0], "site": ["SiteA"] * 3 + ["SiteB"] * 2})
    c2 = pd.DataFrame({"outcome": [1, 1, 0], "site": ["SiteA"] * 3})

    cohorts = {"Cohort1": c1, "Cohort2": c2}
    manifest_path = os.path.join(tmp_path, "numbers_manifest.json")
    manifest = numbers_of_record(cohorts, path=manifest_path)

    assert os.path.exists(manifest_path)
    assert manifest["cohorts"]["Cohort1"]["total_n"] == 5
    assert manifest["cohorts"]["Cohort1"]["events"] == 2

    # Scan text containing verified and stale numbers
    prev_manifest = {"cohorts": {"Cohort1": {"total_n": 999, "events": 888}}}
    text = "We studied a total of 5 patients in Cohort1 and observed 2 events. In an older version we had 999 patients."
    scan = scan_document_numbers(text, manifest, previous_manifest=prev_manifest)

    assert not scan.empty
    assert 5 in scan["number"].values
    assert 999 in scan["number"].values
    assert scan[scan["number"] == 5]["status"].iloc[0] == "verified"
    assert scan[scan["number"] == 999]["status"].iloc[0] == "stale"


def test_consortium_patient_overlap():
    salt = "consortium_secret_salt_2026"
    pids_a = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    pids_b = [8, 9, 10, 11, 12, 13, 14, 15, 16]

    tokens_a = generate_site_patient_tokens(pids_a, salt)
    tokens_b = generate_site_patient_tokens(pids_b, salt)

    assert len(tokens_a) == 12
    assert len(tokens_b) == 9

    overlap = consortium_patient_overlap({"SiteA": tokens_a, "SiteB": tokens_b}, min_cell_count=11)
    assert not overlap.empty
    pair = overlap[(overlap["site_1"] == "SiteA") & (overlap["site_2"] == "SiteB")].iloc[0]
    # Shared is 5 (< 11), so suppressed
    assert pair["is_suppressed"]
    assert pair["shared_patients"] == "<11"


def test_planned_readmission_filter_and_observation_diagnostics(mock_cdm_integrity):
    # Create cohort table with index stay end_date
    mock_cdm_integrity.execute("""
        CREATE TABLE cohort AS
        SELECT
            person_id,
            visit_start_date AS cohort_start_date,
            visit_end_date AS cohort_end_date,
            'SiteA' AS site
        FROM visit_occurrence;
    """)

    # Planned readmission filter
    audit, clean_cohort = planned_readmission_filter(
        mock_cdm_integrity, cohort_table="cohort", by="site", target_table="clean_cohort"
    )
    assert not audit.empty
    # Patient 1 has planned chemo procedure on 2023-05-15 (within 30d of discharge 2023-05-05)
    assert audit[audit["site"] == "ALL"]["planned_readmissions_removed"].iloc[0] == 1
    assert len(clean_cohort) == 9

    # Observation period diagnostics
    obs_diag = observation_period_diagnostics(mock_cdm_integrity, cohort_table="cohort", prior_days=365, by="site")
    assert not obs_diag.empty
    assert obs_diag["total_patients"].iloc[0] == 10
    assert obs_diag["rate_obs_period"].iloc[0] == 1.0
