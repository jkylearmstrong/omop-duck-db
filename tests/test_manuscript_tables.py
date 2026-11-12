from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from omop_etl import manuscript_tables as mt  # noqa: E402
from omop_etl import study_report as sr  # noqa: E402
from omop_etl.study_run import pin_stays  # noqa: E402


def make_features(n=1500, seed=1):
    rng = np.random.default_rng(seed)
    age = rng.normal(65, 14, n).clip(18, 95)
    prior = rng.poisson(1.0, n)
    y = (rng.random(n) < 1 / (1 + np.exp(-(-2.4 + 0.02 * (age - 65) + 0.3 * prior)))).astype(int)
    disp = rng.choice(["home", "home_health", "snf", "ama", "other"], n, p=[.4, .15, .15, .02, .28])
    d = pd.DataFrame({
        "subject_id": np.arange(n), "visit_occurrence_id": np.arange(n) * 7, "site_anon": ["Site " + "ABC"[i % 3] for i in range(n)], "readmitted_30d": y,
        "age_at_index": age, "female": rng.integers(0, 2, n), "race_concept_id": rng.choice([8527, 8516, 0], n, p=[.7, .2, .1]),
        "ethnicity_concept_id": rng.choice([38003563, 38003564], n, p=[.05, .95]), "index_los_days": rng.gamma(2, 2, n) + 1,
        "prior_ip_os_ed_count": prior, "prior_encounters_all_count": prior + rng.poisson(5, n), "n_secondary_dx_ccs": rng.poisson(10, n),
        "days_since_prior_encounter": rng.integers(1, 365, n), "days_since_prior_ip_ed": rng.integers(1, 365, n),
        "readmission_count_twelve_months": rng.poisson(.5, n),
        "vital_bmi": np.where(rng.random(n) < .1, np.nan, rng.normal(30, 6, n).clip(14, 70)), "vital_systolic_bp": rng.normal(130, 18, n),
        "vital_diastolic_bp": rng.normal(72, 10, n),
        "sdi_quintile": rng.choice(mt._SDI + ["SDI: Missing / Unknown"], n), "ruca_category": rng.choice(mt._RUCA, n),
        "tobacco": rng.choice(["Never user", "Former user", "Current user"], n),
        "lab_albumin": np.where(rng.random(n) < .5, np.nan, rng.normal(3.3, .6, n)), "lab_sodium": rng.normal(138, 3, n),
        "med_insulins": rng.integers(0, 2, n), "elix_chf": rng.integers(0, 2, n), "elix_mets": (rng.random(n) < .05).astype(int),
    })
    for lv in ("home", "home_health", "snf", "ama", "other"):
        d[f"disp_{lv}"] = (disp == lv).astype(int)
    return d


@pytest.fixture(scope="module")
def feats():
    return make_features()


def test_manuscript_frame_categories(feats):
    m = mt.manuscript_frame(feats)
    assert set(m["m_age"].dropna()) <= set(mt._AGE) and m["m_age"].notna().all()
    assert set(m["m_race"]) == set(mt._RACE)
    assert set(m["m_disch"]) <= set(mt._DISC)
    assert (m.loc[feats["disp_ama"] == 1, "m_disch"] == "Against Medical Advice").all()
    assert (m.loc[feats["vital_bmi"].isna(), "m_bmi"] == mt._BMI[4]).all()
    assert (m.loc[feats["vital_bmi"] >= 30, "m_bmi"] == mt._BMI[3]).all()


def test_table1_counts_add_up_and_readmit_split(feats):
    t = mt.manuscript_table1(feats).set_index("Feature")
    n = len(feats)
    assert t.loc["# Unique Patients", "Whole cohort"] == f"{n} Unique Patients"
    assert t.loc["# Unique Patients", "Subset with readmission"] == f"{int(feats.readmitted_30d.sum())} Unique Patients"
    assert sum(int(t.loc[a, "Whole cohort"].split(" ")[0]) for a in mt._AGE) == n
    assert t.loc["Sex: Female", "Whole cohort"].startswith(str(int(feats.female.sum())))
    assert mt.PM in t.loc["Systolic Blood Pressure (mmHg)", "Whole cohort"]


