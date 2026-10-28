"""Unit and integration test suite for omop-duck-db v0.6.7+ RFC additions.

Validates:
1. Version bump to 0.6.7.
2. HierarchicalRollup declarative serialization and spec hashing.
3. Concept code prefix, wildcard (LIKE/ILIKE), and regex matching (resolve_concept_set).
4. RollupEngine hierarchy expansion and dose form exclusions.
5. In-database fuzzy matching on unmapped source codes (suggest_source_mappings).
6. Multi-domain provenance filters (filter_visits, filter_conditions, filter_measurements).
7. Materialized crosswalk Parquet caching and spec hashing (materialize_crosswalk).
8. Concept drift auditing across vocabulary versions (audit_concept_drift).
"""

from __future__ import annotations

import os
import tempfile
import duckdb
import pandas as pd
import pytest

import omop_duck_db
import omop_etl
from omop_duck_db.rollups import (
    ConceptPattern,
    HierarchicalRollup,
    RollupEngine,
    audit_concept_drift,
    materialize_crosswalk,
    resolve_concept_set,
)
from omop_etl import (
    DEFAULT_INPATIENT_VISIT_TYPE_CONCEPT_IDS,
    DEFAULT_OUTPATIENT_VISIT_TYPE_CONCEPT_IDS,
    filter_conditions,
    filter_measurements,
    filter_visits,
    suggest_source_mappings,
)


# ---------------------------------------------------------------------------
# 1. Version Bump
# ---------------------------------------------------------------------------
def test_v067_version():
    assert omop_etl.__version__ in ("0.6.7", "0.6.8", "0.7.0", "0.8.6", "0.8.6")
    assert omop_duck_db.__version__ in ("0.6.7", "0.6.8", "0.7.0", "0.8.6", "0.8.6")


# ---------------------------------------------------------------------------
# 2. HierarchicalRollup Declarative Specs & Hashing
# ---------------------------------------------------------------------------
def test_hierarchical_rollup_spec_and_hashing():
    rollup = HierarchicalRollup(
        name="antidiabetic_agents",
        domain="Drug",
        root_vocabulary="ATC",
        roots=["A10BA", "A10BB", "A10BK"],
        target_vocabulary="RxNorm",
        expansion_direction="descendants",
        include_retired_maps_to=True,
        exclude_dose_forms=["Topical", "Ophthalmic", "Otic"],
        combination_policy="attribute_all",
    )

    d = rollup.to_dict()
    assert d["name"] == "antidiabetic_agents"
    assert d["roots"] == ["A10BA", "A10BB", "A10BK"]
    assert d["combination_policy"] == "attribute_all"

    rebuilt = HierarchicalRollup.from_dict(d)
    assert rebuilt.name == rollup.name
    assert rebuilt.spec_hash() == rollup.spec_hash()
    assert len(rebuilt.spec_hash()) == 64  # SHA-256


# ---------------------------------------------------------------------------
# 3. Concept Code Prefix & Wildcard Matching
# ---------------------------------------------------------------------------
def test_concept_code_pattern_matching():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
            (101, 'Metformin Oral Solution', 'Drug', 'ATC', 'ATC 5th', 'C', 'A10BA02', '1970-01-01', '2099-12-31', NULL),
            (102, 'Glimepiride', 'Drug', 'ATC', 'ATC 5th', 'C', 'A10BB12', '1970-01-01', '2099-12-31', NULL),
            (103, 'Aspirin', 'Drug', 'ATC', 'ATC 5th', 'C', 'B01AC06', '1970-01-01', '2099-12-31', NULL),
            (201, 'Type 2 diabetes mellitus without complications', 'Condition', 'ICD10CM', 'ICD10 code', 'N', 'E11.9', '1970-01-01', '2099-12-31', NULL),
            (202, 'Heart failure, unspecified', 'Condition', 'ICD10CM', 'ICD10 code', 'N', 'I50.9', '1970-01-01', '2099-12-31', NULL),
            (301, 'Office outpatient visit 15 minutes', 'Procedure', 'CPT4', 'CPT4', 'S', '99213', '1970-01-01', '2099-12-31', NULL);
    """)

    # Match ATC A10B% (Oral blood glucose lowering drugs) and ICD10CM E11%
    patterns = [
        ConceptPattern(vocabulary_id="ATC", code_pattern="A10B%"),
        {"vocabulary_id": "ICD10CM", "code_pattern": "E11%"},
        "CPT4:992%",
    ]

    resolved = resolve_concept_set(con, patterns=patterns)
    assert len(resolved) == 4
    cids = set(resolved["concept_id"].tolist())
    assert 101 in cids
    assert 102 in cids
    assert 201 in cids
    assert 301 in cids
    assert 103 not in cids  # Aspirin excluded


# ---------------------------------------------------------------------------
# 4. RollupEngine Hierarchy Expansion & Dose Form Filtering
# ---------------------------------------------------------------------------
def test_rollup_engine_hierarchy_and_dose_forms():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
            (1, 'Biguanides', 'Drug', 'ATC', 'ATC 4th', 'C', 'A10BA', '1970-01-01', '2099-12-31', NULL),
            (10, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861004', '1970-01-01', '2099-12-31', NULL),
            (11, 'Metformin 1% Topical Cream', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861005', '1970-01-01', '2099-12-31', NULL),
            (12, 'Metformin 0.5% Ophthalmic Drops', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861006', '1970-01-01', '2099-12-31', NULL);

        CREATE TABLE concept_ancestor (
            ancestor_concept_id BIGINT,
            descendant_concept_id BIGINT,
            min_levels_of_separation INTEGER,
            max_levels_of_separation INTEGER
        );
        INSERT INTO concept_ancestor VALUES
            (1, 10, 1, 1),
            (1, 11, 1, 1),
            (1, 12, 1, 1);
    """)

    engine = RollupEngine(
        name="metformin_systemic",
        rules=[
            ConceptPattern(
                vocabulary_id="ATC",
                code_pattern="A10BA",
                expand="descendants",
                target_vocabulary="RxNorm",
            )
        ],
        exclude_forms=["Topical", "Ophthalmic"],
    )

    mat = engine.materialize(con)
    assert len(mat) == 1
    assert mat["concept_id"].iloc[0] == 10  # Only Oral Tablet retained, Topical & Ophthalmic excluded


