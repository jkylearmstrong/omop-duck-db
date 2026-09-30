"""Tests for Phases 1-5: Location & SDoH, Cohort & Concept Ancestor Helpers,
Native DuckDB DQD Engine, ML Feature Matrix Extractor, and Enterprise Export.
"""

import json
import tempfile
from pathlib import Path
import sys
import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import (
    build_schema,
    combine_cohorts,
    compute_attrition,
    create_cohort,
    ensure_cohort_tables,
    export_cdm,
    extract_patient_features,
    get_cohort_summary,
    get_concept_ancestors,
    get_concept_descendants,
    load_location,
    load_person,
    resolve_concept_set,
    run_dqd,
)
from omop_etl.build_omop_cdm import load_macros
SAMPLE_DIR = REPO_ROOT / "tests" / "fixtures" / "pcornet_sample"


@pytest.fixture
def fresh_cdm():
    con = duckdb.connect(":memory:")
    build_schema(con)
    load_macros(con)
    return con


def test_load_location_and_person_linkage(fresh_cdm):
    con = fresh_cdm
    load_person(con, str(SAMPLE_DIR))
    load_location(con, str(SAMPLE_DIR))

    # Verify location rows
    loc_rows = con.execute("SELECT location_id, city, state, zip, country_concept_id FROM location ORDER BY location_id").fetchall()
    assert len(loc_rows) == 3
    cities = {r[1] for r in loc_rows}
    assert cities == {"Philadelphia", "Pittsburgh", "Camden"}

    # Verify person.location_id linkage
    linked = con.execute("SELECT person_id, location_id FROM person WHERE location_id IS NOT NULL").fetchall()
    assert len(linked) == 3


def test_load_location_missing_skips_gracefully(fresh_cdm):
    con = fresh_cdm
    with tempfile.TemporaryDirectory() as tmp:
        # Empty dir has no address file
        load_location(con, tmp)
        cnt = con.execute("SELECT COUNT(*) FROM location").fetchone()[0]
        assert cnt == 0