def test_completeness_counts_and_suppression(feats):
    t = mt.manuscript_completeness(feats).set_index("Feature")
    assert t.loc["Albumin not measured at index admission", "Whole cohort"].startswith(f"{int(feats.lab_albumin.isna().sum()):,}")
    rare = feats.assign(vital_systolic_bp=np.where(np.arange(len(feats)) < 3, np.nan, feats.vital_systolic_bp))
    got = mt.manuscript_completeness(rare).set_index("Feature").loc["Systolic Blood Pressure not recorded", "Whole cohort"]
    assert got.startswith("<11")


def test_consort_combined_and_sites():
    a = pd.DataFrame({"site": ["P"] * 3 + ["Q"] * 3, "step_name": ["Index", "Age", "Final"] * 2, "subjects_retained": [100, 90, 60, 50, 45, 30]})
    t = mt.manuscript_consort(a, site_labels={"P": "Site A", "Q": "Site B"})
    assert list(t["Site"].unique()) == ["All_Sites_Combined", "Site A", "Site B"]
    comb = t[t.Site == "All_Sites_Combined"]
    assert comb["Remaining Patients (P)"].tolist() == [150, 135, 90]
    assert comb["Excluded Patients (Delta P)"].tolist() == [0, 15, 45]
    assert (t["Remaining Stays (N)"] == t["Remaining Patients (P)"]).all()


def test_regression_table_shape_and_direction(feats):
    t = mt.manuscript_regression(feats)
    assert {"Demographics", "Encounter Information", "Vital Signs", "Laboratory values", "Other clinical features"} <= set(t["Feature"])
    row = t.set_index("Feature").loc["Prior Inpatient / Obs / ED Count"]
    assert row["Increment"] == "Per 1 SD" and float(row["Adjusted OR (95% CI)*"].split(" ")[0]) > 1


def test_workbook_and_compare_roundtrip(feats, tmp_path, monkeypatch):
    import openpyxl
    from sklearn.linear_model import LogisticRegression

    monkeypatch.setattr(sr, "default_model_grid",
                        lambda *a, **k: {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))])})
    df = feats.assign(person_id=feats.subject_id, admit_date=pd.Timestamp("2018-01-01"), discharge_date=pd.Timestamp("2018-01-05"))
    r = sr.build_study_tables(df, tmp_path / "t", prefix="X", n_bootstraps=5, workbook=False, first_stay_col="prior_ip_os_ed_count")
    att = pd.DataFrame({"site": ["a", "a"], "step_name": ["Index", "Final"], "subjects_retained": [2000, len(df)]})
    wb = mt.manuscript_workbook(df, r["tables"], tmp_path / "m.xlsx", attrition=att, site_labels={"a": "Site A"})
    names = openpyxl.load_workbook(wb["path"]).sheetnames
    for want in ("Table 1 - Descriptive", "Table 2 - Model Performance", "Table 5 - Cross-Site LOSO", "Table 6 - Multivariable LR",
                 "Table S2 - Data Completeness", "Table S3 - CONSORT Attrition"):
        assert want in names
    summary, diffs = mt.compare_workbooks(wb["path"], wb["path"])
    assert summary["differ"].fillna(0).sum() == 0 and diffs.empty and summary["cells"].sum() > 100
    wb2 = openpyxl.load_workbook(wb["path"])
    wb2["Table 1 - Descriptive"]["B5"] = "999.00 " + mt.PM + " 1.00"
    wb2.save(tmp_path / "m2.xlsx")
    _, d2 = mt.compare_workbooks(tmp_path / "m2.xlsx", wb["path"])
    assert len(d2) == 1 and d2.iloc[0]["sheet"] == "Table 1 - Descriptive"