# ---------------------------------------------------------------------------
# 5. RollupEngine Exposure Extraction with Provenance Filtering
# ---------------------------------------------------------------------------
def test_rollup_extract_exposures_with_provenance():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
            (10, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861004', '1970-01-01', '2099-12-31', NULL);

        CREATE TABLE drug_exposure (
            drug_exposure_id BIGINT,
            person_id BIGINT,
            drug_concept_id BIGINT,
            drug_exposure_start_date DATE,
            drug_exposure_end_date DATE,
            drug_type_concept_id BIGINT
        );
        INSERT INTO drug_exposure VALUES
            (1, 501, 10, '2023-01-01', '2023-01-30', 32838),      -- EHR prescription (prescribed)
            (2, 502, 10, '2023-02-01', '2023-02-28', 38000177),   -- Prescription written (prescribed)
            (3, 503, 10, '2023-03-01', '2023-03-31', 38000180);   -- Inpatient administration (not prescribed)
    """)

    engine = RollupEngine(
        name="metformin",
        rules=[ConceptPattern(vocabulary_id="RxNorm", code_pattern="861004")],
    )

    exposures = engine.extract_exposures(
        con,
        prescribed_types=[32838, 38000177],
    )
    assert len(exposures) == 2
    assert set(exposures["person_id"].tolist()) == {501, 502}


# ---------------------------------------------------------------------------
# 6. In-Database Fuzzy Matching for Unmapped Codes (concept_id = 0)
# ---------------------------------------------------------------------------
def test_fuzzy_matching_unmapped_codes():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
            (1503297, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
            (1515249, 'Glipizide', 'Drug', 'RxNorm', 'Ingredient', 'S', '4821', '1970-01-01', '2099-12-31', NULL),
            (316139, 'Heart failure', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '84114007', '1970-01-01', '2099-12-31', NULL);

        CREATE TABLE drug_exposure (
            drug_exposure_id BIGINT,
            drug_concept_id BIGINT,
            drug_source_value VARCHAR
        );
        INSERT INTO drug_exposure VALUES
            (1, 0, 'METFORMIN HCL 500MG TAB'),
            (2, 0, 'METFORMIN HCL 500MG TAB'),
            (3, 0, 'GLIPIZIDE 5MG TABLET');
    """)

    suggestions = suggest_source_mappings(
        con,
        unmapped_table="drug_exposure",
        domain="Drug",
        min_similarity=0.85,
    )
    assert not suggestions.empty
    assert "suggested_concept_id" in suggestions.columns
    assert "similarity_score" in suggestions.columns

    metformin_match = suggestions[suggestions["source_value"] == "METFORMIN HCL 500MG TAB"].iloc[0]
    assert metformin_match["suggested_concept_id"] == 1503297
    assert metformin_match["unmapped_count"] == 2
    assert metformin_match["similarity_score"] >= 0.85


