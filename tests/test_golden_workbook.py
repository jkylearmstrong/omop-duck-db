"""Golden workbook: a committed manuscript workbook built from a seeded synthetic cohort. A change that moves any table cell fails here.

Regenerate on purpose with ``OMOP_GOLDEN_UPDATE=1 pytest tests/test_golden_workbook.py`` and commit the new file with the reason.
For the real study use ``scripts/check_golden.py`` (the delivered workbook is not in the repository).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from omop_etl import manuscript_tables as mt  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "synthetic_manuscript_tables.xlsx"


def build(path: Path, **kw) -> Path:
    df = make_features(n=3000, seed=7)
    perf = pd.DataFrame({"Model": ["HistGBM", "Logistic Regression"], "AUC-ROC (95% CI)": ["0.731 (0.728–0.735)", "0.724 (0.721–0.727)"],
                         "Sensitivity": [0.628, 0.63], "Specificity": [0.71, 0.701], "F1": [0.304, 0.299], "Brier": [0.196, 0.21]})
    loso = pd.DataFrame({"site": ["Site A", "Site B", "Site C"], "n": [1200, 1000, 800], "auroc": [0.71, 0.72, 0.69], "auroc_lo": [0.70, 0.70, 0.67],
                         "auroc_hi": [0.72, 0.74, 0.71], "brier_uncalibrated": [0.19, 0.2, 0.21], "sensitivity_050": [0.6, 0.58, 0.62],
                         "specificity_050": [0.72, 0.73, 0.7]})
    att = pd.DataFrame({"site": ["a", "a"], "step_name": ["Index", "Final"], "subjects_retained": [3500, len(df)]})
    return mt.manuscript_workbook(df, {"Model_Performance": perf, "LOSO_By_Site": loso}, path, attrition=att, site_labels={"a": "Site A"}, **kw)["path"]


def test_workbook_matches_the_golden_file(tmp_path):
    new = build(tmp_path / "new.xlsx")
    if os.environ.get("OMOP_GOLDEN_UPDATE") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(new, GOLDEN)
    assert GOLDEN.exists(), "golden file missing; run with OMOP_GOLDEN_UPDATE=1"
    summary, diffs = mt.compare_workbooks(new, GOLDEN, atol=0.0, rtol=0.0)
    assert diffs.empty, diffs.head(10).to_string()
    assert summary["cells"].sum() > 150
    sim = mt.workbook_similarity(new, GOLDEN)
    assert sim["passed"].all(), sim.to_string()


def test_a_changed_decision_is_caught_by_the_golden_file(tmp_path):
    changed = build(tmp_path / "c.xlsx", sdi_missing_term=False)
    _, diffs = mt.compare_workbooks(changed, GOLDEN, atol=0.0, rtol=0.0)
    assert not diffs.empty and diffs["sheet"].str.startswith("Table 6").any()
    clinical = build(tmp_path / "k.xlsx", table6_increments="clinical")
    _, d2 = mt.compare_workbooks(clinical, GOLDEN, atol=0.0, rtol=0.0)
    assert not d2.empty


def test_golden_differences_are_reported_per_sheet(tmp_path):
    import openpyxl

    wb = openpyxl.load_workbook(GOLDEN)
    ws = wb["Table 2 - Model Performance"]
    ws["C4"] = 0.5
    wb.save(tmp_path / "x.xlsx")
    _, d = mt.compare_workbooks(tmp_path / "x.xlsx", GOLDEN, atol=0.0, rtol=0.0)
    assert len(d) == 1 and d.iloc[0]["sheet"] == "Table 2 - Model Performance"
    sim = mt.workbook_similarity(tmp_path / "x.xlsx", GOLDEN, tolerances={"table2_auc_abs": 0.0})
    assert sim["passed"].all()  # only a sensitivity changed, the AUC check is about AUCs


def test_report_facts_and_files(tmp_path):
    from omop_etl.manuscript_report import manuscript_facts, render_results_report

    wb = build(tmp_path / "w.xlsx")
    f = manuscript_facts(wb, window=("2017-01-01", "2022-09-30"))
    assert f["n_patients"] == 3000 and f["auc_range"] == "0.72 to 0.73" and f["best_model"] == "HistGBM"
    r = render_results_report(wb, tmp_path / "rep", window=("2017-01-01", "2022-09-30"))
    assert "3,000 patients" in r["markdown"].read_text(encoding="utf-8")
    assert "<table" in r["html"].read_text(encoding="utf-8") and (tmp_path / "rep" / "tables").is_dir()
    assert len(list((tmp_path / "rep" / "tables").glob("*.csv"))) >= 4


def test_deltas_name_the_cells_and_rows_that_moved(tmp_path):
    from omop_etl.manuscript_tables import delta_summary, workbook_deltas

    changed = build(tmp_path / "c.xlsx", sdi_missing_term=False)
    cells, rows = workbook_deltas(changed, GOLDEN)
    assert (cells["sheet"].str.startswith("Table 6")).all() and (cells["kind"] == "numeric").any()
    assert cells["rel_delta"].abs().dropna().is_monotonic_decreasing
    assert (rows["side"] == "only_old").any() and rows["row"].str.contains("SDI Quintile: Missing").any()
    s = delta_summary(cells)
    assert s.loc[0, "sheet"].startswith("Table 6") and s.loc[0, "cells"] == len(cells)
    c0, r0 = workbook_deltas(GOLDEN, GOLDEN)
    assert c0.empty and r0.empty


def test_sheet_site_labels_relabel_only_the_named_sheets(tmp_path):
    import openpyxl

    df = make_features(n=600, seed=3)
    los = pd.DataFrame({"Health System": ["Site A", "Site B"], "Total Cohort (N)": [300, 300]})
    att = pd.DataFrame({"site": ["a", "b"], "step_name": ["Index", "Index"], "subjects_retained": [300, 300]})
    p = mt.manuscript_workbook(df, {"LOS_Distribution_By_Site": los}, tmp_path / "s.xlsx", attrition=att, site_labels={"a": "Site A", "b": "Site B"},
                               sheet_site_labels={"Table S1": {"Site A": "Site B", "Site B": "Site A"}, "Table S3": {"Site A": "Site B", "Site B": "Site A"}})["path"]
    wb = openpyxl.load_workbook(p)
    assert [r[0].value for r in wb["Table S1 - LOS Distribution"].iter_rows(min_row=4, max_row=5)] == ["Site B", "Site A"]
    s3 = wb["Table S3 - CONSORT Attrition"]
    assert {c.value for c in s3["A"]} >= {"Site A", "Site B"} and wb["Table 1 - Descriptive"]["A1"].value.startswith("Table 1")


def test_consort_final_steps_decision_is_listed():
    from omop_etl.study_run import STUDY_DECISIONS, resolve_decisions

    assert "consort_final_steps" in STUDY_DECISIONS
    assert resolve_decisions("package")["consort_final_steps"] is True and resolve_decisions("delivered_2026_11_11")["consort_final_steps"] is False


def test_age_method_decision_is_listed():
    from omop_etl.study_run import STUDY_DECISIONS, resolve_decisions

    assert "age_method" in STUDY_DECISIONS
    assert resolve_decisions("package")["age_method"] == "completed_years"
    assert resolve_decisions("delivered_2026_11_11")["age_method"] == "completed_years"
    assert resolve_decisions("delivered_2026_11_11", age_method="year_difference")["age_method"] == "year_difference"



@pytest.mark.parametrize("k", [1, 2, 8, 30])
def test_any_number_of_sites(tmp_path, k):
    from omop_etl.manuscript_report import manuscript_facts
    from omop_etl.study_run import assign_site_labels

    labels = list(assign_site_labels([f"s{i}" for i in range(k)], mode="ordered").values())
    assert len(set(labels)) == k
    loso = pd.DataFrame({"site": labels, "n": [500 + i for i in range(k)], "auroc": [0.7] * k, "auroc_lo": [0.68] * k, "auroc_hi": [0.72] * k,
                         "brier_uncalibrated": [0.2] * k, "sensitivity_050": [0.6] * k, "specificity_050": [0.7] * k})
    perf = pd.DataFrame({"Model": ["A"], "AUC-ROC (95% CI)": ["0.7 (0.6-0.8)"], "Sensitivity": [0.6], "Specificity": [0.7], "F1": [0.3], "Brier": [0.2]})
    wb = mt.manuscript_workbook(make_features(n=1000, seed=3), {"Model_Performance": perf, "LOSO_By_Site": loso}, tmp_path / "w.xlsx")["path"]
    assert manuscript_facts(wb)["n_sites"] == k


def test_report_is_parameterised_and_aggregate_only(tmp_path):
    import json

    from omop_etl.manuscript_report import render_results_report

    wb = build(tmp_path / "w.xlsx")
    r = render_results_report(wb, tmp_path / "r", sheets=["Table 2"], formats=["md"], write_csv=False, write_facts=False, sentences=False)
    assert set(r) == {"markdown"} and not (tmp_path / "r" / "tables").exists()
    text = r["markdown"].read_text(encoding="utf-8")
    assert "Table 2" in text and "Table 1:" not in text and str(tmp_path) not in text
    facts = json.loads(render_results_report(wb, tmp_path / "r2")["facts"].read_text(encoding="utf-8"))
    assert facts["workbook"] == "w.xlsx"
    with pytest.raises(ValueError, match="rows"):
        render_results_report(wb, tmp_path / "r3", max_rows=2)


def test_report_refuses_identifier_columns_and_suppresses_small_counts(tmp_path):
    from omop_etl import manuscript_report as mr

    with pytest.raises(ValueError, match="identifier"):
        mr._check_aggregate("S", pd.DataFrame({"person_id": [1]}), 500)
    t = mr._suppress_small(pd.DataFrame({"Patients (N)": [5, 50, "7", "1,200"], "x": ["3 (1.2%)", "30 (4%)", "a", "b"]}), 11)
    assert t["Patients (N)"].tolist() == ["<11", 50, "<11", "1,200"] and t["x"].iloc[0] == "<11" and t["x"].iloc[1] == "30 (4%)"
