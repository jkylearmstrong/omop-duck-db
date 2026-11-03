"""Field-report items O2, O5, O9, O12, O14, F1 and F3 (synthetic data)."""
import datetime
import warnings

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (build_observation_period, check_observation_period_ends, consortium_duplicate_report,
                      remap_discharge_status)
from omop_etl.cohort import AttritionRecords, cohort_attrition
from omop_etl.federated_learning import federated_logistic_regression
from omop_etl.ml import consortium_multivariable_risk_factors


# -- O12 ---------------------------------------------------------------------------------------------------------
def _site(rng, n, separate):
    x = rng.integers(0, 2, n)
    z = rng.normal(size=n)
    y = (rng.random(n) < 1 / (1 + np.exp(-0.5 * z))).astype(int)
    if separate:
        y[x == 1] = 0
    return pd.DataFrame({"x": x, "z": z, "y": y})


def test_o12_separation_detected_before_fitting():
    rng = np.random.default_rng(0)
    with pytest.warns(UserWarning, match="separation"):
        res = federated_logistic_regression({"A": _site(rng, 300, True), "B": _site(rng, 300, True)}, ["x", "z"], "y")
    assert res.separated and res.n_iter == 0 and res.coefficients.estimate.isna().all()


def test_o12_no_false_alarm_and_l2_bypasses_screen():
    rng = np.random.default_rng(1)
    ok = federated_logistic_regression({"A": _site(rng, 300, False), "B": _site(rng, 300, False)}, ["x", "z"], "y")
    assert ok.converged and not ok.separated
    sep = {"A": _site(rng, 300, True), "B": _site(rng, 300, True)}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pen = federated_logistic_regression(sep, ["x", "z"], "y", l2=1.0)
    assert not pen.separated and np.isfinite(pen.coefficients.estimate).all()


# -- O9 ----------------------------------------------------------------------------------------------------------
def test_o9_cluster_col_defaults_to_unpenalised():
    rng = np.random.default_rng(2)
    X = pd.DataFrame({"a": rng.normal(size=400), "b": rng.normal(size=400), "pid": np.repeat(np.arange(200), 2)})
    y = (rng.random(400) < 0.4).astype(int)
    with warnings.catch_warnings():
        warnings.simplefilter("error")      # no ridge-with-clusters warning any more
        res = consortium_multivariable_risk_factors(X, y, feature_cols=["a", "b"], cluster_col="pid")
    assert res.metadata["regularize"] is False
    plain = consortium_multivariable_risk_factors(X, y, feature_cols=["a", "b"])
    assert plain.metadata["regularize"] is True


# -- O5 ----------------------------------------------------------------------------------------------------------
def test_o5_attrition_list_has_deprecated_dataframe_access():
    att = AttritionRecords([{"step": "a", "n": 5}, {"step": "b", "n": 3}])
    a, b = pd.DataFrame({"x": [1]}), pd.DataFrame({"x": [2]})
    a.attrs["attrition"], b.attrs["attrition"] = att, AttritionRecords(list(att))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert len(pd.concat([a, b])) == 2
        assert cohort_attrition(a).shape == (2, 2)
        assert att[0] == {"step": "a", "n": 5}
    with pytest.warns(FutureWarning, match="cohort_attrition"):
        assert att["n"].tolist() == [5, 3]
    with pytest.warns(FutureWarning):
        assert list(att.columns) == ["step", "n"]


# -- O14 ---------------------------------------------------------------------------------------------------------
def test_o14_remap_as_view_on_read_only_database(tmp_path):
    path = str(tmp_path / "prod.duckdb")
    w = duckdb.connect(path)
    w.execute("CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 0, 'IP'), (2, 0, 'HOME'), (3, 8536, 'ip')) "
              "t(visit_occurrence_id, discharged_to_concept_id, discharged_to_source_value)")
    w.close()
    con = duckdb.connect()
    con.execute(f"ATTACH '{path}' AS prod (READ_ONLY)")
    con.execute("USE prod")
    assert remap_discharge_status(con, as_view=True) == 1
    assert con.execute("SELECT discharged_to_concept_id FROM visit_occurrence_recoded ORDER BY visit_occurrence_id"
                       ).fetchall() == [(8717,), (0,), (8536,)]
    assert remap_discharge_status(con, as_view=True, view_name="visit_occurrence") == 1
    assert con.execute("SELECT discharged_to_concept_id FROM visit_occurrence WHERE visit_occurrence_id = 1").fetchone() == (8717,)
    assert con.execute("SELECT discharged_to_concept_id FROM prod.main.visit_occurrence WHERE visit_occurrence_id = 1"
                       ).fetchone() == (0,)          # the file is untouched
    with pytest.raises(ValueError):
        remap_discharge_status(con, as_view=True, view_name="x; DROP TABLE y")


