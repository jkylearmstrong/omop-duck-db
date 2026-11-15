"""Unit tests for v0.6.4 Advanced Features.

Tests:
1. plot_consort_flow (SVG, Plotly) & plot_multisite_attrition_heatmap
2. consortium_propensity_score, match_nearest_neighbor, plot_love_plot
3. generate_negative_controls & calibrate_estimates
4. optimize_concept_set
5. extract_temporal_trajectories
6. consortium_survival_meta_analysis
7. generate_synthetic_omop_twin
"""

import math
import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl.cohort_flow import plot_consort_flow, plot_multisite_attrition_heatmap
from omop_etl.concept_optimizer import optimize_concept_set
from omop_etl.federated_survival import consortium_survival_meta_analysis
from omop_etl.negative_controls import calibrate_estimates, generate_negative_controls
from omop_etl.propensity import consortium_propensity_score, match_nearest_neighbor, plot_love_plot
from omop_etl.synthetic import generate_synthetic_omop_twin
from omop_etl.trajectories import extract_temporal_trajectories


@pytest.fixture
def test_db():
    con = duckdb.connect(":memory:")

    # Minimal CDM tables for testing
    con.execute("""
    CREATE TABLE concept (
        concept_id INTEGER,
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
    INSERT INTO concept VALUES
        (1001, 'Type 2 Diabetes Mellitus', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', '1970-01-01', '2099-12-31', NULL),
        (1002, 'Diabetes Mellitus with Renal Manifestation', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '127013003', '1970-01-01', '2099-12-31', NULL),
        (1003, 'Essential Hypertension', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '59621000', '1970-01-01', '2099-12-31', NULL),
        (2001, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
        (3001, 'Hemoglobin A1c', 'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4', '1970-01-01', '2099-12-31', NULL),
        (4001, 'Inguinal Hernia', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '396232000', '1970-01-01', '2099-12-31', NULL),
        (4002, 'Torn Meniscus', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '239720000', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_ancestor (
        ancestor_concept_id INTEGER,
        descendant_concept_id INTEGER,
        min_levels_of_separation INTEGER,
        max_levels_of_separation INTEGER
    );
    INSERT INTO concept_ancestor VALUES
        (1000, 1001, 1, 1),
        (1000, 1002, 1, 1),
        (1001, 1001, 0, 0),
        (1002, 1002, 0, 0),
        (2001, 2001, 0, 0),
        (4001, 4001, 0, 0),
        (4002, 4002, 0, 0);

    CREATE TABLE concept_relationship (
        concept_id_1 INTEGER,
        concept_id_2 INTEGER,
        relationship_id VARCHAR,
        valid_start_date DATE,
        valid_end_date DATE,
        invalid_reason VARCHAR
    );
    INSERT INTO concept_relationship VALUES
        (2001, 1001, 'Has indication', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE person (
        person_id INTEGER,
        gender_concept_id INTEGER,
        year_of_birth INTEGER,
        month_of_birth INTEGER,
        day_of_birth INTEGER,
        race_concept_id INTEGER,
        ethnicity_concept_id INTEGER
    );
    INSERT INTO person VALUES
        (1, 8507, 1965, 5, 12, 8527, 0),
        (2, 8532, 1972, 8, 20, 8527, 0),
        (3, 8507, 1958, 2, 15, 8527, 0),
        (4, 8532, 1980, 11, 3, 8527, 0),
        (5, 8507, 1970, 7, 24, 8527, 0);

    CREATE TABLE cohort (
        cohort_definition_id INTEGER,
        subject_id INTEGER,
        cohort_start_date DATE,
        cohort_end_date DATE
    );
    INSERT INTO cohort VALUES
        (1, 1, '2022-01-01', '2022-06-30'),
        (1, 2, '2022-01-15', '2022-07-15'),
        (1, 3, '2022-02-01', '2022-08-01'),
        (1, 4, '2022-02-15', '2022-08-15'),
        (1, 5, '2022-03-01', '2022-09-01');

    CREATE TABLE condition_occurrence (
        condition_occurrence_id BIGINT,
        person_id BIGINT,
        condition_concept_id INTEGER,
        condition_start_date DATE,
        condition_end_date DATE,
        condition_type_concept_id INTEGER,
        condition_source_value VARCHAR
    );
    INSERT INTO condition_occurrence VALUES
        (1, 1, 1001, '2021-12-01', '2021-12-01', 38000280, 'Type 2 DM'),
        (2, 2, 1001, '2021-11-15', '2021-11-15', 38000280, 'Type 2 DM'),
        (3, 3, 1001, '2022-01-10', '2022-01-10', 38000280, 'Type 2 DM'),
        (4, 1, 1003, '2021-10-01', '2021-10-01', 38000280, 'HTN'),
        (5, 2, 1003, '2021-09-01', '2021-09-01', 38000280, 'HTN'),
        (6, 4, 4001, '2021-05-01', '2021-05-01', 38000280, 'Hernia'),
        (7, 5, 4002, '2021-06-01', '2021-06-01', 38000280, 'Meniscus'),
        (8, 1, 0, '2021-08-01', '2021-08-01', 38000280, 'ICD10:E11.9_UNMAPPED'),
        (9, 2, 0, '2021-08-01', '2021-08-01', 38000280, 'ICD10:E11.9_UNMAPPED');

    CREATE TABLE drug_exposure (
        drug_exposure_id BIGINT,
        person_id BIGINT,
        drug_concept_id INTEGER,
        drug_exposure_start_date DATE,
        drug_exposure_end_date DATE,
        drug_type_concept_id INTEGER
    );
    INSERT INTO drug_exposure VALUES
        (1, 1, 2001, '2021-12-01', '2022-01-01', 38000177),
        (2, 2, 2001, '2021-11-01', '2021-12-01', 38000177),
        (3, 3, 2001, '2022-01-01', '2022-02-01', 38000177);

    CREATE TABLE measurement (
        measurement_id BIGINT,
        person_id BIGINT,
        measurement_concept_id INTEGER,
        measurement_date DATE,
        value_as_number DOUBLE,
        measurement_type_concept_id INTEGER
    );
    INSERT INTO measurement VALUES
        (1, 1, 3001, '2021-06-01', 8.5, 44818702),
        (2, 1, 3001, '2021-09-01', 8.0, 44818702),
        (3, 1, 3001, '2021-12-01', 7.4, 44818702),
        (4, 2, 3001, '2021-07-01', 9.0, 44818702),
        (5, 2, 3001, '2021-11-01', 7.8, 44818702),
        (6, 3, 3001, '2021-08-01', 6.5, 44818702);
    """)

    yield con
    con.close()


