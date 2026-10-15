"""Test suite for omop-duck-db v0.6.6 features.

Tests all 10 feature requests and gaps:
1. Drug Type Recognition for Modern EHR Prescriptions (32838) & configurable sets
2. Multi-Anchor Temporal Windows (visit_start_date, visit_end_date, index_date)
3. Laboratory Unit Discordance Detection & Bimodal Normalization
4. Flexible Outlier Strategies (drop, winsorize, flag_indicator)
5. On-the-Fly Vectorized Drug Eras & PDC/MPR Adherence Calculator
6. Competing Risks in Federated Survival Meta-Analysis (CIF, cause-specific, Fine-Gray)
7. Out-of-Core Parquet / Arrow Streaming
8. Dual-Path Comorbidity Scoring with Source-Code Fallbacks & Mapping Fidelity
9. Acute In-Hospital Episode Trajectories (Delta, Peak, Nadir, AKI, Shock Index)
10. In-Encounter LOCF & Leakage-Safe Imputation
"""

import os
import tempfile
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    __version__,
    DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS,
    DEFAULT_ADMINISTERED_TYPE_CONCEPT_IDS,
    DEFAULT_DISPENSED_TYPE_CONCEPT_IDS,
    compute_drug_eras,
    compute_adherence_pdc,
    detect_unit_discordance,
    normalize_units,
    extract_acute_trajectories,
    impute_encounter_observations,
    extract_features,
    extract_patient_features,
    extract_medication_classes,
    MedicationExtractor,
    filter_bounds,
    sanitize_measurements,
    plausibility_report,
    consortium_survival_meta_analysis,
    CohortFlow,
    compare_feature_tables,
    extract_charlson_index,
    extract_elixhauser_comorbidities,
    vocabulary_preflight,
)


def test_v066_version():
    """Verify package version is at least 0.6.6."""
    assert __version__ in ("0.6.6", "0.6.7")


# ---------------------------------------------------------------------------
# 1. Drug Type Recognition for EHR Prescriptions (concept 32838)
# ---------------------------------------------------------------------------
def test_ehr_prescription_type_recognition():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE drug_exposure (
            drug_exposure_id BIGINT,
            person_id BIGINT,
            drug_concept_id BIGINT,
            drug_exposure_start_date DATE,
            drug_exposure_end_date DATE,
            drug_type_concept_id BIGINT
        );
        INSERT INTO drug_exposure VALUES
            (1, 101, 1112807, '2023-01-01', '2023-01-30', 32838),  -- EHR prescription
            (2, 102, 1112807, '2023-02-01', '2023-02-28', 38000177), -- Prescription written
            (3, 103, 1112807, '2023-03-01', '2023-03-15', 38000180); -- Inpatient administration
    """)

    # Check defaults include 32838
    assert 32838 in DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS
    assert 38000177 in DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS

    # Preflight vocabulary report
    report = vocabulary_preflight(con, fail_fast=False, required_vocabularies=[])
    assert "drug_types" in report
    assert report["drug_types"]["prescribed"] == 2
    assert report["drug_types"]["prescribed_32838"] == 1
    assert report["drug_types"]["prescribed_38000177"] == 1
    assert report["drug_types"]["administered"] == 1


# ---------------------------------------------------------------------------
# 2. Multi-Anchor Temporal Windows
# ---------------------------------------------------------------------------
def test_multi_anchor_temporal_windows():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE cohort (
            person_id BIGINT,
            cohort_start_date DATE,
            cohort_end_date DATE,
            visit_occurrence_id BIGINT
        );
        INSERT INTO cohort VALUES
            (1, '2023-05-10', '2023-05-20', 1001);

        CREATE TABLE measurement (
            measurement_id BIGINT,
            person_id BIGINT,
            measurement_concept_id BIGINT,
            measurement_date DATE,
            value_as_number DOUBLE
        );
        -- Lab 1 day before admission (May 9) and 1 day before discharge (May 19)
        INSERT INTO measurement VALUES
            (1, 1, 3016723, '2023-05-09', 1.0),
            (2, 1, 3016723, '2023-05-19', 1.8);
    """)

    # Anchor to admission (visit_start_date)
    df_adm = extract_features(
        con,
        cohort_table="cohort",
        measurement_concept_ids=[3016723],
        lookback_days=7,
        lookahead_days=0,
        offset_anchor="visit_start_date",
    )
    assert not df_adm.empty
    assert "mean_measurement_3016723" in df_adm.columns
    assert df_adm["mean_measurement_3016723"].iloc[0] == pytest.approx(1.0)

    # Anchor to discharge (visit_end_date)
    df_dis = extract_features(
        con,
        cohort_table="cohort",
        measurement_concept_ids=[3016723],
        lookback_days=2,
        lookahead_days=0,
        offset_anchor="visit_end_date",
    )
    assert not df_dis.empty
    assert df_dis["mean_measurement_3016723"].iloc[0] == pytest.approx(1.8)