def test_pin_stays(tmp_path):
    coh = pd.DataFrame({"subject_id": [1, 1, 2, 2, 3], "visit_occurrence_id": [10, 11, 20, 21, 30]})
    out = pin_stays(coh, pd.DataFrame({"visit_occurrence_id": [11, 20, 30]}))
    assert out["visit_occurrence_id"].tolist() == [11, 20, 30]
    csv = tmp_path / "sel.csv"
    pd.DataFrame({"visit_occurrence_id": [11, 99]}).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="not in the cohort"):
        pin_stays(coh, csv)
    assert len(pin_stays(coh, csv, strict=False)) == 1
    with pytest.raises(ValueError, match="more than one stay"):
        pin_stays(coh, pd.DataFrame({"visit_occurrence_id": [10, 11]}))


def test_shared_folds_and_performance_from_predictions(feats):
    y = feats["readmitted_30d"]
    f1, f2 = sr.study_folds(y, random_state=3), sr.study_folds(y, random_state=3)
    assert all((a[1] == b[1]).all() for a, b in zip(f1, f2))
    assert sorted(np.concatenate([v for _, v in f1])) == list(range(len(y)))
    ytr = y.to_numpy()[f1[0][0]]
    assert abs(ytr[sr.balanced_training_indices(ytr, random_state=3)].mean() - 0.5) < 1e-9
    p = np.random.default_rng(0).random(len(y)) * 0.5 + y.to_numpy() * 0.2
    row = sr.performance_from_predictions(y, p, model_name="Deep", n_bootstraps=20)
    assert list(row.columns) == ["Model", "AUC-ROC (95% CI)", "Sensitivity", "Specificity", "F1", "Brier"] and "(" in row.iloc[0, 1]


def _fake_study(tmp_path, monkeypatch, feats):
    """Three tiny site databases plus stand-ins for the cohort and feature builders, so run_study is exercised end to end."""
    import duckdb
    from sklearn.linear_model import LogisticRegression

    import omop_etl.benchmark_features as bf
    import omop_etl.cohort as coh

    db = tmp_path / "db"
    db.mkdir()
    sites = ["A", "B", "C"]
    for i, s in enumerate(sites):
        part = feats[feats.site_anon == "Site " + s]
        con = duckdb.connect(str(db / f"omop_{s}.duckdb"))
        con.register("p", pd.DataFrame({"visit_occurrence_id": part.visit_occurrence_id, "person_id": part.subject_id,
                                        "discharged_to_source_value": "IP", "discharged_to_concept_id": 0}))
        con.execute("CREATE TABLE visit_occurrence AS SELECT * FROM p")
        con.close()
    by_id = feats.set_index("visit_occurrence_id")

    def fake_cohort(con, **kw):
        v = con.execute("SELECT visit_occurrence_id, person_id FROM visit_occurrence").df()
        out = pd.DataFrame({"subject_id": v.person_id, "visit_occurrence_id": v.visit_occurrence_id,
                            "outcome_flag": by_id.loc[v.visit_occurrence_id, "readmitted_30d"].to_numpy(),
                            "cohort_start_date": "2018-01-01", "cohort_end_date": "2018-01-05"})
        out.attrs["attrition"] = [{"step_name": "Index", "subjects_retained": len(out) + 5}, {"step_name": "Final", "subjects_retained": len(out)}]
        return out

    def fake_features(con, table, **kw):
        ids = con.execute(f"SELECT visit_occurrence_id FROM {table}").df().visit_occurrence_id
        cols = [c for c in feats.columns if c not in ("subject_id", "readmitted_30d", "site_anon", "index_los_days")]
        return by_id.loc[ids, [c for c in cols if c != "visit_occurrence_id"]].reset_index()

    monkeypatch.setattr(coh, "define_study_cohort", fake_cohort)
    monkeypatch.setattr(bf, "extract_benchmark_features", fake_features)
    monkeypatch.setattr(sr, "default_model_grid",
                        lambda *a, **k: {"Logistic Regression": (True, [("C=1 (benchmark)", lambda: LogisticRegression(max_iter=200))])})
    return db, sites


