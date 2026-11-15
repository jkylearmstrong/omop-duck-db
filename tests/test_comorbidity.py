"""Tests for extract_elixhauser_comorbidities() and extract_charlson_index() (RFC 3.1).

Most cases run on the shared fixture ``tests/fixtures/comorbidity_*.csv`` (see ``comorbidity_make_fixture.py``):
hand-built persons that each target one rule (window boundaries, normalisation, negative controls, hierarchy,
several index dates, cohort ids, the standard-concept side) plus one probe person per domain and channel (ICD-10,
ICD-9, standard descendant). The golden files ``comorbidity_golden_elixhauser.csv`` / ``..._charlson.csv`` hold the
expected output of four configurations, computed by an independent plain-Python model; the R tests
(tests/testthat/test-comorbidity.R) assert the same files, which is the cross-language parity check.

Hand-computed numbers (weights from PROVENANCE.md). Person 40 has heart failure, diabetes with and without
complications, metastatic and solid cancer, hypertension with and without complications, acute and chronic liver
disease and an acute myocardial infarction:

* Elixhauser: chf 7 + liver_disease 11 + mets 12 = 30 after the hierarchy (solid_tumor 4 is dropped because mets is
  present); 34 raw. Counted domains: chf, htn_comp, dm_comp, liver_disease, mets = 5; 8 raw.
* Charlson: mi 1 + chf 1 + mod_severe_liver 3 + dm_comp 2 + mets 6 = 13 after the hierarchy; 17 raw (mild_liver 1,
  dm_uncomp 1 and malignancy 2 are dropped). Counted categories: 5; 8 raw.
"""

import csv
import os
import shutil
import subprocess
import sys
import time
import warnings
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import build_schema
from omop_etl import comorbidity as cm
from omop_etl.comorbidity import extract_charlson_index, extract_elixhauser_comorbidities

FIX = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent
CODES = REPO_ROOT / "inst" / "extdata" / "comorbidity"

INDEXES = {
    "elixhauser": dict(fn=extract_elixhauser_comorbidities, prefix="elix_", score="elix_van_walraven_score",
                       total="elix_total_conditions"),
    "charlson": dict(fn=extract_charlson_index, prefix="cci_", score="charlson_index", total="cci_total_conditions"),
}
CONFIGS = {
    "default": {},
    "raw": {"hierarchy_adjusted": False},
    "source_only": {"source_and_standard": False},
    "all_history": {"lookback_days": None},
}
ELIX_ORDER = ["chf", "arrhythmia", "valvular", "pulm_circ", "pvd", "htn_uncomp", "htn_comp", "paralysis",
              "neuro_other", "copd", "dm_uncomp", "dm_comp", "hypothyroid", "renal_failure", "liver_disease", "pud",
              "hiv", "lymphoma", "mets", "solid_tumor", "rheumatic", "coagulopathy", "obesity", "weight_loss",
              "fluid_electrolyte", "blood_loss_anemia", "deficiency_anemia", "alcohol_abuse", "drug_abuse",
              "psychoses", "depression"]
CCI_ORDER = ["mi", "chf", "pvd", "cevd", "dementia", "copd", "rheum", "pud", "mild_liver", "dm_uncomp", "dm_comp",
             "plegia", "renal", "malignancy", "mod_severe_liver", "mets", "hiv"]
ORDER = {"elixhauser": ELIX_ORDER, "charlson": CCI_ORDER}
# van Walraven 2009 and Charlson 1987 weights as documented in PROVENANCE.md
WEIGHTS = {
    "elixhauser": dict(zip(ELIX_ORDER, [7, 5, -1, 4, 2, 0, 0, 7, 6, 3, 0, 0, 0, 5, 11, 0, 0, 9, 12, 4, 0, 3, -4, 6, 5,
                                        -2, -2, 0, -7, 0, -3])),
    "charlson": dict(zip(CCI_ORDER, [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 3, 6, 6])),
}


# ------------------------------------------------------------------------------------------ fixtures
def _cols(index):
    return [INDEXES[index]["prefix"] + d for d in ORDER[index]]


def load_shared(con):
    """The shared CDM fixture in ``con``: full CDM schema, condition rows, cohort rows, concept_ancestor."""
    build_schema(con)
    p = FIX.as_posix()
    con.execute(f"""
        INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id,
                                          condition_start_date, condition_type_concept_id, condition_source_value)
        SELECT condition_occurrence_id, person_id, condition_concept_id, condition_start_date, 32020,
               condition_source_value
        FROM read_csv('{p}/comorbidity_conditions.csv', header = true, columns = {{
            'condition_occurrence_id': 'INTEGER', 'person_id': 'INTEGER', 'condition_concept_id': 'INTEGER',
            'condition_start_date': 'DATE', 'condition_source_value': 'VARCHAR', 'scenario': 'VARCHAR'}})""")
    con.execute(f"""
        INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
        SELECT cohort_definition_id, subject_id, cohort_start_date, cohort_start_date
        FROM read_csv('{p}/comorbidity_cohort.csv', header = true, columns = {{
            'cohort_definition_id': 'INTEGER', 'subject_id': 'INTEGER', 'cohort_start_date': 'DATE',
            'scenario': 'VARCHAR'}})""")
    con.execute(f"""
        INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation,
                                      max_levels_of_separation)
        SELECT ancestor_concept_id, descendant_concept_id, 0, 0
        FROM read_csv('{p}/comorbidity_concept_ancestor.csv', header = true)""")
    return con


@pytest.fixture(scope="module")
def shared():
    con = load_shared(duckdb.connect(":memory:"))
    yield con
    con.close()


@pytest.fixture(scope="module")
def probes():
    """probe label -> person id, from the cohort fixture's scenario column."""
    with open(FIX / "comorbidity_cohort.csv", newline="") as fh:
        return {r["scenario"]: int(r["subject_id"]) for r in csv.DictReader(fh) if r["scenario"].startswith("probe ")}