# ---------------------------------------------------------------------------
# 7. Multi-Domain Provenance Recognition & Disambiguation
# ---------------------------------------------------------------------------
def test_multi_domain_provenance_filters():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE visit_occurrence (
            visit_occurrence_id BIGINT,
            person_id BIGINT,
            visit_type_concept_id BIGINT
        );
        INSERT INTO visit_occurrence VALUES
            (1, 10, 32035),    -- Inpatient EHR encounter
            (2, 20, 9202),     -- Outpatient
            (3, 30, 9203);     -- Emergency

        CREATE TABLE condition_occurrence (
            condition_occurrence_id BIGINT,
            person_id BIGINT,
            condition_type_concept_id BIGINT
        );
        INSERT INTO condition_occurrence VALUES
            (1, 10, 38000230),  -- Primary discharge diagnosis
            (2, 20, 38000199),  -- Admitting diagnosis
            (3, 30, 32020);     -- Problem list

        CREATE TABLE measurement (
            measurement_id BIGINT,
            person_id BIGINT,
            measurement_type_concept_id BIGINT
        );
        INSERT INTO measurement VALUES
            (1, 10, 44818702),  -- Lab result
            (2, 20, 32836),     -- Lab order
            (3, 30, 32828);     -- Point-of-care test
    """)

    # Visit filter (inpatient & emergency)
    v_filtered = filter_visits(con, kind=["inpatient", "emergency"])
    assert len(v_filtered) == 2
    assert set(v_filtered["visit_occurrence_id"].tolist()) == {1, 3}

    # Condition filter (primary only)
    c_filtered = filter_conditions(con, kind="primary")
    assert len(c_filtered) == 1
    assert c_filtered["condition_occurrence_id"].iloc[0] == 1

    # Measurement filter (lab results and POC)
    m_filtered = filter_measurements(con, kind=["lab_result", "point_of_care"])
    assert len(m_filtered) == 2
    assert set(m_filtered["measurement_id"].tolist()) == {1, 3}


# ---------------------------------------------------------------------------
# 8. Materialized Crosswalk Artifact & Caching
# ---------------------------------------------------------------------------
def test_materialize_crosswalk_parquet_cache():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
            (10, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861004', '1970-01-01', '2099-12-31', NULL);
    """)

    rollup = HierarchicalRollup(
        name="test_rollup",
        roots=["861004"],
        root_vocabulary="RxNorm",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        pq_path = os.path.join(tmpdir, "crosswalk.parquet")
        mat_df = materialize_crosswalk(rollup, con, output_path=pq_path)
        assert os.path.exists(pq_path)
        assert not mat_df.empty
        assert "spec_hash" in mat_df.columns

        read_df = pd.read_parquet(pq_path)
        assert len(read_df) == 1
        assert read_df["spec_hash"].iloc[0] == rollup.spec_hash()


# ---------------------------------------------------------------------------
# 9. Concept Drift Auditor Across Vocabulary Versions
# ---------------------------------------------------------------------------
def test_concept_drift_auditor():
    con_v1 = duckdb.connect(":memory:")
    con_v1.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
            (100, 'Drug A', 'Drug', 'RxNorm', 'Ingredient', 'S', '100', '1970-01-01', '2099-12-31', NULL),
            (200, 'Drug B', 'Drug', 'RxNorm', 'Ingredient', 'S', '200', '1970-01-01', '2099-12-31', NULL);
    """)

    con_v2 = duckdb.connect(":memory:")
    con_v2.execute("""
        CREATE TABLE concept (
            concept_id BIGINT,
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
        -- Drug A unchanged, Drug B deprecated (invalid_reason='D', standard_concept=NULL), Drug C added
        INSERT INTO concept VALUES
            (100, 'Drug A', 'Drug', 'RxNorm', 'Ingredient', 'S', '100', '1970-01-01', '2099-12-31', NULL),
            (200, 'Drug B', 'Drug', 'RxNorm', 'Ingredient', NULL, '200', '1970-01-01', '2025-01-01', 'D'),
            (300, 'Drug C', 'Drug', 'RxNorm', 'Ingredient', 'S', '300', '2025-01-01', '2099-12-31', NULL);
    """)

    rollup = HierarchicalRollup(
        name="test_drift",
        rules=[ConceptPattern(vocabulary_id="RxNorm", code_pattern="%")],
    )

    drift_report = audit_concept_drift(con_v1, con_v2, rollup)
    assert drift_report["old_total_concepts"] == 2
    assert drift_report["new_total_concepts"] == 3
    assert drift_report["added_concept_ids"] == [300]
    assert drift_report["removed_count"] == 0
    assert drift_report["status_changed_count"] == 1
    assert drift_report["status_changed"][0]["concept_id"] == 200
    assert drift_report["status_changed"][0]["new_invalid_reason"] == "D"
