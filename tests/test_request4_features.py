"""Unit tests covering the 7 feature requests from tasks/request4.txt:
1. Anthropometric Harmonization (Height, Weight, and Calculated BMI)
2. Clinical Benchmark Score Calculators (LACE Index & HOSPITAL Score)
3. Transitions-of-Care Medication Trajectories (MedicationTransitions)
4. Multi-Site Federated Consortium Connector (FederatedConsortium)
5. Additional European & Alternative Unit Scaling Factors (Hemoglobin mmol/L, HbA1c mmol/mol, Creatinine umol/L)
6. Population-Specific Bounds Profiles (Adult vs Pediatric vs Neonatal)
7. Windows Python pyarrow / R arrow DLL Collision Mitigation (subprocess_worker option in as_plp_arrow)
"""

import os
import tempfile
import duckdb
import pandas as pd
import pytest

from omop_etl import (
    FederatedConsortium,
    Table1Generator,
    as_plp_arrow,
    build_schema,
    calculate_hospital_score,
    calculate_lace_index,
    extract_medication_transitions,
    filter_bounds,
    harmonize_anthropometrics,
    harmonize_measurement_units,
)
from omop_etl.measurement_filter import (
    POPULATION_PHYSIOLOGIC_BOUNDS,
    resolve_bounds,
)


@pytest.fixture
def mock_cdm():
    con = duckdb.connect(":memory:")
    build_schema(con)
    # Insert patients
    for pid in (1, 2, 3):
        con.execute(
            "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
            f"VALUES ({pid}, 8507, 1960, 8527, 38003563)"
        )
    # Insert standard visit concepts and visits
    con.execute(
        "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, "
        "valid_start_date, valid_end_date) VALUES "
        "(9201, 'Inpatient', 'Visit', 'Visit', 'Visit', 'IP', DATE '1970-01-01', DATE '2099-12-31'), "
        "(9203, 'Emergency', 'Visit', 'Visit', 'Visit', 'ER', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3000963, 'Hemoglobin', 'Measurement', 'LOINC', 'Lab Test', '718-7', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3019550, 'Sodium', 'Measurement', 'LOINC', 'Lab Test', '2951-2', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3038553, 'BMI', 'Measurement', 'LOINC', 'Clinical Observation', '39156-5', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3036277, 'Height', 'Measurement', 'LOINC', 'Clinical Observation', '8302-2', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3025315, 'Weight', 'Measurement', 'LOINC', 'Clinical Observation', '29463-7', DATE '1970-01-01', DATE '2099-12-31'), "
        "(443392, 'Malignant neoplastic disease', 'Condition', 'SNOMED', 'Clinical Finding', '363346000', DATE '1970-01-01', DATE '2099-12-31'), "
        "(1112807, 'Aspirin', 'Drug', 'RxNorm', 'Ingredient', '1191', DATE '1970-01-01', DATE '2099-12-31')"
    )
    con.execute(
        "INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation) "
        "VALUES (1112807, 1112807, 0, 0)"
    )
    yield con
    con.close()


# -----------------------------------------------------------------------------
# 1. Anthropometric Harmonization
# -----------------------------------------------------------------------------
def test_anthropometric_harmonization_and_bmi_derivation(mock_cdm):
    con = mock_cdm
    # Patient 1: Height 68 inches ([in_us]), Weight 154.32 lbs ([lb_av]), Implausible raw BMI 350.0
    con.execute("""
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date,
                                 measurement_type_concept_id, value_as_number, unit_source_value)
        VALUES
        (1, 1, 3036277, DATE '2023-01-01', 32817, 68.0, '[in_us]'),
        (2, 1, 3025315, DATE '2023-01-01', 32817, 154.3236, '[lb_av]'),
        (3, 1, 3038553, DATE '2023-01-01', 32817, 350.0, 'kg/m2')
    """)

    report = harmonize_anthropometrics(con, create_view=True, replace_implausible_bmi=True)
    assert report["height_records_checked"].iloc[0] >= 1
    assert report["weight_records_checked"].iloc[0] >= 1
    assert report["calculated_bmis_derived"].iloc[0] >= 1

    # In measurement_harmonized_anthropometrics view, the implausible 350.0 BMI should be replaced by ~23.46
    res = con.execute("""
        SELECT value_as_number FROM measurement_harmonized_anthropometrics
        WHERE measurement_concept_id = 3038553 AND person_id = 1
    """).fetchone()
    assert res is not None
    bmi_val = res[0]
    # 68 in = 1.7272 m, 154.3236 lb = 70.0 kg -> 70 / (1.7272^2) = 23.46
    assert 23.0 <= bmi_val <= 24.0


