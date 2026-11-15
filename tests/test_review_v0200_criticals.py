"""Regression tests for the v0.20.0 review criticals (C-01..C-03) and majors M-25/M-26."""
import re
from pathlib import Path

import duckdb
import pytest

from omop_etl import consortium_patient_overlap

MACROS = Path(__file__).resolve().parents[1] / "inst" / "sql" / "mapping_macros.sql"


@pytest.fixture()
def con():
    c = duckdb.connect()
    src = MACROS.read_text(encoding="utf-8")
    for m in re.finditer(r"CREATE OR REPLACE MACRO (parse_omop_date|normalize_time_str|parse_omop_datetime)\(.*?\n\);", src, re.S):
        c.execute(m.group(0))
    return c


@pytest.mark.parametrize("d,t,expected", [
    ("2020-01-01", "4:15 PM", "2020-01-01 16:15:00"),
    ("2020-01-01", "12:05 AM", "2020-01-01 00:05:00"),
    ("2020-01-01", "12:30:15 pm", "2020-01-01 12:30:15"),
    ("01JAN2020", "11:59 PM", "2020-01-01 23:59:00"),
    ("2020-01-01", "4:15", "2020-01-01 04:15:00"),
])
def test_parse_omop_datetime_twelve_hour(con, d, t, expected):
    assert str(con.execute("SELECT parse_omop_datetime(?, ?)", [d, t]).fetchone()[0]) == expected


def test_overlap_does_not_leak_small_site_size():
    out = consortium_patient_overlap({"A": [f"t{i}" for i in range(5)], "B": [f"u{i}" for i in range(20)]})
    self_a = out[(out.site_1 == "A") & (out.site_2 == "A")].iloc[0]
    assert self_a["shared_patients"] == "<11"
    assert self_a["n_site_1"] != self_a["n_site_1"]  # NaN: the exact size would reveal the suppressed count
    assert out[(out.site_1 == "B") & (out.site_2 == "B")].iloc[0]["n_site_1"] == 20


def test_run_dqd_accepts_single_level_string():
    from omop_etl.dqd import run_dqd

    c = duckdb.connect()
    with pytest.raises(ValueError):
        run_dqd(c, check_levels="NOPE")


def test_drug_strength_is_a_vocabulary_table():
    from omop_etl.vocabulary import EXPECTED_VOCAB_TABLES

    assert "drug_strength" in EXPECTED_VOCAB_TABLES


# ---- second batch --------------------------------------------------------------------------------------------

def test_build_schema_is_rerunnable_and_keys_are_opt_in():
    from omop_etl.build_omop_cdm import build_schema

    c = duckdb.connect()
    build_schema(c)
    build_schema(c)  # second run is a no-op, not a "table already exists" error
    assert c.execute("SELECT count(*) FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'").fetchone()[0] == 0
    build_schema(c, keys=True)
    assert c.execute("SELECT count(*) FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'").fetchone()[0] > 0


def test_build_schema_refuses_a_partial_schema():
    from omop_etl.build_omop_cdm import build_schema

    c = duckdb.connect()
    c.execute("CREATE TABLE person (person_id INTEGER)")
    with pytest.raises(RuntimeError, match="part of the CDM schema"):
        build_schema(c)


@pytest.mark.parametrize("bad", ["person; DROP TABLE person", "a b", "x'y", "", None, "a.b.c.d"])
def test_check_ident_rejects_injection(bad):
    from omop_etl.sqlsafe import check_ident

    with pytest.raises(ValueError):
        check_ident(bad)


def test_check_ident_accepts_schema_qualified_names():
    from omop_etl.sqlsafe import check_ident

    assert check_ident("main.cohort_1") == "main.cohort_1"


def test_output_table_injection_is_blocked():
    from omop_etl.sqlsafe import check_ident

    c = duckdb.connect()
    c.execute("CREATE TABLE person (person_id INTEGER)")
    with pytest.raises(ValueError):
        check_ident("t AS SELECT 1; DROP TABLE person; --", "output_table")
    assert c.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='person'").fetchone()[0] == 1


def test_load_vocabulary_reports_failure_when_nothing_loaded(tmp_path):
    from omop_etl.vocabulary import load_vocabulary

    (tmp_path / "CONCEPT.csv").write_text("concept_id\tconcept_name\n1\tx\n", encoding="utf-8")
    assert load_vocabulary(str(tmp_path), str(tmp_path / "empty.duckdb")) is False  # no schema -> nothing loaded


def test_usagi_import_skips_unreviewed_and_targetless_rows(tmp_path):
    from omop_etl.mapping import import_usagi_mappings

    c = duckdb.connect()
    c.execute("CREATE TABLE source_to_concept_map (source_code VARCHAR, source_concept_id INTEGER, source_vocabulary_id VARCHAR, "
              "source_code_description VARCHAR, target_concept_id INTEGER, target_vocabulary_id VARCHAR, valid_start_date DATE, "
              "valid_end_date DATE, invalid_reason VARCHAR)")
    f = tmp_path / "u.csv"
    f.write_text("sourceCode,sourceName,targetConceptId,targetVocabularyId,mappingStatus\n"
                 "A01,none,,,\nB02,ok,201826,SNOMED,APPROVED\nC03,flagged,5,SNOMED,FLAGGED\n", encoding="utf-8")
    import_usagi_mappings(c, str(f))
    assert [r[0] for r in c.execute("SELECT source_code FROM source_to_concept_map").fetchall()] == ["B02"]