def test_plot_consort_flow_and_heatmap():
    attrition_data = [
        {"step_number": 1, "step_name": "Initial Inpatient Visits", "subjects_retained": 10000, "subjects_dropped": 0, "exclusion_reason": "None"},
        {"step_number": 2, "step_name": "Age >= 18", "subjects_retained": 8500, "subjects_dropped": 1500, "exclusion_reason": "Pediatric age < 18"},
        {"step_number": 3, "step_name": "365-day Lookback", "subjects_retained": 6200, "subjects_dropped": 2300, "exclusion_reason": "Insufficient prior observation"},
        {"step_number": 4, "step_name": "Final Study Cohort", "subjects_retained": 5800, "subjects_dropped": 400, "exclusion_reason": "In-hospital mortality"},
    ]

    # SVG format
    svg = plot_consort_flow(attrition_data, format="svg", title="Test CONSORT Flow")
    assert "<svg" in svg
    assert "Test CONSORT Flow" in svg
    assert "Initial Inpatient Visits" in svg
    assert "Excluded" in svg

    # Plotly format
    plotly_obj = plot_consort_flow(attrition_data, format="plotly")
    if isinstance(plotly_obj, dict):
        assert "data" in plotly_obj
        assert plotly_obj["data"][0]["type"] == "sankey"
    else:
        assert hasattr(plotly_obj, "to_json")

    # Multi-site heatmap
    multisite_df = pd.DataFrame([
        {"site": "Site_A", "step_name": "Step 1: Base", "percent_retained": 100.0},
        {"site": "Site_A", "step_name": "Step 2: Adult", "percent_retained": 92.5},
        {"site": "Site_A", "step_name": "Step 3: Follow-up", "percent_retained": 85.0},
        {"site": "Site_B", "step_name": "Step 1: Base", "percent_retained": 100.0},
        {"site": "Site_B", "step_name": "Step 2: Adult", "percent_retained": 65.0},
        {"site": "Site_B", "step_name": "Step 3: Follow-up", "percent_retained": 35.0},
    ])
    heatmap_svg = plot_multisite_attrition_heatmap(multisite_df, title="Consortium Retention Heatmap")
    assert "<svg" in heatmap_svg
    assert "Site_A" in heatmap_svg
    assert "Site_B" in heatmap_svg
    assert "92.5%" in heatmap_svg


