"""Elixhauser ``code_table="ederri"`` (the study protocol's own ICD prefix lists) and the benchmark ``profile="ederri"`` window.
Synthetic data only."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from omop_etl.benchmark_features import extract_benchmark_features  # noqa: E402
from omop_etl.comorbidity import extract_elixhauser_comorbidities  # noqa: E402

TABLES = ROOT / "inst" / "extdata" / "comorbidity"


def _rows(name):
    with open(TABLES / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_ederri_table_has_the_same_domains_columns_and_weights_as_quan():
    quan = {(r["domain"], r["column"], r["weight"]) for r in _rows("elixhauser_codes.csv")}
    ed = {(r["domain"], r["column"], r["weight"]) for r in _rows("elixhauser_codes_ederri.csv")}
    assert quan == ed and len(ed) == 31
    assert all(r["code"].isalnum() and r["code"].isupper() or r["code"].isdigit() for r in _rows("elixhauser_codes_ederri.csv"))


def _con(codes):
    con = duckdb.connect()
    con.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,"
                " condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
    con.executemany("INSERT INTO condition_occurrence VALUES (?, 0, ?, ?, NULL)",
                    [(i + 1, d, c) for i, (c, d) in enumerate(codes)])
    con.execute("CREATE TABLE cohort AS SELECT person_id AS subject_id, DATE '2020-06-01' AS cohort_start_date FROM condition_occurrence")
    return con


CASES = [  # (source value, start date, flag that must be 1, flag that must be 0)
    ("G40.9", "2020-05-01", "elix_neuro_other", "elix_paralysis"),      # any G code except G81 / G82 is "other neuro"
    ("G81.9", "2020-05-01", "elix_paralysis", "elix_neuro_other"),
    ("342.9", "2020-05-01", "elix_paralysis", "elix_neuro_other"),      # ICD-9 342 is paralysis, not other neuro
    ("345.9", "2020-05-01", "elix_neuro_other", "elix_paralysis"),
    ("C50.9", "2020-05-01", "elix_solid_tumor", "elix_mets"),
    ("C78.0", "2020-05-01", "elix_mets", "elix_solid_tumor"),           # C77-C85 are not solid tumour
    ("C7A.1", "2020-05-01", "elix_solid_tumor", "elix_mets"),
    ("D50.0", "2020-05-01", "elix_blood_loss_anemia", "elix_deficiency_anemia"),
    ("D50.9", "2020-05-01", "elix_deficiency_anemia", "elix_blood_loss_anemia"),
    ("E11.9", "2020-05-01", "elix_dm_uncomp", "elix_dm_comp"),
    ("E11.65", "2020-05-01", "elix_dm_comp", "elix_dm_uncomp"),
    ("250.00", "2020-05-01", "elix_dm_uncomp", "elix_dm_comp"),
    ("250.40", "2020-05-01", "elix_dm_comp", "elix_dm_uncomp"),
    ("I50.9", "2020-05-01", "elix_chf", "elix_copd"),
]


@pytest.mark.parametrize("code,day,on,off", CASES)
def test_ederri_code_table_flags(code, day, on, off):
    con = _con([(code, day)])
    df = extract_elixhauser_comorbidities(con, source_and_standard=False, code_table="ederri", lookback_days=None)
    row = df.iloc[0]
    assert row[on] == 1 and row[off] == 0, (code, on, off)


def test_default_code_table_is_unchanged_and_unknown_table_is_rejected():
    con = _con([("G40.9", "2020-05-01")])
    quan = extract_elixhauser_comorbidities(con, source_and_standard=False).iloc[0]
    assert quan["elix_neuro_other"] == 1                       # Quan lists epilepsy G40 under other neuro as well
    with pytest.raises(ValueError, match="code_table must be"):
        extract_elixhauser_comorbidities(con, code_table="icd11")


def test_ederri_profile_uses_all_history_up_to_discharge_and_default_uses_the_lookback():
    con = duckdb.connect()
    con.execute("CREATE TABLE person AS SELECT 1 AS person_id, 8532 AS gender_concept_id, 1950 AS year_of_birth, 1 AS month_of_birth, "
                "1 AS day_of_birth, 8527 AS race_concept_id, 38003564 AS ethnicity_concept_id")
    con.execute("CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES (1, 0, DATE '2012-03-01', 'I50.9', NULL::BIGINT), "
                "(1, 0, DATE '2020-01-14', 'G40.9', NULL::BIGINT)) t(person_id, condition_concept_id, condition_start_date, "
                "condition_source_value, visit_occurrence_id)")
    con.execute("CREATE TABLE cohort_t AS SELECT 1 AS subject_id, DATE '2020-01-10' AS cohort_start_date, "
                "DATE '2020-01-15' AS cohort_end_date, 101 AS visit_occurrence_id, 0 AS outcome_flag")
    kw = dict(include_medications=False, include_utilization=False, include_vitals=False, include_labs=False,
              include_disposition=False, include_demographics=False, vocabulary_db=None)
    default = extract_benchmark_features(con, "cohort_t", **kw).iloc[0]
    ed = extract_benchmark_features(con, "cohort_t", profile="ederri", **kw).iloc[0]
    assert default["elix_chf"] == 0 and ed["elix_chf"] == 1       # a 2012 diagnosis is outside 365 days, inside "all history"
    assert default["elix_neuro_other"] == 1 and ed["elix_neuro_other"] == 1   # the index stay's own diagnosis counts (discharge-anchored)
    explicit = extract_benchmark_features(con, "cohort_t", profile="ederri", elixhauser_lookback_days=365, **kw).iloc[0]
    assert explicit["elix_chf"] == 0
    with pytest.raises(ValueError, match="non-negative"):
        extract_benchmark_features(con, "cohort_t", elixhauser_lookback_days=-1, **kw)
