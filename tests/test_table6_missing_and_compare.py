"""Table 6 handles mostly-missing measures with a 'not measured' term; the delta tool sees CI-only changes; the delivered-manuscript
comparison pairs sites by their numbers. Synthetic data only."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_tables as mt  # noqa: E402
from omop_etl.delivered_compare import compare_with_delivered, match_sites, quoted_numbers_changed  # noqa: E402
from omop_etl.study_run import STUDY_DECISIONS, resolve_decisions  # noqa: E402
from test_golden_workbook import GOLDEN, build  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402


def _with_lactate(n=4000, seed=1):
    rng = np.random.default_rng(seed)
    df = make_features(n=n, seed=5).copy()
    v = rng.normal(1.5, 0.8, len(df))
    v[rng.random(len(df)) < 0.75] = np.nan  # measured in a quarter of stays
    df["lab_lactate"] = v
    return df


def test_presets_pin_table6_missing():
    assert resolve_decisions("package")["table6_missing"] == "indicator"
    assert resolve_decisions("delivered_2026_11_11")["table6_missing"] == "median"
    assert set(STUDY_DECISIONS["table6_missing"]["options"]) == {"indicator", "median"}
    with pytest.raises(ValueError):
        mt.manuscript_regression(make_features(n=200, seed=1), missing="mice")


def test_indicator_term_for_a_mostly_missing_lab():
    df = _with_lactate()
    ind = mt.manuscript_regression(df, increments="clinical").set_index("Feature")
    med = mt.manuscript_regression(df, missing="median", increments="clinical").set_index("Feature")
    assert "Lactate: not measured" in ind.index and ind.loc["Lactate: not measured", "Increment"] == "Yes vs. no"
    assert "Lactate: not measured" not in med.index and ind.loc["Lactate", "Increment"] == "1 mmol/L"
    rows = list(ind.index)
    assert rows.index("Lactate: not measured") == rows.index("Lactate") + 1  # printed under its measure
    # a measure with fewer than min_missing missing stays gets no indicator
    df2 = df.copy()
    df2["lab_lactate"] = df2["lab_lactate"].fillna(1.5)
    df2.loc[df2.index[:5], "lab_lactate"] = np.nan
    assert "Lactate: not measured" not in set(mt.manuscript_regression(df2)["Feature"])


def test_median_mode_reproduces_the_previous_golden_rows(tmp_path):
    med = build(tmp_path / "m.xlsx", table6_missing="median")
    ind = build(tmp_path / "i.xlsx")
    _, d = mt.compare_workbooks(ind, GOLDEN, atol=0.0, rtol=0.0)
    assert d.empty  # the golden file is the indicator default
    _, rows = mt.workbook_deltas(med, GOLDEN)  # median differs only by the indicator rows it leaves out
    assert set(rows["row"]) == {r for r in rows["row"] if r.endswith("not measured") or r.endswith("missing")}
    assert (rows["side"] == "only_old").all()


def test_numbers_do_not_read_a_range_hyphen_as_a_minus():
    assert mt._numbers("0.731 (0.728-0.735)") == [0.731, 0.728, 0.735]
    assert mt._numbers("OR -0.5") == [-0.5]
    assert mt._numbers("1,234 (−2.0)") == [1234.0, -2.0]


def _edit(src: Path, dst: Path, sheet: str, cell: str, value) -> Path:
    import openpyxl

    wb = openpyxl.load_workbook(src)
    wb[sheet][cell] = value
    wb.save(dst)
    return dst


def test_exact_deltas_list_a_ci_bound_change(tmp_path):
    import openpyxl

    ws = openpyxl.load_workbook(GOLDEN)["Table 2 - Model Performance"]
    old = ws["B4"].value
    assert old == "0.73 (0.73–0.73)"  # two decimals since 0.34.0
    changed = _edit(GOLDEN, tmp_path / "c.xlsx", "Table 2 - Model Performance", "B4", "0.73 (0.73–0.731)")
    tol, _ = mt.workbook_deltas(changed, GOLDEN)
    exact, _ = mt.workbook_deltas(changed, GOLDEN, atol=0.0, rtol=0.0, text=True)
    assert tol.empty and len(exact) == 1
    assert exact.loc[0, "delta"] == 0 and exact.loc[0, "max_abs_delta"] == pytest.approx(0.001)
    s = mt.delta_summary(exact)
    assert s.loc[0, "max_abs_delta"] == pytest.approx(0.001)
    renamed = _edit(GOLDEN, tmp_path / "r.xlsx", "Table 2 - Model Performance", "B4", "0.73 [0.73–0.73]")
    assert len(mt.workbook_deltas(renamed, GOLDEN, atol=0.0, rtol=0.0, text=True)[0]) == 1
    # the CLI is exact unless --tolerant
    script = ROOT / "scripts" / "compare_study_deltas.py"
    base = [sys.executable, str(script), "--new", str(changed), "--baseline", str(GOLDEN), "--out-dir", str(tmp_path / "o")]
    assert subprocess.run(base + ["--fail-on-diff"], capture_output=True, text=True).returncode == 1
    assert subprocess.run(base + ["--fail-on-diff", "--tolerant"], capture_output=True, text=True).returncode == 0


def test_match_sites_by_numbers():
    old = {"Site A": [1200, 0.71], "Site B": [1000, 0.72], "Site C": [800, 0.69]}
    new = {"Site C": [1201, 0.711], "Site A": [999, 0.72], "Site B": [800, 0.69]}
    m, ok = match_sites(new, old)
    assert ok and m == {"Site C": "Site A", "Site A": "Site B", "Site B": "Site C"}
    _, ok2 = match_sites({"Site A": [1000, 0.7], "Site B": [1001, 0.7]}, {"Site X": [1000, 0.7], "Site Y": [1000.5, 0.7]})
    assert not ok2


def _shuffle_sites(src: Path, dst: Path) -> Path:
    import openpyxl

    wb = openpyxl.load_workbook(src)
    ws = wb["Table 5 - Cross-Site LOSO"]
    perm = {"Site A": "Site C", "Site B": "Site A", "Site C": "Site B"}
    for row in ws.iter_rows(min_row=4):
        if row[0].value in perm:
            row[0].value = perm[row[0].value]
    wb.save(dst)
    return dst


def test_compare_with_delivered_pairs_sites_and_reports_aggregates(tmp_path):
    new = _shuffle_sites(GOLDEN, tmp_path / "new.xlsx")
    new = _edit(new, new, "Table 2 - Model Performance", "B4", "0.73 (0.73–0.74)")  # two decimals since 0.34.0
    r = compare_with_delivered(new, GOLDEN)
    a = r["answers"]
    assert a["14_site_matching_unambiguous"] and a["4_same_sheets"] and a["7_same_cohort_n"]
    assert a["9_per_sheet"]["Table 5 - Cross-Site LOSO"]["differing_cells"] == 0  # letters differ, numbers paired
    assert a["9_per_sheet"]["Table 2 - Model Performance"] == {"cells_compared": 10, "differing_cells": 1, "max_abs_auc_change": 0.01}
    assert a["8_descriptive_identical"] and a["13_no_direction_or_significance_change"] and a["19_rows_line_up"]
    assert a["10_table2_auc"]["HistGBM"] == {"delivered": "0.73 (0.73–0.73)", "new": "0.73 (0.73–0.74)"}
    assert a["11_loso_mean_auc"] == {"delivered": 0.71, "new": 0.71}  # the Mean row, two decimals
    import re

    assert not re.search(r"Site [A-Z]\b", str(a))  # answers carry no site letters
    assert "Site A -> Site B" in r["detail"]  # the pairing stays in the private detail
    r2 = compare_with_delivered(new, GOLDEN, previous={**a, "7_same_cohort_n": False})
    assert r2["answers"]["18_changed_since_previous"] == ["7_same_cohort_n"]
    script = ROOT / "scripts" / "compare_with_delivered.py"
    res = subprocess.run([sys.executable, str(script), "--new", str(new), "--delivered", str(GOLDEN), "--answers", str(tmp_path / "a.json"),
                          "--detail", str(tmp_path / "d.md")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "a.json").exists() and "Site A ->" not in res.stdout


def test_quoted_numbers_changed_counts_whole_tokens():
    diffs = pd.DataFrame([{"sheet": "Table 2", "row": "HistGBM", "column": "AUC", "new": "0.733 (0.730–0.737)", "old": "0.731 (0.728–0.735)"},
                          {"sheet": "Table 1", "row": "N", "column": "x", "new": "225,798", "old": "225,939"}])
    pages = ["The best model reached an AUC of 0.731 (95% CI 0.728 to 0.735) in the cohort of 225,939 patients overall.",
             "HistGBM 0.731 (0.728-0.735) 0.628 0.710\nno change for the value 0.7311 written here in this sentence.",
             "Again the model reached an AUC of 0.731. in the final validation cohort of these patients."]
    q = quoted_numbers_changed(pages, diffs)
    assert (q["count"], q["pages"], q["distinct_numbers"]) == (5, [1, 3], 4)  # the table row on page 2 is not prose
    assert (q["count_all_text"], q["pages_all_text"]) == (8, [1, 2, 3])


def _full_workbook(path: Path, labels: dict[str, str], auc_hi: float = 0.735) -> Path:
    df = make_features(n=900, seed=4)
    sites = ["s1", "s2", "s3"]
    n = {"s1": 400, "s2": 300, "s3": 200}
    perf = pd.DataFrame({"Model": ["HistGBM"], "AUC-ROC (95% CI)": [f"0.731 (0.728–{auc_hi:.3f})"], "Sensitivity": [0.6], "Specificity": [0.7],
                         "F1": [0.3], "Brier": [0.2]})
    tun = pd.DataFrame({"Model": ["HistGBM"] * 3, "Selection": ["Fixed (benchmark)", "Tuned for AUC-ROC", "Tuned for F1"],
                        "Configuration": ["depth 3", "depth 5", "depth 5"], "AUC-ROC": [0.72, 0.731, 0.731], "F1": [0.29, 0.3, 0.3]})
    allc = pd.DataFrame({"Model": ["HistGBM", "HistGBM"], "Configuration": ["depth 3", "depth 5"], "auc_raw": [0.72, 0.731], "f1_raw": [0.29, 0.3]})
    loso = pd.DataFrame({"site": [labels[s] for s in sites], "n": [n[s] for s in sites], "auroc": [0.71, 0.72, 0.69], "auroc_lo": [0.7, 0.7, 0.67],
                         "auroc_hi": [0.72, 0.74, 0.71], "brier_uncalibrated": [0.19, 0.2, 0.21], "sensitivity_050": [0.6, 0.58, 0.62],
                         "specificity_050": [0.72, 0.73, 0.7]})
    los = pd.DataFrame({"Health System": [labels[s] for s in sites], "Total Cohort (N)": [n[s] for s in sites], "Median LOS": [3.0, 4.0, 5.0]})
    att = pd.DataFrame([{"site": s, "step_name": st, "subjects_retained": v} for s in sites for st, v in (("Index", n[s] + 50), ("Final", n[s]))])
    tables = {"Model_Performance": perf, "Tuning_AUC_vs_F1": tun, "Tuning_All_Configs": allc, "LOSO_By_Site": loso, "LOS_Distribution_By_Site": los}
    return mt.manuscript_workbook(df, tables, path, attrition=att, site_labels=labels, table6_missing="median")["path"]


def test_compare_with_delivered_files_and_documents(tmp_path):
    import json
    import zipfile

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    old = _full_workbook(tmp_path / "old.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    new = _full_workbook(tmp_path / "new.xlsx", {"s1": "Site B", "s2": "Site C", "s3": "Site A"}, auc_hi=0.746)
    # documents: the new run's Table 5 lists its sites in workbook order; the delivered PDF quotes the old CI bound
    docx = tmp_path / "new.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p>Table 5 is below.</w:p><w:p>Table 5: LOSO</w:p><w:p>Site B</w:p><w:p>Site C</w:p>"
                                        "<w:p>Site A</w:p><w:p>Table 6: LR</w:p><w:p>Site Z</w:p></w:document>")

    def pdf(path: Path, text: str) -> Path:
        f = plt.figure()
        f.text(0.05, 0.5, text)
        f.savefig(path)
        plt.close(f)
        return path

    new_pdf = pdf(tmp_path / "new.pdf", "Table 5: LOSO Site B Site C Site A")
    old_pdf = pdf(tmp_path / "old.pdf", "The best model had an AUC of 0.73 (0.73 to 0.74) in the whole cohort")
    import hashlib

    sha = hashlib.sha256(old_pdf.read_bytes()).hexdigest()
    r = compare_with_delivered(new, old, new_pdf=new_pdf, delivered_pdf=old_pdf, new_docx=docx, delivered_pdf_sha256=sha)
    a = r["answers"]
    assert a["1_delivered_pdf_hash_matches"] and a["2_new_pdf_differs"] and a["3_files_present"]
    assert a["14_site_matching_unambiguous"] and a["15_same_letter_across_sheets"] and a["16_document_table5_letters_match"]
    assert a["8_descriptive_identical"], a["8_per_sheet"]
    assert a["9_per_sheet"]["Table 2 - Model Performance"]["differing_cells"] == 1 and a["12_selections_unchanged"]
    assert a["17_quoted_numbers_changed"] and a["17_detail"]["pages"] == [1]
    assert "Site B -> Site A" in r["detail"]
    out = tmp_path / "res"
    script = ROOT / "scripts" / "compare_with_delivered.py"
    (tmp_path / "prev.json").write_text(json.dumps(a), encoding="utf-8")
    res = subprocess.run([sys.executable, str(script), "--new", str(new), "--delivered", str(old), "--new-pdf", str(new_pdf), "--new-docx", str(docx),
                          "--delivered-pdf", str(old_pdf), "--delivered-sha256", sha, "--previous", str(tmp_path / "prev.json"),
                          "--answers", str(out / "a.json"), "--detail", str(out / "d.md")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert json.loads((out / "a.json").read_text(encoding="utf-8"))["18_changed_since_previous"] == []


def test_benchmark_suffix_stage_keyed_s3_and_s4_layout(tmp_path):
    import openpyxl

    old = _full_workbook(tmp_path / "old.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    new = _full_workbook(tmp_path / "new.xlsx", {"s1": "Site C", "s2": "Site A", "s3": "Site B"})
    wb = openpyxl.load_workbook(old)
    # delivered style: "(benchmark)" on the fixed configuration, and S4 lists each configuration once per selection rule
    t3 = wb["Table 3 - Tuning AUC vs F1"]
    for row in t3.iter_rows(min_row=4):
        if row[2].value == "depth 3":
            row[2].value = "depth 3 (benchmark)"
    s4 = wb["Table S4 - All Tuning Grid"]
    rows = [[c.value for c in r] for r in s4.iter_rows(min_row=4)]
    s4.insert_cols(2)
    s4.cell(row=3, column=2, value="Selection")
    for i, r in enumerate(rows):
        s4.cell(row=4 + i, column=2, value="Tuned for F1")
    for j, r in enumerate(rows):
        s4.append([r[0], "Tuned for AUC-ROC", *r[1:]])
    s4.cell(row=4, column=4, value=0.999)  # one real change: HistGBM depth 3 auc_raw
    # S3: one stage only in the delivered workbook, so positions shift
    s3 = wb["Table S3 - CONSORT Attrition"]
    s3.append(["Site B", "Step 3", "Extra rule", 290, 290, 10, 10, "x"])
    wb.save(tmp_path / "old2.xlsx")
    a = compare_with_delivered(new, tmp_path / "old2.xlsx")["answers"]
    assert a["12_selections_unchanged"] and a["14_site_matching_unambiguous"] and a["14_ambiguous_sheets"] == []
    assert a["15_same_letter_across_sheets"] and a["15_sheets_out_of_step"] == []
    s4a = a["9_per_sheet"]["Table S4 - All Tuning Grid"]
    assert s4a["cells_compared"] == 4 and s4a["differing_cells"] == 1 and s4a["columns_compared"] == ["auc_raw", "f1_raw"]
    assert s4a["max_abs_auc_change"] == round(0.999 - 0.72, 4)  # "auc_raw" counts as an AUC column
    assert a["9_per_sheet"]["Table 3 - Tuning AUC vs F1"]["differing_cells"] == 0


def test_s3_with_a_different_attrition_layout_is_reported_not_ambiguous(tmp_path):
    import openpyxl

    old = _full_workbook(tmp_path / "old.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    new = _full_workbook(tmp_path / "new.xlsx", {"s1": "Site C", "s2": "Site A", "s3": "Site B"})
    # another preset's CONSORT steps: no stage in common with the delivered workbook
    wb = openpyxl.load_workbook(new)
    s3 = wb["Table S3 - CONSORT Attrition"]
    col = next(c.column for c in s3[3] if str(c.value or "").startswith("Stage"))
    for (cell,) in s3.iter_rows(min_row=4, min_col=col, max_col=col):
        if cell.value:
            cell.value = f"Other rule: {cell.value}"
    wb.save(tmp_path / "new2.xlsx")
    a = compare_with_delivered(tmp_path / "new2.xlsx", old)["answers"]
    assert a["14_layout_differs_sheets"] == ["Table S3 - CONSORT Attrition"]
    assert a["14_ambiguous_sheets"] == [] and a["14_site_matching_unambiguous"]


def test_document_cross_site_table_found_by_title_not_number():
    """The manuscript numbers tables in reading order, so the cross-site table can be Table 4 and Table 5 the regression (item 16)."""
    from omop_etl.delivered_compare import _section_sites

    text = ("Table 4: Harmonized Leave-One-Site-Out (LOSO) Cross-Site External\nValidation\nSite B 0.71\nSite A 0.72\nSite C 0.70\n"
            "Mean 0.71\nTable 5: Multivariable Logistic Regression\nAge 1.01\nTable S3: CONSORT Attrition\nSite A 100\n")
    assert _section_sites(text) == ["Site B", "Site A", "Site C"]
    assert _section_sites(text, caption=None) == []  # the old rule: the section after "Table 5:"
    old = "Table 5: Cross-Site LOSO\nSite A 0.7\nSite B 0.7\nTable 6: Regression\n"
    assert _section_sites(old) == ["Site A", "Site B"]