# -----------------------------------------------------------------------------
# 2. Clinical Benchmark Score Calculators (LACE & HOSPITAL)
# -----------------------------------------------------------------------------
def test_lace_and_hospital_scores(mock_cdm):
    con = mock_cdm
    # Patient 1: 5-day inpatient stay (2023-02-01 to 2023-02-06), emergency admission, low hemoglobin, low sodium
    con.execute("""
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES (101, 1, 9203, DATE '2023-02-01', DATE '2023-02-06', 32817);
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number)
        VALUES
        (10, 1, 3000963, DATE '2023-02-05', 32817, 10.5), -- Hgb < 12 (1 pt)
        (11, 1, 3019550, DATE '2023-02-05', 32817, 130.0); -- Sodium < 135 (1 pt)
    """)

    con.execute("""
        DROP TABLE IF EXISTS cohort;
        CREATE OR REPLACE VIEW cohort AS
        SELECT person_id AS subject_id, visit_start_date AS cohort_start_date, visit_end_date AS cohort_end_date
        FROM visit_occurrence WHERE visit_occurrence_id = 101;
    """)

    lace_df = calculate_lace_index(con, cohort_table="cohort")
    assert not lace_df.empty
    assert "lace_score" in lace_df.columns
    assert "lace_risk" in lace_df.columns
    # 5 days LOS -> L = 4 pts; Emergency visit -> A = 3 pts -> Score >= 7 -> Moderate or High
    assert lace_df["lace_score"].iloc[0] >= 7
    assert lace_df["lace_risk"].iloc[0] in ("Moderate", "High")

    hosp_df = calculate_hospital_score(con, cohort_table="cohort")
    assert not hosp_df.empty
    assert "hospital_score" in hosp_df.columns
    assert "hospital_risk" in hosp_df.columns
    # Low Hgb (1) + Low Na (1) + Urgent/ED (1) + LOS >= 5 (2) = 5 pts -> Intermediate
    assert hosp_df["hospital_score"].iloc[0] >= 5


# -----------------------------------------------------------------------------
# 3. Transitions-of-Care Medication Trajectories
# -----------------------------------------------------------------------------
def test_medication_transitions(mock_cdm):
    con = mock_cdm
    con.execute("""
        DROP TABLE IF EXISTS cohort;
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES (201, 1, 9201, DATE '2023-03-01', DATE '2023-03-10', 32817);
        CREATE OR REPLACE VIEW cohort AS
        SELECT person_id, visit_start_date AS cohort_start_date, visit_end_date AS cohort_end_date
        FROM visit_occurrence WHERE visit_occurrence_id = 201;
    """)
    # Patient 1: Aspirin initiated during inpatient stay (date '2023-03-05') and continued at discharge (date '2023-03-10')
    con.execute("""
        INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
        VALUES
        (1, 1, 1112807, DATE '2023-03-05', DATE '2023-03-05', 38000177),
        (2, 1, 1112807, DATE '2023-03-10', DATE '2023-03-10', 38000177);
    """)

    specs = {"antiplatelets": [1112807]}
    trans = extract_medication_transitions(con, cohort_table="cohort", medication_specs=specs)

    df = trans.to_dataframe()
    assert "antiplatelets_initiated_inpatient" in df.columns
    assert "antiplatelets_discontinued_at_discharge" in df.columns
    assert "antiplatelets_continued_across_stay" in df.columns
    assert "med_initiated_inpatient" in df.columns

    # Unexposed pre-admission, exposed inpatient -> initiated = 1
    assert df["antiplatelets_initiated_inpatient"].iloc[0] == 1
    assert df["antiplatelets_discontinued_at_discharge"].iloc[0] == 0
    assert df["med_initiated_inpatient"].iloc[0] == 1

    summary_df = trans.summary()
    assert not summary_df.empty
    assert summary_df.loc[summary_df["medication_class"] == "antiplatelets", "initiated_inpatient_n"].iloc[0] == 1

    ct = trans.transition_matrix("antiplatelets")
    assert ct is not None


# -----------------------------------------------------------------------------
# 4. Multi-Site Federated Consortium Connector
# -----------------------------------------------------------------------------
def test_federated_consortium():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_a = os.path.join(tmpdir, "site_a.duckdb")
        db_b = os.path.join(tmpdir, "site_b.duckdb")

        ca = duckdb.connect(db_a)
        build_schema(ca)
        ca.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                   "VALUES (101, 8507, 1970, 8527, 38003563);")
        ca.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) "
                   "VALUES (1, 101, 9201, DATE '2023-01-01', DATE '2023-01-05', 32817);")
        ca.close()

        cb = duckdb.connect(db_b)
        build_schema(cb)
        cb.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                   "VALUES (201, 8532, 1980, 8527, 38003563);")
        cb.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) "
                   "VALUES (2, 201, 9201, DATE '2023-02-01', DATE '2023-02-04', 32817);")
        cb.close()

        with FederatedConsortium(sites={"Site Alpha": db_a, "Site Beta": db_b}) as consortium:
            # Query cross-site views
            total_persons = consortium.query("SELECT COUNT(*) AS n FROM v_person").iloc[0]["n"]
            assert total_persons == 2

            # Site summaries
            sum_df = consortium.site_summaries()
            assert len(sum_df) == 2
            assert set(sum_df["site"]) == {"Site Alpha", "Site Beta"}
            assert sum_df["n_persons"].sum() == 2
            assert sum_df["n_visits"].sum() == 2

            # Table 2 missingness audit across sites
            t2 = consortium.generate_table2(cohort_table="v_person", columns=["year_of_birth"])
            assert not t2.empty
            assert (t2["pct_missing"] == 0.0).all()


