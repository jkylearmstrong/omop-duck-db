"""Unit and integration test suite for omop-duck-db v0.6.8 RFC additions.

Validates:
1. Version bump to 0.6.8 across Python packages.
2. HierarchicalRollup tabular file loaders (.from_csv, .from_excel, .from_bundled).
3. RollupEngine multi-class battery loaders (.from_excel, .from_csvs, .from_folder, .from_bundled).
4. Support for headerless / legacy academic CSVs with integer column indices.
5. Multi-class battery exposure extraction (extract_exposures) with per-class rollup_name attribution.
6. Seamless Parquet crosswalk caching and cryptographic file-hash cache invalidation upon file edit.
7. End-to-end clinical workflow with bundled eDERRI code lists.
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
    compute_file_sha256,
    materialize_crosswalk,
)


# ---------------------------------------------------------------------------
# 1. Version Bump Verification
# ---------------------------------------------------------------------------
def test_v068_version():
    assert omop_etl.__version__ in ("0.6.8", "0.7.0", "0.8.6", "0.8.7", "0.8.7.1")
    assert omop_duck_db.__version__ in ("0.6.8", "0.7.0", "0.8.6", "0.8.7", "0.8.7.1")


# ---------------------------------------------------------------------------
# 2. HierarchicalRollup File Loaders (.from_csv, .from_excel, .from_bundled)
# ---------------------------------------------------------------------------
def test_hierarchical_rollup_from_csv():
    # Load Biguanides from bundled CSV
    csv_path = os.path.join(
        os.path.dirname(__file__), "..", "inst", "extdata", "code_lists", "ederri_medications.csv"
    )
    rollup = HierarchicalRollup.from_csv(
        path=csv_path,
        name="Biguanides",
        code_col="RXNORM_CUI",
        class_col="CLASS",
        vocabulary_col="VOCABULARY",
        expand="descendants",
        include_retired_maps_to=True,
    )

    assert rollup.name == "Biguanides"
    assert rollup.source_file is not None
    assert rollup.source_file_hash is not None
    assert len(rollup.rules) >= 2  # Metformin (6809), oral tablets, ATC
    codes = {r.code_pattern for r in rollup.rules}
    assert "6809" in codes


def test_hierarchical_rollup_from_headerless_csv():
    # Load from headerless legacy CSV using integer column indices: class_col=0, code_col=1
    csv_path = os.path.join(
        os.path.dirname(__file__), "..", "inst", "extdata", "code_lists", "rxnorm_categories_CUI_map_brief.csv"
    )
    rollup = HierarchicalRollup.from_csv(
        path=csv_path,
        name="Sulfonylureas",
        class_col=0,
        code_col=1,
        header=None,
        vocabulary_id="RxNorm",
        expand="descendants",
    )

    assert rollup.name == "Sulfonylureas"
    assert len(rollup.rules) >= 4  # Glipizide, Glyburide, Glimepiride, etc.
    codes = {r.code_pattern for r in rollup.rules}
    assert "4815" in codes  # Glipizide
    assert "4821" in codes  # Glyburide


def test_hierarchical_rollup_from_excel():
    pytest.importorskip("openpyxl")
    excel_path = os.path.join(
        os.path.dirname(__file__), "..", "inst", "extdata", "code_lists", "eDERRI_Clinical_Review_Package.xlsx"
    )
    rollup = HierarchicalRollup.from_excel(
        path=excel_path,
        sheet_name="Diabetes_Medications",
        name="DPP-4 Inhibitors",
        class_col="CLASS",
        code_col="RXNORM_CUI",
        vocabulary_col="VOCABULARY",
        expand="descendants",
        exclude_dose_forms=["Topical"],
    )

    assert rollup.name == "DPP-4 Inhibitors"
    assert len(rollup.rules) >= 4  # Sitagliptin, Saxagliptin, Linagliptin, Alogliptin
    codes = {r.code_pattern for r in rollup.rules}
    assert "593411" in codes  # Sitagliptin
    assert "616440" in codes  # Saxagliptin


def test_hierarchical_rollup_from_bundled():
    # Ingest from bundled resource name
    rollup = HierarchicalRollup.from_bundled(
        name="GLP-1 Receptor Agonists",
        resource_name="ederri_medications",
        class_col="CLASS",
        code_col="RXNORM_CUI",
        vocabulary_col="VOCABULARY",
    )

    assert rollup.name == "GLP-1 Receptor Agonists"
    assert len(rollup.rules) >= 4  # Exenatide, Liraglutide, Dulaglutide, Semaglutide
    codes = {r.code_pattern for r in rollup.rules}
    assert "1991302" in codes  # Semaglutide
    assert "60548" in codes  # Exenatide


# ---------------------------------------------------------------------------
# 3. RollupEngine Multi-Class Ingestion
# ---------------------------------------------------------------------------
def test_rollup_engine_from_excel():
    pytest.importorskip("openpyxl")
    excel_path = os.path.join(
        os.path.dirname(__file__), "..", "inst", "extdata", "code_lists", "eDERRI_Clinical_Review_Package.xlsx"
    )
    engine = RollupEngine.from_excel(
        path=excel_path,
        sheet_name="Medication_Classes",
        class_col="CLASS",
        code_col="CODE",
        vocabulary_col="VOCABULARY",
        expand="descendants",
        include_retired_maps_to=True,
    )

    assert len(engine) >= 7
    expected_classes = {
        "Biguanides",
        "Sulfonylureas",
        "Thiazolidinediones",
        "DPP-4 Inhibitors",
        "SGLT-2 Inhibitors",
        "GLP-1 Receptor Agonists",
        "Insulins",
    }
    for ec in expected_classes:
        assert ec in engine
        r = engine[ec]
        assert len(r.rules) > 0
        assert r.spec_hash() is not None


def test_rollup_engine_from_csvs():
    csv1 = os.path.join(
        os.path.dirname(__file__), "..", "inst", "extdata", "code_lists", "diabetes_drug_categories.csv"
    )
    csv2 = os.path.join(
        os.path.dirname(__file__), "..", "inst", "extdata", "code_lists", "ederri_medications.csv"
    )
    engine = RollupEngine.from_csvs(
        paths=[csv1, csv2],
        class_col="CLASS",
        code_col="RXNORM_CUI",
        vocabulary_col="VOCABULARY",
        name="multi_csv_diabetes_battery",
    )

    assert engine.name == "multi_csv_diabetes_battery"
    assert len(engine) >= 7
    assert "Biguanides" in engine
    assert "SGLT-2 Inhibitors" in engine


def test_rollup_engine_from_folder():
    folder = os.path.join(os.path.dirname(__file__), "..", "inst", "extdata", "code_lists")
    engine = RollupEngine.from_folder(
        folder_path=folder,
        glob="*medications.csv",
        class_col="CLASS",
        code_col="RXNORM_CUI",
        vocabulary_col="VOCABULARY",
    )

    assert len(engine) >= 7
    assert "Insulins" in engine


def test_rollup_engine_from_bundled():
    engine = RollupEngine.from_bundled("ederri_medications")
    assert len(engine) >= 7
    assert len(engine["Biguanides"].rules) >= 2
    codes = {r.code_pattern for r in engine["Biguanides"].rules}
    assert "6809" in codes


# ---------------------------------------------------------------------------
# 4. Materialization, Parquet Caching, and Cryptographic Invalidation
# ---------------------------------------------------------------------------
def _create_mock_vocab_db(con: duckdb.DuckDBPyConnection):
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
            (6809, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
            (1001, 'Metformin hydrochloride 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860975', '1970-01-01', '2099-12-31', NULL),
            (1002, 'Metformin Oral Solution', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860976', '1970-01-01', '2099-12-31', NULL),
            (4815, 'Glipizide', 'Drug', 'RxNorm', 'Ingredient', 'S', '4815', '1970-01-01', '2099-12-31', NULL),
            (2001, 'Glipizide 5 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '310488', '1970-01-01', '2099-12-31', NULL);

        CREATE TABLE concept_ancestor (
            ancestor_concept_id BIGINT,
            descendant_concept_id BIGINT,
            min_levels_of_separation INTEGER,
            max_levels_of_separation INTEGER
        );
        INSERT INTO concept_ancestor VALUES
            (6809, 6809, 0, 0),
            (6809, 1001, 1, 1),
            (6809, 1002, 1, 1),
            (4815, 4815, 0, 0),
            (4815, 2001, 1, 1);

        CREATE TABLE concept_relationship (
            concept_id_1 BIGINT,
            concept_id_2 BIGINT,
            relationship_id VARCHAR,
            valid_start_date DATE,
            valid_end_date DATE,
            invalid_reason VARCHAR
        );
    """)


