"""0.34.0: Dr. Rubin's review of C9 (2026-11-23), locked in as package defaults. Table 1 to 1 decimal; Tables 2 and 3 (workbook
Tables 2 and 5) and the text that quotes them to 2 decimals, the same values; Figure 2 a SHAP violin plot; Figure 1's random-draw
exclusion explained in Section 2.1 and the caption; no ``_last`` lab and no empty column in the model matrix; the BMI by age table.
All data are made up."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_document as md  # noqa: E402
from omop_etl import manuscript_tables as mt  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402

ONE_DEC_PCT = re.compile(r"^(<11|\d[\d,]*) \(\d+\.\d%\)$")
ONE_DEC_MS = re.compile(r"^-?\d+\.\d ± \d+\.\d$")


# ------------------------------------------------------------------ Table 1
def test_table1_percentages_and_means_have_one_decimal():
    t = mt.manuscript_table1(make_features(n=2000, seed=5))
    cells = [str(v) for col in t.columns[1:] for v in t[col] if str(v) and not str(v).endswith("Unique Patients")]
    assert cells
    bad = [c for c in cells if not (ONE_DEC_PCT.match(c) or ONE_DEC_MS.match(c))]
    assert bad == [], bad[:5]


# ------------------------------------------------------------------ Tables 2 and 3 (workbook Tables 2 and 5)
def _workbook(tmp_path):
    import test_table6_missing_and_compare as t6

    real = mt.manuscript_workbook

    def with_f1(df, tables, path, **kw):
        t = dict(tables)
        t["LOSO_By_Site"] = t["LOSO_By_Site"].assign(f1_050=[0.3141, 0.2987, 0.2899], auroc=[0.7134, 0.7251, 0.6949])
        t["Model_Performance"] = pd.DataFrame({"Model": ["Logistic Regression", "XGBoost"],
                                               "AUC-ROC (95% CI)": ["0.7012 (0.6955–0.7068)", "0.7349 (0.7296–0.7401)"],
                                               "Sensitivity": [0.6123, 0.6544], "Specificity": [0.6681, 0.6702], "F1": [0.2877, 0.3012],
                                               "Brier": [0.2216, 0.2149]})
        return real(df, t, path, **kw)

    mt.manuscript_workbook = with_f1
    try:
        return t6._full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    finally:
        mt.manuscript_workbook = real


def test_tables_2_and_3_print_two_decimals(tmp_path):
    from omop_etl.manuscript_report import read_workbook_tables

    tabs = read_workbook_tables(_workbook(tmp_path))
    t2 = next(t for k, (_, t) in tabs.items() if k.startswith("Table 2"))
    t5 = next(t for k, (_, t) in tabs.items() if k.startswith("Table 5"))
    assert list(t2["AUC-ROC (95% CI)"]) == ["0.70 (0.70–0.71)", "0.73 (0.73–0.74)"]
    for frame, skip in ((t2, {"Model"}), (t5, {"Held-out site", "Test patients", "Training patients (other systems)"})):
        for c in frame.columns:
            if c in skip:
                continue
            for v in frame[c].dropna().astype(str):
                assert all(len(d) <= 2 for d in re.findall(r"\d\.(\d+)", v)), (c, v)  # a number cell reads back as 0.3 for 0.30
    assert "F1" in t5.columns and "Brier" not in t5.columns
    mean = t5[t5.iloc[:, 0].astype(str).str.startswith("Mean of")]
    assert mean.iloc[0]["AUC-ROC (95% CI)"] == f"{np.mean([0.7134, 0.7251, 0.6949]):.2f}"


def test_round_metric_text():
    assert mt.round_metric_text("0.7312 (0.7254–0.7371)") == "0.73 (0.73–0.74)"
    assert mt.round_metric_text(0.6381) == 0.64 and mt.round_metric_text("12,345") == "12,345" and mt.round_metric_text(None) is None


def test_text_quotes_the_tables_two_decimal_values(tmp_path):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables

    wb = _workbook(tmp_path)
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    out = mtx.full_manuscript_markdown(tabs, [], workbook=wb, run={"n_sites": 3, "window": ["2017-01-01", "2022-09-30"]},
                                       summary={"n_features": 95}, version="0.34.0")
    assert out["template"] == "0.34.0"
    text = re.sub(r"```.*?```|\$\$.*?\$\$", "", out["markdown"], flags=re.S)
    prose = "\n".join(ln for ln in text.splitlines() if not ln.startswith("|"))
    assert "0.73 (0.73–0.74)" in prose and "0.7349" not in prose
    assert not re.findall(r"\b0\.\d{3}\b", prose), re.findall(r".{40}\b0\.\d{3}\b.{10}", prose)[:5]
    ctx = mtx.manuscript_context(wb, run={"n_sites": 3})
    assert ctx["loso"]["auc_mean"] == float(f"{np.mean([0.7134, 0.7251, 0.6949]):.2f}")  # the table's own mean row


# ------------------------------------------------------------------ model matrix
def test_no_last_lab_and_no_empty_column_enters_the_model_matrix():
    pytest.importorskip("sklearn")
    from omop_etl.study_report import prepare_model_matrix
    from omop_etl.study_run import drop_last_lab_values

    df = make_features(n=500, seed=3)
    empty = ["lab_potassium", "lab_chloride", "lab_calcium", "lab_platelets", "lab_bilirubin_total", "lab_alkaline_phosphatase",
             "vital_heart_rate", "vital_respiratory_rate", "vital_body_temperature", "vital_oxygen_saturation"]
    df = df.assign(**{c: np.nan for c in empty}, lab_bun_last=1.0, lab_creatinine_last=2.0)
    X, _ = prepare_model_matrix(drop_last_lab_values(df))
    assert not [c for c in X.columns if c.endswith("_last")]
    assert not set(empty) & set(X.columns)


# ------------------------------------------------------------------ Figure 2
def test_shap_summary_is_a_violin_plot_with_clinical_labels():
    pytest.importorskip("matplotlib")
    from matplotlib.collections import PathCollection, PolyCollection

    from omop_etl.study_report import plot_shap_beeswarm

    rng = np.random.default_rng(0)
    data = pd.DataFrame({"lab_bun": rng.normal(20, 5, 400), "elix_van_walraven_score": rng.normal(5, 3, 400), "age_at_index": rng.normal(60, 10, 400)})
    sv = np.column_stack([0.01 * (data["lab_bun"] - 20), 0.02 * data["elix_van_walraven_score"], rng.normal(0, 0.01, 400)])
    fig = plot_shap_beeswarm(sv, data)
    ax = fig.axes[0]
    assert any(isinstance(c, PolyCollection) for c in ax.collections) and not any(isinstance(c, PathCollection) for c in ax.collections)
    labels = [t.get_text() for t in ax.get_yticklabels()]
    assert not [x for x in labels if "_" in x] and "Elixhauser Score (van Walraven)" in labels
    bees = plot_shap_beeswarm(sv, data, kind="beeswarm")
    assert any(isinstance(c, PathCollection) for c in bees.axes[0].collections)
    with pytest.raises(ValueError):
        plot_shap_beeswarm(sv, data, kind="bar")


# ------------------------------------------------------------------ BMI by age
def test_bmi_by_age_table():
    t = mt.bmi_age_readmission(make_features(n=4000, seed=9))
    assert list(t.columns[:5]) == ["Age group", "BMI category", "N", "% of age group", "30-day readmission (%)"]
    assert len(t) == 4 * 5 and set(t["Age group"]) == {"18–44 years", "45–64 years", "65–74 years", "75+ years"}
    big = t[t["N"] != "<11"]
    for age, g in big.groupby("Age group"):
        assert abs(sum(float(x) for x in g["% of age group"]) - 100) < 1.0 or (t[(t["Age group"] == age)]["N"] == "<11").any()
    assert (t.loc[t["N"] == "<11", "30-day readmission (%)"] == "").all()


# ------------------------------------------------------------------ Figure 1 note and the rendered PDF
@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_rendered_pdf_keeps_title_references_and_explains_the_random_draw(tmp_path):
    pytest.importorskip("pypdf")
    import test_manuscript_fixes_0337 as t37
    from omop_etl.manuscript_checks import TITLE, final_draft_checks
    from omop_etl.study_run import figure1_attrition

    wb = t37._manuscript(tmp_path)
    figure1_attrition(t37._attrition(), "2017-01-01", "2022-09-30").drop(columns=["excluded_label", "excluded_shown", "into_final"]).to_csv(
        tmp_path / "Study_CONSORT_Attrition.csv", index=False)
    params = {"title": TITLE, "authors": [{"name": "A. Author, PhD", "role": "Design."}]}
    r = md.render_manuscript_document(wb, tmp_path / "doc", params=params, run={"n_sites": 3, "window": ["2017-01-01", "2022-09-30"]},
                                      window=("2017-01-01", "2022-09-30"))
    assert r["missing"] == [], r["note"]
    res = final_draft_checks(r["files"]["pdf"])
    assert [x for x in res if not x["ok"]] == [], res
    from pypdf import PdfReader

    text = " ".join(re.sub(r"\s+", " ", p.extract_text()) for p in PdfReader(str(r["files"]["pdf"])).pages)
    assert "13 patients whose qualifying stays in the window were not in that draw are excluded at the last step" in text
    assert "the 13 patients whose qualifying stays in the window were not in that draw branch off the last arrow" in text