# ---------------------------------------------------------------------------
# 3. Unit Discordance Detection & Bimodal Normalization
# ---------------------------------------------------------------------------
def test_unit_discordance_and_normalization():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE measurement (
            measurement_id BIGINT,
            person_id BIGINT,
            measurement_concept_id BIGINT,
            unit_concept_id BIGINT,
            measurement_date DATE,
            value_as_number DOUBLE
        );
        -- Creatinine: 50 rows in mg/dL (~1.0), 50 rows in umol/L (~88.42)
        INSERT INTO measurement
        SELECT
            range AS measurement_id,
            range AS person_id,
            3016723 AS measurement_concept_id,
            0 AS unit_concept_id,
            DATE '2023-01-01' AS measurement_date,
            1.0 + (range % 5) * 0.1 AS value_as_number
        FROM range(1, 51);

        INSERT INTO measurement
        SELECT
            range AS measurement_id,
            range AS person_id,
            3016723 AS measurement_concept_id,
            0 AS unit_concept_id,
            DATE '2023-01-01' AS measurement_date,
            88.42 * (1.0 + (range % 5) * 0.1) AS value_as_number
        FROM range(51, 101);
    """)

    disc = detect_unit_discordance(con, measurement_concept_id=3016723, target_unit="mg/dL")
    assert not disc.empty
    assert (disc["is_discordant"] == True).any()
    assert disc["detected_ratio"].iloc[0] == pytest.approx(88.42, rel=0.1)

    norm_df = normalize_units(con, measurement_concept_id=3016723, auto_convert_ratio=88.42)
    # The normalized values for IDs 51-100 should now be scaled down to ~1.0
    scaled_vals = norm_df[norm_df["measurement_id"] >= 51]["value_as_number"]
    assert (scaled_vals <= 3.0).all()


# ---------------------------------------------------------------------------
# 4. Flexible Outlier Strategies (drop, winsorize, flag_indicator)
# ---------------------------------------------------------------------------
def test_outlier_strategies():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_labs (
            person_id BIGINT,
            measurement_id BIGINT,
            measurement_concept_id BIGINT,
            unit_concept_id BIGINT,
            measurement_date DATE,
            value_as_number DOUBLE
        );
        INSERT INTO raw_labs VALUES
            (1, 1, 3016723, 8840, '2023-01-01', 1.0),   -- Normal
            (2, 2, 3016723, 8840, '2023-01-01', 50.0);  -- Extreme Outlier (Physiologic max is 25.0)
    """)

    # 1. Drop
    dropped = sanitize_measurements(con, measurement_table="raw_labs", outlier_action="drop")
    assert len(dropped) == 1
    assert dropped["person_id"].iloc[0] == 1

    # 2. Winsorize
    winsorized = sanitize_measurements(con, measurement_table="raw_labs", outlier_action="winsorize")
    assert len(winsorized) == 2
    row2 = winsorized[winsorized["person_id"] == 2].iloc[0]
    assert row2["value_as_number"] == 30.0  # Capped at default bound (30.0 mg/dL for creatinine)

    # 3. Flag Indicator
    flagged = sanitize_measurements(con, measurement_table="raw_labs", outlier_action="flag_indicator")
    assert "is_outlier" in flagged.columns
    assert flagged[flagged["person_id"] == 1]["is_outlier"].iloc[0] == 0
    assert flagged[flagged["person_id"] == 2]["is_outlier"].iloc[0] == 1
    assert flagged[flagged["person_id"] == 2]["value_as_number"].iloc[0] == 30.0