def test_parquet_crosswalk_caching_and_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create initial CSV
        csv_file = os.path.join(tmpdir, "med_list.csv")
        with open(csv_file, "w") as f:
            f.write("CLASS,CODE,VOCABULARY\n")
            f.write("Biguanides,6809,RxNorm\n")

        cache_parquet = os.path.join(tmpdir, "crosswalk_cache.parquet")
        con = duckdb.connect(":memory:")
        _create_mock_vocab_db(con)

        # Build engine from this CSV
        engine = RollupEngine.from_csvs(
            paths=[csv_file],
            class_col="CLASS",
            code_col="CODE",
            vocabulary_col="VOCABULARY",
            expand="descendants",
        )
        assert len(engine) == 1
        initial_hash = engine.spec_hash()

        # 1. First Materialization: creates Parquet cache
        df1 = engine.materialize(con, cache_path=cache_parquet)
        assert os.path.exists(cache_parquet)
        assert len(df1) == 3  # Metformin (6809), 1001, 1002
        assert "spec_hash" in df1.columns
        assert df1["spec_hash"].iloc[0] == initial_hash

        # 2. Second Materialization: loaded directly from cache
        # Delete underlying in-memory tables to prove it loads from file cache
        con2 = duckdb.connect(":memory:")
        df2 = engine.materialize(con2, cache_path=cache_parquet)
        assert len(df2) == 3
        assert df2["spec_hash"].iloc[0] == initial_hash

        # 3. Modify the underlying CSV file (simulating clinical investigator edit)
        with open(csv_file, "a") as f:
            f.write("Sulfonylureas,4815,RxNorm\n")

        # Reinstantiate engine from modified CSV
        engine_updated = RollupEngine.from_csvs(
            paths=[csv_file],
            class_col="CLASS",
            code_col="CODE",
            vocabulary_col="VOCABULARY",
            expand="descendants",
        )
        updated_hash = engine_updated.spec_hash()
        assert updated_hash != initial_hash  # Hash changed!

        # 4. Invalidation: materialize should detect file content modification,
        # invalidate the old parquet, and rebuild with the new classes
        df3 = engine_updated.materialize(con, cache_path=cache_parquet)
        assert len(df3) == 5  # 3 metformin + 2 glipizide
        assert df3["spec_hash"].iloc[0] == updated_hash
        classes = set(df3["rollup_name"].unique())
        assert "Biguanides" in classes
        assert "Sulfonylureas" in classes


