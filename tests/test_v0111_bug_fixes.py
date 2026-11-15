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
    assert out.iloc[-1].n_site_1 == 150


def test_o1c_pooled_n_withheld_so_sum_of_sizes_cannot_recover_suppressed_pair():
    # three sites of 1,000 sharing 7 patients: sizes 3,000 minus pooled 2,993 would reveal the 7
    ids = {"A": list(range(1000)), "B": list(range(993, 1993)), "C": list(range(5000, 6000))}
    out = consortium_cross_site_duplicates(ids, key="k", min_cell_count=11)
    total = out[out.site_1 == "ALL"].iloc[0]
    assert total.is_suppressed and total.shared_patients in ("withheld", "<11")
    assert total.n_site_1 != total.n_site_1 and total.n_site_2 != total.n_site_2
    assert not out.astype(str).apply(lambda col: col.str.contains("2993")).any().any()


def test_o1c_pooled_n_withheld_when_total_overlap_is_small():
    out = consortium_cross_site_duplicates({"A": list(range(100)), "B": list(range(95, 195))}, key="k")
    total = out.iloc[-1]
    assert total.is_suppressed and total.n_site_1 != total.n_site_1


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


def test_o7b_features_do_not_multiply_across_tables():
    con = duckdb.connect()
    con.execute("CREATE TABLE cohort AS SELECT * FROM (VALUES (1, 1, DATE '2020-03-01', DATE '2020-03-05')) "
                "t(cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)")
    con.execute("CREATE TABLE condition_occurrence AS SELECT i AS condition_occurrence_id, 1 AS person_id, DATE '2020-02-01' AS condition_start_date FROM range(1, 21) t(i)")
    con.execute("CREATE TABLE drug_exposure AS SELECT i AS drug_exposure_id, 1 AS person_id, DATE '2020-02-10' AS drug_exposure_start_date FROM range(1, 8) t(i)")
    con.execute("CREATE TABLE measurement AS SELECT * FROM (VALUES (1, 1, 3001, DATE '2020-02-15', 10.0), (2, 1, 3001, DATE '2020-02-16', 20.0))"
                " t(measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number)")
    df = extract_patient_features(con, cohort_id=1, lookback_days=[30, 90], include_demographics=False,
                                  measurement_concept_ids=[3001])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["condition_count_30d"] == 20 and row["drug_count_30d"] == 7
    assert row["mean_measurement_3001"] == 15.0      # not weighted by the 20 x 7 other-table rows


def _multi_table_fixture(n_persons, events_per_person):
    con = duckdb.connect()
    con.execute(f"""
        CREATE TABLE person AS SELECT i person_id, 1950 + (i % 50) year_of_birth, 1 month_of_birth, 1 day_of_birth,
            8507 gender_concept_id, 8527 race_concept_id, 0 ethnicity_concept_id FROM range({n_persons}) t(i);
        CREATE TABLE cohort AS SELECT 1 cohort_definition_id, p.i subject_id, p.i person_id,
            DATE '2020-01-01' + CAST(s * 200 + p.i % 30 AS INT) cohort_start_date,
            DATE '2020-01-06' + CAST(s * 200 + p.i % 30 AS INT) cohort_end_date,
            p.i * 10 + s visit_occurrence_id FROM range({n_persons}) p(i), range(3) q(s);
    """)
    for tbl, idc, dc in [("condition_occurrence", "condition_occurrence_id", "condition_start_date"),
                         ("drug_exposure", "drug_exposure_id", "drug_exposure_start_date"),
                         ("procedure_occurrence", "procedure_occurrence_id", "procedure_date"),
                         ("measurement", "measurement_id", "measurement_date")]:
        extra = ", 3 + (i % 7) measurement_concept_id, (i % 100) * 1.5 value_as_number" if tbl == "measurement" else ""
        con.execute(f"CREATE TABLE {tbl} AS SELECT i {idc}, i % {n_persons} person_id, "
                    f"DATE '2020-01-01' + CAST(hash(i) % 700 AS INT) {dc}{extra} FROM range({n_persons * events_per_person}) t(i)")
    return con


def test_o7b_features_equal_independent_per_table_reference():
    # every table has many events per stay; the reference counts each table on its own with a correlated subquery,
    # so it cannot share the row multiplication that the per-table aggregates avoid
    con = _multi_table_fixture(60, 30)
    df = extract_patient_features(con, cohort_id=1, index_key="visit_occurrence_id", lookback_days=(30, 365),
                                  measurement_concept_ids=[3, 4]).sort_values("visit_occurrence_id").reset_index(drop=True)
    ref = con.execute("""
        SELECT c.visit_occurrence_id,
            (SELECT COUNT(*) FROM condition_occurrence t WHERE t.person_id = c.person_id AND t.condition_start_date BETWEEN c.cohort_start_date - 30 AND c.cohort_start_date - 1) AS condition_count_30d,
            (SELECT COUNT(*) FROM drug_exposure t WHERE t.person_id = c.person_id AND t.drug_exposure_start_date BETWEEN c.cohort_start_date - 365 AND c.cohort_start_date - 1) AS drug_count_365d,
            (SELECT COUNT(*) FROM procedure_occurrence t WHERE t.person_id = c.person_id AND t.procedure_date BETWEEN c.cohort_start_date - 365 AND c.cohort_start_date - 1) AS procedure_count_365d,
            (SELECT AVG(value_as_number) FROM measurement t WHERE t.person_id = c.person_id AND t.measurement_concept_id = 3 AND t.measurement_date BETWEEN c.cohort_start_date - 30 AND c.cohort_start_date) AS mean_measurement_3
        FROM cohort c ORDER BY c.visit_occurrence_id""").df()
    assert len(df) == len(ref) == 180
    for col in ("condition_count_30d", "drug_count_365d", "procedure_count_365d"):
        assert (df[col].to_numpy() == ref[col].to_numpy()).all(), col
    assert ref.drug_count_365d.sum() > 0
    # with several windows the output columns repeat the name; the first (pandas keeps it as is) is the smallest window
    both_nan = df.mean_measurement_3.isna() & ref.mean_measurement_3.isna()
    assert (both_nan | ((df.mean_measurement_3 - ref.mean_measurement_3).abs() < 1e-9)).all()


def test_r1_features_timing_multi_table_stays():
    # 900 stays, 12,000 events in each of four tables: about 0.1 s now; PR #40 code needed about 40 s here
    import time
    con = _multi_table_fixture(300, 40)
    start = time.time()
    df = extract_patient_features(con, cohort_id=1, index_key="visit_occurrence_id")
    assert len(df) == 900
    assert time.time() - start < 20