def mini(conds=(), cohort=(), ancestors=None):
    """A minimal CDM: ``conds`` = (person, concept, date, source), ``cohort`` = (cohort_id, person, index date)."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE condition_occurrence (condition_occurrence_id INTEGER, person_id INTEGER, "
                "condition_concept_id INTEGER, condition_start_date DATE, condition_source_value VARCHAR)")
    con.execute("CREATE TABLE cohort (cohort_definition_id INTEGER, subject_id INTEGER, cohort_start_date DATE, "
                "cohort_end_date DATE)")
    if conds:
        con.executemany("INSERT INTO condition_occurrence VALUES (?, ?, ?, ?, ?)",
                        [(n, p, c, d, s) for n, (p, c, d, s) in enumerate(conds, start=1)])
    if cohort:
        con.executemany("INSERT INTO cohort VALUES (?, ?, ?, ?)", [(c, p, d, d) for c, p, d in cohort])
    if ancestors is not None:
        con.execute("CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER, "
                    "min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)")
        if ancestors:
            con.executemany("INSERT INTO concept_ancestor VALUES (?, ?, 0, 0)", list(ancestors))
    return con


def flagged(df, index, person, index_date=None):
    """Raw flags of one row as a set of domain names."""
    sub = df[df["subject_id"] == person]
    if index_date is not None:
        sub = sub[pd.to_datetime(sub["cohort_start_date"]) == pd.Timestamp(index_date)]
    assert len(sub) == 1, f"expected exactly one row for person {person}, got {len(sub)}"
    row = sub.iloc[0]
    prefix = INDEXES[index]["prefix"]
    return {d for d in ORDER[index] if row[prefix + d] == 1}


def summary(df, index, person, index_date=None):
    sub = df[df["subject_id"] == person]
    if index_date is not None:
        sub = sub[pd.to_datetime(sub["cohort_start_date"]) == pd.Timestamp(index_date)]
    row = sub.iloc[0]
    return int(row[INDEXES[index]["score"]]), int(row[INDEXES[index]["total"]])


def _golden(index):
    return pd.read_csv(FIX / f"comorbidity_golden_{index}.csv", dtype={"cohort_start_date": str})


# ------------------------------------------------------------------------------------------ parity with golden
@pytest.mark.parametrize("index", list(INDEXES))
@pytest.mark.parametrize("config", list(CONFIGS))
def test_matches_golden_file(shared, index, config):
    got = INDEXES[index]["fn"](shared, **CONFIGS[config])
    gold = _golden(index)
    exp = gold[gold["config"] == config].drop(columns="config").reset_index(drop=True)
    assert list(got.columns) == list(exp.columns)
    got = got.assign(cohort_start_date=pd.to_datetime(got["cohort_start_date"]).dt.strftime("%Y-%m-%d"))
    pd.testing.assert_frame_equal(got, exp, check_dtype=False)


@pytest.mark.parametrize("index", list(INDEXES))
def test_columns_order_and_types(shared, index):
    meta = INDEXES[index]
    df = meta["fn"](shared)
    assert list(df.columns) == ["subject_id", "cohort_start_date", *_cols(index), meta["score"], meta["total"]]
    assert len(_cols(index)) == (31 if index == "elixhauser" else 17)
    for c in [*_cols(index), meta["score"], meta["total"]]:
        assert pd.api.types.is_integer_dtype(df[c]), c
        assert df[c].notna().all(), c
    assert set(df[_cols(index)].to_numpy().ravel()) <= {0, 1}


@pytest.mark.parametrize("index", list(INDEXES))
def test_code_tables_have_documented_weights_and_shape(index):
    with open(CODES / f"{index}_codes.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    domains = list(dict.fromkeys(r["domain"] for r in rows))
    assert domains == ORDER[index]
    assert {r["domain"]: int(r["weight"]) for r in rows} == WEIGHTS[index]
    for d in domains:
        systems = {r["code_system"] for r in rows if r["domain"] == d}
        assert {"ICD10CM", "ICD9CM"} <= systems, f"{d} lacks an ICD-10 or ICD-9 prefix"


# ------------------------------------------------------------------------------------------ every domain, every channel
@pytest.mark.parametrize("index", list(INDEXES))
def test_every_domain_triggers_from_icd10_icd9_and_standard_descendant(shared, probes, index):
    meta = INDEXES[index]
    prefix = meta["prefix"]
    full = meta["fn"](shared)
    src_only = meta["fn"](shared, source_and_standard=False).set_index("subject_id")
    full = full.set_index("subject_id")
    with open(CODES / f"{index}_codes.csv", newline="") as fh:
        with_ancestors = {r["domain"] for r in csv.DictReader(fh) if r["code_system"] == "SNOMED_ANCESTOR"}
    no_standard_side = {d for d in ORDER[index] if d not in with_ancestors}
    # Metastatic solid tumour is detected from ICD source values only in the Charlson table (PROVENANCE.md).
    assert no_standard_side == ({"mets"} if index == "charlson" else set())
    for domain in ORDER[index]:
        col = prefix + domain
        p10, p9 = probes[f"probe {index} {domain} icd10"], probes[f"probe {index} {domain} icd9"]
        assert full.loc[p10, col] == 1, f"{index}/{domain}: ICD-10 probe did not trigger"
        assert src_only.loc[p10, col] == 1
        assert full.loc[p9, col] == 1, f"{index}/{domain}: ICD-9 probe did not trigger"
        assert src_only.loc[p9, col] == 1
        label = f"probe {index} {domain} standard"
        if domain in no_standard_side:
            assert label not in probes
            continue
        ps = probes[label]
        assert full.loc[ps, col] == 1, f"{index}/{domain}: standard-descendant probe did not trigger"
        assert src_only.loc[ps, col] == 0, f"{index}/{domain}: standard probe matched through its source value"


# ------------------------------------------------------------------------------------------ window
@pytest.mark.parametrize("index", list(INDEXES))
def test_window_boundary_days(shared, index):
    df = INDEXES[index]["fn"](shared)
    # persons 10-15: heart failure code on index-1, the index day, index-365, index-366, index+1 and in 2015
    assert flagged(df, index, 10) == {"chf"}, "index-1 must be inside the window"
    assert flagged(df, index, 11) == set(), "the index day itself must be outside (strictly before)"
    assert flagged(df, index, 12) == {"chf"}, "index-lookback must be inside the window"
    assert flagged(df, index, 13) == set(), "index-lookback-1 must be outside the window"
    assert flagged(df, index, 14) == set(), "a record after the index date must never count"
    assert flagged(df, index, 15) == set()


@pytest.mark.parametrize("index", list(INDEXES))
def test_lookback_none_means_all_history_before_the_index(shared, index):
    df = INDEXES[index]["fn"](shared, lookback_days=None)
    assert flagged(df, index, 13) == {"chf"} and flagged(df, index, 15) == {"chf"}
    assert flagged(df, index, 11) == set() and flagged(df, index, 14) == set(), "None must not include the index day or later"


@pytest.mark.parametrize("index", list(INDEXES))
def test_short_and_zero_lookback(shared, index):
    one = INDEXES[index]["fn"](shared, lookback_days=1)
    assert flagged(one, index, 10) == {"chf"} and flagged(one, index, 12) == set()
    zero = INDEXES[index]["fn"](shared, lookback_days=0)
    assert zero[_cols(index)].to_numpy().sum() == 0, "[index, index) is empty"
    assert len(zero) == len(INDEXES[index]["fn"](shared))


def test_lookback_accepts_whole_floats_and_numpy_integers(shared):
    base = extract_charlson_index(shared)
    for value in (365.0, np.int64(365), np.float64(365)):
        pd.testing.assert_frame_equal(extract_charlson_index(shared, lookback_days=value), base)


def test_timestamp_index_column_uses_the_calendar_day():
    con = mini([(1, 0, date(2021, 6, 1), "I50.9"), (2, 0, date(2021, 5, 31), "J44.9")])
    con.execute("CREATE TABLE stays (pid INTEGER, admit_ts TIMESTAMP)")
    con.execute("INSERT INTO stays VALUES (1, TIMESTAMP '2021-06-01 15:00:00'), (2, TIMESTAMP '2021-06-01 00:00:01')")
    df = extract_elixhauser_comorbidities(con, "stays", person_col="pid", index_date_col="admit_ts",
                                          source_and_standard=False)
    assert list(df.columns[:2]) == ["pid", "admit_ts"]
    # same-day record with a later timestamp is NOT strictly before the index date; the day before is
    assert df.loc[df["pid"] == 1, "elix_chf"].item() == 0
    assert df.loc[df["pid"] == 2, "elix_copd"].item() == 1


# ------------------------------------------------------------------------------------------ source matching
@pytest.mark.parametrize("index", list(INDEXES))
def test_source_value_normalisation(shared, index):
    df = INDEXES[index]["fn"](shared)
    assert flagged(df, index, 20) == {"chf"}, "lower case"
    assert flagged(df, index, 21) == {"chf"}, "spaces and dots anywhere"
    assert flagged(df, index, 22) == {"chf"}, "ICD-9 with and without the dot"
    assert flagged(df, index, 23) == set(), "NULL source value"
    assert flagged(df, index, 24) == set(), "empty source value"


def test_whitespace_of_every_kind_is_removed():
    con = mini([(1, 0, date(2021, 5, 1), "i\t50 .\n9"), (2, 0, date(2021, 5, 1), "  428 . 0  "),
                (3, 0, date(2021, 5, 1), "J 44\r.9")],
               [(1, p, date(2021, 6, 1)) for p in (1, 2, 3)])
    df = extract_elixhauser_comorbidities(con, source_and_standard=False)
    assert df["elix_chf"].tolist() == [1, 1, 0]
    assert df["elix_copd"].tolist() == [0, 0, 1]


@pytest.mark.parametrize("index", list(INDEXES))
def test_negative_controls(shared, index):
    df = INDEXES[index]["fn"](shared)
    for person, why in [(30, "unrelated codes"), (31, "shorter than the prefix"), (32, "contains the prefix but does not start with it"),
                        (33, "neighbouring codes")]:
        assert flagged(df, index, person) == set(), why
        assert summary(df, index, person) == (0, 0), why


def test_prefix_matching_is_a_prefix_not_a_substring_or_exact_match():
    con = mini([(1, 0, date(2021, 5, 1), "I50"), (2, 0, date(2021, 5, 1), "I509XYZ"), (3, 0, date(2021, 5, 1), "I5"),
                (4, 0, date(2021, 5, 1), "CI50.9"), (5, 0, date(2021, 5, 1), "50.9")],
               [(1, p, date(2021, 6, 1)) for p in range(1, 6)])
    df = extract_charlson_index(con, source_and_standard=False)
    assert df["cci_chf"].tolist() == [1, 1, 0, 0, 0]


# ------------------------------------------------------------------------------------------ rows
@pytest.mark.parametrize("index", list(INDEXES))
def test_persons_without_conditions_are_retained(shared, index):
    meta = INDEXES[index]
    df = meta["fn"](shared)
    for person in (70, 71):  # no condition rows at all / only a record after the index date
        assert flagged(df, index, person) == set()
        assert summary(df, index, person) == (0, 0)
    distinct = shared.execute("SELECT count(*) FROM (SELECT DISTINCT subject_id, cohort_start_date FROM cohort)").fetchone()[0]
    assert len(df) == distinct


@pytest.mark.parametrize("index", list(INDEXES))
def test_multiple_index_dates_per_person_and_duplicates(shared, index):
    df = INDEXES[index]["fn"](shared)
    p50 = df[df["subject_id"] == 50]
    assert pd.to_datetime(p50["cohort_start_date"]).dt.strftime("%Y-%m-%d").tolist() == ["2021-02-01", "2021-09-01", "2022-09-20"]
    assert flagged(df, index, 50, "2021-02-01") == {"chf"}, "the copd record is after this index date"
    assert flagged(df, index, 50, "2021-09-01") == {"chf", "copd"}
    assert flagged(df, index, 50, "2022-09-20") == set(), "both records are older than 365 days"
    assert (df["subject_id"] == 51).sum() == 1, "a duplicated cohort row must give one output row"
    assert not df.duplicated(["subject_id", "cohort_start_date"]).any()
    keys = list(zip(df["subject_id"], pd.to_datetime(df["cohort_start_date"])))
    assert keys == sorted(keys), "rows are ordered by person then index date"


@pytest.mark.parametrize("index", list(INDEXES))
def test_cohort_id_filter(shared, index):
    fn = INDEXES[index]["fn"]
    everyone = set(fn(shared)["subject_id"])
    one = set(fn(shared, cohort_id=1)["subject_id"])
    two = fn(shared, cohort_id=2)
    assert set(two["subject_id"]) == {60, 61, 62} and len(two) == 3
    assert flagged(two, index, 60) == {"chf"} and flagged(two, index, 61) == set() and flagged(two, index, 62) == {"copd"}
    assert {60, 61}.isdisjoint(one) and 62 in one
    assert everyone == one | {60, 61}
    assert len(fn(shared, cohort_id=999)) == 0
    assert list(fn(shared, cohort_id=999).columns) == list(fn(shared).columns)


def test_cohort_id_without_a_cohort_definition_id_column_warns_and_profiles_everything():
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")])
    con.execute("CREATE TABLE plain AS SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
    with pytest.warns(UserWarning, match="cohort_id=7 was ignored"):
        df = extract_charlson_index(con, "plain", cohort_id=7, source_and_standard=False)
    assert df["cci_chf"].tolist() == [1]


def test_empty_cohort_returns_an_empty_frame_with_all_columns():
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")])
    for index, meta in INDEXES.items():
        df = meta["fn"](con, source_and_standard=False)
        assert len(df) == 0
        assert list(df.columns) == ["subject_id", "cohort_start_date", *_cols(index), meta["score"], meta["total"]]


# ------------------------------------------------------------------------------------------ hierarchy
# person: {index: (raw flags, (score, total) adjusted, (score, total) raw)} -- see the module docstring for person 40
HAND = {
    40: {"elixhauser": ({"chf", "htn_uncomp", "htn_comp", "dm_uncomp", "dm_comp", "liver_disease", "mets", "solid_tumor"},
                        (30, 5), (34, 8)),
         "charlson": ({"mi", "chf", "mild_liver", "dm_uncomp", "dm_comp", "malignancy", "mod_severe_liver", "mets"},
                      (13, 5), (17, 8))},
    # E11.22 + E11.9: both diabetes flags. Elixhauser weights are 0 (count 1 vs 2); Charlson dm_comp 2 vs 2 + 1.
    41: {"elixhauser": ({"dm_uncomp", "dm_comp"}, (0, 1), (0, 2)), "charlson": ({"dm_uncomp", "dm_comp"}, (2, 1), (3, 2))},
    # E66.9 obesity -4, F11.20 drug abuse -7, F32.9 depression -3, E03.9 hypothyroid 0: the score can be negative
    42: {"elixhauser": ({"hypothyroid", "obesity", "drug_abuse", "depression"}, (-14, 4), (-14, 4)),
         "charlson": (set(), (0, 0), (0, 0))},
    # C78.0 mets 12 + C50.9 solid tumour 4: 12 after the hierarchy, 16 raw. Charlson mets 6 + malignancy 2: 6 vs 8
    43: {"elixhauser": ({"mets", "solid_tumor"}, (12, 1), (16, 2)), "charlson": ({"malignancy", "mets"}, (6, 1), (8, 2))},
    # K73.9 (mild) + K72.9 (severe): Elixhauser one liver_disease 11; Charlson severe 3 vs 1 + 3
    44: {"elixhauser": ({"liver_disease"}, (11, 1), (11, 1)), "charlson": ({"mild_liver", "mod_severe_liver"}, (3, 1), (4, 2))},
    # I10 + I12.9: both hypertension flags, weights 0, so only the count differs
    45: {"elixhauser": ({"htn_uncomp", "htn_comp"}, (0, 1), (0, 2)), "charlson": (set(), (0, 0), (0, 0))},
}


@pytest.mark.parametrize("index", list(INDEXES))
@pytest.mark.parametrize("person", sorted(HAND))
def test_hierarchy_with_hand_computed_scores(shared, index, person):
    fn = INDEXES[index]["fn"]
    flags, adjusted, raw = HAND[person][index]
    on, off = fn(shared, hierarchy_adjusted=True), fn(shared, hierarchy_adjusted=False)
    # the 0/1 columns are the raw flags either way
    assert flagged(on, index, person) == flags and flagged(off, index, person) == flags
    assert summary(on, index, person) == adjusted
    assert summary(off, index, person) == raw
    # the plain weighted sum is recomputable from the flags and the documented weights
    assert raw[0] == sum(WEIGHTS[index][d] for d in flags) and raw[1] == len(flags)


def test_each_hierarchy_rule_in_isolation():
    conds = [(1, 0, date(2021, 5, 1), "E11.22"), (1, 0, date(2021, 5, 1), "E11.9"),     # dm_comp + dm_uncomp
             (2, 0, date(2021, 5, 1), "C78.0"), (2, 0, date(2021, 5, 1), "C50.9"),      # mets + solid tumour
             (3, 0, date(2021, 5, 1), "I12.9"), (3, 0, date(2021, 5, 1), "I10"),        # htn_comp + htn_uncomp
             (4, 0, date(2021, 5, 1), "K72.9"), (4, 0, date(2021, 5, 1), "K73.9")]      # severe + mild liver
    con = mini(conds, [(1, p, date(2021, 6, 1)) for p in (1, 2, 3, 4)])
    elix_on = extract_elixhauser_comorbidities(con, source_and_standard=False)
    elix_off = extract_elixhauser_comorbidities(con, source_and_standard=False, hierarchy_adjusted=False)
    assert elix_on["elix_total_conditions"].tolist() == [1, 1, 1, 1]
    assert elix_off["elix_total_conditions"].tolist() == [2, 2, 2, 1]
    assert elix_on["elix_van_walraven_score"].tolist() == [0, 12, 0, 11]
    assert elix_off["elix_van_walraven_score"].tolist() == [0, 16, 0, 11]
    cci_on = extract_charlson_index(con, source_and_standard=False)
    cci_off = extract_charlson_index(con, source_and_standard=False, hierarchy_adjusted=False)
    assert cci_on["charlson_index"].tolist() == [2, 6, 0, 3] and cci_on["cci_total_conditions"].tolist() == [1, 1, 0, 1]
    assert cci_off["charlson_index"].tolist() == [3, 8, 0, 4] and cci_off["cci_total_conditions"].tolist() == [2, 2, 0, 2]


def test_weights_come_from_the_csv_not_from_code(monkeypatch, tmp_path):
    lines = (CODES / "elixhauser_codes.csv").read_text(encoding="utf-8").splitlines()
    out = [lines[0]] + [ln.replace(",chf,elix_chf,Congestive heart failure,7,", ",chf,elix_chf,Congestive heart failure,70,")
                        for ln in lines[1:]]
    assert out != lines
    (tmp_path / "elixhauser_codes.csv").write_text("\n".join(out) + "\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_resource_path", lambda *parts: str(tmp_path / parts[-1]))
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")], [(1, 1, date(2021, 6, 1))])
    assert extract_elixhauser_comorbidities(con, source_and_standard=False)["elix_van_walraven_score"].tolist() == [70]


# ------------------------------------------------------------------------------------------ standard concepts
@pytest.mark.parametrize("index", list(INDEXES))
def test_standard_concept_side(shared, index):
    full = INDEXES[index]["fn"](shared)
    src = INDEXES[index]["fn"](shared, source_and_standard=False)
    # 316139 = chf ancestor, 255573 = copd ancestor; 9000001/9000002 are its child and grandchild; 9000003 sits under both
    expected_full = {80: {"chf"}, 81: {"chf"}, 82: {"chf"}, 83: {"chf", "copd"}, 84: set(), 85: {"chf"},
                     86: {"chf", "copd"}, 87: set(), 88: {"copd"}}
    expected_src = {80: set(), 81: set(), 82: set(), 83: set(), 84: set(), 85: {"chf"}, 86: {"copd"}, 87: set(),
                    88: {"copd"}}
    for person, domains in expected_full.items():
        assert flagged(full, index, person) == domains, f"person {person}"
        assert flagged(src, index, person) == expected_src[person], f"person {person} (source only)"


def test_standard_side_respects_the_window():
    con = mini([(1, 9001, date(2021, 6, 1), None), (2, 9001, date(2021, 5, 31), None), (3, 9001, date(2020, 5, 31), None)],
               [(1, p, date(2021, 6, 1)) for p in (1, 2, 3)], ancestors=[(316139, 316139), (316139, 9001), (9001, 9001)])
    assert extract_elixhauser_comorbidities(con)["elix_chf"].tolist() == [0, 1, 0]


def test_ancestor_matches_even_without_its_self_row():
    con = mini([(1, 316139, date(2021, 5, 1), None)], [(1, 1, date(2021, 6, 1))], ancestors=[(316139, 9001)])
    assert extract_elixhauser_comorbidities(con)["elix_chf"].tolist() == [1]


def test_missing_concept_ancestor_warns_and_falls_back_to_source_only():
    con = mini([(1, 9001, date(2021, 5, 1), None), (2, 0, date(2021, 5, 1), "I50.9")],
               [(1, p, date(2021, 6, 1)) for p in (1, 2)])  # no concept_ancestor table at all
    for index, meta in INDEXES.items():
        with pytest.warns(UserWarning, match=r"concept_ancestor could not be resolved .*source-value \(ICD prefix\) matching only"):
            df = meta["fn"](con)
        src = meta["fn"](con, source_and_standard=False)
        pd.testing.assert_frame_equal(df, src)
        assert df[meta["prefix"] + "chf"].tolist() == [0, 1]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        extract_elixhauser_comorbidities(con, source_and_standard=False)  # an explicit choice is not warned about


def test_empty_concept_ancestor_warns_too():
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")], [(1, 1, date(2021, 6, 1))], ancestors=[])
    with pytest.warns(UserWarning, match="concept_ancestor exists on this connection but is empty"):
        df = extract_charlson_index(con)
    assert df["cci_chf"].tolist() == [1]


def _vocab_file(tmp_path, rows=((316139, 316139), (316139, 9001), (9001, 9001))):
    path = tmp_path / "vocab.duckdb"
    v = duckdb.connect(str(path))
    v.execute("CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER, "
              "min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)")
    v.executemany("INSERT INTO concept_ancestor VALUES (?, ?, 0, 0)", list(rows))
    v.close()
    return path


@pytest.mark.parametrize("set_search_path", [False, True])
def test_concept_ancestor_from_an_attached_central_vocab(tmp_path, set_search_path):
    con = mini([(1, 9001, date(2021, 5, 1), None)], [(1, 1, date(2021, 6, 1))])  # no concept_ancestor in main
    con.execute(f"ATTACH '{_vocab_file(tmp_path).as_posix()}' AS central_vocab (READ_ONLY)")
    if set_search_path:
        con.execute("SET search_path = 'main,central_vocab.main'")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert extract_elixhauser_comorbidities(con)["elix_chf"].tolist() == [1]
        assert extract_charlson_index(con)["cci_chf"].tolist() == [1]


def test_empty_main_concept_ancestor_does_not_shadow_an_attached_vocabulary(tmp_path):
    con = mini([(1, 9001, date(2021, 5, 1), None)], [(1, 1, date(2021, 6, 1))], ancestors=[])  # empty table in main
    con.execute(f"ATTACH '{_vocab_file(tmp_path).as_posix()}' AS central_vocab (READ_ONLY)")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert extract_elixhauser_comorbidities(con)["elix_chf"].tolist() == [1]


def test_read_only_cdm_with_an_attached_read_only_vocabulary_on_the_search_path(tmp_path):
    """The omop_connect(read_only=TRUE) layout: a read-only CDM file, a read-only vocabulary file attached as
    central_vocab and ``SET search_path = 'main,central_vocab.main'``. Nothing may be created in either database."""
    cdm = tmp_path / "cdm.duckdb"
    rw = duckdb.connect(str(cdm))
    rw.execute("CREATE TABLE condition_occurrence (condition_occurrence_id INTEGER, person_id INTEGER, "
               "condition_concept_id INTEGER, condition_start_date DATE, condition_source_value VARCHAR)")
    rw.execute("INSERT INTO condition_occurrence VALUES (1, 1, 9001, DATE '2021-05-01', NULL), "
               "(2, 2, 0, DATE '2021-05-01', 'I50.9')")
    rw.execute("CREATE TABLE cohort AS SELECT i AS subject_id, DATE '2021-06-01' AS cohort_start_date FROM range(1, 4) AS t(i)")
    rw.close()
    ro = duckdb.connect(str(cdm), read_only=True)
    try:
        ro.execute(f"ATTACH '{_vocab_file(tmp_path).as_posix()}' AS central_vocab (READ_ONLY)")
        ro.execute("SET search_path = 'main,central_vocab.main'")
        before = _catalog(ro)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            elix = extract_elixhauser_comorbidities(ro)
            cci = extract_charlson_index(ro)
        assert elix["elix_chf"].tolist() == [1, 1, 0] and cci["cci_chf"].tolist() == [1, 1, 0]
        assert _catalog(ro) == before
    finally:
        ro.close()


# ------------------------------------------------------------------------------------------ connections and cleanliness
def test_works_on_a_read_only_connection(tmp_path):
    path = tmp_path / "cdm.duckdb"
    rw = load_shared(duckdb.connect(str(path)))
    expected = {i: m["fn"](rw) for i, m in INDEXES.items()}
    rw.close()
    ro = duckdb.connect(str(path), read_only=True)
    try:
        assert ro.execute("SELECT readonly FROM duckdb_databases() WHERE database_name = current_database()").fetchone()[0]
        for index, meta in INDEXES.items():
            for config, kwargs in CONFIGS.items():
                got = meta["fn"](ro, **kwargs)
                assert len(got) == len(expected[index])
            pd.testing.assert_frame_equal(meta["fn"](ro), expected[index])
    finally:
        ro.close()


def _catalog(con):
    return {
        "tables": con.execute("SELECT database_name, schema_name, table_name, temporary FROM duckdb_tables() ORDER BY ALL").fetchall(),
        "views": con.execute("SELECT database_name, schema_name, view_name, temporary FROM duckdb_views() "
                             "WHERE NOT internal ORDER BY ALL").fetchall(),
        "functions": con.execute("SELECT database_name, schema_name, function_name FROM duckdb_functions() "
                                 "WHERE NOT internal ORDER BY ALL").fetchall(),
        "sequences": con.execute("SELECT database_name, sequence_name FROM duckdb_sequences() ORDER BY ALL").fetchall(),
        "schemas": con.execute("SELECT database_name, schema_name FROM duckdb_schemas() ORDER BY ALL").fetchall(),
    }


def test_leaves_no_objects_behind(shared):
    before = _catalog(shared)
    for meta in INDEXES.values():
        for kwargs in CONFIGS.values():
            meta["fn"](shared, **kwargs)
        meta["fn"](shared, cohort_id=2)
    assert _catalog(shared) == before
    assert not any(t[3] for t in before["tables"]), "the shared fixture itself must have no temporary tables"


# ------------------------------------------------------------------------------------------ arguments
BAD_ARGUMENTS = [
    ({"cohort_table": "no_such_table"}, "does not exist"),
    ({"cohort_table": "cohort; DROP TABLE condition_occurrence"}, "cohort_table must be"),
    ({"cohort_table": "cohort --"}, "cohort_table must be"),
    ({"cohort_table": 5}, "cohort_table must be"),
    ({"person_col": "nope"}, "person_col 'nope' is not a column"),
    ({"index_date_col": "nope"}, "index_date_col 'nope' is not a column"),
    ({"person_col": 'subject_id" FROM x; --'}, "is not a column"),
    ({"person_col": None}, "person_col must be a column name"),
    ({"person_col": "cohort_start_date", "index_date_col": "cohort_start_date"}, "different columns"),
    ({"lookback_days": -1}, "lookback_days must be a non-negative integer or None"),
    ({"lookback_days": 1.5}, "lookback_days must be"),
    ({"lookback_days": "365"}, "lookback_days must be"),
    ({"lookback_days": True}, "lookback_days must be"),
    ({"lookback_days": float("nan")}, "lookback_days must be"),
    ({"cohort_id": "a"}, "cohort_id must be an integer"),
    ({"cohort_id": True}, "cohort_id must be an integer"),
    ({"cohort_id": 1.5}, "cohort_id must be an integer"),
    ({"source_and_standard": 1}, "source_and_standard must be True or False"),
    ({"source_and_standard": None}, "source_and_standard must be True or False"),
    ({"hierarchy_adjusted": "yes"}, "hierarchy_adjusted must be True or False"),
    ({"format": "csv"}, "format must be one of"),
    ({"format": None}, "format must be one of"),
]


@pytest.mark.parametrize("index", list(INDEXES))
@pytest.mark.parametrize("kwargs,message", BAD_ARGUMENTS)
def test_bad_arguments_raise_clear_errors(shared, index, kwargs, message):
    with pytest.raises(ValueError, match=message):
        INDEXES[index]["fn"](shared, **kwargs)
    assert shared.execute("SELECT count(*) FROM condition_occurrence").fetchone()[0] > 0  # nothing was dropped


def test_columns_are_never_guessed():
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")])
    con.execute("CREATE TABLE stays AS SELECT 1 AS person_id, DATE '2021-06-01' AS visit_start_date")
    with pytest.raises(ValueError, match="person_col 'subject_id' is not a column of 'stays'.*never guessed"):
        extract_elixhauser_comorbidities(con, "stays")
    with pytest.raises(ValueError, match="index_date_col 'cohort_start_date' is not a column of 'stays'"):
        extract_charlson_index(con, "stays", person_col="person_id")


def test_custom_columns_schema_qualified_table_and_case_insensitive_names():
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")])
    con.execute("CREATE SCHEMA study")
    con.execute("CREATE TABLE study.stays AS SELECT 1 AS \"Person_ID\", DATE '2021-06-01' AS visit_start_date")
    df = extract_elixhauser_comorbidities(con, "study.stays", person_col="person_id", index_date_col="VISIT_START_DATE",
                                          source_and_standard=False)
    assert list(df.columns[:2]) == ["person_id", "VISIT_START_DATE"], "output columns are named as passed"
    assert df["elix_chf"].tolist() == [1]


def test_missing_or_incomplete_condition_occurrence():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE cohort AS SELECT 1 AS subject_id, DATE '2021-06-01' AS cohort_start_date")
    with pytest.raises(ValueError, match="condition_occurrence does not exist"):
        extract_elixhauser_comorbidities(con)
    con.execute("CREATE TABLE condition_occurrence (person_id INTEGER, condition_concept_id INTEGER, "
                "condition_start_date DATE)")
    with pytest.raises(ValueError, match=r"condition_occurrence is missing column\(s\) \['condition_source_value'\]"):
        extract_charlson_index(con)


def test_null_person_or_index_date_is_an_error_not_a_silent_zero_row():
    con = mini([(1, 0, date(2021, 5, 1), "I50.9")])
    con.execute("INSERT INTO cohort VALUES (1, 1, DATE '2021-06-01', DATE '2021-06-01'), (1, 2, NULL, NULL)")
    with pytest.raises(ValueError, match="1 row.*NULL subject_id or cohort_start_date"):
        extract_elixhauser_comorbidities(con)
    # excluded by the cohort_id filter, so the remaining rows are fine
    con.execute("UPDATE cohort SET cohort_definition_id = 2 WHERE subject_id = 2")
    assert extract_elixhauser_comorbidities(con, cohort_id=1, source_and_standard=False)["elix_chf"].tolist() == [1]


# ------------------------------------------------------------------------------------------ code-table loader
@pytest.mark.parametrize("edit,message", [
    (lambda lines: [lines[0].replace("code_system", "system")] + lines[1:], "expected header"),
    (lambda lines: [lines[0], lines[1].replace(",ICD10CM,", ",ICD11,")] + lines[2:], "unknown code_system"),
    (lambda lines: [lines[0], lines[1].replace(",1,ICD10CM,", ",x,ICD10CM,")] + lines[2:], "not an integer"),
    (lambda lines: [lines[0], lines[1].replace(",1,ICD10CM,", ",5,ICD10CM,")] + lines[2:], "more than one column or weight"),
    (lambda lines: [lines[0], lines[1].replace(",I21", ",i21")] + lines[2:], "upper-case"),
    (lambda lines: [lines[0], lines[1].replace("charlson,", "elixhauser,", 1)] + lines[2:], "expected 'charlson'"),
    (lambda lines: [lines[0], lines[1].replace(",mi,cci_mi", ",MI,cci_mi")] + lines[2:], "lower-case identifiers"),
])
def test_code_table_loader_rejects_malformed_files(monkeypatch, tmp_path, edit, message):
    lines = (CODES / "charlson_codes.csv").read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith("charlson,mi,cci_mi,") and ",1,ICD10CM,I21" in lines[1]
    (tmp_path / "charlson_codes.csv").write_text("\n".join(edit(lines)) + "\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_resource_path", lambda *parts: str(tmp_path / parts[-1]))
    with pytest.raises(ValueError, match=message):
        extract_charlson_index(mini())


def test_missing_code_table_names_both_layouts(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_resource_path", lambda *parts: str(tmp_path / parts[-1]))
    with pytest.raises(FileNotFoundError, match="inst/extdata/comorbidity/elixhauser_codes.csv.*omop_etl/resources/extdata/comorbidity"):
        extract_elixhauser_comorbidities(mini())


def test_code_tables_resolve_from_a_wheel_style_layout(tmp_path):
    """omop_etl/resources/extdata/comorbidity/*.csv (pyproject force-include target) is found with no inst/ tree."""
    site = tmp_path / "site"
    pkg = site / "omop_etl"
    shutil.copytree(REPO_ROOT / "python" / "omop_etl", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    probe = ("import os, omop_etl.comorbidity as c; "
             "p = c._resource_path('extdata', 'comorbidity', 'elixhauser_codes.csv'); print(p); "
             "print(len(c._load_code_table(c._ELIXHAUSER).domains), len(c._load_code_table(c._CHARLSON).domains))")
    env = {**os.environ, "PYTHONPATH": str(site)}

    bare = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert bare.returncode != 0 and "omop_etl/resources/extdata/comorbidity/elixhauser_codes.csv" in bare.stderr

    dest = pkg / "resources" / "extdata" / "comorbidity"
    dest.mkdir(parents=True)
    for name in ("elixhauser_codes.csv", "charlson_codes.csv", "PROVENANCE.md"):
        shutil.copy(CODES / name, dest / name)
    ok = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    path_line, counts = ok.stdout.strip().splitlines()[-2:]
    assert Path(path_line) == dest / "elixhauser_codes.csv"
    assert counts == "31 17"


def test_draft_block_is_gone_from_features():
    from omop_etl import features
    for name in ("extract_elixhauser_comorbidities", "extract_charlson_index", "ELIXHAUSER_DOMAINS", "CHARLSON_CATEGORIES"):
        assert not hasattr(features, name), f"the unreviewed draft {name} is still in features.py"


# ------------------------------------------------------------------------------------------ output formats
@pytest.mark.parametrize("index", list(INDEXES))
def test_arrow_output_matches_the_dataframe(shared, index):
    pa = pytest.importorskip("pyarrow")
    fn = INDEXES[index]["fn"]
    tbl = fn(shared, format="arrow")
    assert isinstance(tbl, pa.Table)
    df = fn(shared)
    assert tbl.column_names == list(df.columns)
    assert pa.types.is_date32(tbl.schema.field("cohort_start_date").type), "a DATE index stays a DATE in arrow"
    back = tbl.to_pandas()
    back["cohort_start_date"] = pd.to_datetime(back["cohort_start_date"])
    pd.testing.assert_frame_equal(back, df, check_dtype=False)
    assert isinstance(fn(shared, format="PyArrow "), pa.Table)


@pytest.mark.parametrize("index", list(INDEXES))
def test_polars_output_matches_the_dataframe(shared, index):
    pl = pytest.importorskip("polars")
    fn = INDEXES[index]["fn"]
    out = fn(shared, format="polars")
    assert isinstance(out, pl.DataFrame)
    assert out.columns == list(fn(shared).columns) and out.height == len(fn(shared))


def test_polars_requested_but_missing_raises(shared, monkeypatch):
    monkeypatch.setitem(sys.modules, "polars", None)
    with pytest.raises(ImportError, match="polars"):
        extract_charlson_index(shared, format="polars")


# ------------------------------------------------------------------------------------------ scale
def test_twenty_thousand_index_rows_four_hundred_thousand_conditions():
    codes = ["I50.9", "I10", "E11.9", "J44.9", "C50.9", "C78.0", "K72.9", "F32.9", "E66.9", "428.0", "250.00", "401.9",
             "Z00.00", "J01.90", "S72.001A", "N18.3", "I48.91", "D64.9", "M54.5", "R07.9", "K21.9", "E78.5", "G47.33",
             "F41.1", "I25.10", "N39.0", "L03.90", "H25.9", "M17.9", "Z79.4"]
    with open(CODES / "elixhauser_codes.csv", newline="") as fh:
        ancestors = sorted({int(r["code"]) for r in csv.DictReader(fh) if r["code_system"] == "SNOMED_ANCESTOR"})
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE concept_ancestor AS SELECT CAST(a AS BIGINT) AS ancestor_concept_id, "
                "CAST(a AS BIGINT) * 1000 + k AS descendant_concept_id, 1 AS min_levels_of_separation, "
                "1 AS max_levels_of_separation FROM (SELECT unnest($1) AS a) CROSS JOIN range(1, 51) AS t(k)", [ancestors])
    con.execute("INSERT INTO concept_ancestor SELECT a, a, 0, 0 FROM (SELECT unnest($1::BIGINT[]) AS a)", [ancestors])
    con.execute("CREATE TABLE cohort AS SELECT 1 AS cohort_definition_id, i + 1 AS subject_id, "
                "DATE '2021-01-01' + CAST(i % 500 AS INTEGER) AS cohort_start_date, "
                "DATE '2021-01-01' + CAST(i % 500 AS INTEGER) AS cohort_end_date FROM range(20000) AS t(i)")
    con.execute("CREATE TABLE condition_occurrence AS SELECT i AS condition_occurrence_id, (i % 20000) + 1 AS person_id, "
                "CASE WHEN i % 7 = 0 THEN CAST(CAST(list_element($1, 1 + CAST(hash(i) % 40 AS INTEGER)) AS BIGINT) * 1000 "
                "+ 1 + CAST(hash(i + 1) % 50 AS INTEGER) AS BIGINT) ELSE 0 END AS condition_concept_id, "
                "DATE '2020-01-01' + CAST(hash(i * 7) % 900 AS INTEGER) AS condition_start_date, "
                "list_element($2, 1 + CAST(hash(i * 3) % 30 AS INTEGER)) AS condition_source_value "
                "FROM range(400000) AS t(i)", [ancestors[:40], codes])
    assert con.execute("SELECT count(*) FROM condition_occurrence").fetchone()[0] == 400000

    started = time.perf_counter()
    elix = extract_elixhauser_comorbidities(con)
    cci = extract_charlson_index(con)
    elapsed = time.perf_counter() - started
    assert len(elix) == len(cci) == 20000
    assert elapsed < 30, f"two profiles of 20,000 rows took {elapsed:.1f}s"

    # independent plain-SQL check of one domain (chf): ICD prefixes by LIKE, standard side by IN (descendants)
    with open(CODES / "elixhauser_codes.csv", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["domain"] == "chf"]
    like = " OR ".join(f"x.src LIKE '{r['code']}%'" for r in rows if r["code_system"] != "SNOMED_ANCESTOR")
    anc = ", ".join(r["code"] for r in rows if r["code_system"] == "SNOMED_ANCESTOR")
    expected = con.execute(f"""
        SELECT DISTINCT c.subject_id FROM cohort c JOIN (
            SELECT person_id, condition_concept_id, condition_start_date,
                   upper(replace(replace(condition_source_value, '.', ''), ' ', '')) AS src
            FROM condition_occurrence) x ON x.person_id = c.subject_id
        WHERE x.condition_start_date < c.cohort_start_date AND x.condition_start_date >= c.cohort_start_date - 365
          AND (({like}) OR x.condition_concept_id IN (
               SELECT descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id IN ({anc})))
        ORDER BY 1""").fetchall()
    assert expected, "the scale fixture must contain heart failure patients"
    assert elix.loc[elix["elix_chf"] == 1, "subject_id"].tolist() == [r[0] for r in expected]
    assert elix[_cols("elixhauser")].to_numpy().sum() > 0 and cci[_cols("charlson")].to_numpy().sum() > 0


# ------------------------------------------------------------------------------------------ optional real vocabulary
VOCAB_DB = os.environ.get("OMOP_VOCAB_DB")


@pytest.mark.skipif(not VOCAB_DB or not Path(VOCAB_DB).exists(),
                    reason="set OMOP_VOCAB_DB to an Athena vocabulary DuckDB file to run the real-vocabulary check")
def test_real_vocabulary_standard_concepts():
    """Concept ids verified against the vocabulary: 316139 heart failure, 312327 acute myocardial infarction (not a
    pulmonary-circulation disorder), 201826 type 2 diabetes mellitus, 320128 essential hypertension, 316866 hypertensive
    disorder (the parent, so not 'uncomplicated hypertension' by itself)."""
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{Path(VOCAB_DB).as_posix()}' AS central_vocab (READ_ONLY)")
    con.execute("CREATE TABLE condition_occurrence (condition_occurrence_id INTEGER, person_id INTEGER, "
                "condition_concept_id INTEGER, condition_start_date DATE, condition_source_value VARCHAR)")
    con.execute("INSERT INTO condition_occurrence VALUES (1, 1, 316139, DATE '2021-05-01', NULL), "
                "(2, 2, 312327, DATE '2021-05-01', NULL), (3, 3, 201826, DATE '2021-05-01', NULL), "
                "(4, 4, 320128, DATE '2021-05-01', NULL), (5, 5, 316866, DATE '2021-05-01', NULL)")
    con.execute("CREATE TABLE cohort AS SELECT i AS subject_id, DATE '2021-06-01' AS cohort_start_date FROM range(1, 7) AS t(i)")
    con.execute("SET search_path = 'main,central_vocab.main'")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        elix = extract_elixhauser_comorbidities(con)
        cci = extract_charlson_index(con)
    assert flagged(elix, "elixhauser", 1) == {"chf"} and flagged(cci, "charlson", 1) == {"chf"}
    assert flagged(elix, "elixhauser", 2) == set() and flagged(cci, "charlson", 2) == {"mi"}
    assert flagged(elix, "elixhauser", 3) == {"dm_uncomp"} and flagged(cci, "charlson", 3) == {"dm_uncomp"}
    assert flagged(elix, "elixhauser", 4) == {"htn_uncomp"}
    assert flagged(elix, "elixhauser", 5) == set()
    assert flagged(elix, "elixhauser", 6) == set()