def test_run_study_end_to_end(feats, tmp_path, monkeypatch):
    from omop_etl.study_run import run_study

    db, sites = _fake_study(tmp_path, monkeypatch, feats)
    out = tmp_path / "out"
    kw = dict(sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None)
    r = run_study(db, out, **kw)
    assert len(r["data"]) == len(feats) and set(r["data"].site_anon) == {"Site A", "Site B", "Site C"} and Path(r["workbook"]).exists()
    assert r["comparison"] is None and (out / "Study_run.json").exists()
    # pinned draw: only the listed stays survive; reuse_features re-reads the saved parquet; compare_to compares with the first workbook
    pins = feats[feats.subject_id % 2 == 0][["visit_occurrence_id"]].assign(site_source=lambda d: "A")
    r2 = run_study(db, tmp_path / "out2", pinned_stays=pins, **kw)
    assert 0 < len(r2["data"]) < len(feats)
    r3 = run_study(db, out, reuse_features=True, compare_to=r["workbook"], prefix="Again", **kw)
    assert len(r3["data"]) == len(r["data"]) and r3["comparison"][0]["cells"].sum() > 0
    assert (out / "Again_Comparison_Summary.csv").exists()


def test_reproduce_study_script(feats, tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import reproduce_study

    db, sites = _fake_study(tmp_path, monkeypatch, feats)
    assert reproduce_study.main(["--db-dir", str(db), "--out", str(tmp_path / "o"), "--sites", *sites, "--bootstraps", "3", "--no-loso",
                                 "--no-figures", "--profile", "none"]) == 0
    assert "N = 1,500" in capsys.readouterr().out


def test_run_study_full_history_attrition_options_and_timings(feats, tmp_path, monkeypatch):
    from omop_etl.study_run import run_study

    db, sites = _fake_study(tmp_path, monkeypatch, feats)
    r = run_study(db, tmp_path / "o", sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None, attrition_mode="full_history",
                  sdi_missing_term=False, table5="calibrated", address_tie_break="ambiguous_missing")
    consort = pd.read_excel(r["workbook"], sheet_name="Table S3 - CONSORT Attrition", header=2)
    comb = consort[consort["Site"] == "All_Sites_Combined"]
    assert comb["Stage / Description"].iloc[-1].startswith("Index admission 2017-01-01 to 2022-09-30, one stay per patient")
    assert comb["Remaining Patients (P)"].iloc[-1] == len(feats)
    assert {"site_A", "tables_figures", "total"} <= set(r["timings"])
    with pytest.raises(ValueError, match="attrition_mode"):
        run_study(db, tmp_path / "o2", sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None, attrition_mode="nope")


def test_random_site_labels_end_to_end_keep_the_mapping_out_of_the_outputs(feats, tmp_path, monkeypatch):
    import json

    from omop_etl.study_run import load_site_labels, run_study

    db, sites = _fake_study(tmp_path, monkeypatch, feats)
    priv = tmp_path / "private" / "labels.json"
    priv.parent.mkdir()
    r = run_study(db, tmp_path / "out", sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None,
                  site_labels="random", label_seed=11, labels_out=priv)
    mapping = load_site_labels(priv)
    assert set(mapping) == set(sites) and set(mapping.values()) == {"Site A", "Site B", "Site C"}
    assert set(r["data"].site_anon) == set(mapping.values())
    run = json.loads((tmp_path / "out" / "Study_run.json").read_text(encoding="utf-8"))
    assert run["labels"] == ["Site A", "Site B", "Site C"] and "sites" not in run and not any(s in json.dumps(run) for s in ('"A":', '"B":'))
    # the same seed rebuilds the mapping; a saved file is accepted as site_labels
    r2 = run_study(db, tmp_path / "out2", sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None, site_labels=priv)
    assert (r2["data"].groupby("site_anon").size().sort_index().tolist() == r["data"].groupby("site_anon").size().sort_index().tolist())
