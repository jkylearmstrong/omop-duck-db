"""Kyle's review of PR 104 (0.34.1): BMI Methods wording, tobacco from PCORnet VITAL with a "Missing / Unknown" level, observation
stays in days_since_prior_ip_ed, ZIPs that lost a leading zero or sit in ADDRESS_ZIP9 only, deprecated 'Maps to' rows, and BMI units
read from unit_source_value. Synthetic data only."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl.benchmark_features import extract_benchmark_features, extract_protocol_history_features  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "tests" / "fixtures" / "pcornet_sample"


def _history_db():
    c = duckdb.connect()
    c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,"
              " visit_end_date DATE)")
    c.execute("""INSERT INTO visit_occurrence VALUES (1, 1, 9201, DATE '2019-06-01', DATE '2019-06-03'),
                 (2, 1, 581385, DATE '2019-12-20', DATE '2019-12-21')""")
    c.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,"
              " condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
    c.execute("""INSERT INTO condition_occurrence VALUES (5, 1, DATE '2019-01-01', 'f17210', NULL), (6, 1, DATE '2019-01-01', 'Z87891', NULL),
                 (7, 1, DATE '2019-01-01', 'V1582', NULL)""")
    c.execute("CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE, value_as_concept_id INT,"
              " value_source_value VARCHAR)")
    c.execute("""INSERT INTO observation VALUES
        (1, 43054909, DATE '2019-01-01', 45881517, '01'),   -- VITAL.SMOKING 01: current every day
        (2, 43054909, DATE '2019-01-01', 45883458, '03'),   -- 03: former
        (3, 43054909, DATE '2019-01-01', 45879404, '04'),   -- 04: never
        (4, 43054909, DATE '2019-01-01', 45885135, '06'),   -- 06: unknown if ever smoked -> no status
        (8, 43054909, DATE '2019-01-01', 0, '04')           -- a builder that kept only the PCORnet code""")
    return c


def _cohort(n):
    return pd.DataFrame({"row_id": range(n), "subject_id": range(1, n + 1),
                         "cohort_start_date": pd.to_datetime(["2020-01-10"] * n), "cohort_end_date": pd.to_datetime(["2020-01-15"] * n)})


def test_tobacco_reads_vital_smoking_undotted_codes_and_keeps_unknown():
    h = extract_protocol_history_features(_history_db(), _cohort(9))
    assert h["tobacco"].tolist() == ["Current user", "Former user", "Never user", "Missing / Unknown", "Current user", "Former user",
                                     "Former user", "Never user", "Missing / Unknown"]


def test_days_since_prior_ip_ed_counts_observation_stays():
    h = extract_protocol_history_features(_history_db(), _cohort(1))
    assert h.loc[0, "days_since_prior_ip_ed"] == 21 and h.loc[0, "prior_ip_os_ed_count"] == 2   # the observation stay of 2019-12-20


def test_unknown_tobacco_is_a_table_level_only_when_present():
    from omop_etl.manuscript_tables import TOB_MISSING, _tob_levels

    assert TOB_MISSING not in _tob_levels({"m_tobacco": pd.Series(["Tobacco: Never user"])})
    assert _tob_levels({"m_tobacco": pd.Series(["Tobacco: Never user", TOB_MISSING])})[-1] == TOB_MISSING


def test_bmi_units_from_unit_source_value():
    con = duckdb.connect()
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE,"
                " measurement_datetime TIMESTAMP, value_as_number DOUBLE, unit_concept_id INT, unit_source_value VARCHAR)")
    con.executemany("INSERT INTO measurement VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, 1, 3036277, "2020-01-11", None, 175.0, 0, "cm"), (2, 1, 3025315, "2020-01-11", None, 70.0, 0, "kg"),   # metric, unit concept 0
        (3, 2, 3036277, "2020-01-11", None, 70.0, 9326, "[in_us]"), (4, 2, 3025315, "2020-01-11", None, 200.0, 8739, "[lb_av]")])
    con.execute("CREATE TABLE cohort_t AS SELECT subject_id, DATE '2020-01-10' AS cohort_start_date, DATE '2020-01-15' AS cohort_end_date, "
                "100 + subject_id AS visit_occurrence_id, 0 AS outcome_flag FROM range(1, 3) t(subject_id)")
    off = dict(include_demographics=False, include_elixhauser=False, include_medications=False, include_labs=False, include_utilization=False,
               include_disposition=False, vital_concept_ids={"bmi": [1]}, vocabulary_db=None)
    bmi = dict(zip(*extract_benchmark_features(con, "cohort_t", **off)[["person_id", "vital_bmi"]].T.values))
    assert bmi[1] == pytest.approx(70 / 1.75 ** 2, abs=0.01)     # was 70 lb -> 10.4, outside the plausible range
    assert bmi[2] == pytest.approx(28.70, abs=0.01)


def test_methods_describe_the_bmi_fill(tmp_path):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_rubin_fixes_0340 import _workbook

    wb = _workbook(tmp_path)
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"], "profile": "ederri"}

    def text(**extra):
        md = mtx.full_manuscript_markdown(tabs, [], workbook=wb, run={**run, **extra}, summary={"n_features": 95}, version="0.34.1")["markdown"]
        return re.sub(r"\s+", " ", md)

    t = text()                                                   # an ederri run record without the field: the profile default
    assert "the latest BMI recorded in the 365 days before admission was used" in t
    assert "a missing BMI was derived from height and weight or taken from the prior year" in t
    assert "AUPRC" not in t
    assert "BMI recorded in the 365 days" not in text(bmi_fill="none") and "a missing BMI" not in text(bmi_fill="none")
    assert "computed from the stay's mean height and weight" in text(bmi_fill="derive")


def _sample(tmp_path, address_rows):
    src = tmp_path / "src"
    shutil.copytree(SAMPLE_DIR, src)
    pat = duckdb.connect().execute(f"SELECT PATID FROM read_csv_auto('{(src / 'demographic.csv').as_posix()}', all_varchar=true)").fetchone()[0]
    header = ("ADDRESSID,PATID,ADDRESS_USE,ADDRESS_TYPE,ADDRESS_PREFERRED,ADDRESS_CITY,ADDRESS_STATE,ADDRESS_ZIP5,ADDRESS_ZIP9,"
              "ADDRESS_PERIOD_START,ADDRESS_PERIOD_END\n")
    (src / "lds_address_history.csv").write_text(header + "".join(r.format(pat=pat) for r in address_rows), encoding="utf-8")
    return src


def test_address_zip_keeps_leading_zero_and_reads_zip9(tmp_path):
    from omop_etl import build_schema, load_location, load_person
    from omop_etl.build_omop_cdm import load_macros

    src = _sample(tmp_path, ["1,{pat},HO,PO,Y,Camden,NJ,8101,,01JAN2010,31DEC2012\n",
                             "2,{pat},HO,PO,N,Boston,MA,,021081234,01JAN2013,31DEC2014\n",
                             "3,{pat},HO,PO,N,Trenton,NJ,,08608-1234,01JAN2015,\n"])
    con = duckdb.connect()
    build_schema(con)
    load_macros(con)
    load_person(con, str(src))
    load_location(con, str(src))
    got = [r[0] for r in con.execute("SELECT zip5 FROM person_address_history ORDER BY address_start").fetchall()]
    assert got == ["08101", "02108", "08608"]


def test_maps_to_skips_deprecated_rows():
    from omop_etl import build_omop_cdm

    src = Path(build_omop_cdm.__file__).read_text()
    assert src.count("WHERE relationship_id = 'Maps to'\n") == 0
    assert src.count("WHERE relationship_id = 'Maps to' AND invalid_reason IS NULL") == 8
