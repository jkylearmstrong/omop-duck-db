"""0.33.1, after run C3 (2026-11-20): every Figure 1 box is as tall as its wrapped text (the phenotype box had three lines in a
two-line box), and the workbook titles and index sheet are again those of the workbook delivered on 2026-11-11 ("5-Fold Grouped
CV" in Table 2, "(N = ...)" in Table S2, a "Manifest & Index" sheet with a byline row and a description column)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_tables as mt  # noqa: E402
from omop_etl import study_report as sr  # noqa: E402
from test_golden_workbook import build  # noqa: E402

# the step names of the study's cohort definition; the phenotype step is the one that overflowed in run C3
STEPS = ["Index visit type", "Age >= 18 years at index", "Length of stay >= 1 day(s)", "Prior observation >= 365 days (wash-in)",
         "No in-hospital death at index stay", "Not transferred to another acute-care hospital",
         "Not metformin-only prediabetes / PCOS (patient-level)",
         "Diabetes phenotype (ICD-9 249/250, ICD-10 E08-E13, antidiabetic medication, or HbA1c >= 6.5%)",
         "Verified follow-up (evidence >= 30 days after discharge)"]


def _attrition(names, start=600_000, step=7_000):
    kept = [start - i * step for i in range(len(names))]
    return pd.DataFrame({"step_name": names, "subjects_retained": kept,
                         "subjects_dropped": [0] + [kept[i - 1] - kept[i] for i in range(1, len(kept))]})


# ------------------------------------------------------------------ Figure 1
@pytest.mark.parametrize("names", [
    STEPS,
    # longer than any real step: four or five wrapped lines in a box and in its exclusion note
    STEPS[:2] + ["An eligibility criterion with a very long name " * 4, "Short", "Another long criterion, with commas, slashes / and (brackets) " * 3],
    ["Index"] + [f"Step {i}" for i in range(1, 15)],
])
def test_no_flowchart_text_leaves_its_box_or_runs_into_another(names):
    pytest.importorskip("matplotlib")
    from matplotlib.patches import Rectangle

    fig = sr.plot_cohort_flowchart(_attrition(names), 200_000, 20_000)
    assert sr.flowchart_overlaps(fig) == []
    ax = fig.axes[0]
    main = [p for p in ax.patches if isinstance(p, Rectangle) and p.get_x() < 1]
    assert len(main) >= len(names) + 1  # every step, the analytic cohort and the left outcome box
    # boxes never overlap each other
    spans = sorted((p.get_y(), p.get_y() + p.get_height()) for p in main)
    assert all(a_hi <= b_lo for (_, a_hi), (b_lo, _) in zip(spans, spans[1:]))
    # nothing is cut: each step name appears in full
    texts = " ".join(t.get_text() for t in ax.texts).replace("\n", " ")
    assert all(" ".join(n.split()) in " ".join(texts.split()) for n in names[1:])


def test_the_phenotype_box_grows_to_three_lines():
    pytest.importorskip("matplotlib")
    from matplotlib.patches import Rectangle

    ax = sr.plot_cohort_flowchart(_attrition(STEPS), 200_000, 20_000).axes[0]
    pheno = next(t for t in ax.texts if t.get_text().startswith("Diabetes phenotype"))
    age = next(t for t in ax.texts if t.get_text().startswith("Age >= 18"))
    assert pheno.get_text().count("\n") >= 2 and age.get_text().count("\n") == 1

    def height(t):
        x, y = t.get_position()
        return next(p.get_height() for p in ax.patches if isinstance(p, Rectangle)
                    and p.get_x() <= x <= p.get_x() + p.get_width() and p.get_y() <= y <= p.get_y() + p.get_height())

    assert height(pheno) > height(age)


def test_flowchart_overlaps_reports_a_box_too_small_for_its_text():
    pytest.importorskip("matplotlib")
    from matplotlib.patches import Rectangle

    fig = sr.plot_cohort_flowchart(_attrition(STEPS), 200_000, 20_000)
    ax = fig.axes[0]
    pheno = next(t for t in ax.texts if t.get_text().startswith("Diabetes phenotype"))
    x, y = pheno.get_position()
    box = next(p for p in ax.patches if isinstance(p, Rectangle) and p.get_x() <= x <= p.get_x() + p.get_width()
               and p.get_y() <= y <= p.get_y() + p.get_height())
    box.set_y(y - 0.15)
    box.set_height(0.3)  # the C3 situation: a two-line box for three lines of text
    assert any("overflows its box" in p and "Diabetes phenotype" in p for p in sr.flowchart_overlaps(fig))


# ------------------------------------------------------------------ workbook titles and index as delivered on 2026-11-11
@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    openpyxl = pytest.importorskip("openpyxl")
    return openpyxl.load_workbook(build(tmp_path_factory.mktemp("wb") / "m.xlsx", byline="Authors: Study team"))


def test_table_titles_match_the_delivered_workbook(workbook):
    n = 3000
    assert workbook["Table 2 - Model Performance"]["A1"].value == \
        "Table 2: Primary Benchmark Model Performance (50/50 Balanced Training, F1-Tuned, 5-Fold Grouped CV)"
    assert workbook["Table S2 - Data Completeness"]["A1"].value == \
        f"Table S2: Missingness and Measurement Completeness Across Clinical Features (N = {n:,})"
    assert workbook["Table 1 - Descriptive"]["A1"].value == \
        f"Table 1: Baseline Descriptive Characteristics at the Randomly Drawn Index Stay (N = {n:,})"


def test_the_index_sheet_has_the_delivered_layout(workbook):
    assert workbook.sheetnames[0] == "Manifest & Index"
    ws = workbook["Manifest & Index"]
    assert ws["A3"].value == "Authors: Study team" and ws["A4"].value is None
    assert [c.value for c in ws[5]][:4] == ["Sheet Name", "Manuscript Table Title", "Rows", "Description / Key Dimension"]
    rows = {r[0]: r for r in ws.iter_rows(min_row=6, values_only=True) if r[0]}
    assert set(rows) == set(workbook.sheetnames[1:])
    for name, (_, title, n_rows, desc) in rows.items():
        assert title == workbook[name]["A1"].value and isinstance(n_rows, int)
        assert desc == ("Supplementary Material / Listing" if name.startswith("Table S") else "Primary Manuscript Table")


def test_without_a_byline_the_row_stays_blank_and_readers_skip_the_index(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = build(tmp_path / "b.xlsx")
    ws = openpyxl.load_workbook(p)["Manifest & Index"]
    assert ws["A3"].value is None and ws["A5"].value == "Sheet Name"
    summary, _ = mt.compare_workbooks(p, p)
    assert not summary["sheet"].str.startswith("Manifest").any()


def test_export_workbook_default_layout_is_unchanged(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    p = sr.export_workbook([("T", "Table T", pd.DataFrame({"x": [1, 2]}))], tmp_path / "t.xlsx", title="T")
    ws = openpyxl.load_workbook(p)["Manifest"]
    assert [c.value for c in ws[4]] == ["Sheet", "Caption", "Rows"] and ws["C5"].value == 2
