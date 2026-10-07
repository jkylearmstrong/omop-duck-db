"""Unit tests covering newly completed RFC and roadmap features:
- Pre-compiled OHDSI phenotype bundles
- Bedside clinical risk scores (LACE, HOSPITAL, SOFA, CHA2DS2-VASc)
- Core 14 standard lab panel harmonizer with winsorization
- CONSORT attrition engine (Mermaid, LaTeX, Markdown, DataFrame)
- Longitudinal treatment episode collapsing
- Publication Table 1 & Table 2 exporters
- Heuristic unmapped concept repair
- Federation privacy cell suppression & cross-site discrepancy checking
- Native CIRCE / Atlas JSON-to-DuckDB compiler
- Multi-modal SARD visit tensor dense feature fusion & Arrow to PyTorch streaming
- Cluster job submission connector
"""

import json
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    build_schema,
    get_phenotype_concept_set,
    list_available_phenotypes,
    calculate_bedside_scores,
    extract_standard_labs,
    CORE_14_LAB_PANEL,
    generate_consort_attrition,
    build_treatment_episodes,
    export_table1,
    generate_table1,
    auto_remap_unmapped,
    create_federated_consortium,
    with_cell_suppression,
    check_cross_database_discrepancy,
    compile_circe_to_duckdb,
    execute_circe_cohort,
    extract_sard_visit_tensors,
    arrow_to_pytorch,
    build_cluster_command,
    cluster_submit,
)


