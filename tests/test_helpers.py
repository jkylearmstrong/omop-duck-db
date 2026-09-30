"""Tests for vocabulary loaders, STCM/Usagi mapping, retroactive remapping,
and modular table handling (including death)."""

import csv
import os
import sys
import tempfile
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import (
    build_condition_era,
    build_drug_era,
    build_observation_period,
    build_schema,
    check_vocabulary_version,
    export_unmapped_codes,
    import_source_to_concept_map,
    import_usagi_mappings,
    load_death,
    load_drug_exposure,
    load_person,
    load_provider,
    load_vocabulary,
    remap_all,
    remap_cdm_table,
)
from omop_etl.build_omop_cdm import load_macros

TINY_VOCAB_DIR = REPO_ROOT / "tests" / "testthat" / "testdata" / "tiny_vocab"


def test_python_load_vocabulary_and_check_version():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "vocab_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        con.close()

        ok = load_vocabulary(str(TINY_VOCAB_DIR), db_path=db_path)
        assert ok is True

        con = duckdb.connect(db_path)
        version_info = check_vocabulary_version(con)
        assert version_info["total_concepts"] == 2
        assert "FAKEVOCAB" in version_info["vocabularies"]

        rows = con.execute("SELECT concept_id, concept_code FROM concept ORDER BY concept_id").fetchall()
        assert rows == [(1, "FAKE001"), (2, "FAKE002")]
        con.close()


