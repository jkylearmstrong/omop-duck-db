"""Regression tests for the v0.11.0 field-test findings (O1, O3, O6, O8)."""
import duckdb
import numpy as np
import pandas as pd

from omop_etl import consortium_cross_site_duplicates, consortium_patient_overlap, remap_discharge_status
from omop_etl.federated_learning import federated_logistic_regression
from omop_etl.features import extract_patient_features


def test_suppressed_overlap_hides_percentage():
    tokens = {"A": [f"t{i}" for i in range(1000)], "B": [f"t{i}" for i in range(993, 1993)]}
    df = consortium_patient_overlap(tokens)
    row = df[df.site_1 != df.site_2].iloc[0]
    assert row.shared_patients == "<11" and np.isnan(row.overlap_pct)
    dup = consortium_cross_site_duplicates({"A": list(range(1000)), "B": list(range(993, 1993))}, key="k")
    assert dup["overlap_pct"].isna().all()


def test_remap_discharge_status_is_case_insensitive_and_idempotent():
    con = duckdb.connect()
    con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 0, 'ip'), (2, 0, ' IP '), (3, 5, 'IP'), (4, 0, 'HOME'))"
                " t(visit_occurrence_id, discharged_to_concept_id, discharged_to_source_value)")
    assert remap_discharge_status(con) == 2
    assert remap_discharge_status(con) == 0
    assert con.execute("SELECT discharged_to_concept_id FROM visit_occurrence ORDER BY 1").fetchall() == [(0,), (5,), (8717,), (8717,)]


def test_separated_logistic_returns_nan_and_flag():
    rng = np.random.default_rng(0)
    x = rng.normal(size=60)
    df = pd.DataFrame({"x": x, "y": (x > 0).astype(int)})
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = federated_logistic_regression({"A": df.iloc[:30], "B": df.iloc[30:]}, ["x"], "y")
    assert res.separated and res.coefficients["estimate"].isna().all()


def test_features_accept_timestamp_cohort_dates():
    con = duckdb.connect()
    con.execute("CREATE TABLE person AS SELECT 1 AS person_id, 1970 AS year_of_birth, 1 AS month_of_birth, 1 AS day_of_birth,"
                " 0 AS gender_concept_id, 0 AS race_concept_id, 0 AS ethnicity_concept_id")
    con.execute("CREATE TABLE condition_occurrence AS SELECT 1 AS condition_occurrence_id, 1 AS person_id, DATE '2020-01-05' AS condition_start_date")
    con.execute("CREATE TABLE cohort AS SELECT 1 AS subject_id, TIMESTAMP '2020-01-10 08:00:00' AS cohort_start_date,"
                " TIMESTAMP '2020-01-12 08:00:00' AS cohort_end_date")
    df = extract_patient_features(con, cohort_table="cohort", lookback_days=[30])
    assert int(df["condition_count_30d"].iloc[0]) == 1


def test_remap_discharge_status_with_schema():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA cdm")
    con.execute("CREATE TABLE cdm.visit_occurrence AS SELECT 1 AS visit_occurrence_id, 0 AS discharged_to_concept_id, 'IP' AS discharged_to_source_value")
    assert remap_discharge_status(con, schema="cdm", concept_id=1) == 1
    assert con.execute("SELECT discharged_to_concept_id FROM cdm.visit_occurrence").fetchone() == (1,)


def test_o1b_pooled_row_withheld_when_a_pair_is_suppressed():
    ids = {"A": list(range(0, 507)), "B": list(range(500, 1000)) + list(range(2000, 2007)),
           "C": list(range(2000, 2493)) + list(range(5000, 5100))}
    out = consortium_cross_site_duplicates(ids, key="k", min_cell_count=11)
    pairs = out[out.site_1 != "ALL"]
    assert pairs.is_suppressed.any()
    total = out[out.site_1 == "ALL"].iloc[0]
    assert total.is_suppressed and total.shared_patients == "withheld" and total.overlap_pct != total.overlap_pct


def test_o1b_pooled_row_shown_when_no_pair_suppressed():
    out = consortium_cross_site_duplicates({"A": list(range(100)), "B": list(range(50, 150))}, key="k")
    assert out.iloc[-1].shared_patients == "50"


def test_remap_discharge_status_dry_run_changes_nothing():
    con = duckdb.connect()
    con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 0, 'IP'), (2, 0, 'HOME'))"
                " t(visit_occurrence_id, discharged_to_concept_id, discharged_to_source_value)")
    assert remap_discharge_status(con, dry_run=True) == 1
    assert con.execute("SELECT SUM(discharged_to_concept_id) FROM visit_occurrence").fetchone()[0] == 0


def test_cap_observation_period():
    from omop_etl import cap_observation_period
    con = duckdb.connect()
    con.execute("CREATE TABLE observation_period AS SELECT * FROM (VALUES (1, DATE '2020-01-01', DATE '2026-10-23'),"
                " (2, DATE '2026-10-23', DATE '2026-10-23'), (3, DATE '2020-01-01', DATE '2020-06-01'))"
                " t(observation_period_id, observation_period_start_date, observation_period_end_date)")
    assert cap_observation_period(con, "2026-09-30", dry_run=True) == {"shortened": 1, "deleted": 1}
    assert con.execute("SELECT COUNT(*) FROM observation_period").fetchone()[0] == 3
    assert cap_observation_period(con, "2026-09-30") == {"shortened": 1, "deleted": 1}
    assert con.execute("SELECT MAX(observation_period_end_date)::VARCHAR, COUNT(*) FROM observation_period").fetchone() == ("2026-09-30", 2)
