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
    create_federated_consortium,
    etl_pcornet,
    export_unmapped_codes,
    import_source_to_concept_map,
    import_usagi_mappings,
    load_care_site,
    load_cdm_source,
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


def test_python_extended_enc_type_and_sex_mappings():
    from omop_etl import load_visit_occurrence

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "enc_sex_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        load_macros(con)

        src_dir = Path(tmp) / "src"
        src_dir.mkdir()

        demo_csv = src_dir / "demographic.csv"
        with open(demo_csv, "w", newline="", encoding="utf-8") as f:
            f.write("PATID,SEX,BIRTH_DATE,RACE,HISPANIC\n")
            f.write("P1,OT,1990-01-01,05,N\n")
            f.write("P2,UN,1985-05-05,05,N\n")
            f.write("P3,NI,1980-10-10,05,N\n")

        enc_csv = src_dir / "encounter.csv"
        with open(enc_csv, "w", newline="", encoding="utf-8") as f:
            f.write("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_STATUS\n")
            f.write("E1,P1,TH,2024-01-01,10:00,A\n")
            f.write("E2,P2,OS,2024-01-02,11:00,A\n")
            f.write("E3,P3,OT,2024-01-03,12:00,A\n")

        load_person(con, str(src_dir))
        load_visit_occurrence(con, str(src_dir))

        genders = con.execute("SELECT person_source_value, gender_concept_id FROM person ORDER BY person_source_value").fetchall()
        assert genders == [("P1", 8521), ("P2", 8551), ("P3", 8551)]

        visits = con.execute("SELECT visit_source_value, visit_concept_id FROM visit_occurrence ORDER BY visit_source_value").fetchall()
        assert visits == [("E1", 5083), ("E2", 9201), ("E3", 9202)]
        con.close()


def test_python_load_vital_unpivoting():
    from omop_etl import load_vital

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "vital_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        load_macros(con)

        src_dir = Path(tmp) / "vital_src"
        src_dir.mkdir()

        # Person for foreign key
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1990, 8527, 38003564, 'P1')")

        vital_csv = src_dir / "vital.csv"
        with open(vital_csv, "w", newline="", encoding="utf-8") as f:
            f.write("VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,VITAL_SOURCE,HT,WT,ORIGINAL_BMI,SYSTOLIC,DIASTOLIC\n")
            f.write("V1,P1,E1,2024-01-01,10:00,PR,70,180,25.8,120,80\n")

        load_vital(con, str(src_dir))

        meas = con.execute("SELECT measurement_source_value, value_as_number, unit_source_value, unit_concept_id FROM measurement ORDER BY measurement_source_value").fetchall()
        assert len(meas) == 5
        # 29463-7 (weight), 39156-5 (bmi), 8302-2 (height), 8462-4 (diastolic), 8480-6 (systolic)
        assert [m[0] for m in meas] == ["29463-7", "39156-5", "8302-2", "8462-4", "8480-6"]
        assert [m[1] for m in meas] == [180.0, 25.8, 70.0, 80.0, 120.0]
        assert [m[2] for m in meas] == ["[lb_av]", "kg/m2", "[in_us]", "mm[Hg]", "mm[Hg]"]
        assert [m[3] for m in meas] == [8739, 9531, 9326, 8876, 8876]
        con.close()


def test_python_attach_central_vocabulary():
    from omop_etl import attach_central_vocabulary

    with tempfile.TemporaryDirectory() as tmp:
        vocab_path = str(Path(tmp) / "central_vocab.duckdb")
        c_vocab = duckdb.connect(vocab_path)
        c_vocab.execute("CREATE TABLE concept (concept_id INT, concept_name VARCHAR, vocabulary_id VARCHAR, concept_code VARCHAR, standard_concept VARCHAR);")
        c_vocab.execute("INSERT INTO concept VALUES (3036277, 'Body height', 'LOINC', '8302-2', 'S');")
        c_vocab.close()

        cdm_path = str(Path(tmp) / "cdm.duckdb")
        con = duckdb.connect(cdm_path)
        build_schema(con)

        attach_central_vocabulary(con, vocab_path, temporary=True)
        res = con.execute("SELECT concept_id, concept_name FROM concept WHERE concept_code = '8302-2'").fetchall()
        assert len(res) == 1
        assert res[0] == (3036277, "Body height")
        con.close()


