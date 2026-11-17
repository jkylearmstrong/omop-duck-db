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
    assert old == "0.731 (0.728–0.735)"
    changed = _edit(GOLDEN, tmp_path / "c.xlsx", "Table 2 - Model Performance", "B4", "0.731 (0.728–0.736)")
    tol, _ = mt.workbook_deltas(changed, GOLDEN)
    exact, _ = mt.workbook_deltas(changed, GOLDEN, atol=0.0, rtol=0.0, text=True)
    assert tol.empty and len(exact) == 1
    assert exact.loc[0, "delta"] == 0 and exact.loc[0, "max_abs_delta"] == pytest.approx(0.001)
    s = mt.delta_summary(exact)
    assert s.loc[0, "max_abs_delta"] == pytest.approx(0.001)
    renamed = _edit(GOLDEN, tmp_path / "r.xlsx", "Table 2 - Model Performance", "B4", "0.731 [0.728–0.735]")
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
    new = _edit(new, new, "Table 2 - Model Performance", "B4", "0.733 (0.730–0.737)")
    r = compare_with_delivered(new, GOLDEN)
    a = r["answers"]
    assert a["14_site_matching_unambiguous"] and a["4_same_sheets"] and a["7_same_cohort_n"]
    assert a["9_per_sheet"]["Table 5 - Cross-Site LOSO"]["differing_cells"] == 0  # letters differ, numbers paired
    assert a["9_per_sheet"]["Table 2 - Model Performance"] == {"differing_cells": 1, "max_abs_auc_change": 0.002}
    assert a["8_descriptive_identical"] and a["13_no_direction_or_significance_change"] and a["19_rows_line_up"]
    assert a["10_table2_auc"]["HistGBM"] == {"delivered": "0.731 (0.728–0.735)", "new": "0.733 (0.730–0.737)"}
    assert a["11_loso_mean_auc"] == {"delivered": 0.707, "new": 0.707}
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
    pages = ["AUC 0.731 (95% CI 0.728 to 0.735) in 225,939 patients", "no change 0.7311 here", "again 0.731."]
    q = quoted_numbers_changed(pages, diffs)
    assert q == {"count": 5, "pages": [1, 3], "distinct_numbers": 4}
