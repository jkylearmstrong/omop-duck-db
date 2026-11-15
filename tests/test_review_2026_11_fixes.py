"""Regression tests for the 2026-11-12 external review (v0.20.0).

One focused test per defect; each docstring names the review id (C-xx critical, M-xx major, m-xx minor).
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from omop_etl.build_omop_cdm import build_schema, etl_pcornet, load_macros, load_vital
from omop_etl.dqd import run_dqd
from omop_etl.integrity import consortium_patient_overlap
from omop_etl.mapping import export_unmapped_codes, import_source_to_concept_map, import_usagi_mappings
from omop_etl.remapping import remap_cdm_table
from omop_etl.site_export import SiteSummaryError, export_site_summary
from omop_etl._arrow import to_arrow_table as fetch_arrow
from omop_etl.vocabulary import EXPECTED_VOCAB_TABLES, load_vocabulary

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pcornet_sample"


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    build_schema(c)
    yield c
    c.close()


@pytest.fixture()
def macro_con(con):
    load_macros(con)
    return con


# ---- C-01: AM/PM and compact formats ---------------------------------------------------------------







# ---- M-03 / M-04 / M-05: demographic and vocabulary routing ----------------------------------------

def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)




def test_snomed_diagnoses_are_looked_up_in_snomed(tmp_path):
    """M-05: DX_TYPE='SM' used to be searched in ICD10CM and resolve to 0."""
    src = tmp_path / "src"
    shutil.copytree(FIXTURE, src)
    _write_csv(src / "diagnosis.csv",
               ["DIAGNOSISID", "PATID", "DX_TYPE", "DX", "DX_DATE", "ADMIT_DATE", "PDX", "PROVIDERID", "ENCOUNTERID"],
               [("D1", "FAKE-P001", "SM", "44054006", "2022-01-02", "2022-01-02", "P", "", "FAKE-E001"),
                ("D2", "FAKE-P001", "ZZ", "44054006", "2022-01-02", "2022-01-02", "S", "", "FAKE-E001")])
    db = tmp_path / "cdm.duckdb"
    c = duckdb.connect(str(db))
    build_schema(c)
    c.execute("INSERT INTO concept VALUES (201826, 'T2DM', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', "
              "DATE '1970-01-01', DATE '2099-12-31', NULL)")
    c.close()
    etl_pcornet(str(src), str(db), key_check="ignore")
    c = duckdb.connect(str(db), read_only=True)
    rows = dict(c.execute("SELECT condition_status_source_value, condition_concept_id FROM condition_occurrence").fetchall())
    c.close()
    assert rows["P"] == 201826
    assert rows["S"] == 0  # unknown DX_TYPE: no vocabulary guess


# ---- C-04: load_vital -------------------------------------------------------------------------------

def test_load_vital_uses_fallback_concepts_derives_bmi_and_filters(con, tmp_path):
    """C-04 (Python reference behaviour the R port must match)."""
    load_macros(con)
    src = tmp_path / "v"
    src.mkdir()
    _write_csv(src / "vital.csv",
               ["VITALID", "PATID", "ENCOUNTERID", "MEASURE_DATE", "MEASURE_TIME", "VITAL_SOURCE", "HT", "WT",
                "ORIGINAL_BMI", "BMI", "SYSTOLIC", "DIASTOLIC", "SMOKING"],
               [("V1", "P1", "", "2021-01-01", "08:00", "HC", "70", "180", "", "", "120", "80", ""),
                ("V2", "P1", "", "2021-02-01", "08:00", "HC", "", "", "25.8", "", "999", "", "")])
    out = load_vital(con, str(src), plausible_ranges=True)
    assert out["rows_loaded"]["BMI_DERIVED"] == 1
    assert out["rows_dropped_implausible"]["SYSTOLIC"] == 1
    cids = {r[0] for r in con.execute("SELECT DISTINCT measurement_concept_id FROM measurement").fetchall()}
    assert 0 not in cids
    ids = [r[0] for r in con.execute("SELECT measurement_id FROM measurement ORDER BY measurement_id").fetchall()]
    assert ids == sorted(set(ids))  # deterministic, unique surrogate keys (m-14)


# ---- C-03: overlap self-pairs ----------------------------------------------------------------------



# ---- M-26 / M-25: DQD levels, vocabulary table list ------------------------------------------------

def test_run_dqd_accepts_a_single_level_string_and_rejects_unknown(con):
    """M-26: check_levels='TABLE' used to iterate into characters, run 0 checks and report 100% passed."""
    res = run_dqd(con, check_levels="TABLE")
    assert res["Overview"]["count_total"] > 0
    with pytest.raises(ValueError, match="Unknown check_levels"):
        run_dqd(con, check_levels="TABEL")


def test_drug_strength_is_a_known_vocabulary_table():
    """M-25."""
    assert "drug_strength" in EXPECTED_VOCAB_TABLES


def test_load_vocabulary_reports_failure_on_empty_schema(tmp_path):
    """M-16: no schema, nothing loaded, yet it used to return True."""
    vocab = tmp_path / "vocab"
    vocab.mkdir()
    (vocab / "CONCEPT.csv").write_text("concept_id\tconcept_name\n1\tx\n", encoding="utf-8")
    assert load_vocabulary(str(vocab), str(tmp_path / "empty.duckdb")) is False


def test_load_vocabulary_requested_table_without_file_fails(tmp_path):
    """M-16(b)."""
    db = tmp_path / "cdm.duckdb"
    c = duckdb.connect(str(db))
    build_schema(c)
    c.close()
    vocab = tmp_path / "vocab"
    vocab.mkdir()
    (vocab / "OTHER.csv").write_text("a\n1\n", encoding="utf-8")
    assert load_vocabulary(str(vocab), str(db), tables=["concept"]) is False


# ---- mapping imports (M-20, M-27, m-13) ------------------------------------------------------------

def test_usagi_requires_explicit_approval_and_a_target(con, tmp_path):
    """M-27: blank status was treated as approved; NULL targets were imported; the largest id won."""
    f = tmp_path / "usagi.csv"
    _write_csv(f, ["sourceCode", "sourceName", "targetConceptId", "targetVocabularyId", "mappingStatus"],
               [("A01", "unreviewed", "", "SNOMED", ""),
                ("B02", "approved", "201826", "SNOMED", "APPROVED"),
                ("B02", "approved dup", "999999", "SNOMED", "APPROVED"),
                ("C03", "flagged", "555", "SNOMED", "FLAGGED")])
    n = import_usagi_mappings(con, str(f))
    rows = con.execute("SELECT source_code, target_concept_id FROM source_to_concept_map ORDER BY 1").fetchall()
    assert rows == [("B02", 201826)]  # lowest approved id, deterministic
    assert n == 1


def test_vocabulary_id_with_quote_cannot_break_out(con, tmp_path):
    """M-20."""
    f = tmp_path / "m.csv"
    _write_csv(f, ["source_code", "target_concept_id"], [("E11", "201826")])
    evil = "x'); DROP TABLE person; --"
    assert import_source_to_concept_map(con, str(f), default_vocabulary_id=evil) == 1
    assert con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='person'").fetchone()[0] == 1
    assert con.execute("SELECT source_vocabulary_id FROM source_to_concept_map").fetchone()[0] == evil


def test_stcm_import_requires_target_column_and_counts_inserted_rows(con, tmp_path):
    """m-13."""
    bad = tmp_path / "bad.csv"
    _write_csv(bad, ["source_code"], [("E11",)])
    with pytest.raises(ValueError, match="target_concept_id"):
        import_source_to_concept_map(con, str(bad))
    good = tmp_path / "good.csv"
    _write_csv(good, ["source_code", "target_concept_id"], [("E11", "1"), ("E12", "2")])
    con.execute("INSERT INTO source_to_concept_map (source_code, source_concept_id, source_vocabulary_id, target_concept_id, "
                "target_vocabulary_id, valid_start_date, valid_end_date) VALUES ('Z', 0, 'L', 9, '', DATE '2020-01-01', DATE '2099-12-31')")
    assert import_source_to_concept_map(con, str(good)) == 2  # rows inserted, not table total


def test_export_unmapped_codes_validates_min_frequency(con, tmp_path):
    with pytest.raises(ValueError):
        export_unmapped_codes(con, "condition_occurrence", str(tmp_path / "o.csv"), min_frequency="1; DROP TABLE person")


# ---- M-11: remap priority --------------------------------------------------------------------------

def test_remap_prefers_custom_map_over_larger_concept_id(con):
    """M-11: the numerically largest candidate used to win over the intended precedence."""
    con.execute("INSERT INTO concept VALUES (1, 'icd', 'Condition', 'ICD10CM', 'x', NULL, 'E11.9', DATE '2000-01-01', DATE '2099-12-31', NULL)")
    con.execute("INSERT INTO concept VALUES (999999, 'std', 'Condition', 'SNOMED', 'x', 'S', 'X', DATE '2000-01-01', DATE '2099-12-31', NULL)")
    con.execute("INSERT INTO concept_relationship VALUES (1, 999999, 'Maps to', DATE '2000-01-01', DATE '2099-12-31', NULL)")
    con.execute("INSERT INTO source_to_concept_map (source_code, source_concept_id, source_vocabulary_id, target_concept_id, "
                "target_vocabulary_id, valid_start_date, valid_end_date) VALUES ('E11.9', 0, 'ICD10CM', 201826, 'SNOMED', DATE '2020-01-01', DATE '2099-12-31')")
    con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
                "condition_type_concept_id, condition_source_value) VALUES (1, 1, 0, DATE '2021-01-01', 0, 'E11.9')")
    remap_cdm_table(con, "condition_occurrence")
    assert con.execute("SELECT condition_concept_id FROM condition_occurrence").fetchone()[0] == 201826


# ---- M-24: site export -----------------------------------------------------------------------------

def test_site_export_extra_tables_use_complementary_suppression(tmp_path):
    """M-24: a suppressed cell was recoverable as total - sibling."""
    df = pd.DataFrame({"grp": ["A", "A", "B"], "cat": ["x", "y", "x"], "n": [3, 100, 50]})
    out = export_site_summary(tmp_path, "A", tables={"t": df}, count_cols={"t": ["n"]}, group_cols={"t": ["grp"]}, min_cell=11)
    got = pd.read_csv(out["t"])
    a = got[got.grp == "A"]
    assert a["n"].isna().all()  # the sibling 100 is blanked too, so 103 - 100 cannot recover 3


def test_site_export_refuses_unlisted_count_columns(tmp_path):
    df = pd.DataFrame({"grp": ["A"], "n": [3]})
    with pytest.raises(SiteSummaryError, match="count-like"):
        export_site_summary(tmp_path, "A", tables={"t": df}, min_cell=11)


# ---- sqlutil ---------------------------------------------------------------------------------------



def test_fetch_arrow_returns_a_table():
    c = duckdb.connect(":memory:")
    t = fetch_arrow(c.execute("SELECT 1 AS a, 'x' AS b"))
    assert t.num_rows == 1 and t.column_names == ["a", "b"]