def test_cpt4_vocabulary_sanitization():
    with tempfile.TemporaryDirectory() as tmp:
        vocab_dir = Path(tmp) / "cpt4_vocab"
        vocab_dir.mkdir()

        # Create a CONCEPT.csv with a CPT4 concept having empty/NULL concept_name
        cpt4_concept_csv = vocab_dir / "CONCEPT.csv"
        with open(cpt4_concept_csv, "w", newline="", encoding="utf-8") as f:
            f.write(
                "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\tstandard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
            )
            f.write("1001\t\tProcedure\tCPT4\tCPT4\tS\t99213\t20200101\t20991231\t\n")

        db_path = str(Path(tmp) / "cpt4_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        con.close()

        ok = load_vocabulary(str(vocab_dir), db_path=db_path, sanitize_cpt4=True)
        assert ok is True

        con = duckdb.connect(db_path)
        row = con.execute("SELECT concept_id, concept_name, concept_code FROM concept WHERE concept_id = 1001").fetchone()
        assert row == (1001, "CPT4 99213", "99213")
        con.close()


def test_source_to_concept_map_and_usagi_import():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "stcm_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)

        # 1. Test custom STCM CSV import
        stcm_csv = Path(tmp) / "custom_stcm.csv"
        with open(stcm_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["source_code", "target_concept_id", "source_vocabulary_id", "source_code_description"])
            writer.writerow(["LOCAL-LAB-01", 3004410, "CustomLab", "Hemoglobin A1c local assay"])
            writer.writerow(["LOCAL-LAB-02", 3004411, "CustomLab", "Glucose fast local"])

        n = import_source_to_concept_map(con, str(stcm_csv))
        assert n == 2

        # 2. Test Usagi export CSV import
        usagi_csv = Path(tmp) / "usagi_export.csv"
        with open(usagi_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["sourceCode", "sourceName", "sourceFrequency", "mappingStatus", "targetConceptId", "targetVocabularyId"])
            writer.writerow(["LOCAL-DRUG-01", "Amoxicillin Susp Local", "500", "APPROVED", 1713332, "RxNorm"])
            writer.writerow(["LOCAL-DRUG-99", "Unverified Med", "10", "UNCHECKED", 0, ""])

        n_usagi = import_usagi_mappings(con, str(usagi_csv), source_vocabulary_id="LocalDrug", approved_only=True)
        assert n_usagi == 1

        rows = con.execute("SELECT source_code, target_concept_id FROM source_to_concept_map ORDER BY source_code").fetchall()
        assert ("LOCAL-DRUG-01", 1713332) in rows
        assert ("LOCAL-LAB-01", 3004410) in rows

        con.close()


def test_export_unmapped_codes_for_usagi():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "unmapped_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)

        con.execute("""
            INSERT INTO drug_exposure (
                drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
                drug_type_concept_id, drug_source_value
            ) VALUES
            (1, 1, 0, DATE '2024-01-01', DATE '2024-01-01', 32838, 'CUSTOM-MED-A'),
            (2, 1, 0, DATE '2024-01-02', DATE '2024-01-02', 32838, 'CUSTOM-MED-A'),
            (3, 2, 0, DATE '2024-01-03', DATE '2024-01-03', 32838, 'CUSTOM-MED-B'),
            (4, 2, 19078461, DATE '2024-01-04', DATE '2024-01-04', 32838, 'MAPPED-MED');
        """)

        out_csv = Path(tmp) / "unmapped_drugs.csv"
        count = export_unmapped_codes(con, "drug_exposure", str(out_csv), min_frequency=1)
        assert count == 2

        with open(out_csv, encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 2
            assert reader[0]["sourceCode"] == "CUSTOM-MED-A"
            assert reader[0]["frequency"] == "2"
            assert reader[1]["sourceCode"] == "CUSTOM-MED-B"
            assert reader[1]["frequency"] == "1"

        con.close()


def test_retroactive_remapping_and_era_rebuilding():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "remap_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)

        # Populate a drug with concept_id = 0
        con.execute("""
            INSERT INTO drug_exposure (
                drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
                drug_type_concept_id, drug_source_value, drug_source_concept_id
            ) VALUES (1, 101, 0, DATE '2024-01-01', DATE '2024-01-01', 32838, 'LOCAL-ASPIRIN', 0);
        """)

        # Add mapping into source_to_concept_map
        con.execute("""
            INSERT INTO source_to_concept_map (
                source_code, source_concept_id, source_vocabulary_id, source_code_description,
                target_concept_id, target_vocabulary_id, valid_start_date, valid_end_date
            ) VALUES ('LOCAL-ASPIRIN', 0, 'RxNorm', 'Local Aspirin 81mg', 1112807, 'RxNorm', DATE '2020-01-01', DATE '2099-12-31');
        """)

        # 1. Dry run
        dry_res = remap_cdm_table(con, "drug_exposure", dry_run=True)
        assert dry_res["eligible_for_remapping"] == 1
        assert dry_res["remappable_rows"] == 1
        assert dry_res["remapped"] == 0

        # Unmodified before live run
        val = con.execute("SELECT drug_concept_id FROM drug_exposure WHERE drug_exposure_id = 1").fetchone()[0]
        assert val == 0

        # 2. Live remapping with era rebuilding
        live_res = remap_all(con, dry_run=False, rebuild_eras=True)
        assert live_res["drug_exposure"]["remapped"] == 1

        updated_val = con.execute("SELECT drug_concept_id FROM drug_exposure WHERE drug_exposure_id = 1").fetchone()[0]
        assert updated_val == 1112807

        # Era table was synthesized automatically
        eras = con.execute("SELECT person_id, drug_concept_id, drug_exposure_count FROM drug_era").fetchall()
        assert eras == [(101, 1112807, 1)]

        con.close()


def test_modular_death_handling():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "death_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        load_macros(con)

        # 1. Missing death.csv -> skips gracefully without error
        empty_source = Path(tmp) / "empty_source"
        empty_source.mkdir()
        load_death(con, str(empty_source))
        count = con.execute("SELECT COUNT(*) FROM death").fetchone()[0]
        assert count == 0

        # 2. Present death.csv -> loads and deduplicates
        death_source = Path(tmp) / "death_source"
        death_source.mkdir()

        # Add person
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1960, 8527, 38003564, 'P001')")

        death_csv = death_source / "death.csv"
        with open(death_csv, "w", newline="", encoding="utf-8") as f:
            f.write("PATID,DEATH_DATE,DEATH_SOURCE\n")
            f.write("P001,2024-05-10,EHR\n")
            f.write("P001,2024-05-09,STATE\n")  # duplicate PATID, should keep latest

        load_death(con, str(death_source))
        rows = con.execute("SELECT person_id, death_date, cause_source_value FROM death").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert str(rows[0][1]) == "2024-05-10"
        con.close()


def test_column_heterogeneity_and_date_parsing():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "hetero_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        load_macros(con)

        src_dir = Path(tmp) / "hetero_source"
        src_dir.mkdir()

        # demographic using 'ssid' instead of PATID and PCORnet date format '01JAN1980'
        demo_csv = src_dir / "demographic.csv"
        with open(demo_csv, "w", newline="", encoding="utf-8") as f:
            f.write("ssid,SEX,BIRTH_DATE,RACE,HISPANIC\n")
            f.write("SSN-1234,M,01JAN1980,05,N\n")

        load_person(con, str(src_dir))

        row = con.execute("SELECT person_id, year_of_birth, month_of_birth, day_of_birth, person_source_value FROM person").fetchone()
        assert row == (1, 1980, 1, 1, "SSN-1234")
        con.close()
