"""extract_benchmark_features: physiologic bounds, WBC rescaling, alternative LOINC codes, measured-first anion gap,
hemoglobin from hematocrit and the ``ederri`` profile. Synthetic data only."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from omop_etl import benchmark_features as bf  # noqa: E402
from omop_etl.benchmark_features import EDERRI_MEDICATION_SPECS, extract_benchmark_features  # noqa: E402

CODES = {"bmi": ("39156-5", 1), "sbp": ("8480-6", 2), "wbc": ("6690-2", 3), "alt": ("1743-4", 4), "hct": ("4544-3", 5),
         "na": ("2951-2", 6), "cl": ("2075-0", 7), "hco3": ("1963-8", 8), "ag": ("33037-3", 9), "hgb": ("718-7", 10)}


def _db():
    con = duckdb.connect()
    con.execute("CREATE TABLE person (person_id BIGINT, gender_concept_id INT, year_of_birth INT, month_of_birth INT, day_of_birth INT,"
                " race_concept_id INT, ethnicity_concept_id INT)")
    con.execute("INSERT INTO person VALUES (1, 8532, 1960, 1, 1, 8527, 38003564), (2, 8507, 1950, 1, 1, 8527, 38003564), "
                "(3, 8532, 1970, 1, 1, 8527, 38003564)")
    con.execute("CREATE TABLE concept (concept_id INT, concept_code VARCHAR, vocabulary_id VARCHAR)")
    con.execute("INSERT INTO concept VALUES " + ", ".join(f"({cid}, '{code}', 'LOINC')" for code, cid in CODES.values()))
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE,"
                " measurement_datetime TIMESTAMP, value_as_number DOUBLE)")
    rows, mid = [], 0

    def add(person, key, value, day="2020-01-12", hour=8):
        nonlocal mid
        mid += 1
        rows.append((mid, person, CODES[key][1], day, f"{day} {hour:02d}:00:00", value))

    add(1, "bmi", 30.0); add(1, "bmi", 562000.0, hour=9); add(1, "sbp", 120.0); add(1, "sbp", 999.0, hour=9)   # artifacts must be dropped
    add(1, "wbc", 7500.0); add(1, "wbc", 9.0, hour=9)                                                          # per-uL value is rescaled
    add(1, "alt", 40.0)                                                                                        # only coded 1743-4
    add(1, "na", 140.0); add(1, "cl", 100.0); add(1, "hco3", 24.0)                                             # calculated gap 16
    add(1, "hct", 36.0)                                                                                        # no hemoglobin measured
    add(2, "na", 140.0); add(2, "cl", 100.0); add(2, "hco3", 24.0); add(2, "ag", 12.0)                         # measured gap 12 wins over 16
    add(3, "hct", 30.0); add(3, "hgb", 13.0)                                                                   # measured hemoglobin kept
    add(3, "bmi", 5.0)                                                                                         # below the lower bound
    con.executemany("INSERT INTO measurement VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE cohort_t AS SELECT * FROM (VALUES (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0), "
                "(2, DATE '2020-01-10', DATE '2020-01-15', 102, 1), (3, DATE '2020-01-10', DATE '2020-01-15', 103, 0)) "
                "t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)")
    return con


@pytest.fixture()
def con():
    c = _db()
    yield c
    c.close()


KW = dict(include_elixhauser=False, include_medications=False, include_utilization=False, include_disposition=False, vocabulary_db=None)


def _row(df, person):
    return df.loc[df["person_id"] == person].iloc[0]


def test_bounds_drop_artifacts_not_clip(con):
    df = extract_benchmark_features(con, "cohort_t", **KW)
    assert _row(df, 1)["vital_bmi"] == 30.0                     # 562000 dropped, mean of the rest
    assert _row(df, 1)["vital_systolic_bp"] == 120.0            # 999 dropped
    assert np.isnan(_row(df, 3)["vital_bmi"])                   # BMI 5 is below 10: dropped, not clipped to 10


def test_bounds_can_be_disabled_or_overridden(con):
    raw = extract_benchmark_features(con, "cohort_t", bounds=None, **KW)
    assert _row(raw, 1)["vital_bmi"] == pytest.approx((30.0 + 562000.0) / 2)
    assert _row(raw, 3)["vital_bmi"] == 5.0
    loose = extract_benchmark_features(con, "cohort_t", bounds={"bmi": (1, None)}, **KW)
    assert _row(loose, 3)["vital_bmi"] == 5.0                    # overridden lower bound, open upper bound
    assert _row(loose, 1)["vital_bmi"] == pytest.approx((30.0 + 562000.0) / 2)
    assert _row(loose, 1)["vital_systolic_bp"] == 120.0          # other names keep their default bounds
    with pytest.raises(ValueError, match="bounds must be"):
        extract_benchmark_features(con, "cohort_t", bounds="strict", **KW)


def test_wbc_per_ul_is_rescaled_then_bounded(con):
    df = extract_benchmark_features(con, "cohort_t", **KW)
    assert _row(df, 1)["lab_wbc"] == pytest.approx((7.5 + 9.0) / 2)
    assert _row(df, 1)["lab_wbc_last"] == 9.0                    # latest by time of day
    raw = extract_benchmark_features(con, "cohort_t", bounds=None, **KW)
    assert _row(raw, 1)["lab_wbc"] == pytest.approx((7500.0 + 9.0) / 2)


def test_alt_found_under_alternative_loinc_and_alt_default_list_is_a_superset(con):
    df = extract_benchmark_features(con, "cohort_t", **KW)
    assert _row(df, 1)["lab_alt"] == 40.0                        # coded 1743-4, not 1742-6
    from omop_etl.features import CORE_14_LAB_PANEL
    assert all(set(codes) <= set(bf.BENCHMARK_LAB_LOINCS[name]) for name, codes in CORE_14_LAB_PANEL.items())


def test_measured_anion_gap_wins_and_calculated_fills_the_rest(con):
    df = extract_benchmark_features(con, "cohort_t", **KW)
    assert _row(df, 2)["lab_anion_gap"] == 12.0                  # measured, although Na - Cl - HCO3 = 16
    assert _row(df, 1)["lab_anion_gap"] == 16.0                  # nothing measured: calculated
    assert df.columns.tolist().count("lab_anion_gap") == 1


def test_hemoglobin_from_hematocrit_only_when_asked(con):
    off = extract_benchmark_features(con, "cohort_t", **KW)
    assert np.isnan(_row(off, 1)["lab_hemoglobin"])
    on = extract_benchmark_features(con, "cohort_t", hematocrit_fallback=True, **KW)
    assert _row(on, 1)["lab_hemoglobin"] == pytest.approx(12.0)  # 36 / 3
    assert _row(on, 3)["lab_hemoglobin"] == 13.0                 # a measured hemoglobin is never replaced
    via_profile = extract_benchmark_features(con, "cohort_t", profile="ederri", **KW)
    assert _row(via_profile, 1)["lab_hemoglobin"] == pytest.approx(12.0)
    explicit_off = extract_benchmark_features(con, "cohort_t", profile="ederri", hematocrit_fallback=False, **KW)
    assert np.isnan(_row(explicit_off, 1)["lab_hemoglobin"])


def test_profile_ederri_selects_protocol_medication_classes_and_discharge_window(con, monkeypatch):
    seen = {}

    def fake(db_con, cohort_table, specs, **kw):
        seen["specs"], seen["kw"] = dict(specs), kw
        return pd.DataFrame({"person_id": [1, 2, 3], "index_date": pd.to_datetime(["2020-01-10"] * 3),
                             **{k: [0, 0, 0] for k in specs}})

    import omop_etl.medication_classes as mc
    monkeypatch.setattr(mc, "extract_medication_classes", fake)
    kw = {**KW, "include_medications": True}
    extract_benchmark_features(con, "cohort_t", profile="ederri", **kw)
    assert set(seen["specs"]) == set(EDERRI_MEDICATION_SPECS)
    assert seen["specs"]["antihypertensives"]["ancestor_concept_ids"] == [21600381]            # ATC C02 only, as in the protocol
    assert seen["kw"]["window"] is None and seen["kw"]["offset_anchor"] == "visit_end_date" and seen["kw"]["lookback_days"] == 365
    extract_benchmark_features(con, "cohort_t", **kw)
    assert "metformin" in seen["specs"] and seen["kw"]["window"] == "pre_admission"
    extract_benchmark_features(con, "cohort_t", profile="ederri", medication_window="pre_admission", **kw)
    assert seen["kw"]["window"] == "pre_admission"                                              # explicit arguments win over the profile
    with pytest.raises(ValueError, match="profile must be"):
        extract_benchmark_features(con, "cohort_t", profile="other", **KW)


def test_default_bounds_cover_every_default_lab_and_vital():
    names = set(bf.BENCHMARK_LAB_LOINCS) | set(bf.BENCHMARK_VITAL_LOINCS)
    assert names <= set(bf.DEFAULT_BOUNDS)
    assert all(lo < hi for lo, hi in bf.DEFAULT_BOUNDS.values())


def test_ederri_disposition_rule_uses_source_value_and_concept_sets(con):
    con.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, discharged_to_concept_id INT, discharged_to_source_value VARCHAR)")
    con.execute("INSERT INTO visit_occurrence VALUES (101, 0, 'ho'), (102, 8844, NULL), (103, 0, 'AM')")
    kw = dict(include_labs=False, include_vitals=False, include_medications=False, include_elixhauser=False, include_utilization=False,
              include_protocol_history=False, vocabulary_db=None)
    generic = extract_benchmark_features(con, "cohort_t", **kw)
    assert generic["disp_home"].sum() == 0 and generic["disp_other"].sum() == 3    # the default concept map matches none of them
    f = extract_benchmark_features(con, "cohort_t", profile="ederri", **kw).sort_values("visit_occurrence_id")
    assert f["disp_home"].tolist() == [1, 0, 0] and f["disp_home_health"].tolist() == [0, 1, 0] and f["disp_ama"].tolist() == [0, 0, 1]
    explicit = extract_benchmark_features(con, "cohort_t", disposition_map="ederri", **kw)
    assert explicit["disp_home"].sum() == 1
    with pytest.raises(ValueError, match="disposition_map"):
        extract_benchmark_features(con, "cohort_t", disposition_map="nope", **kw)