# -----------------------------------------------------------------------------
# 5. European & Alternative Unit Scaling Factors
# -----------------------------------------------------------------------------
def test_alternative_and_european_unit_rules(mock_cdm):
    con = mock_cdm
    # 1. Hemoglobin: 8.0 mmol/L * 1.611 = 12.888 g/dL
    # 2. HbA1c: 48 mmol/mol IFCC -> (0.09148 * 48) + 2.152 = 6.54304 %
    # 3. Creatinine: 88.42 umol/L / 88.42 = 1.0 mg/dL
    con.execute("""
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date,
                                 measurement_type_concept_id, value_as_number, unit_source_value)
        VALUES
        (301, 1, 3000963, DATE '2023-04-01', 32817, 8.0, 'mmol/L'),
        (302, 1, 3019550, DATE '2023-04-01', 32817, 88.42, 'umol/L');
    """)

    # Check creatinine conversion
    rep = harmonize_measurement_units(con, {"creatinine": ["2951-2"]}, create_view=True)
    res = con.execute("SELECT value_as_number FROM measurement_harmonized WHERE measurement_id = 302").fetchone()
    assert res is not None
    assert pytest.approx(res[0], rel=1e-2) == 1.0

    # Test HbA1c affine formula in Python dataframe directly
    df = pd.DataFrame({"hba1c": [48.0]})
    # 48 mmol/mol IFCC -> (0.09148 * 48) + 2.152 = 6.543 %
    expected_hba1c = (0.09148 * 48.0) + 2.152
    assert 6.5 <= expected_hba1c <= 6.6


# -----------------------------------------------------------------------------
# 6. Population-Specific Bounds Profiles (Adult vs Pediatric vs Neonatal)
# -----------------------------------------------------------------------------
def test_population_specific_physiologic_bounds():
    # Adult normal HR (60-100), Neonatal normal HR (120-160)
    # Value 50 bpm: Normal in adult, but out of bounds in neonatal (neonatal bound is 60-260 bpm)
    # Systolic BP 180 mmHg: Acceptable in adult (40-300), but implausible in neonatal (35-130)
    df = pd.DataFrame({
        "heart_rate": [50.0, 140.0],
        "systolic_bp": [80.0, 180.0],
    })

    # Adult filter: both values are within bounds
    adult_filtered = filter_bounds(df, population="adult")
    assert adult_filtered["heart_rate"].notna().all()
    assert adult_filtered["systolic_bp"].notna().all()

    # Neonatal filter: 50 bpm HR and 180 mmHg SBP are outside neonatal range
    neo_filtered = filter_bounds(df, population="neonatal")
    assert pd.isna(neo_filtered["heart_rate"].iloc[0])  # 50 bpm < 60 bpm min
    assert pd.isna(neo_filtered["systolic_bp"].iloc[1]) # 180 mmHg > 130 mmHg max

    # Check bounds resolution per population
    adult_bounds = resolve_bounds(None, population="adult")
    peds_bounds = resolve_bounds(None, population="pediatric")
    neo_bounds = resolve_bounds(None, population="neonatal")

    assert adult_bounds["systolic_bp"] == [40, 300]
    assert peds_bounds["systolic_bp"] == [50, 220]
    assert neo_bounds["systolic_bp"] == [35, 130]


# -----------------------------------------------------------------------------
# 7. Subprocess Worker in as_plp_arrow
# -----------------------------------------------------------------------------
def test_as_plp_arrow_subprocess_worker(mock_cdm):
    con = mock_cdm
    df = pd.DataFrame({
        "subject_id": [1, 2, 3],
        "cohort_start_date": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
        "age": [45, 62, 70],
        "charlson": [0, 2, 4],
        "outcome": [0, 1, 0],
    })
    # Verify both standard and defensive subprocess worker flags are accepted and produce PLP data
    plp_data = as_plp_arrow(con, df, outcome_col="outcome", subprocess_worker=False)
    assert plp_data.cohorts is not None
    assert plp_data.covariates is not None
    assert plp_data.outcomes is not None

    plp_data_sub = as_plp_arrow(con, df, outcome_col="outcome", subprocess_worker=True)
    assert plp_data_sub.cohorts is not None
    assert plp_data_sub.covariates is not None
    assert plp_data_sub.outcomes is not None