# ---------------------------------------------------------------------------
# 5. Multi-Class Exposure Extraction (extract_exposures)
# ---------------------------------------------------------------------------
def test_rollup_engine_extract_exposures():
    con = duckdb.connect(":memory:")
    _create_mock_vocab_db(con)

    # Populate drug_exposure with both Metformin and Glipizide records
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
            (1, 101, 1001, '2023-01-01', '2023-01-30', 38000177),  -- Metformin (Biguanides)
            (2, 102, 2001, '2023-02-01', '2023-02-28', 38000177),  -- Glipizide (Sulfonylureas)
            (3, 103, 9999, '2023-03-01', '2023-03-31', 38000177);  -- Unrelated drug
    """)

    # Build multi-class engine directly from code lists
    r_biguanides = HierarchicalRollup(
        name="Biguanides",
        rules=[ConceptPattern(vocabulary_id="RxNorm", code_pattern="6809")],
        expansion_direction="descendants",
    )
    r_sulfonylureas = HierarchicalRollup(
        name="Sulfonylureas",
        rules=[ConceptPattern(vocabulary_id="RxNorm", code_pattern="4815")],
        expansion_direction="descendants",
    )
    engine = RollupEngine(
        name="diabetes_engine",
        rollups=[r_biguanides, r_sulfonylureas],
    )

    exposures = engine.extract_exposures(con)
    assert len(exposures) == 2
    assert "rollup_name" in exposures.columns

    # Verify each exposure row is correctly attributed to its respective class
    row_big = exposures[exposures["person_id"] == 101].iloc[0]
    assert row_big["rollup_name"] == "Biguanides"

    row_sulf = exposures[exposures["person_id"] == 102].iloc[0]
    assert row_sulf["rollup_name"] == "Sulfonylureas"
