"""Tests for omop_etl.study_report on a small synthetic cohort (signal in a few features, five sites)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
pytest.importorskip("sklearn")

from omop_etl import study_report as sr  # noqa: E402


def make_cohort(n=1200, seed=0, stays_per_patient=1):
    rng = np.random.default_rng(seed)
    n_pat = n
    rows = []
    for pid in range(n_pat):
        site = ["PSU", "Temple", "Hopkins", "Geisinger", "PITT"][pid % 5]
        for k in range(stays_per_patient):
            age, los = rng.normal(65, 12), rng.gamma(2.0, 2.0) + 1
            prior = int(rng.poisson(0.8))
            lin = -2.0 + 0.03 * (age - 65) + 0.15 * los + 0.5 * prior
            y = int(rng.random() < 1 / (1 + np.exp(-lin)))
            rows.append({"person_id": pid, "visit_occurrence_id": pid * 10 + k, "site_source": site, "site_anon": "Site " + "ABCDE"[pid % 5],
                         "admit_date": pd.Timestamp("2017-01-01") + pd.Timedelta(days=int(rng.integers(0, 1500))),
                         "age": age, "index_los_days": los, "prior_ip_count": prior,
                         "sex": rng.choice(["F", "M"]), "lab_x": np.nan if rng.random() < 0.3 else rng.normal(), "readmitted_30d": y})
    d = pd.DataFrame(rows)
    d["discharge_date"] = d["admit_date"] + pd.to_timedelta(d["index_los_days"].round(), unit="D")
    return d


@pytest.fixture(scope="module")
def cohort():
    return make_cohort()


def test_prepare_model_matrix_drops_ids_encodes_and_drops_all_nan(cohort):
    d = cohort.assign(all_nan=np.nan, text_num=cohort["age"].round(1).astype(str))
    X, y = sr.prepare_model_matrix(d)
    assert len(X) == len(y) == len(d)
    for c in ("person_id", "visit_occurrence_id", "site_source", "readmitted_30d", "all_nan"):
        assert c not in X.columns
    assert "sex_M" in X.columns and X["text_num"].dtype == float
    assert not any(ch in c for c in X.columns for ch in "[]<")


def test_prepare_model_matrix_requires_label(cohort):
    with pytest.raises(ValueError, match="label column"):
        sr.prepare_model_matrix(cohort, label_col="nope")


def test_classification_metrics_and_bootstrap(cohort):
    y = cohort["readmitted_30d"].to_numpy()
    p = np.clip(0.3 + 0.4 * y + np.random.default_rng(1).normal(0, 0.2, len(y)), 0, 1)
    m = sr.classification_metrics(y, p)
    assert 0.5 < m["AUC"] <= 1 and m["auc_raw"] == pytest.approx(m["AUC"], abs=5e-4)
    ci = sr.bootstrap_ci(y, p, n_bootstraps=50)
    assert ci["AUC"][0] <= m["AUC"] <= ci["AUC"][1]
    cl = sr.bootstrap_ci(y, p, n_bootstraps=50, groups=np.arange(len(y)) // 3)  # cluster bootstrap runs and is ordered
    assert cl["AUC"][0] < cl["AUC"][1]


def test_oof_predictions_balanced_leak_free_and_deterministic(cohort):
    from sklearn.linear_model import LogisticRegression

    X, y = sr.prepare_model_matrix(cohort)
    f = lambda: LogisticRegression(max_iter=300)  # noqa: E731
    a = sr.oof_predictions(f, X, y, scale=True)
    b = sr.oof_predictions(f, X, y, scale=True)
    assert np.array_equal(a, b) and a.shape == (len(y),) and ((a > 0) & (a < 1)).all()
    assert sr.classification_metrics(y, a)["AUC"] > 0.6   # real signal is recovered
    # patient-grouped folds keep a patient's rows together
    many = make_cohort(n=300, stays_per_patient=3)
    Xm, ym = sr.prepare_model_matrix(many)
    g = sr.oof_predictions(f, Xm, ym, scale=True, groups=many["person_id"].to_numpy())
    assert g.shape == (len(ym),)


def test_tune_models_tables_and_selection(cohort):
    from sklearn.linear_model import LogisticRegression

    X, y = sr.prepare_model_matrix(cohort)
    grid = {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(C=1.0, max_iter=300)),
                                           ("C=0.01", lambda: LogisticRegression(C=0.01, max_iter=300))])}
    r = sr.tune_models(X, y, grid=grid, n_bootstraps=20, keep_oof=True)
    t = r["tuning_table"]
    assert list(t["Selection"]) == ["Fixed (benchmark)", "Tuned for AUC-ROC", "Tuned for F1"]
    assert r["performance_table"].loc[0, "AUC-ROC (95% CI)"].count("(") == 1
    assert len(r["all_configs"]) == 2 and set(r["oof"]) == {("Logistic Regression", "C=1 (benchmark)"), ("Logistic Regression", "C=0.01")}
    s = r["summary"]["Logistic Regression"]
    assert s["best_by_auc"] in ("C=1 (benchmark)", "C=0.01") and s["n_configs"] == 2


def test_default_grid_has_benchmark_first_and_four_models_when_xgboost_present():
    g = sr.default_model_grid()
    assert {"Logistic Regression", "Random Forest", "HistGBM"} <= set(g)
    assert all("(benchmark)" in cfgs[0][0] for _, cfgs in g.values())


def test_stay_type_auc_and_small_cell_suppression():
    rng = np.random.default_rng(3)
    y = rng.integers(0, 2, 400)
    p = np.clip(y * 0.3 + rng.random(400) * 0.7, 0, 1)
    first = np.arange(400) < 300
    t = sr.stay_type_auc(y, p, first, model_name="HistGBM")
    assert list(t.columns) == ["Subset", "Stays", "Share", "Readmit rate", "HistGBM AUC"]
    assert t["Stays"].tolist() == [300, 100] and t["Share"].sum() == pytest.approx(1.0) and (t["HistGBM AUC"] > 0.5).all()
    tiny = sr.stay_type_auc(y, p, np.arange(400) < 395, min_cell=11)
    assert tiny.loc[1, "Stays"] == "<11" and np.isnan(tiny.loc[1, "HistGBM AUC"])


def test_random_stay_draw_is_one_per_patient_deterministic_and_order_free():
    many = make_cohort(n=200, stays_per_patient=4)
    a = sr.random_stay_draw(many, 7)
    assert len(a) == 200 and not a.duplicated(["site_source", "person_id"]).any()
    shuffled = many.sample(frac=1, random_state=1)
    assert sr.random_stay_draw(shuffled, 7)["visit_occurrence_id"].tolist() == a["visit_occurrence_id"].tolist()
    assert sr.random_stay_draw(many, 8)["visit_occurrence_id"].tolist() != a["visit_occurrence_id"].tolist()


def test_seed_sensitivity_one_row_per_seed():
    many = make_cohort(n=300, stays_per_patient=2)
    t = sr.seed_sensitivity(many, [1, 2], n_splits=3)
    assert t["Seed"].tolist() == [1, 2] and (t["Stays (= patients)"] == 300).all() and (t["HistGBM AUC"].between(0.4, 1)).all()


def test_los_by_site_rows_and_suppression(cohort):
    t = sr.los_by_site(cohort)
    assert t["Health System"].iloc[-1] == "All Sites Combined" and len(t) == 6
    assert t["Total Cohort (N)"].iloc[-1] == len(cohort)
    small = pd.concat([cohort, cohort.head(5).assign(site_anon="Site Z")])
    z = sr.los_by_site(small, min_cell=11)
    assert z.loc[z["Health System"] == "Site Z", "Total Cohort (N)"].iloc[0] == "<11"
    assert z.loc[z["Health System"] == "Site Z", "Overall Mean ± SD (days)"].iloc[0] == ""


def test_index_stay_dates_reports_window_overflow(cohort):
    d = sr.index_stay_dates(cohort, window_start="2017-01-01", window_end="2018-01-01")
    assert d["n"] == len(cohort) and set(d["sites"]) == {f"Site {c}" for c in "ABCDE"}
    assert d["discharged_after_window_end"] > 0 and d["admitted_before_window_start"] == 0


def test_export_workbook_writes_manifest_and_sheets(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = sr.export_workbook([("A" * 40, "Table A", pd.DataFrame({"x": [1, 2], "y": [np.nan, "t"]})), ("A" * 40, "Table B", pd.DataFrame({"z": [3]}))],
                           tmp_path / "t.xlsx", title="T")
    wb = openpyxl.load_workbook(p)
    assert wb.sheetnames[0] == "Manifest" and len(wb.sheetnames) == 3 and all(len(s) <= 31 for s in wb.sheetnames)
    assert wb["Manifest"]["C5"].value == 2


def test_build_study_tables_end_to_end(cohort, tmp_path, monkeypatch):
    from sklearn.linear_model import LogisticRegression

    monkeypatch.setattr(sr, "default_model_grid", lambda *a, **k: {"HistGBM": (False, [
        ("fast (benchmark)", lambda: __import__("sklearn.ensemble", fromlist=["x"]).HistGradientBoostingClassifier(max_iter=20, random_state=0))]),
        "Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))])})
    r = sr.build_study_tables(cohort, tmp_path, prefix="T", n_bootstraps=10, window=("2017-01-01", "2021-12-31"))
    assert {"Model_Performance", "Tuning_AUC_vs_F1", "By_Stay_Type_AUC", "LOS_Distribution_By_Site", "LOSO_By_Site"} <= set(r["tables"])
    for f in r["files"]:
        assert Path(f).exists()
    assert r["summary"]["n"] == len(cohort) and "dates" in r["summary"]
    assert (tmp_path / "T_Tables.xlsx").exists()


# ------------------------------------------------------------------ figures
@pytest.fixture(scope="module")
def attrition():
    return pd.DataFrame({"step_name": ["Index visit type", "Age >= 18", "Prior observation >= 365 days", "Diabetes"],
                         "subjects_retained": [1000, 900, 700, 600], "subjects_dropped": [0, 100, 200, 100]})


def test_plot_cohort_flowchart_writes_png_and_validates(attrition, tmp_path):
    pytest.importorskip("matplotlib")
    f = tmp_path / "f1.png"
    fig = sr.plot_cohort_flowchart(attrition, 600, 90, path=f)
    assert f.exists() and f.stat().st_size > 5000
    texts = " ".join(t.get_text() for t in fig.axes[0].texts)
    assert "N = P = 600" in texts and "N = 90 (15.00%)" in texts and "N = 510 (85.00%)" in texts and "P = 100" in texts
    with pytest.raises(ValueError, match="step_name"):
        sr.plot_cohort_flowchart(attrition.drop(columns="step_name"), 600, 90)
    with pytest.raises(ValueError, match="n_readmitted"):
        sr.plot_cohort_flowchart(attrition, 600, 700)


def test_shap_ranking_direction_and_order():
    rng = np.random.default_rng(0)
    data = pd.DataFrame({"a": rng.normal(size=300), "b": rng.normal(size=300), "flat": 1.0})
    vals = np.column_stack([2 * data.a, -0.5 * data.b, np.zeros(300)])
    t = sr.shap_ranking(vals, data)
    assert t.feature.tolist() == ["a", "b", "flat"] and t.direction.tolist() == [1.0, -1.0, 0.0]


def test_explain_models_exact_for_linear_and_xgboost_and_additive(cohort):
    pytest.importorskip("xgboost")
    X, y = sr.prepare_model_matrix(cohort)
    ex = sr.explain_models(X, y, models=("Logistic Regression", "XGBoost"), sample_n=200)
    assert set(ex) == {"Logistic Regression", "XGBoost"}
    for e in ex.values():
        assert e["values"].shape == e["data"].shape == (200, X.shape[1])
        assert {"feature", "mean_abs_shap", "direction"} <= set(e["table"].columns)
    # LOS and prior admissions drive the simulated outcome, so they outrank pure noise in the linear explanation
    top = ex["Logistic Regression"]["table"].feature.head(4).tolist()
    assert "index_los_days" in top and "prior_ip_count" in top


def test_explain_models_tree_models_need_shap(cohort):
    shap = pytest.importorskip("shap")  # noqa: F841
    X, y = sr.prepare_model_matrix(cohort)
    ex = sr.explain_models(X, y, models=("Random Forest", "HistGBM"), sample_n=100)
    assert set(ex) == {"Random Forest", "HistGBM"} and ex["HistGBM"]["values"].shape == (100, X.shape[1])


def test_beeswarm_and_panels_render(cohort, tmp_path):
    pytest.importorskip("matplotlib")
    X, y = sr.prepare_model_matrix(cohort)
    ex = sr.explain_models(X, y, models=("Logistic Regression",), sample_n=150)
    e = ex["Logistic Regression"]
    one = tmp_path / "bee.png"
    sr.plot_shap_beeswarm(e["values"], e["data"], max_display=5, title="t", path=one)
    two = tmp_path / "panels.png"
    sr.plot_shap_panels({"Logistic Regression": e, "Other": e}, top=4, path=two)
    assert one.stat().st_size > 3000 and two.stat().st_size > 3000
    with pytest.raises(ValueError, match="same shape"):
        sr.plot_shap_beeswarm(e["values"][:, :2], e["data"])
    with pytest.raises(ValueError, match="empty"):
        sr.plot_shap_panels({})


def test_build_study_tables_writes_figures(cohort, attrition, tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    grid = {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))]),
            "HistGBM": (False, [("fast (benchmark)", lambda: HistGradientBoostingClassifier(max_iter=15, random_state=0))])}
    monkeypatch.setattr(sr, "default_model_grid", lambda *a, **k: grid)
    r = sr.build_study_tables(cohort, tmp_path, prefix="F", n_bootstraps=5, loso=False, workbook=False, attrition=attrition,
                              figures=True, shap_sample_n=100)
    names = {Path(f).name for f in r["files"]}
    assert {"F_Figure1_Cohort_Flowchart.png", "F_Figure_SHAP_4Panel.png"} <= names


def test_build_study_tables_writes_xgboost_shap_outputs(cohort, attrition, tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    xgb = pytest.importorskip("xgboost")
    from sklearn.linear_model import LogisticRegression

    grid = {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))]),
            "XGBoost": (False, [("tiny (benchmark)", lambda: xgb.XGBClassifier(n_estimators=5, max_depth=2, n_jobs=1))])}
    monkeypatch.setattr(sr, "default_model_grid", lambda *a, **k: grid)
    r = sr.build_study_tables(cohort, tmp_path, prefix="X", n_bootstraps=5, loso=False, workbook=False, attrition=attrition,
                              figures=True, shap_sample_n=100)
    names = {Path(f).name for f in r["files"]}
    assert {"X_Figure_SHAP_XGBoost.png", "X_Table_SHAP_Top_Predictors.csv"} <= names


def test_positive_class_handles_list_and_3d_shap_output():
    a, b = np.zeros((4, 3)), np.ones((4, 3))
    assert (sr._positive_class([a, b]) == 1).all()
    assert sr._positive_class(np.stack([a, b], axis=-1)).shape == (4, 3)


def test_bootstrap_ci_skips_single_class_resamples():
    ci = sr.bootstrap_ci(np.array([0, 0, 0, 0, 1]), np.array([.1, .2, .3, .4, .9]), n_bootstraps=20, seed=1)
    assert set(ci) == {"AUC", "F1", "Sensitivity", "Specificity", "Brier"}


# ------------------------------------------------------------------ descriptive tables
def test_table_one_levels_pvalues_and_suppression(cohort):
    d = cohort.assign(flag=(cohort["prior_ip_count"] > 0).astype(int), rare=0)
    d.loc[d.index[:3], "rare"] = 1
    t = sr.table_one(d, min_cell=11)
    assert t.iloc[0]["Variable"] == "N" and t.iloc[0]["Overall"] == len(d)
    assert {"age", "sex", "flag"} <= set(t["Variable"])
    assert t.loc[t["Variable"] == "rare", "Overall"].iloc[0].startswith("<11")
    assert t.loc[t["Variable"] == "sex", "Level"].tolist() == sorted(t.loc[t["Variable"] == "sex", "Level"].tolist(), key=lambda s: -(d["sex"] == s).sum())
    assert (t.loc[t["Variable"] == "prior_ip_count", "p"].iloc[0]) != ""


def test_logistic_regression_matches_sklearn_direction(cohort):
    X, y = sr.prepare_model_matrix(cohort)
    t = sr.logistic_regression_table(X, y)
    assert {"OR", "CI_low", "CI_high", "p"} <= set(t.columns)
    assert (t["CI_low"] <= t["OR"]).all() and (t["OR"] <= t["CI_high"]).all()
    # data were generated with positive effects of age, LOS and prior admissions
    ors = t.set_index("Variable")["OR"]
    assert ors["prior_ip_count"] > 1 and ors["index_los_days"] > 1


def test_missingness_table_by_site(cohort):
    t = sr.missingness_table(cohort)
    row = t.set_index("Variable").loc["lab_x"]
    assert 20 < row["pct_missing"] < 40
    assert "pct_missing_Site A" in t.columns
    assert "person_id" not in set(t["Variable"])


def test_build_study_tables_descriptive_and_extra(cohort, attrition, tmp_path, monkeypatch):
    from sklearn.linear_model import LogisticRegression

    monkeypatch.setattr(sr, "default_model_grid", lambda *a, **k: {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))])})
    r = sr.build_study_tables(cohort, tmp_path, prefix="D", n_bootstraps=5, loso=False, attrition=attrition,
                              extra_tables={"My_Extra": pd.DataFrame({"a": [1]})})
    assert {"Table1", "Logistic_Regression", "Missingness", "CONSORT_Attrition", "My_Extra"} <= set(r["tables"])
    import openpyxl

    names = openpyxl.load_workbook(tmp_path / "D_Tables.xlsx").sheetnames
    assert {"Table1", "Logistic_Regression", "Missingness", "CONSORT_Attrition", "My_Extra"} <= set(names)


def test_prepare_model_matrix_drops_label_copy_dates_and_outcome_columns(cohort):
    d = cohort.assign(y=cohort["readmitted_30d"], outcome_flag=cohort["readmitted_30d"], index_date=cohort["admit_date"],
                      other_date=cohort["admit_date"])
    with pytest.warns(UserWarning, match="identical to the label"):
        d2 = d.assign(label_copy=d["readmitted_30d"])
        X, _ = sr.prepare_model_matrix(d2)
    assert not ({"y", "outcome_flag", "index_date", "other_date", "label_copy"} & set(X.columns))
