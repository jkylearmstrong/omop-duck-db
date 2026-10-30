"""Tests for v0.6.5 features and enhancements."""

import tempfile
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    __version__,
    audit_combination_products,
    cohort_from_index_stays,
    compare_feature_tables,
    export_companion_manifest,
    extract_medication_classes,
    provenance,
    vocabulary_preflight,
)
from omop_etl.build_omop_cdm import load_drug_exposure, load_macros


def test_package_version_and_commit():
    """Verify version is at least 0.6.5 and commit is present."""
    assert __version__ in ("0.6.5", "0.6.6", "0.6.7", "0.6.8", "0.7.0", "0.8.6", "0.8.7", "0.8.7.1", "0.9.0", "0.10.0")
    import omop_etl
    assert hasattr(omop_etl, "__commit__")
    assert len(omop_etl.__commit__) >= 7


def test_multi_vocab_pcornet_etl():
    """Verify PCORnet MED_ADMIN and DISPENSING multi-vocabulary routing (NDC, HCPCS, RxNorm)."""
    con = duckdb.connect(":memory:")
    load_macros(con, skip_unresolved=True)

    # Minimal OMOP tables
    con.execute("""
    CREATE TABLE person (person_id BIGINT, person_source_value VARCHAR);
    INSERT INTO person VALUES (1, 'PAT1'), (2, 'PAT2');

    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_source_value VARCHAR);
    INSERT INTO visit_occurrence VALUES (101, 1, 'ENC1'), (102, 2, 'ENC2');

    CREATE TABLE provider (provider_id BIGINT, provider_source_value VARCHAR);

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
        (19000001, 'Target RxNorm Clinical Drug', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '12345', '1970-01-01', '2099-12-31', NULL),
        (20000001, 'Source NDC Concept', 'Drug', 'NDC', '11-digit NDC', NULL, '00002821501', '1970-01-01', '2099-12-31', NULL),
        (30000001, 'Source HCPCS Concept', 'Drug', 'HCPCS', 'HCPCS', NULL, 'J0135', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_relationship (
        concept_id_1 BIGINT,
        concept_id_2 BIGINT,
        relationship_id VARCHAR,
        valid_start_date DATE,
        valid_end_date DATE,
        invalid_reason VARCHAR
    );
    INSERT INTO concept_relationship VALUES
        (20000001, 19000001, 'Maps to', '1970-01-01', '2099-12-31', NULL),
        (30000001, 19000001, 'Maps to', '1970-01-01', '2099-12-31', NULL),
        (19000001, 19000001, 'Maps to', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE drug_exposure (
        drug_exposure_id BIGINT,
        person_id BIGINT,
        drug_concept_id BIGINT,
        drug_exposure_start_date DATE,
        drug_exposure_start_datetime TIMESTAMP,
        drug_exposure_end_date DATE,
        drug_exposure_end_datetime TIMESTAMP,
        verbatim_end_date DATE,
        drug_type_concept_id BIGINT,
        stop_reason VARCHAR,
        refills INTEGER,
        quantity NUMERIC,
        days_supply INTEGER,
        sig VARCHAR,
        route_concept_id BIGINT,
        lot_number VARCHAR,
        provider_id BIGINT,
        visit_occurrence_id BIGINT,
        visit_detail_id BIGINT,
        drug_source_value VARCHAR,
        drug_source_concept_id BIGINT,
        route_source_value VARCHAR,
        dose_unit_source_value VARCHAR
    );

    CREATE TABLE source_to_concept_map (
        source_code VARCHAR,
        source_concept_id BIGINT,
        source_vocabulary_id VARCHAR,
        source_code_description VARCHAR,
        target_concept_id BIGINT,
        target_vocabulary_id VARCHAR,
        valid_start_date DATE,
        valid_end_date DATE,
        invalid_reason VARCHAR
    );
    """)

    # Raw PCORnet tables
    med_admin_df = pd.DataFrame([
        # Row 1: NDC coded administration
        {"medadminid": "MA1", "patid": "PAT1", "encounterid": "ENC1", "medadmin_code": "00002821501", "medadmin_type": "ND", "medadmin_start_date": "2023-01-01"},
        # Row 2: HCPCS coded administration
        {"medadminid": "MA2", "patid": "PAT1", "encounterid": "ENC1", "medadmin_code": "J0135", "medadmin_type": "CH", "medadmin_start_date": "2023-01-02"},
        # Row 3: RxNorm CUI administration
        {"medadminid": "MA3", "patid": "PAT2", "encounterid": "ENC2", "medadmin_code": "12345", "medadmin_type": "RX", "medadmin_start_date": "2023-01-03"},
    ])
    dispensing_df = pd.DataFrame([
        # Row 4: NDC dispensing
        {"dispensingid": "DISP1", "patid": "PAT1", "prescribingid": None, "raw_rx_ndc": "00002821501", "dispensing_date": "2023-01-10", "dispensing_sup": 30, "dispensing_amt": 30, "dispensing_type": "ND"},
        # Row 5: RxNorm dispensing
        {"dispensingid": "DISP2", "patid": "PAT2", "prescribingid": None, "raw_rx_ndc": "12345", "dispensing_type": "RX", "dispensing_date": "2023-01-11", "dispensing_sup": 30, "dispensing_amt": 30},
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        med_admin_df.to_csv(Path(tmpdir) / "med_admin.csv", index=False)
        dispensing_df.to_csv(Path(tmpdir) / "dispensing.csv", index=False)
        load_drug_exposure(con, tmpdir)

    res = con.execute("SELECT drug_exposure_id, person_id, drug_concept_id, drug_source_value, drug_type_concept_id FROM drug_exposure ORDER BY drug_exposure_id").df()
    assert len(res) == 5
    # All rows should map to the standard RxNorm concept 19000001!
    assert (res["drug_concept_id"] == 19000001).all()
    # Check drug_type_concept_id
    # MedAdmin -> 38000180 (Inpatient administration)
    # Dispensing -> 38000175 (Prescription dispensed in pharmacy)
    types = res["drug_type_concept_id"].tolist()
    assert types[:3] == [38000180, 38000180, 38000180]
    assert types[3:] == [38000175, 38000175]


def test_vocab_schema_and_preflight_drug_types():
    """Verify native attached/split vocabulary schema support and drug exposure type breakdowns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        vocab_path = Path(tmpdir) / "central_vocab.duckdb"
        v_con = duckdb.connect(str(vocab_path))
        v_con.execute("""
        CREATE TABLE concept (
            concept_id BIGINT, concept_name VARCHAR, domain_id VARCHAR,
            vocabulary_id VARCHAR, concept_class_id VARCHAR, standard_concept VARCHAR,
            concept_code VARCHAR, valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR
        );
        INSERT INTO concept VALUES
            (111, 'Test Drug', 'Drug', 'RxNorm', 'Ingredient', 'S', '111', '1970-01-01', '2099-12-31', NULL),
            (222, 'Test Drug 2', 'Drug', 'RxNorm', 'Ingredient', 'S', '222', '1970-01-01', '2099-12-31', NULL);
        CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT, min_levels_of_separation INT, max_levels_of_separation INT);
        INSERT INTO concept_ancestor VALUES (111, 111, 0, 0), (222, 222, 0, 0);
        CREATE TABLE vocabulary (vocabulary_id VARCHAR, vocabulary_name VARCHAR, vocabulary_version VARCHAR);
        INSERT INTO vocabulary VALUES ('RxNorm', 'RxNorm', 'RxNorm 2026-01-01');
        """)
        v_con.close()

        # Site db without local vocabulary tables
        con = duckdb.connect(":memory:")
        con.execute(f"ATTACH '{vocab_path}' AS central_vocab")

        con.execute("""
        CREATE TABLE person (person_id BIGINT, year_of_birth INT, gender_concept_id INT, race_concept_id INT, ethnicity_concept_id INT);
        INSERT INTO person VALUES (1, 1980, 8507, 0, 0);
        CREATE TABLE drug_exposure (drug_exposure_id BIGINT, person_id BIGINT, drug_concept_id BIGINT, drug_type_concept_id BIGINT, drug_exposure_start_date DATE);
        INSERT INTO drug_exposure VALUES
            (1, 1, 111, 38000177, '2023-01-01'),  -- prescribed
            (2, 1, 111, 38000180, '2023-01-02'),  -- administered
            (3, 1, 222, 38000175, '2023-01-03');  -- dispensed
        """)

        # Call vocabulary_preflight with vocab_schema
        res = vocabulary_preflight(con, required=["RxNorm"], vocab_schema="central_vocab")
        assert res["vocabulary_reachable"] is True
        assert res["concept_count"] == 2
        assert "drug_types" in res
        assert res["drug_types"]["prescribed"] == 1
        assert res["drug_types"]["administered"] == 1
        assert res["drug_types"]["dispensed"] == 1
        assert res["drug_type_breakdown"]["prescribed_pct"] == pytest.approx(33.33, abs=0.5)

        # Call provenance with vocab_schema
        prov = provenance(con, vocab_schema="central_vocab")
        assert prov["vocabulary"]["reachable"] is True
        assert prov["vocabulary"]["version_string"] == "RxNorm 2026-01-01"


def test_combination_products_audit_and_extraction():
    """Verify combination product disaggregation helper and extract_medication_classes."""
    con = duckdb.connect(":memory:")
    con.execute("""
    CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, concept_class_id VARCHAR);
    INSERT INTO concept VALUES
        (1001, 'Metformin', 'Ingredient'),
        (1002, 'Sitagliptin', 'Ingredient'),
        (2001, 'Metformin 500 MG Oral Tablet', 'Clinical Drug'),
        (2002, 'Metformin 500 MG / Sitagliptin 50 MG Oral Tablet', 'Clinical Drug');

    CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT);
    INSERT INTO concept_ancestor VALUES
        (1001, 1001),
        (1002, 1002),
        (1001, 2001),  -- 2001 descends only from 1001 (plain)
        (1001, 2002),  -- 2002 descends from both 1001 and 1002 (combination)
        (1002, 2002);

    CREATE TABLE cohort (person_id BIGINT, cohort_start_date DATE, index_date DATE);
    INSERT INTO cohort VALUES
        (1, '2023-01-01', '2023-01-01'),  -- plain only
        (2, '2023-01-01', '2023-01-01'),  -- combo only
        (3, '2023-01-01', '2023-01-01'),  -- both (overlapping)
        (4, '2023-01-01', '2023-01-01');  -- unexposed

    CREATE TABLE drug_exposure (person_id BIGINT, drug_concept_id BIGINT, drug_exposure_start_date DATE, drug_source_value VARCHAR);
    INSERT INTO drug_exposure VALUES
        (1, 2001, '2023-01-05', 'Metformin plain'),
        (2, 2002, '2023-01-05', 'Metformin combo'),
        (3, 2001, '2023-01-05', 'Metformin plain'),
        (3, 2002, '2023-01-06', 'Metformin combo');
    """)

    # 1. Test audit_combination_products
    audit = audit_combination_products(con, class_concept_id=1001)
    assert audit["total_patients"] == 3
    assert audit["plain_only_patients"] == 1
    assert audit["combination_only_patients"] == 1
    assert audit["overlapping_patients"] == 1
    assert audit["combination_share_patients"] == pytest.approx(0.6667, abs=0.01)

    # 2. Test extract_medication_classes with disaggregate_combinations=True
    specs = {"metformin": [1001]}
    res = extract_medication_classes(con, "cohort", specs, strategy="standard", lookahead_days=30, disaggregate_combinations=True)
    assert "metformin_plain" in res.columns
    assert "metformin_combination" in res.columns
    assert "metformin" in res.columns

    p_map = res.set_index("person_id")
    # Patient 1: plain=1, combo=0, overall=1
    assert p_map.loc[1, "metformin_plain"] == 1
    assert p_map.loc[1, "metformin_combination"] == 0
    assert p_map.loc[1, "metformin"] == 1
    # Patient 2: plain=0, combo=1, overall=1
    assert p_map.loc[2, "metformin_plain"] == 0
    assert p_map.loc[2, "metformin_combination"] == 1
    assert p_map.loc[2, "metformin"] == 1
    # Patient 3: plain=1, combo=1, overall=1
    assert p_map.loc[3, "metformin_plain"] == 1
    assert p_map.loc[3, "metformin_combination"] == 1
    assert p_map.loc[3, "metformin"] == 1
    # Patient 4: unexposed
    assert p_map.loc[4, "metformin"] == 0


def test_standardized_dose_form_filtering():
    """Verify dose-form filtering via 'RxNorm has dose form' relationship and source exclusions."""
    con = duckdb.connect(":memory:")
    con.execute("""
    CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, concept_class_id VARCHAR);
    INSERT INTO concept VALUES
        (500, 'Hydrocortisone', 'Ingredient'),
        (501, 'Hydrocortisone Oral Tablet', 'Clinical Drug'),
        (502, 'Hydrocortisone 1% Topical Gel', 'Clinical Drug'),
        (901, 'Oral Tablet', 'Dose Form'),
        (902, 'Topical Gel', 'Dose Form');

    CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR);
    INSERT INTO concept_relationship VALUES
        (501, 901, 'RxNorm has dose form'),
        (502, 902, 'RxNorm has dose form');

    CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT);
    INSERT INTO concept_ancestor VALUES
        (500, 500),
        (500, 501),
        (500, 502);

    CREATE TABLE cohort (person_id BIGINT, cohort_start_date DATE, index_date DATE);
    INSERT INTO cohort VALUES
        (1, '2023-01-01', '2023-01-01'),  -- oral only
        (2, '2023-01-01', '2023-01-01');  -- topical only

    CREATE TABLE drug_exposure (person_id BIGINT, drug_concept_id BIGINT, drug_exposure_start_date DATE, drug_source_value VARCHAR);
    INSERT INTO drug_exposure VALUES
        (1, 501, '2023-01-05', 'Hydrocortisone tab'),
        (2, 502, '2023-01-05', 'Hydrocortisone gel');
    """)

    specs = {"steroid": [500]}

    # Test filtering by explicit dose_form_concept_id 902
    res = extract_medication_classes(con, "cohort", specs, strategy="standard", lookahead_days=30, exclude_dose_form_concept_ids=[902])
    p_map = res.set_index("person_id")
    assert p_map.loc[1, "steroid"] == 1
    assert p_map.loc[2, "steroid"] == 0

    # Test apply_exclude_to_source with lexical string 'gel'
    res_both = extract_medication_classes(
        con, "cohort", specs, strategy="both", lookahead_days=30,
        crosswalk=pd.DataFrame([{"source_value": "HYDROCORTISONE GEL", "drug_class": "steroid"},
                                {"source_value": "HYDROCORTISONE TAB", "drug_class": "steroid"}]),
        exclude_forms=["gel"], apply_exclude_to_source=True
    )
    p_both = res_both.set_index("person_id")
    assert p_both.loc[1, "steroid_source"] == 1
    assert p_both.loc[2, "steroid_source"] == 0
    assert p_both.loc[2, "steroid_standard"] == 0


def test_companion_manifest_and_compare_feature_tables():
    """Verify companion manifest exporter preserves row order and integrates into compare_feature_tables."""
    con = duckdb.connect(":memory:")
    con.execute("""
    CREATE TABLE person (person_id BIGINT, person_source_value VARCHAR);
    INSERT INTO person VALUES (10, 'MRN001'), (20, 'MRN002'), (30, 'MRN003');

    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_source_value VARCHAR, visit_start_date DATE);
    INSERT INTO visit_occurrence VALUES
        (1001, 10, 'ENC_A', '2023-01-01'),
        (1002, 20, 'ENC_B', '2023-02-01'),
        (1003, 30, 'ENC_C', '2023-03-01');
    """)

    # 1. Input cohort df with person_id and visit_occurrence_id (missing source values)
    cohort_df = pd.DataFrame({
        "person_id": [30, 10, 20],  # specific non-sorted order
        "visit_occurrence_id": [1003, 1001, 1002],
        "index_date": ["2023-03-01", "2023-01-01", "2023-02-01"],
    })

    manifest = export_companion_manifest(cohort_df, con=con)
    assert list(manifest.columns) == ["person_id", "person_source_value", "visit_occurrence_id", "visit_source_value", "index_date"]
    assert len(manifest) == 3
    # Check strict row order preservation
    assert manifest["person_id"].tolist() == [30, 10, 20]
    assert manifest["person_source_value"].tolist() == ["MRN003", "MRN001", "MRN002"]
    assert manifest["visit_source_value"].tolist() == ["ENC_C", "ENC_A", "ENC_B"]

    # 2. Test feature table comparison using manifests
    # Suppose ML feature matrices strip person_id and keep only features
    left_features = pd.DataFrame({
        "feat_age": [65.0, 45.0, 55.0],
        "feat_bmi": [28.0, 24.0, 31.0],
    })
    right_features = pd.DataFrame({
        "feat_age": [65.0, 45.0, 54.5],
        "feat_bmi": [28.2, 24.1, 30.9],
    })

    ledger = compare_feature_tables(
        left_features, right_features,
        left_manifest=manifest, right_manifest=manifest,
        min_cell_count=1
    )
    assert not ledger.empty
    assert "feat_age" in ledger["feature"].values
    assert "feat_bmi" in ledger["feature"].values
    assert (ledger["matched_n"] == 3).all()


def test_cohort_from_index_stays_metrics_and_group_by():
    """Verify encounter_match_rate and group_by in cohort_from_index_stays."""
    con = duckdb.connect(":memory:")
    con.execute("""
    CREATE TABLE person (person_id BIGINT, person_source_value VARCHAR);
    INSERT INTO person VALUES (1, 'P1'), (2, 'P2'), (3, 'P3');

    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_source_value VARCHAR, visit_start_date DATE);
    INSERT INTO visit_occurrence VALUES (101, 1, 'V1', '2023-01-01');
    """)

    stays = pd.DataFrame([
        {"person_source_value": "P1", "visit_source_value": "V1", "index_date": "2023-01-01", "site": "SiteA"},
        {"person_source_value": "P2", "visit_source_value": "V2_missing", "index_date": "2023-01-02", "site": "SiteA"},
        {"person_source_value": "P3", "visit_source_value": "V3_missing", "index_date": "2023-01-03", "site": "SiteB"},
    ])

    cohort_df, audit = cohort_from_index_stays(con, stays, group_by="SITE")

    assert "encounter_match_rate" in audit
    # 1 matched encounter out of 3 stays = 0.3333
    assert audit["encounter_match_rate"] == pytest.approx(0.3333, abs=0.01)
    assert "by_site" in audit
    assert "SiteA" in audit["by_site"]
    assert "SiteB" in audit["by_site"]
    assert audit["by_site"]["SiteA"]["matched_encounters"] == 1
    assert audit["by_site"]["SiteB"]["matched_encounters"] == 0
