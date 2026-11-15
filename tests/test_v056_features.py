"""Unit tests covering Milestone v0.5.6 feature requests:
1. Automated OHDSI PLP Study Package Generator (export_plp_study_package)
2. Direct Andromeda Zero-Copy View Adapter (export_to_andromeda, AndromedaAdapter)
3. Automated CONSORT / STROBE Flowchart Exporters (to_typst, to_tikz, CohortExtractor attrition)
4. Foundation Model Tokenizer Integration (export_clinical_sequence_tokens, ClinicalSequenceTokens)
5. Federated Meta-Analysis Aggregator (federated_meta_analysis, FederatedMetaAnalysisResult)
"""

import json
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    build_schema,
    CohortExtractor,
    export_plp_study_package,
    export_to_andromeda,
    AndromedaAdapter,
    export_clinical_sequence_tokens,
    extract_sard_visit_tensors,
    ClinicalSequenceTokens,
    federated_meta_analysis,
    FederatedMetaAnalysisResult,
    generate_consort_attrition,
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
            (3, 8507, 2010, 10, 1, 8527, 38003564); -- Male, 10y (Pediatric)
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
            (1125315, 'Acetaminophen', 'Drug', 'RxNorm', 'Ingredient', 'S', '161', DATE '1970-01-01', DATE '2099-12-31');
    """)

    # 4. Visits
    con.execute("""
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES 
            (101, 1, 9201, DATE '2020-05-01', DATE '2020-05-05', 32817),
            (102, 2, 9201, DATE '2020-06-01', DATE '2020-06-10', 32817),
            (103, 3, 9201, DATE '2020-07-01', DATE '2020-07-02', 32817);
    """)

    # 5. Conditions and drugs
    con.execute("""
        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, visit_occurrence_id)
        VALUES 
            (1, 1, 201826, DATE '2020-01-10', 32817, 101),
            (2, 2, 316139, DATE '2020-02-15', 32817, 102);

        INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, visit_occurrence_id)
        VALUES 
            (1, 1, 1125315, DATE '2020-01-15', DATE '2020-01-25', 32817, 101),
            (2, 2, 1125315, DATE '2020-02-20', DATE '2020-03-01', 32817, 102);
    """)

    return con


def test_export_plp_study_package(tmp_path):
    """Test generating reproducible OHDSI PLP R study packages."""
    pkg_dir = export_plp_study_package(
        output_dir=tmp_path,
        package_name="InpatientReadmissionPlp",
        target_id=1,
        outcome_id=2,
        db_path="cdm.duckdb",
        model_type="lasso",
        cv_folds=5,
        test_fraction=0.25,
    )

    assert pkg_dir.exists()
    assert (pkg_dir / "DESCRIPTION").exists()
    assert (pkg_dir / "NAMESPACE").exists()
    assert (pkg_dir / "R" / "Main.R").exists()
    assert (pkg_dir / "R" / "ShinyApp.R").exists()
    assert (pkg_dir / "inst" / "settings" / "study_specs.json").exists()
    assert (pkg_dir / "inst" / "shiny" / "app.R").exists()
    assert (pkg_dir / "inst" / "sql" / "cohorts.sql").exists()
    assert (pkg_dir / "README.md").exists()

    # Validate study specs json
    specs = json.loads((pkg_dir / "inst" / "settings" / "study_specs.json").read_text(encoding="utf-8"))
    assert specs["packageName"] == "InpatientReadmissionPlp"
    assert specs["targetCohortId"] == 1
    assert specs["outcomeCohortId"] == 2
    assert specs["modelType"] == "lasso"
    assert specs["crossValidationFolds"] == 5

    # Check R/Main.R contents
    main_r = (pkg_dir / "R" / "Main.R").read_text(encoding="utf-8")
    assert "PatientLevelPrediction::runPlp" in main_r
    assert "setLassoLogisticRegression" in main_r

    # Test with gradient boosting
    pkg_gbm = export_plp_study_package(
        output_dir=tmp_path,
        package_name="GbmStudy",
        model_type="gradient_boosting",
    )
    main_gbm = (pkg_gbm / "R" / "Main.R").read_text(encoding="utf-8")
    assert "setGradientBoostingMachine" in main_gbm


def test_andromeda_adapter(tmp_path, mock_cdm):
    """Test direct Andromeda SQLite export and zero-copy view adapter."""
    mock_cdm.execute("""
        CREATE TABLE covariates AS 
        SELECT 1 AS row_id, 201826001 AS covariate_id, 1.0 AS covariate_value
        UNION ALL
        SELECT 2 AS row_id, 316139001 AS covariate_id, 1.0 AS covariate_value;

        CREATE TABLE covariateref AS
        SELECT 201826001 AS covariate_id, 'Type 2 Diabetes' AS covariate_name, 102 AS analysis_id, 201826 AS concept_id
        UNION ALL
        SELECT 316139001 AS covariate_id, 'Heart Failure' AS covariate_name, 102 AS analysis_id, 316139 AS concept_id;

        CREATE TABLE analysisref AS
        SELECT 102 AS analysis_id, 'ConditionOccurrence' AS analysis_name, 'Condition' AS domain_id, 'Y' AS is_binary;
    """)

    sqlite_path = tmp_path / "andromeda_test.sqlite"
    out_path = export_to_andromeda(mock_cdm, sqlite_path, table_names=["covariates", "covariateref", "analysisref"])
    assert out_path.exists()

    adapter = AndromedaAdapter(sqlite_path)
    tables = [t.lower() for t in adapter.tables]
    assert "covariates" in tables
    assert "covariateref" in tables
    assert "analysisref" in tables

    # Test reading table
    covs_df = adapter.get_table("covariates")
    assert len(covs_df) == 2
    assert "covariate_id" in covs_df.columns

    # Test row count summary
    summary = adapter.summary()
    assert summary["covariates"] == 2
    assert summary["analysisref"] == 1

    # Test mounting back into a fresh DuckDB connection
    new_con = duckdb.connect(":memory:")
    adapter.mount_in_duckdb(new_con, schema="andr")
    res = new_con.execute("SELECT COUNT(*) FROM andr.covariates;" if "andr.covariates" in [t[0] for t in new_con.execute("SHOW TABLES;").fetchall()] else "SELECT COUNT(*) FROM covariates;").fetchone()[0]
    assert res == 2


def test_consort_strobe_flowchart_exporters(mock_cdm, tmp_path):
    """Test native Typst and LaTeX TikZ flowchart exports from CohortExtractor."""
    extractor = CohortExtractor(con=mock_cdm)
    extractor.define_index(
        visit_concept_ids=[9201],
        min_age=18,
        verified_followup_days=30,
        index_selection_rule="first",
    )

    assert extractor.attrition is not None
    att_df = extractor.to_dataframe()
    assert len(att_df) >= 3
    assert "subjects_retained" in att_df.columns
    # Person 3 is 10yo, so 1 pediatric dropped: 3 -> 2
    assert int(att_df.loc[att_df["step_number"] == 2, "subjects_retained"].values[0]) == 2

    # 1. Mermaid
    mermaid_str = extractor.to_mermaid()
    assert "flowchart TD" in mermaid_str
    assert "S1" in mermaid_str

    # 2. Typst
    typst_str = extractor.to_typst(output_path=tmp_path / "consort.typ")
    assert (tmp_path / "consort.typ").exists()
    assert "#align(center)" in typst_str
    assert "#rect" in typst_str
    assert "Step 1:" in typst_str
    assert "N = 2" in typst_str

    # 3. TikZ
    tikz_str = extractor.to_tikz(output_path=tmp_path / "consort.tex")
    assert (tmp_path / "consort.tex").exists()
    assert "\\begin{tikzpicture}" in tikz_str
    assert "\\node [box]" in tikz_str
    assert "\\end{tikzpicture}" in tikz_str


def test_clinical_sequence_tokens(mock_cdm, tmp_path):
    """Test Foundation Model clinical sequence tokenizer (EHR-SHOT / CEHR-BERT / FEMR)."""
    # Create cohort parquet
    cohort_df = pd.DataFrame({
        "person_id": [1, 2],
        "index_date": ["2020-05-01", "2020-06-01"],
        "y": [1, 0],
    })
    pq_path = tmp_path / "cohort.parquet"
    cohort_df.to_parquet(pq_path)

    tokens = export_clinical_sequence_tokens(
        con=mock_cdm,
        features_parquet=pq_path,
        max_seq_len=16,
    )

    assert isinstance(tokens, ClinicalSequenceTokens)
    assert tokens["input_ids"].shape == (2, 16)
    assert tokens["attention_mask"].shape == (2, 16)
    assert tokens["token_type_ids"].shape == (2, 16)

    # Check CLS token at position 0
    assert np.all(tokens["input_ids"][:, 0] == 2)
    # Check padding mask
    assert np.all(tokens["attention_mask"][:, 0] == 1)

    # Check extract_sard_visit_tensors with return_format='huggingface'
    sard_tokens = extract_sard_visit_tensors(
        con=mock_cdm,
        features_parquet=pq_path,
        return_format="huggingface",
        max_seq_len=16,
    )
    assert isinstance(sard_tokens, ClinicalSequenceTokens)
    assert sard_tokens["input_ids"].shape == (2, 16)


def test_federated_meta_analysis():
    """Test DerSimonian-Laird random-effects federated meta-analysis estimator."""
    # 5 hospital sites with log odds ratios and standard errors
    estimates = [0.45, 0.38, 0.52, 0.40, 0.49]
    std_errors = [0.08, 0.10, 0.09, 0.07, 0.11]
    sites = ["Temple", "PSU", "UPMC", "Hopkins", "Geisinger"]

    res = federated_meta_analysis(
        estimates=estimates,
        std_errors=std_errors,
        sites=sites,
        effect_type="log_or",
        model="random",
    )

    assert isinstance(res, FederatedMetaAnalysisResult)
    assert res.data["k_studies"] == 5
    assert 0.40 <= res.pooled_estimate <= 0.50
    assert res.se > 0.0
    assert res.ci_lower < res.pooled_estimate < res.ci_upper
    assert res.p_value < 0.001
    assert res.q_statistic >= 0.0
    assert res.i_squared >= 0.0
    assert res.tau_squared >= 0.0

    # Test summary DataFrame
    df = res.to_dataframe()
    assert len(df) == 6  # 5 sites + 1 pooled
    assert "Temple" in df["site"].values
    assert "Pooled (Random)" in df["site"].values

    # Test forest text representation
    forest_txt = res.to_forest_text()
    assert "Temple" in forest_txt
    assert "POOLED" in forest_txt
    assert "Heterogeneity:" in forest_txt

    # Test Mermaid representation
    mermaid_txt = res.to_forest_mermaid()
    assert "flowchart TD" in mermaid_txt
    assert "POOLED" in mermaid_txt