def test_cohort_and_concept_hierarchy_helpers(fresh_cdm):
    con = fresh_cdm
    ensure_cohort_tables(con)

    # Seed mock vocabulary
    con.execute("""
        INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
        VALUES 
            (10, 'Type 2 Diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'T2D', DATE '2000-01-01', DATE '2099-12-31'),
            (11, 'T2D with neuropathy', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'T2DN', DATE '2000-01-01', DATE '2099-12-31'),
            (12, 'Secondary Diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'SEC_D', DATE '2000-01-01', DATE '2099-12-31'),
            (13, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', 'MET', DATE '2000-01-01', DATE '2099-12-31');

        INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
        VALUES 
            (10, 10, 0, 0),
            (10, 11, 1, 1),
            (12, 12, 0, 0);
    """)

    # 1. get_concept_descendants
    desc = get_concept_descendants(con, 10, include_self=True)
    assert len(desc) == 2
    assert set(desc["descendant_concept_id"]) == {10, 11}

    desc_no_self = get_concept_descendants(con, 10, include_self=False)
    assert len(desc_no_self) == 1
    assert desc_no_self["descendant_concept_id"].iloc[0] == 11

    # 2. get_concept_ancestors
    anc = get_concept_ancestors(con, 11, include_self=False)
    assert len(anc) == 1
    assert anc["ancestor_concept_id"].iloc[0] == 10

    # 3. resolve_concept_set
    cset = resolve_concept_set(con, include_concepts=[10, 12], exclude_concepts=[12])
    assert cset == [10, 11]

    # 4. create_cohort
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (1001, 8507, 1980, 1, 1, 8527, 38003564),
            (1002, 8532, 1990, 5, 10, 8516, 38003564),
            (1003, 8507, 2010, 3, 15, 8527, 38003564);

        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id)
        VALUES 
            (1, 1001, 10, DATE '2020-01-15', 32020),
            (2, 1002, 11, DATE '2020-03-01', 32020),
            (3, 1003, 10, DATE '2020-06-20', 32020);
    """)

    n = create_cohort(
        con,
        cohort_id=1,
        cohort_name="T2D Cohort",
        entry_sql="SELECT person_id, condition_start_date AS start_date FROM condition_occurrence WHERE condition_concept_id IN (10, 11)",
        exit_rule="fixed_days",
        exit_offset_days=365,
    )
    assert n == 3

    # 5. compute_attrition
    attrition = compute_attrition(
        con,
        cohort_id=1,
        steps=[
            ("Initial T2D diagnosis", "SELECT subject_id FROM cohort WHERE cohort_definition_id = 1"),
            ("Adults only (Age >= 18)", "SELECT c.subject_id FROM cohort c JOIN person p ON c.subject_id = p.person_id WHERE date_diff('year', make_date(p.year_of_birth, coalesce(p.month_of_birth, 1), coalesce(p.day_of_birth, 1)), c.cohort_start_date) >= 18"),
            ("Female patients", "SELECT c.subject_id FROM _step_current c JOIN person p ON c.subject_id = p.person_id WHERE p.gender_concept_id = 8532"),
        ]
    )
    assert len(attrition) == 3
    assert attrition["subjects_retained"].tolist() == [3, 2, 1]
    assert attrition["subjects_dropped"].tolist() == [0, 1, 1]

    # 6. combine_cohorts
    create_cohort(
        con,
        cohort_id=2,
        cohort_name="Single Female Cohort",
        entry_sql="SELECT person_id, DATE '2020-03-01' AS start_date FROM person WHERE person_id = 1002",
        exit_rule="fixed_days",
        exit_offset_days=365,
    )
    intersect_n = combine_cohorts(con, new_cohort_id=3, cohort_id_a=1, cohort_id_b=2, operation="INTERSECT")
    assert intersect_n == 1

    # 7. get_cohort_summary
    summary = get_cohort_summary(con, cohort_id=1)
    assert summary["total_subjects"].iloc[0] == 3
    assert summary["mean_duration_days"].iloc[0] == 365.0


def test_native_dqd(fresh_cdm):
    con = fresh_cdm
    load_person(con, str(SAMPLE_DIR))

    with tempfile.TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "dqd_test.json"
        res = run_dqd(con, output_json=json_path, check_levels=["TABLE", "FIELD", "TEMPORAL"])

        assert "Metadata" in res
        assert "Overview" in res
        assert "CheckResults" in res
        assert res["Overview"]["count_total"] > 0
        assert json_path.exists()

        with open(json_path) as f:
            saved = json.load(f)
            assert saved["Overview"]["count_total"] == res["Overview"]["count_total"]

    # Test intentional temporal check failure
    con.execute("""
        INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
        VALUES (999, 1, 9201, DATE '2022-05-10', DATE '2022-05-01', 32827);
    """)
    res_violation = run_dqd(con, check_levels=["TEMPORAL"])
    failed_checks = [r for r in res_violation["CheckResults"] if r["failed"]]
    assert any("TEMPORAL_START_BEFORE_END_VISIT_OCCURRENCE" in r["check_id"] for r in failed_checks)


def test_ml_feature_extractor(fresh_cdm):
    con = fresh_cdm
    # Seed person, condition, drug
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (501, 8507, 1975, 6, 15, 8527, 38003564),
            (502, 8532, 1985, 10, 20, 8516, 38003564);

        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id)
        VALUES 
            (1, 501, 201826, DATE '2021-05-01', 32020),
            (2, 501, 201826, DATE '2021-05-20', 32020);

        INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
        VALUES (1, 501, 1503297, DATE '2021-05-15', DATE '2021-06-15', 32838);
    """)

    ensure_cohort_tables(con)
    create_cohort(
        con,
        cohort_id=10,
        cohort_name="Index Cohort",
        entry_sql="SELECT person_id, DATE '2021-06-01' AS start_date FROM person",
        exit_rule="fixed_days",
        exit_offset_days=180,
    )
    create_cohort(
        con,
        cohort_id=20,
        cohort_name="Outcome Cohort",
        entry_sql="SELECT 501 AS person_id, DATE '2021-07-01' AS start_date",
        exit_rule="fixed_days",
        exit_offset_days=30,
    )

    # 1. Dense DataFrame format
    df_dense = extract_patient_features(
        con,
        cohort_id=10,
        outcome_cohort_id=20,
        lookback_days=[30, 90, 365],
        format="df",
    )
    assert len(df_dense) == 2
    assert "y" in df_dense.columns
    assert "age_at_index" in df_dense.columns
    assert "condition_count_30d" in df_dense.columns
    assert "drug_count_30d" in df_dense.columns

    p501 = df_dense[df_dense["subject_id"] == 501].iloc[0]
    assert p501["y"] == 1
    assert p501["condition_count_30d"] == 1
    assert p501["condition_count_90d"] == 2
    assert p501["drug_count_30d"] == 1

    p502 = df_dense[df_dense["subject_id"] == 502].iloc[0]
    assert p502["y"] == 0
    assert p502["condition_count_30d"] == 0

    # 2. PyArrow format
    arrow_tbl = extract_patient_features(con, cohort_id=10, format="arrow")
    assert arrow_tbl.num_rows == 2

    # 3. Sparse format
    sparse_df = extract_patient_features(con, cohort_id=10, format="sparse")
    assert "row_id" in sparse_df.columns
    assert "covariate_id" in sparse_df.columns
    assert "covariate_value" in sparse_df.columns


def test_enterprise_export(fresh_cdm):
    con = fresh_cdm
    load_person(con, str(SAMPLE_DIR))

    with tempfile.TemporaryDirectory() as tmp:
        # Parquet export
        pq_dir = Path(tmp) / "parquet_out"
        res_pq = export_cdm(con, target_type="parquet", output_path=pq_dir)
        assert res_pq["target_type"] == "parquet"
        assert (pq_dir / "person.parquet").exists()

        # CSV export
        csv_dir = Path(tmp) / "csv_out"
        res_csv = export_cdm(con, target_type="csv", output_path=csv_dir)
        assert (csv_dir / "person.csv").exists()

        # Oracle export
        ora_dir = Path(tmp) / "oracle_out"
        res_ora = export_cdm(con, target_type="oracle", output_path=ora_dir)
        assert (ora_dir / "person.csv").exists()
        assert (ora_dir / "person.ctl").exists()
        assert (ora_dir / "omop_oracle_ddl.sql").exists()
        assert (ora_dir / "load_oracle.sh").exists()