# ---------------------------------------------------------------------------
# 5. On-the-Fly Vectorized Drug Eras & PDC Calculator
# ---------------------------------------------------------------------------
def test_drug_eras_and_pdc():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE cohort (
            person_id BIGINT,
            cohort_start_date DATE,
            cohort_end_date DATE
        );
        INSERT INTO cohort VALUES
            (1, '2023-01-01', '2023-06-30');

        CREATE TABLE drug_exposure (
            drug_exposure_id BIGINT,
            person_id BIGINT,
            drug_concept_id BIGINT,
            drug_exposure_start_date DATE,
            drug_exposure_end_date DATE,
            days_supply INTEGER
        );
        -- 3 fills: Jan 1 - Jan 30, Feb 5 - Mar 5 (gap 6 days <= 30), May 10 - Jun 9 (gap > 30)
        INSERT INTO drug_exposure VALUES
            (1, 1, 1112807, '2023-01-01', '2023-01-30', 30),
            (2, 1, 1112807, '2023-02-05', '2023-03-06', 30),
            (3, 1, 1112807, '2023-05-10', '2023-06-08', 30);
    """)

    # Eras with 30-day window: should produce 2 eras
    eras = compute_drug_eras(con, persistence_window_days=30)
    assert len(eras) == 2
    assert eras.iloc[0]["drug_exposure_count"] == 2
    assert eras.iloc[1]["drug_exposure_count"] == 1

    # PDC Adherence over 180 days lookahead
    pdc_df = compute_adherence_pdc(
        con,
        cohort_table="cohort",
        lookback_days=0,
        lookahead_days=180,
        offset_anchor="visit_start_date",
    )
    assert not pdc_df.empty
    assert "pdc" in pdc_df.columns
    assert "mpr" in pdc_df.columns
    assert "is_adherent_80" in pdc_df.columns
    assert 0.0 <= pdc_df["pdc"].iloc[0] <= 1.0


# ---------------------------------------------------------------------------
# 6. Competing Risks in Federated Survival
# ---------------------------------------------------------------------------
def test_competing_risks_federated_survival():
    site_a = pd.DataFrame({
        "time": [5, 10, 15, 20, 25, 30],
        "event": [1, 2, 0, 1, 2, 0],  # 1 = primary event (readmission), 2 = competing risk (death)
        "group": [1, 1, 1, 0, 0, 0],
    })
    site_b = pd.DataFrame({
        "time": [4, 9, 14, 18, 24, 28],
        "event": [1, 0, 2, 1, 0, 2],
        "group": [1, 1, 1, 0, 0, 0],
    })

    result = consortium_survival_meta_analysis(
        {"SiteA": site_a, "SiteB": site_b},
        time_col="time",
        event_col="event",
        group_col="group",
        competing_event=2,
    )
    assert result.pooled_cif is not None
    assert result.cause_specific_hr is not None
    assert result.fine_gray_subdistribution_hr is not None
    assert "SiteA" in result.site_cifs


# ---------------------------------------------------------------------------
# 7. Out-of-Core Parquet / Arrow Streaming
# ---------------------------------------------------------------------------
def test_out_of_core_parquet_streaming():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE cohort (
            person_id BIGINT,
            cohort_start_date DATE,
            cohort_end_date DATE,
            site VARCHAR
        );
        INSERT INTO cohort VALUES
            (1, '2023-01-01', '2023-01-10', 'Site1'),
            (2, '2023-02-01', '2023-02-10', 'Site2');

        CREATE TABLE measurement (
            measurement_id BIGINT,
            person_id BIGINT,
            measurement_concept_id BIGINT,
            measurement_date DATE,
            value_as_number DOUBLE
        );
        INSERT INTO measurement VALUES
            (1, 1, 3016723, '2023-01-05', 1.1),
            (2, 2, 3016723, '2023-02-05', 1.4);
    """)

    with tempfile.TemporaryDirectory() as tmpdir:
        pq_path = os.path.join(tmpdir, "features.parquet")
        df = extract_features(
            con,
            cohort_table="cohort",
            measurement_concept_ids=[3016723],
            stream_to_parquet=pq_path,
        )
        assert os.path.exists(pq_path)
        read_back = pd.read_parquet(pq_path)
        assert len(read_back) == 2