# -- O2 / F1 -----------------------------------------------------------------------------------------------------
def _obs_con(build_date_pileup):
    con = duckdb.connect()
    con.execute("CREATE TABLE person (person_id INT)")
    con.execute("CREATE TABLE visit_occurrence (visit_occurrence_id INT, person_id INT, visit_concept_id INT, "
                "visit_start_date DATE, visit_end_date DATE, visit_type_concept_id INT)")
    con.execute("CREATE TABLE condition_occurrence (person_id INT, condition_start_date DATE, condition_end_date DATE)")
    con.execute("CREATE TABLE procedure_occurrence (person_id INT, procedure_date DATE)")
    con.execute("CREATE TABLE drug_exposure (person_id INT, drug_exposure_start_date DATE, drug_exposure_end_date DATE)")
    con.execute("CREATE TABLE measurement (person_id INT, measurement_date DATE)")
    con.execute("CREATE TABLE observation_period (observation_period_id BIGINT, person_id INT, "
                "observation_period_start_date DATE, observation_period_end_date DATE, period_type_concept_id INT)")
    today = datetime.date.today()
    for i in range(300):
        end = today - datetime.timedelta(days=10 + (i % 60))
        if build_date_pileup and i % 2 == 0:
            end = today - datetime.timedelta(days=5)          # a past build date shared by half the patients
        con.execute(f"INSERT INTO visit_occurrence VALUES ({i}, {i}, 9201, DATE '2020-01-01', DATE '{end}', 0)")
        con.execute(f"INSERT INTO observation_period VALUES ({i}, {i}, DATE '2020-01-01', DATE '{end}', 0)")
    return con


def test_o2_default_cutoff_is_auto_and_none_keeps_today():
    con = _obs_con(False)
    today = datetime.date.today().isoformat()
    con.execute(f"INSERT INTO visit_occurrence VALUES (999, 0, 9201, DATE '{today}', DATE '{today}', 0)")
    build_observation_period(con)
    assert con.execute("SELECT max(observation_period_end_date) FROM observation_period").fetchone()[0].isoformat() != today
    build_observation_period(con, max_date=None)
    assert con.execute("SELECT max(observation_period_end_date) FROM observation_period").fetchone()[0].isoformat() == today


def test_f1_build_date_pileup_is_flagged_and_normal_data_is_not():
    with pytest.warns(UserWarning, match="cap_observation_period"):
        res = check_observation_period_ends(_obs_con(True))
    assert res["flagged"] and res["periods"] >= 150
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert not check_observation_period_ends(_obs_con(False))["flagged"]
    assert check_observation_period_ends(duckdb.connect()) == {"flagged": False}


def test_f1b_small_share_pileup_is_flagged():
    con = duckdb.connect()
    con.execute("CREATE TABLE observation_period (person_id INT, observation_period_end_date DATE)")
    con.execute("INSERT INTO observation_period SELECT i, DATE '2026-09-01' + (i % 30)::INT FROM range(30) t(i)")
    con.execute("INSERT INTO observation_period SELECT 1000 + i, DATE '2020-01-01' FROM range(30000) t(i)")
    con.execute("INSERT INTO observation_period SELECT 100000 + i, DATE '2026-10-23' FROM range(33) t(i)")
    res = check_observation_period_ends(con, warn=False)
    assert res["flagged"] and res["periods"] == 33 and res["periods"] < 0.01 * res["total"]


# -- F3 ----------------------------------------------------------------------------------------------------------
def test_f3_duplicate_report_is_one_safe_object():
    ids = {"A": list(range(1000)), "B": list(range(993, 1993)), "C": list(range(5000, 6000))}
    rep = consortium_duplicate_report(ids, key="k")
    assert set(rep) == {"pairs", "pooled", "withheld", "policy"}
    assert rep["withheld"] and rep["pooled"]["n_site_1"].isna().all() and (rep["pairs"].site_1 != "ALL").all()
    assert "n_site_1" in rep["policy"]
    ok = consortium_duplicate_report({"A": list(range(100)), "B": list(range(50, 150))}, key="k")
    assert not ok["withheld"] and ok["pooled"]["shared_patients"].iloc[0] == "50"


# -- V3 ----------------------------------------------------------------------------------------------------------
def test_v3_std_beta_and_or_increment_match_statsmodels_unpenalised():
    sm = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(3)
    n = 3000
    X = pd.DataFrame({"sbp": rng.normal(130, 15, n), "age": rng.normal(60, 10, n), "bin": rng.integers(0, 2, n)})
    lp = -0.5 + 0.03 * (X.sbp - 130) + 0.02 * (X.age - 60) + 0.4 * X.bin
    y = (rng.random(n) < 1 / (1 + np.exp(-lp))).astype(int)
    res = consortium_multivariable_risk_factors(X, y, feature_cols=["sbp", "age", "bin"], regularize=False,
                                                increments={"sbp": (10, "per 10 mmHg")})
    row = res.table.set_index("variable").loc["sbp"]
    fit = sm.Logit(y, sm.add_constant(X)).fit(disp=0)
    b, se = fit.params["sbp"], fit.bse["sbp"]
    assert row.or_increment == pytest.approx(np.exp(10 * b), rel=1e-6)
    assert row.or_increment_ci_95_lower == pytest.approx(np.exp(10 * (b - 1.96 * se)), rel=1e-4)
    assert row.or_increment_ci_95_upper == pytest.approx(np.exp(10 * (b + 1.96 * se)), rel=1e-4)
    assert row.std_beta == pytest.approx(b * X.sbp.std(ddof=1), rel=1e-5)