def test_python_care_site_ingestion_and_root_site():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "care_site_test.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        load_macros(con)

        src_dir = Path(tmp) / "src_facility"
        src_dir.mkdir()
        fac_csv = src_dir / "facility.csv"
        with open(fac_csv, "w", newline="", encoding="utf-8") as f:
            f.write("FACILITYID,FACILITY_TYPE,FACILITY_LOCATION\n")
            f.write("FAC1,Hospital,Building A\n")

        # Case 1: Ingest facility.csv with root site seed
        load_care_site(con, str(src_dir), site_id=1, site_anon="Site A", site_name="Hospital Alpha")
        rows = con.execute("SELECT care_site_id, care_site_name, care_site_source_value FROM care_site ORDER BY care_site_id").fetchall()
        assert len(rows) == 2
        root = [r for r in rows if r[0] == 1][0]
        assert root[1] == "Site A"
        assert root[2] == "Hospital Alpha"

        # Case 2: No facility.csv, only site_id
        src_empty = Path(tmp) / "src_empty"
        src_empty.mkdir()
        con.execute("DELETE FROM care_site")
        load_care_site(con, str(src_empty), site_id=2, site_anon="Site B", site_name="Hospital Beta")
        rows2 = con.execute("SELECT care_site_id, care_site_name, care_site_source_value FROM care_site").fetchall()
        assert len(rows2) == 1
        assert rows2[0] == (2, "Site B", "Hospital Beta")
        con.close()


def test_python_site_identification_and_disambiguate_patids():
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = Path(tmp) / "site_src"
        src_dir.mkdir()

        # Write sample PCORnet tables
        with open(src_dir / "demographic.csv", "w", newline="", encoding="utf-8") as f:
            f.write("PATID,SEX,BIRTH_DATE,RACE,HISPANIC,PROVIDERID\n")
            f.write("100,M,1990-01-01,05,N,PR1\n")

        with open(src_dir / "encounter.csv", "w", newline="", encoding="utf-8") as f:
            f.write("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,PROVIDERID,FACILITYID,DISCHARGE_STATUS\n")
            f.write("E1,100,IP,2024-01-01,10:00,2024-01-02,12:00,PR1,,A\n")

        with open(src_dir / "diagnosis.csv", "w", newline="", encoding="utf-8") as f:
            f.write("DIAGNOSISID,PATID,ENCOUNTERID,ENC_TYPE,ADMIT_DATE,PROVIDERID,DX,DX_TYPE,DX_SOURCE,PDX\n")
            f.write("D1,100,E1,IP,2024-01-01,PR1,I10,10,DI,P\n")

        with open(src_dir / "procedures.csv", "w", newline="", encoding="utf-8") as f:
            f.write("PROCEDURESID,PATID,ENCOUNTERID,PX,PX_TYPE,PX_DATE,PROVIDERID\n")
            f.write("PX1,100,E1,99213,01,2024-01-01,PR1\n")

        with open(src_dir / "lab_result_cm.csv", "w", newline="", encoding="utf-8") as f:
            f.write("LAB_RESULT_CM_ID,PATID,ENCOUNTERID,SPECIMEN_DATE,SPECIMEN_TIME,RESULT_DATE,RESULT_TIME,RESULT_NUM,RESULT_UNIT,LAB_LOINC,PROVIDERID,RAW_LAB_NAME,RAW_LAB_CODE\n")
            f.write("L1,100,E1,2024-01-01,10:00,2024-01-01,10:30,5.4,mg/dL,2345-7,PR1,Glucose,GLU\n")

        with open(src_dir / "prescribing.csv", "w", newline="", encoding="utf-8") as f:
            f.write("PRESCRIBINGID,PATID,ENCOUNTERID,RX_PROVIDERID,RX_ORDER_DATE,RX_ORDER_TIME,RX_START_DATE,RX_END_DATE,RX_DOSE_ORDERED,RX_DOSE_ORDERED_UNIT,RX_QUANTITY,RX_REFILLS,RXNORM_CUI,RAW_RX_MED_NAME,RAW_RX_NDC\n")
            f.write("RX1,100,E1,PR1,2024-01-01,10:00,2024-01-01,2024-01-10,10,mg,30,0,197361,Aspirin,\n")

        with open(src_dir / "death.csv", "w", newline="", encoding="utf-8") as f:
            f.write("PATID,DEATH_DATE,DEATH_DATE_IMPUTE,DEATH_SOURCE,DEATH_MATCH_CONFIDENCE\n")
            f.write("100,2024-02-01,N,L,H\n")

        db_path = str(Path(tmp) / "site1.duckdb")
        con = duckdb.connect(db_path)
        build_schema(con)
        con.close()

        etl_pcornet(
            str(src_dir),
            db_path=db_path,
            site_id=1,
            site_anon="Site A",
            site_name="Hospital Alpha",
            disambiguate_patids=True,
        )

        con = duckdb.connect(db_path, read_only=True)
        # Check cdm_source
        src_row = con.execute("SELECT cdm_source_name, cdm_source_abbreviation, cdm_holder FROM cdm_source").fetchone()
        assert src_row == ("Site A", "Site A", "Hospital Alpha")

        # Check person
        p_row = con.execute("SELECT person_id, person_source_value, care_site_id FROM person").fetchone()
        assert p_row[1] == "100-1"
        assert p_row[2] == 1
        expected_person_id = p_row[0]

        # Check foreign keys across clinical tables
        for tbl in ["visit_occurrence", "condition_occurrence", "procedure_occurrence", "measurement", "drug_exposure", "death"]:
            pid = con.execute(f"SELECT person_id FROM {tbl} LIMIT 1").fetchone()[0]
            assert pid == expected_person_id, f"Mismatch in {tbl}: {pid} != {expected_person_id}"

        # Check visit_occurrence care_site_id falls back to site_id when facility.csv is absent
        vo_cs_id = con.execute("SELECT care_site_id FROM visit_occurrence").fetchone()[0]
        assert vo_cs_id == 1

        con.close()

        # Run for site 2 with the same PATID 100
        db_path2 = str(Path(tmp) / "site2.duckdb")
        con2 = duckdb.connect(db_path2)
        build_schema(con2)
        con2.close()

        etl_pcornet(
            str(src_dir),
            db_path=db_path2,
            site_id=2,
            site_anon="Site B",
            site_name="Hospital Beta",
            disambiguate_patids=True,
        )
        con2 = duckdb.connect(db_path2, read_only=True)
        p_row2 = con2.execute("SELECT person_id, person_source_value, care_site_id FROM person").fetchone()
        assert p_row2[1] == "100-2"
        assert p_row2[2] == 2
        # Surrogate keys MUST NOT collide
        assert p_row2[0] != expected_person_id
        con2.close()


