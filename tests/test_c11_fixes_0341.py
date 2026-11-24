"""0.34.1 follow-ups to run C11: BMI computed from height and weight and taken from the year before the admission, and duplicate
predictors kept out of the models. Synthetic data only."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from omop_etl.benchmark_features import extract_benchmark_features  # noqa: E402
from omop_etl.study_run import DUPLICATE_PREDICTORS, drop_duplicate_predictors  # noqa: E402

BMI, HT, WT = 1, 3036277, 3025315
OFF = dict(include_demographics=False, include_elixhauser=False, include_medications=False, include_labs=False, include_utilization=False,
           include_disposition=False, vital_concept_ids={"bmi": [BMI]}, vocabulary_db=None)


def _db():
    con = duckdb.connect()
    con.execute("CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE,"
                " measurement_datetime TIMESTAMP, value_as_number DOUBLE, unit_concept_id INT)")
    rows = [
        (1, 1, HT, "2020-01-11", None, 70.0, None), (2, 1, WT, "2020-01-11", None, 200.0, None),     # in stay, inches / pounds, no unit
        (3, 2, BMI, "2019-06-01", None, 31.0, None), (4, 2, BMI, "2019-03-01", None, 25.0, None),   # prior year: the latest wins
        (5, 3, HT, "2019-09-01", None, 170.0, 8582), (6, 3, WT, "2019-12-01", None, 70.0, 9529),    # prior year, metric
        (7, 4, BMI, "2018-11-01", None, 29.0, None),                                                # more than 365 days before
        (8, 5, BMI, "2020-01-12", None, 22.0, None), (9, 5, HT, "2020-01-12", None, 70.0, None),    # recorded BMI wins over
        (10, 5, WT, "2020-01-12", None, 300.0, None),                                               # a computed one
        (11, 6, HT, "2020-01-12", None, 7.0, None), (12, 6, WT, "2020-01-12", None, 200.0, None),   # implausible height: no BMI
    ]
    con.executemany("INSERT INTO measurement VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE cohort_t AS SELECT subject_id, DATE '2020-01-10' AS cohort_start_date, DATE '2020-01-15' AS cohort_end_date, "
                "100 + subject_id AS visit_occurrence_id, 0 AS outcome_flag FROM range(1, 7) t(subject_id)")
    return con


def test_bmi_is_computed_then_taken_from_the_year_before_admission():
    con = _db()
    f = extract_benchmark_features(con, "cohort_t", profile="ederri", include_protocol_history=False, **OFF)
    bmi = dict(zip(f["person_id"], f["vital_bmi"]))
    assert bmi[1] == pytest.approx(28.70, abs=0.01)        # 90.72 kg / 1.778 m^2
    assert bmi[2] == 31.0
    assert bmi[3] == pytest.approx(24.22, abs=0.01)
    assert np.isnan(bmi[4]) and np.isnan(bmi[6])
    assert bmi[5] == 22.0
    assert f.attrs["bmi_filled"] == {"derived_in_stay": 1, "recorded_prior": 1, "derived_prior": 1}


def test_bmi_fill_options():
    con = _db()
    f = extract_benchmark_features(con, "cohort_t", **OFF)  # generic default: computed from the stay only
    bmi = dict(zip(f["person_id"], f["vital_bmi"]))
    assert bmi[1] == pytest.approx(28.70, abs=0.01) and np.isnan(bmi[2]) and np.isnan(bmi[3])
    f = extract_benchmark_features(con, "cohort_t", bmi_fill="none", **OFF)
    assert f["vital_bmi"].notna().sum() == 1  # the recorded in-stay BMI only
    f = extract_benchmark_features(con, "cohort_t", bmi_fill="derive_lookback", bmi_lookback_days=800, **OFF)
    assert dict(zip(f["person_id"], f["vital_bmi"]))[4] == 29.0
    with pytest.raises(ValueError, match="bmi_fill"):
        extract_benchmark_features(con, "cohort_t", bmi_fill="impute", **OFF)


def test_duplicate_predictors_leave_the_model_matrix():
    df = pd.DataFrame({"los_days": [1, 2], "index_los_days": [1, 2], "days_since_last_encounter": [3, 365],
                       "days_since_prior_encounter": [3, np.nan], "age": [50, 60]})
    assert list(drop_duplicate_predictors(df).columns) == ["index_los_days", "days_since_prior_encounter", "age"]
    only = df.drop(columns=["index_los_days"])  # without its counterpart a column is kept
    assert "los_days" in drop_duplicate_predictors(only)
    assert set(DUPLICATE_PREDICTORS) == {"los_days", "days_since_last_encounter"}


# ------------------------------------------------------------------ the draw samples eligible stays only
def _cohort():
    return pd.DataFrame({"subject_id": [1, 1, 2, 2, 2, 3, 4], "visit_occurrence_id": [11, 12, 21, 22, 23, 31, 41]})


def test_pin_stays_redraws_patients_whose_drawn_stay_is_not_eligible():
    from omop_etl.study_run import pin_stays

    sel = pd.DataFrame({"visit_occurrence_id": [12, 29, 31]})  # patient 2's drawn stay (29) is no longer eligible; 4 is not listed
    kept = pin_stays(_cohort(), sel, strict=False)
    assert sorted(kept["visit_occurrence_id"]) == [12, 31] and kept.attrs.get("redrawn", 0) == 0
    with pytest.raises(ValueError, match="patients"):
        pin_stays(_cohort(), sel, strict=False, redraw=True)
    sel = sel.assign(subject_id=[1, 2, 3])
    out = pin_stays(_cohort(), sel, strict=False, redraw=True, random_state=7)
    assert out.attrs["redrawn"] == 1 and not out["subject_id"].duplicated().any()
    assert sorted(out["subject_id"]) == [1, 2, 3]  # patient 4 is not in the draw and is not added
    assert pin_stays(_cohort(), sel.drop(columns="subject_id"), strict=False, redraw=True, patients=[1, 2, 3]).attrs["redrawn"] == 1
    assert set(out.loc[out["subject_id"] == 1, "visit_occurrence_id"]) == {12}  # an eligible drawn stay is kept
    assert out.loc[out["subject_id"] == 2, "visit_occurrence_id"].iloc[0] in (21, 22, 23)
    again = pin_stays(_cohort().sample(frac=1, random_state=1), sel, strict=False, redraw=True, random_state=7)  # row order does not matter
    assert dict(zip(out["subject_id"], out["visit_occurrence_id"])) == dict(zip(again["subject_id"], again["visit_occurrence_id"]))
    draws = {pin_stays(_cohort(), sel, strict=False, redraw=True, random_state=s).set_index("subject_id").loc[2, "visit_occurrence_id"]
             for s in range(30)}
    assert draws == {21, 22, 23}  # every eligible stay can be drawn


def test_ineligible_draw_decision():
    from omop_etl.study_run import STUDY_PRESETS, resolve_decisions

    assert STUDY_PRESETS["package"]["ineligible_draw"] == STUDY_PRESETS["delivered_2026_11_11"]["ineligible_draw"] == "redraw"
    assert resolve_decisions("package", ineligible_draw="exclude")["ineligible_draw"] == "exclude"
    with pytest.raises(ValueError):
        resolve_decisions("package", ineligible_draw="drop")


def test_methods_say_how_many_patients_were_redrawn(tmp_path):
    pytest.importorskip("jinja2")
    import re

    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_rubin_fixes_0340 import _workbook

    wb = _workbook(tmp_path)
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"]}
    out = mtx.full_manuscript_markdown(tabs, [], workbook=wb, run={**run, "random_draw_redrawn": 923}, summary={"n_features": 95}, version="0.34.1")
    assert out["template"] == "0.34.1"
    text = re.sub(r"\s+", " ", out["markdown"])
    assert "for 923 patients whose stay in that draw no longer met the criteria" in text
    assert "pinned" not in text.lower()
    none = mtx.full_manuscript_markdown(tabs, [], workbook=wb, run=run, summary={"n_features": 95}, version="0.34.1")
    assert "no longer met the criteria" not in none["markdown"]
