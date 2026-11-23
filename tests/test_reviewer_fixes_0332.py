"""0.33.2 fixes after the reviewer check of run C4: nearly identical "not measured" indicators enter Table 6 as one term, the
abstract's risk factors leave out missing-value indicators, labels keep their acronyms in running text, the cohort call prints
its seed, the SDI wording follows the run's setting, and the grid sheet drops its unrounded columns."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_tables as mt  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402


def _bp_missing(n: int = 4000, seed: int = 11, n_only_diastolic: int = 4) -> pd.DataFrame:
    """Systolic and diastolic missing together in ~11% of stays; diastolic alone missing in a handful more (as in run C4)."""
    df = make_features(n=n, seed=seed)
    rng = np.random.default_rng(seed)
    both = rng.random(n) < 0.11
    only_d = np.zeros(n, bool)
    only_d[rng.choice(np.flatnonzero(~both), n_only_diastolic, replace=False)] = True
    df.loc[both, "vital_systolic_bp"] = np.nan
    df.loc[both | only_d, "vital_diastolic_bp"] = np.nan
    return df


# ------------------------------------------------------------------ BP indicators
def test_nearly_identical_bp_indicators_enter_as_one_term():
    t = mt.manuscript_regression(_bp_missing())
    feats = t["Feature"].tolist()
    merged = "Systolic Blood Pressure (mmHg) / Diastolic Blood Pressure (mmHg): not measured"
    assert merged in feats
    assert "Systolic Blood Pressure (mmHg): not measured" not in feats and "Diastolic Blood Pressure (mmHg): not measured" not in feats
    # both measures still have their own OR, and the merged term sits right after the systolic one
    assert feats.index(merged) == feats.index("Systolic Blood Pressure (mmHg)") + 1
    assert "Diastolic Blood Pressure (mmHg)" in feats


def test_indicators_that_really_differ_stay_separate():
    df = make_features(n=4000, seed=11)
    rng = np.random.default_rng(3)
    df.loc[rng.random(len(df)) < 0.2, "vital_systolic_bp"] = np.nan
    df.loc[rng.random(len(df)) < 0.2, "vital_diastolic_bp"] = np.nan
    feats = mt.manuscript_regression(df)["Feature"].tolist()
    assert "Systolic Blood Pressure (mmHg): not measured" in feats and "Diastolic Blood Pressure (mmHg): not measured" in feats


def test_merge_rule_on_its_own():
    a = pd.Series([1.0] * 100 + [0.0] * 900)
    b = a.copy()
    b.iloc[100] = 1.0  # differs in 1 of 101 stays where either is missing: kept apart at 0.5%, merged at 2%
    terms = [("V", "S", "Per 1 SD", None), ("V", "S: not measured", "Yes vs. no", a), ("V", "D", "Per 1 SD", None),
             ("V", "D: not measured", "Yes vs. no", b)]
    assert len(mt._merge_identical_indicators(terms, {"S", "D"}, tolerance=0.005)) == 4
    out = mt._merge_identical_indicators(terms, {"S", "D"}, tolerance=0.02)
    assert [t[1] for t in out] == ["S", "S / D: not measured", "D"]
    assert out[1][3].sum() == 101  # missing when either measure is missing
    # identical columns still merge at any tolerance (creatinine / eGFR)
    same = [("L", "C", "Per 1 SD", None), ("L", "C: not measured", "Yes vs. no", a), ("L", "E", "Per 1 SD", None),
            ("L", "E: not measured", "Yes vs. no", a.copy())]
    assert [t[1] for t in mt._merge_identical_indicators(same, {"C", "E"}, tolerance=0.0)] == ["C", "C / E: not measured", "E"]


# ------------------------------------------------------------------ abstract risk factors
def test_headline_risk_factors_skip_missing_value_indicators(tmp_path):
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables

    path = mt.manuscript_workbook(make_features(n=3000, seed=7), {}, tmp_path / "w.xlsx")["path"]
    lr = mtx._regression(read_workbook_tables(path))
    assert lr["indicator_terms"], "the synthetic data should have at least one indicator term"
    assert not any(r["feature"].endswith((": not measured", ": missing")) for r in lr["top_risk"])
    # a strong indicator would have been ranked before 0.33.2
    rows = [{"feature": "A: not measured", "or": 1.9, "lo": 1.3, "hi": 2.9, "p": 0.002, "beta": 0.2, "increment": "Yes vs. no", "group": "Vital Signs"},
            {"feature": "Prior Readmissions", "or": 1.17, "lo": 1.15, "hi": 1.18, "p": 0.0001, "beta": 0.19, "increment": "1 readmission", "group": "Encounter Information"}]
    assert mtx._is_indicator_term(rows[0]["feature"]) and not mtx._is_indicator_term(rows[1]["feature"])


# ------------------------------------------------------------------ casing
@pytest.mark.parametrize("label, expected", [
    ("Diastolic Blood Pressure (mmHg): not measured", "diastolic blood pressure (mmHg): not measured"),
    ("Prior Readmissions in Past 12 Months", "prior readmissions in past 12 months"),
    ("ALT: not measured", "ALT: not measured"),
    ("BUN: not measured", "BUN: not measured"),
    ("RUCA: Micropolitan (2.0)", "RUCA: Micropolitan (2.0)"),
    ("BMI: Obese (30.0+ kg/m²)", "BMI: Obese (30.0+ kg/m²)"),
    ("pCO2: not measured", "pCO2: not measured"),
    ("Race: Black / African American", "race: Black / African American"),
    ("Days Since Prior Inpatient / Obs / ED Encounter", "days since prior inpatient / obs / ED encounter"),
    ("Troponin I: not measured", "troponin I: not measured"),
    ("", ""),
])
def test_lower_first_keeps_acronyms(label, expected):
    from omop_etl.manuscript_text import lower_first

    assert lower_first(label) == expected


# ------------------------------------------------------------------ cohort call seed
def test_cohort_call_prints_the_seed():
    from omop_etl.manuscript_text import cohort_call

    call = cohort_call(None, ("2017-01-01", "2022-09-30"))
    assert "sample_stay_rule='random'," in call and "random_seed=42," in call
    assert "random_seed=7," in cohort_call({"random_seed": 7}) and "random_seed=42" not in cohort_call({"random_seed": 7})


# ------------------------------------------------------------------ template v0.33.2
def _render(tmp_path, decisions, summary=None):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_table6_missing_and_compare import _full_workbook

    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"], "bootstraps": 100, "decisions": decisions}
    return mtx.full_manuscript_markdown(tabs, [], workbook=wb, run=run, summary=summary or {"n_features": 95}, version="0.33.2")


def test_v0332_template_is_the_default_and_prints_the_seed(tmp_path):
    from omop_etl import manuscript_text as mtx

    assert mtx.template_dir("0.33.2").name == "v0.33.2" and mtx.template_dir("0.33.1").name == "v0.33.0"
    out = _render(tmp_path, {"sdi_missing_term": True})
    assert out["template"] == "0.33.2"
    assert "random_seed=42" in out["markdown"] and "with the seed given in the call (random_seed)" in out["markdown"]


def test_sdi_wording_follows_the_setting(tmp_path):
    on = _render(tmp_path, {"sdi_missing_term": True})["markdown"]
    off = _render(tmp_path, {"sdi_missing_term": False})["markdown"]
    assert "missing SDI has its own term" in on and "share the reference category with SDI quintile 1" not in on
    assert "share the reference category with SDI quintile 1" in off and "missing SDI has its own term" not in off


def test_empty_columns_named_once_but_counted_per_column(tmp_path):
    summary = {"n_features": 95, "n_columns": 104, "n_binary": 60, "n_continuous": 35,
               "empty_columns": ["lab_potassium_mean", "lab_potassium_last", "vital_heart_rate_mean"]}
    md = _render(tmp_path, {"sdi_missing_term": True}, summary)["markdown"]
    assert "three columns with no observed values (potassium, heart rate)" in md  # clinical names since 0.33.6


def test_printed_grid_table_has_no_unrounded_columns(tmp_path):
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_table6_missing_and_compare import _full_workbook

    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    grid = next(t for k, (_, t) in tabs.items() if k.startswith("Table S4"))
    assert "auc_raw" in grid.columns  # the workbook keeps them (the delivered comparison reads them)
    seen = {}
    real = mtx.render_manuscript_body

    def spy(*a, **k):
        seen.update(k.get("tables") or (a[1] if len(a) > 1 else {}))
        return real(*a, **k)

    mtx.render_manuscript_body = spy
    try:
        mtx.full_manuscript_markdown(tabs, [], workbook=wb, run={"n_sites": 3}, summary={"n_features": 95}, version="0.33.2")
    finally:
        mtx.render_manuscript_body = real
    assert "grid" in seen and "auc_raw" not in seen["grid"][1] and "f1_raw" not in seen["grid"][1]