# ---------------------------------------------------------------------------
# 8. Dual-Path Comorbidity Scoring with Source-Code Fallbacks
# ---------------------------------------------------------------------------
def test_dual_path_comorbidity_scoring():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE cohort (
            subject_id BIGINT,
            cohort_start_date DATE
        );
        INSERT INTO cohort VALUES
            (10, '2023-01-01'),
            (20, '2023-01-01');

        CREATE TABLE condition_occurrence (
            condition_occurrence_id BIGINT,
            person_id BIGINT,
            condition_concept_id BIGINT,
            condition_start_date DATE,
            condition_source_value VARCHAR
        );
        INSERT INTO condition_occurrence VALUES
            (1, 10, 0, '2022-12-15', 'I21.9'),  -- Unmapped standard concept (0), but valid ICD-10 for MI
            (2, 20, 4329847, '2022-12-15', 'I21.9'); -- Standard concept mapped
    """)

    charlson = extract_charlson_index(
        con,
        cohort_table="cohort",
        fallback_on_unmapped=True,
        return_mapping_fidelity=True,
    )
    assert not charlson.empty
    assert "charlson_index" in charlson.columns
    assert "mapping_fidelity_score" in charlson.columns
    # Person 10 mapped via fallback, so charlson_index should be > 0
    p10 = charlson[charlson["subject_id"] == 10].iloc[0]
    assert p10["charlson_index"] >= 1


# ---------------------------------------------------------------------------
# 9. Acute In-Hospital Episode Trajectories
# ---------------------------------------------------------------------------
def test_acute_trajectories():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE cohort (
            person_id BIGINT,
            visit_occurrence_id BIGINT,
            cohort_start_date DATE,
            cohort_end_date DATE
        );
        INSERT INTO cohort VALUES
            (1, 101, '2023-01-01', '2023-01-10');

        CREATE TABLE measurement (
            measurement_id BIGINT,
            person_id BIGINT,
            visit_occurrence_id BIGINT,
            measurement_concept_id BIGINT,
            measurement_datetime TIMESTAMP,
            value_as_number DOUBLE
        );
        -- Admission Creatinine = 1.0, peak Creatinine = 3.2 (Stage 3 AKI: delta >= 0.3, ratio > 3.0)
        -- Glucose admission = 120, max = 280, min = 100
        -- Heart rate = 110, SBP = 90 (Shock Index = 110 / 90 = 1.22 > 0.9)
        INSERT INTO measurement VALUES
            (1, 1, 101, 3016723, '2023-01-01 08:00:00', 1.0),
            (2, 1, 101, 3016723, '2023-01-04 12:00:00', 3.2),
            (3, 1, 101, 3016723, '2023-01-09 10:00:00', 1.4),
            (4, 1, 101, 3004501, '2023-01-01 09:00:00', 120.0),
            (5, 1, 101, 3004501, '2023-01-03 09:00:00', 280.0),
            (6, 1, 101, 3004501, '2023-01-05 09:00:00', 100.0),
            (7, 1, 101, 3027018, '2023-01-02 10:00:00', 110.0),
            (8, 1, 101, 3004249, '2023-01-02 10:00:00', 90.0);
    """)

    acute = extract_acute_trajectories(con, cohort_table="cohort")
    assert not acute.empty
    row = acute.iloc[0]
    assert row["creatinine_admission"] == pytest.approx(1.0)
    assert row["creatinine_peak"] == pytest.approx(3.2)
    assert row["creatinine_delta"] == pytest.approx(2.2)
    assert row["kdigo_aki_stage"] == 3
    assert row["glucose_volatility"] == pytest.approx(180.0)
    assert row["shock_index"] == pytest.approx(110.0 / 90.0, rel=1e-2)


# ---------------------------------------------------------------------------
# 10. In-Encounter LOCF & Leakage-Safe Imputation
# ---------------------------------------------------------------------------
def test_encounter_locf_and_imputation():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE encounter_obs (
            person_id BIGINT,
            visit_occurrence_id BIGINT,
            obs_hour INTEGER,
            heart_rate DOUBLE,
            creatinine DOUBLE
        );
        INSERT INTO encounter_obs VALUES
            (1, 101, 1, 72.0, 1.0),
            (1, 101, 2, NULL, NULL),  -- Should LOCF heart_rate to 72.0, creatinine to 1.0
            (1, 101, 3, 80.0, NULL),  -- Heart rate updated to 80.0, creatinine LOCF 1.0
            (2, 102, 1, NULL, 1.2);   -- Heart rate never observed in encounter: imputed by median
    """)

    training_medians = {"heart_rate": 75.0, "creatinine": 1.1}
    imputed = impute_encounter_observations(
        con,
        feature_table="encounter_obs",
        time_col="obs_hour",
        median_values=training_medians,
        add_indicators=True,
    )
    assert not imputed.empty
    assert "miss_heart_rate" in imputed.columns
    assert "miss_creatinine" in imputed.columns

    # Check LOCF on encounter 101
    enc101_hr = imputed[imputed["visit_occurrence_id"] == 101].sort_values("obs_hour")["heart_rate"].tolist()
    assert enc101_hr == [72.0, 72.0, 80.0]

    # Check median fallback on encounter 102
    enc102_hr = imputed[imputed["visit_occurrence_id"] == 102]["heart_rate"].iloc[0]
    assert enc102_hr == 75.0
    assert imputed[imputed["visit_occurrence_id"] == 102]["miss_heart_rate"].iloc[0] == 1