def test_python_create_federated_consortium():
    with tempfile.TemporaryDirectory() as tmp:
        db1_path = str(Path(tmp) / "site1.duckdb")
        con1 = duckdb.connect(db1_path)
        build_schema(con1)
        load_macros(con1)
        load_care_site(con1, str(tmp), site_id=1, site_anon="Site A", site_name="Hospital Alpha")
        con1.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value, care_site_id) VALUES (101, 8507, 1990, 8527, 38003564, 'P1-1', 1)")
        con1.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, care_site_id) VALUES (201, 101, 9201, '2024-01-01', '2024-01-01', 32827, 1)")
        con1.close()

        db2_path = str(Path(tmp) / "site2.duckdb")
        con2 = duckdb.connect(db2_path)
        build_schema(con2)
        load_macros(con2)
        load_care_site(con2, str(tmp), site_id=2, site_anon="Site B", site_name="Hospital Beta")
        con2.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value, care_site_id) VALUES (102, 8532, 1985, 8527, 38003564, 'P2-2', 2)")
        con2.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, care_site_id) VALUES (202, 102, 9202, '2024-02-01', '2024-02-01', 32827, 2)")
        con2.close()

        fed_con = create_federated_consortium({"Site A": db1_path, "Site B": db2_path})

        # Test federated view v_person
        persons = fed_con.execute("SELECT site_id, site_anon, person_id, person_source_value FROM v_person ORDER BY site_id").fetchall()
        assert len(persons) == 2
        assert persons[0] == (1, "Site A", 101, "P1-1")
        assert persons[1] == (2, "Site B", 102, "P2-2")

        # Test federated view v_visit_occurrence
        visits = fed_con.execute("SELECT site_id, site_anon, visit_occurrence_id, person_id FROM v_visit_occurrence ORDER BY site_id").fetchall()
        assert len(visits) == 2
        assert visits[0] == (1, "Site A", 201, 101)
        assert visits[1] == (2, "Site B", 202, 102)

        fed_con.close()