def test_propensity_matching_and_love_plot():
    np.random.seed(42)
    n = 200
    treated = np.random.binomial(1, 0.4, n)
    age = np.random.normal(60 + 5 * treated, 10, n)
    charlson = np.random.poisson(2 + 1 * treated, n)

    df = pd.DataFrame({
        "subject_id": range(1, n + 1),
        "treatment": treated,
        "age": age,
        "charlson": charlson,
    })

    # Run matching
    res = consortium_propensity_score(
        cohort_table=df,
        exposure_col="treatment",
        covariate_cols=["age", "charlson"],
        method="matching",
        caliper=0.25,
        ratio=1,
    )

    assert res.converged
    assert not res.data.empty
    assert "matched" in res.data.columns
    assert res.balance is not None
    assert "post_smd" in res.balance.columns

    # Check Love plot
    love_svg = plot_love_plot(res.balance, threshold=0.10)
    assert "<svg" in love_svg
    assert "Threshold (0.1)" in love_svg
    assert "age" in love_svg


def test_negative_controls_and_calibration(test_db):
    # 1. Negative control discovery for Metformin (2001)
    neg_controls = generate_negative_controls(test_db, exposure_concept_id=2001, target_domain="Condition", candidate_limit=10)
    assert not neg_controls.empty
    cids = list(neg_controls["concept_id"])
    assert 2001 not in cids
    assert 1001 not in cids  # T2DM is an indication, excluded!
    assert 4001 in cids or 4002 in cids  # Inguinal Hernia or Torn Meniscus should qualify

    # 2. Empirical calibration
    np.random.seed(123)
    null_effects = np.random.normal(0.05, 0.15, size=25)
    null_ses = np.random.uniform(0.08, 0.12, size=25)
    null_df = pd.DataFrame({"log_rr": null_effects, "se_log_rr": null_ses})

    cal_res = calibrate_estimates(null_df, observed_effect=0.35, observed_se=0.10)
    assert cal_res.n_negative_controls == 25
    assert cal_res.calibrated_p >= 0.0
    assert cal_res.calibrated_p <= 1.0
    assert cal_res.null_sd >= 0.0


def test_semantic_concept_set_optimizer(test_db):
    suggestions = optimize_concept_set(test_db, concept_ids=[1001], threshold_frequency=1)
    assert not suggestions.empty
    sug_names = list(suggestions["concept_name"])
    sug_types = list(suggestions["suggestion_type"])

    assert "sibling_hierarchy" in sug_types or "clinical_cooccurrence" in sug_types
    # Should identify co-occurring hypertension (1003) or sibling diabetes (1002)
    sug_ids = list(suggestions["concept_id"])
    assert 1002 in sug_ids or 1003 in sug_ids or 0 in sug_ids


def test_extract_temporal_trajectories(test_db):
    trajs = extract_temporal_trajectories(
        test_db,
        cohort_table="cohort",
        measurement_concept_ids=[3001],
        window_days=365,
        id_col="subject_id",
        index_date_col="cohort_start_date",
    )
    assert not trajs.empty
    assert "subject_id" in trajs.columns
    assert "traj_3001_slope" in trajs.columns
    assert "traj_3001_r2" in trajs.columns
    assert "traj_3001_volatility" in trajs.columns
    assert "traj_3001_count" in trajs.columns

    # Person 1 had HbA1c values 8.5 -> 8.0 -> 7.4 (improving/decreasing)
    p1 = trajs[trajs["subject_id"] == 1].iloc[0]
    assert p1["traj_3001_count"] == 3
    assert not pd.isna(p1["traj_3001_slope"])


def test_federated_survival_meta_analysis():
    site_dict = {
        "Site_Alpha": {"log_hr": 0.25, "se": 0.08, "n_patients": 500, "n_events": 45},
        "Site_Beta": {"log_hr": 0.30, "se": 0.10, "n_patients": 400, "n_events": 35},
        "Site_Gamma": {"log_hr": 0.20, "se": 0.09, "n_patients": 600, "n_events": 50},
    }

    res = consortium_survival_meta_analysis(site_dict)
    assert res.pooled_hr > 1.0
    assert res.pooled_hr_ci[0] > 0
    assert res.pooled_p < 0.05
    assert res.i_squared >= 0.0
    assert len(res.site_estimates) == 3


def test_generate_synthetic_omop_twin(test_db):
    with tempfile.TemporaryDirectory() as tmpdir:
        out_db = Path(tmpdir) / "synthetic_twin.duckdb"
        summary = generate_synthetic_omop_twin(
            test_db,
            cohort_table="cohort",
            output_db_path=out_db,
            noise_level=0.15,
            n_patients=20,
            seed=42,
        )

        assert summary["status"] == "success"
        assert summary["n_patients"] == 20
        assert out_db.exists()

        # Connect and check synthetic database integrity
        syn_con = duckdb.connect(str(out_db))
        n_p = syn_con.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        n_c = syn_con.execute("SELECT COUNT(*) FROM cohort").fetchone()[0]
        n_m = syn_con.execute("SELECT COUNT(*) FROM measurement").fetchone()[0]
        syn_con.close()

        assert n_p == 20
        assert n_c == 20
        assert n_m > 0