@pytest.fixture
def mock_cdm():
    """Sets up an in-memory DuckDB CDM with synthetic records for testing."""
    con = duckdb.connect(":memory:")
    build_schema(con)

    # 1. Person
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (1, 8507, 1950, 1, 1, 8527, 38003564), -- Male, 70y
            (2, 8532, 1940, 5, 1, 8527, 38003564), -- Female, 80y
            (3, 8507, 1980, 10, 1, 8527, 38003564); -- Male, 40y
    """)

    # 2. Observation period
    con.execute("""
        INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
        VALUES 
            (1, 1, DATE '2019-01-01', DATE '2022-12-31', 32817),
            (2, 2, DATE '2019-01-01', DATE '2022-12-31', 32817),
            (3, 3, DATE '2019-01-01', DATE '2022-12-31', 32817);
    """)

    # 3. Concepts
    con.execute("""
        INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
        VALUES 
            (316139, 'Heart failure', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '84114007', DATE '1970-01-01', DATE '2099-12-31'),
            (316866, 'Hypertensive disorder', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '38341003', DATE '1970-01-01', DATE '2099-12-31'),
            (201826, 'Type 2 diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', DATE '1970-01-01', DATE '2099-12-31'),
            (443454, 'Cerebral infarction', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '432504007', DATE '1970-01-01', DATE '2099-12-31'),
            (3004501, 'Hemoglobin [Mass/volume] in Blood', 'Measurement', 'LOINC', 'Lab Test', 'S', '718-7', DATE '1970-01-01', DATE '2099-12-31'),
            (3019550, 'Sodium [Moles/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2951-2', DATE '1970-01-01', DATE '2099-12-31'),
            (3016723, 'Creatinine [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2160-0', DATE '1970-01-01', DATE '2099-12-31'),
            (1125315, 'Acetaminophen', 'Drug', 'RxNorm', 'Ingredient', 'S', '161', DATE '1970-01-01', DATE '2099-12-31');
    """)

    # Concept ancestor
    con.execute("""
        INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
        VALUES 
            (316139, 316139, 0, 0),
            (316866, 316866, 0, 0),
            (201826, 201826, 0, 0),
            (443454, 443454, 0, 0);
    """)

    # 4. Visits
    con.execute("""
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES 
            (101, 1, 9201, DATE '2020-05-01', DATE '2020-05-05', 32817),
            (102, 2, 9201, DATE '2020-06-01', DATE '2020-06-10', 32817),
            (103, 3, 9201, DATE '2020-07-01', DATE '2020-07-02', 32817);
    """)

    # 5. Cohort table
    con.execute("""
        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT 
            1 AS cohort_definition_id,
            person_id AS subject_id,
            visit_start_date AS cohort_start_date,
            visit_end_date AS cohort_end_date
        FROM visit_occurrence;
    """)

    # 6. Conditions
    con.execute("""
        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value)
        VALUES 
            (1, 1, 316139, DATE '2020-01-15', 32817, 'I50.9'),
            (2, 2, 316866, DATE '2020-02-20', 32817, 'I10'),
            (3, 2, 201826, DATE '2020-03-10', 32817, 'E11.9'),
            (4, 2, 443454, DATE '2020-04-01', 32817, 'I63.9'),
            (5, 3, 0, DATE '2020-05-01', 32817, 'I10.0'); -- Unmapped condition for repair test
    """)

    # 7. Measurements
    con.execute("""
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, measurement_source_value)
        VALUES 
            (1, 1, 3004501, DATE '2020-05-03', 32817, 10.5, '718-7'),  -- Hgb low
            (2, 1, 3019550, DATE '2020-05-03', 32817, 130.0, '2951-2'), -- Na low
            (3, 2, 3016723, DATE '2020-06-05', 32817, 3.2, '2160-0'),   -- Creatinine
            (4, 3, 3016723, DATE '2020-07-01', 32817, 999.0, '2160-0'); -- Outlier creatinine for winsorize test
    """)

    # 8. Drug exposures for episode collapsing
    con.execute("""
        INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
        VALUES 
            (1, 1, 1125315, DATE '2020-01-01', DATE '2020-01-30', 32817),
            (2, 1, 1125315, DATE '2020-02-10', DATE '2020-03-10', 32817), -- 11 day gap <= 30d -> same episode
            (3, 1, 1125315, DATE '2020-06-01', DATE '2020-06-30', 32817); -- >30d gap -> new episode
    """)

    return con


def test_phenotype_library_bundles():
    phenos = list_available_phenotypes()
    assert "heart_failure" in phenos
    assert "type_2_diabetes" in phenos
    assert "sepsis" in phenos
    assert "acute_kidney_injury" in phenos

    hf = get_phenotype_concept_set("heart_failure")
    assert 316139 in hf["standard_concept_ids"]
    assert "I50" in hf["icd10_prefixes"]
    assert hf["domain_id"] == "Condition"

    with pytest.raises(KeyError):
        get_phenotype_concept_set("non_existent_disease")


def test_calculate_bedside_scores(mock_cdm):
    scores_df = calculate_bedside_scores(mock_cdm, cohort_table="cohort", scores=["lace", "hospital", "chads_vasc", "sofa"])
    assert len(scores_df) == 3
    assert "lace_score" in scores_df.columns
    assert "hospital_score" in scores_df.columns
    assert "chads_vasc_score" in scores_df.columns
    assert "sofa_score" in scores_df.columns

    # Patient 2 (Female, age 80, HTN, T2D, Stroke) should have high CHA2DS2-VASc
    p2 = scores_df[scores_df["person_id"] == 2].iloc[0]
    # Age >= 75 (2) + Female (1) + HT (1) + DM (1) + Stroke (2) = 7
    assert p2["chads_vasc_score"] >= 6


def test_extract_standard_labs_and_winsorize(mock_cdm):
    labs_df = extract_standard_labs(mock_cdm, cohort_table="cohort", panel="core_14", winsorize=True)
    assert len(labs_df) == 3
    assert "creatinine" in labs_df.columns
    # Person 3 had raw creatinine of 999.0; winsorize should clamp to 30.0
    p3_creat = labs_df[labs_df["subject_id"] == 3]["creatinine"].values[0]
    assert p3_creat <= 30.0


def test_generate_consort_attrition(mock_cdm):
    steps = [
        ("All Patients", "SELECT person_id AS subject_id FROM person"),
        ("Age >= 50", "SELECT person_id AS subject_id FROM person WHERE year_of_birth <= 1970"),
        ("Has Visit", "SELECT DISTINCT person_id AS subject_id FROM visit_occurrence"),
    ]
    attrition = generate_consort_attrition(con=mock_cdm, steps=steps)
    df = attrition.to_dataframe()
    assert len(df) == 3
    assert df["subjects_retained"].iloc[0] >= df["subjects_retained"].iloc[1]

    mermaid = attrition.to_mermaid()
    assert "flowchart TD" in mermaid
    assert "All Patients" in mermaid

    latex = attrition.to_latex()
    assert "\\begin{tabular}" in latex


def test_build_treatment_episodes(mock_cdm):
    episodes = build_treatment_episodes(mock_cdm, max_gap_days=30)
    assert len(episodes) == 2  # 2 episodes for person 1
    assert list(episodes["episode_number"]) == [1, 2]
    assert episodes["exposure_count"].iloc[0] == 2  # first episode has 2 merged fills


def test_export_table1(mock_cdm):
    cohort_df = mock_cdm.execute("""
        SELECT 
            p.person_id AS subject_id,
            CASE WHEN p.person_id = 1 THEN 1 ELSE 0 END AS readmitted_30d,
            date_diff('year', make_date(p.year_of_birth, 1, 1), DATE '2020-01-01') AS age,
            CASE WHEN p.gender_concept_id = 8532 THEN 'Female' ELSE 'Male' END AS sex
        FROM person p
    """).df()

    t1_res = generate_table1(cohort_df, strata_col="readmitted_30d", continuous_vars=["age"], categorical_vars=["sex"])
    md_str = export_table1(t1_res, format="markdown")
    assert "| age" in md_str or "age" in md_str
    csv_str = export_table1(t1_res, format="csv")
    assert "age" in csv_str


def test_auto_remap_unmapped(mock_cdm):
    # Person 3 had condition_source_value = 'I10.0' with concept_id = 0
    # Map ICD-10 'I10.0' to SNOMED 316866 via concept_relationship
    mock_cdm.execute("""
        INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
        VALUES (99901, 'Essential hypertension', 'Condition', 'ICD10CM', 'Diagnosis', 'N', 'I10', DATE '1970-01-01', DATE '2099-12-31');
    """)
    mock_cdm.execute("""
        INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, valid_start_date, valid_end_date)
        VALUES (99901, 316866, 'Maps to', DATE '1970-01-01', DATE '2099-12-31');
    """)

    res = auto_remap_unmapped(mock_cdm, "condition_occurrence", strip_formatting=True, dry_run=False)
    assert res["table"] == "condition_occurrence"
    assert res["total_unmapped"] >= 1


def test_cell_suppression_and_cross_database(mock_cdm, tmp_path):
    # Setup two small site databases for federation testing
    db1 = str(tmp_path / "site1.duckdb")
    db2 = str(tmp_path / "site2.duckdb")
    c1 = duckdb.connect(db1)
    build_schema(c1)
    c1.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES (1, 1, 316139, DATE '2020-01-01', 32817);")
    c1.close()

    c2 = duckdb.connect(db2)
    build_schema(c2)
    c2.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES (2, 2, 316139, DATE '2020-01-01', 32817);")
    c2.close()

    fed_con = create_federated_consortium({"Site A": db1, "Site B": db2})
    
    # Test cross-database discrepancy check
    disc = check_cross_database_discrepancy(fed_con, table_name="condition_occurrence")
    assert disc["evaluated_concepts"] >= 1

    # Test cell suppression
    fed_con.execute("CREATE TABLE agg_counts AS SELECT 316139 AS concept_id, 3 AS n_patients;")
    safe_view = with_cell_suppression(fed_con, "agg_counts", min_cell_size=5)
    row = fed_con.execute(f"SELECT * FROM {safe_view}").fetchone()
    assert row[1] == "<10"  # Suppressed because 3 < 5


def test_compile_circe_to_duckdb(mock_cdm):
    circe_dict = {
        "ConceptSets": [
            {
                "id": 0,
                "name": "Heart Failure",
                "expression": {
                    "items": [
                        {"concept": {"CONCEPT_ID": 316139}, "includeDescendants": True, "isExcluded": False}
                    ]
                }
            }
        ],
        "PrimaryCriteria": {
            "CriteriaList": [
                {
                    "ConditionOccurrence": {
                        "CodesetId": 0
                    }
                }
            ],
            "ObservationWindow": {"PriorDays": 0, "PostDays": 0},
            "PrimaryCriteriaLimit": {"Type": "All"}
        }
    }
    sql = compile_circe_to_duckdb(circe_dict, target_cohort_id=10)
    assert "WITH _cs_resolved AS" in sql
    assert "10 AS cohort_definition_id" in sql

    cohort_res = execute_circe_cohort(mock_cdm, circe_dict, target_cohort_id=10)
    assert len(cohort_res) >= 1
    assert cohort_res["cohort_definition_id"].iloc[0] == 10


def test_sard_dense_features_and_cluster_command(mock_cdm, tmp_path):
    # SARD visit tensors with dense features
    pq_path = tmp_path / "cohort.parquet"
    mock_cdm.execute(f"COPY (SELECT subject_id AS person_id, cohort_start_date AS index_date, 1 AS y FROM cohort) TO '{pq_path.as_posix()}' (FORMAT PARQUET)")
    dense_covs = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], dtype=np.float32)

    res = extract_sard_visit_tensors(mock_cdm, pq_path, dense_features=dense_covs)
    assert "dense_features" in res
    assert res["dense_features"].shape == (3, 2)

    # Cluster submission command
    cmd = build_cluster_command("data.parquet", model="sard", gpus=4)
    assert "python" in cmd
    assert "--gpus" in cmd
    sub = cluster_submit("localhost", dataset="data.parquet", dry_run=True)
    assert sub["status"] == "DRY_RUN"
