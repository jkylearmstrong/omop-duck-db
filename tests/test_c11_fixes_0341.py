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
