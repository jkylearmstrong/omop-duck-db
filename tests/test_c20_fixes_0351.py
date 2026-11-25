"""0.35.1: Kyle's review of run C20 (2026-11-25). A seed range that rounds to one value no longer prints "ranged from 0.73.";
the cohort is called self-contained only when no saved draw was reapplied, and Section 6 then prints the pin_stays call; Table 1
ends the SDI quintiles with Missing / Unknown; the Section 2.3 increment examples are the ones Table 4 prints (prior outpatient visits
per 10 visits again); Table S1 prints AUC-ROC to 2 decimals and Table S3 the length of stay to 1; Table S3 is kept on one page;
Excel shows the metric sheets with 2 decimals; manuscript_checks catches each of these. All counts are made up."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _render(tpl: str, **ctx) -> str:
    pytest.importorskip("jinja2")
    from omop_etl.manuscript_text import _environment

    return _environment([]).from_string(tpl).render(**ctx)


def test_seed_range_that_rounds_to_one_value_reads_as_one_value():
    t = '{{ r | from_range(2, same="was {v} in every draw ({range})") }}'
    assert _render(t, r=[0.7321, 0.7346]) == "was 0.73 in every draw (0.732 to 0.735)"
    assert _render(t, r=[0.7321, 0.7323]) == "was 0.73 in every draw"
    assert _render(t, r=[0.71, 0.742]) == "ranged from 0.71 to 0.74"
    assert _render('{{ r | from_range(2, verb="range from", same="are") }}', r=[0.21, 0.214]) == "are 0.21"


def test_cohort_call_prints_the_pin_stays_step_only_for_a_saved_draw():
    from omop_etl.manuscript_text import cohort_call

    own = cohort_call(None, ("2017-01-01", "2022-09-30"))
    assert "sample_stay_rule='random'" in own and "random_seed=42" in own and "pin_stays" not in own
    saved = cohort_call(None, ("2017-01-01", "2022-09-30"), pinned={"file": "selected_stays.csv"}, random_state=7)
    assert "sample_stay_rule='all'" in saved and "random_seed" not in saved.split("pin_stays")[0]
    assert "pin_stays(cohort, 'selected_stays.csv', strict=False, redraw=True, random_state=7)" in saved


def _wb(tmp_path):
    import test_manuscript_fixes_0337 as t37

    return t37._manuscript(tmp_path)


def test_text_matches_how_the_index_stays_were_chosen(tmp_path):
    from omop_etl.manuscript_text import manuscript_context, render_manuscript_body

    wb = _wb(tmp_path)
    own = " ".join(render_manuscript_body(manuscript_context(wb, run={"n_sites": 3, "pinned": False}))["markdown"].split())
    assert "with no externally supplied list of index stays" in own and "pin_stays" not in own
    assert "sample_stay_rule='random'" in own
    ctx = manuscript_context(wb, run={"n_sites": 3, "pinned": True, "random_draw_redrawn": 4,
                                     "inputs": {"pinned_stays": {"file": "selected_stays.csv", "sha256": "x"}}})
    saved = " ".join(render_manuscript_body(ctx)["markdown"].split())
    assert "no externally supplied list" not in saved
    assert "reapplied with the package's pin_stays() function" in saved and "pin_stays(cohort, 'selected_stays.csv'" in saved
    assert "sample_stay_rule='random'" not in saved


def test_table1_sdi_missing_row_and_section_23_increments(tmp_path):
    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_tables import CLINICAL_INCREMENTS
    from omop_etl.manuscript_text import manuscript_context, render_manuscript_body

    assert CLINICAL_INCREMENTS["Prior Outpatient Visit Count"] == ("10 visits", 10)
    wb = _wb(tmp_path)
    tabs = read_workbook_tables(wb)
    t1 = next(t for k, (_, t) in tabs.items() if k.startswith("Table 1"))
    feats = t1.iloc[:, 0].astype(str).tolist()
    if any(f.startswith("SDI Quintile 1") for f in feats):
        i5 = feats.index("SDI Quintile 5 (Most Deprived)")
        assert feats[i5 + 1] == "SDI Quintile: Missing / Unknown"
    t6 = next(t for k, (_, t) in tabs.items() if k.startswith("Table 6"))
    incs = set(t6["Increment"].dropna().astype(str))
    md = " ".join(render_manuscript_body(manuscript_context(wb, run={"n_sites": 3, "decisions": {"table6_increments": "clinical"}}))["markdown"].split())
    m = re.search(r"increment Δ \(e\.g\., (.*?)\) or", md)
    if m:  # every example names an increment that Table 4 prints
        for ex in m.group(1).split(", "):
            assert re.match(r"(\d+(?:\.\d+)?\s+\S+)", ex).group(1) in incs, ex
    assert "10 outpatient encounters" not in md


def test_los_by_site_prints_one_decimal():
    from omop_etl.study_report import los_by_site

    rng = np.random.default_rng(1)
    df = pd.DataFrame({"site_anon": ["Site A"] * 60, "los_days": rng.integers(1, 15, 60) + 0.37, "readmitted_30d": [0, 1] * 30})
    t = los_by_site(df, site_col="site_anon", los_col="los_days")
    assert all(re.fullmatch(r"\d+\.\d ± \d+\.\d", v) for v in t["Overall Mean ± SD (days)"])


def test_printed_stay_type_table_and_workbook_metric_format(tmp_path):
    pytest.importorskip("openpyxl")
    import openpyxl

    from omop_etl.manuscript_checks import workbook_checks
    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_text import full_manuscript_markdown

    wb = _wb(tmp_path)
    md = full_manuscript_markdown(read_workbook_tables(wb), [], workbook=wb, run={"n_sites": 3})["markdown"]
    s1 = md[md.find("Table S1:"):md.find("Table S2:")]
    assert s1 and not re.findall(r"\b0\.\d{3}\b", s1)
    assert all(r["ok"] for r in workbook_checks(wb)), workbook_checks(wb)
    book = openpyxl.load_workbook(wb)
    ws = book["Table 5 - Cross-Site LOSO"]
    assert any(c.number_format == "0.00" for row in ws.iter_rows(min_row=4) for c in row if isinstance(c.value, float))
    for row in ws.iter_rows(min_row=4):
        for c in row:
            c.number_format = "General"
    book.save(tmp_path / "plain.xlsx")
    assert not workbook_checks(tmp_path / "plain.xlsx")[0]["ok"]


def test_layout_keeps_the_los_table_on_one_page():
    import json

    from omop_etl.manuscript_text import template_dir

    layout = json.loads((template_dir("0.35.1") / "layout.json").read_text(encoding="utf-8"))
    assert "los" in layout["keep_together"]


def test_final_draft_checks_catch_the_c20_slips(tmp_path):
    pytest.importorskip("pypdf")
    import test_manuscript_fixes_0337 as t37

    from omop_etl.manuscript_checks import final_draft_checks

    pdf = t37.text_pdf(tmp_path / "c20.pdf", [
        "with no externally supplied list of index stays. The study's original random draw, made once and reused.",
        "Table 1: x Prior Outpatient Visit Count 27.0 +/- 33.2 SDI Quintile 1 (Least Deprived) SDI Quintile 5 (Most Deprived) 3.2 Models",
        "increment D (e.g., 10 mmHg of blood pressure, 10 outpatient encounters) or, for indicator variables",
        "Table 4: Reference 10 mmHg 3.5 SHAP",
        "across ten repeated random draws the AUC-ROC ranged from 0.73. Readers",
        "Table S1: Cohort Partition by Whether the Drawn Stay HistGBM AUC-ROC 0.698 0.693 Table S2: x",
        "Table S3: Distribution of Index Stay Length of Stay 5.28 +/- 6.81 Table S4: grid",  # the test PDF's font has no ±
        "Mann-Whitney U Test P-Value <0.001"])
    failed = {x["check"] for x in final_draft_checks(pdf) if not x["ok"]}
    assert {"no range missing its upper end", "self-contained cohort claim matches the draw", "Table 1 SDI Missing / Unknown row",
            "Section 2.3 increments are Table 4's", "Table S1 AUC-ROC to 2 decimals", "Table S3 LOS mean ± SD to 1 decimal", "Table S3 on one page"} <= failed


def test_export_workbook_formats_whole_metric_columns_only(tmp_path):
    pytest.importorskip("openpyxl")
    import openpyxl

    from omop_etl.study_report import export_workbook

    t = pd.DataFrame({"Site": ["A", "B"], "Patients": [1200.0, 900.0], "Standardized Beta": [0.0, -0.1]})
    p = export_workbook([("Table 6 - Multivariable LR", "Table 6", t)], tmp_path / "w.xlsx", number_formats={"Table 6 - Multivariable LR": "0.00"})
    ws = openpyxl.load_workbook(p)["Table 6 - Multivariable LR"]
    assert ws["C4"].number_format == "0.00" and ws["C5"].number_format == "0.00"  # 0.0 shows as 0.00 too
    assert ws["B4"].number_format == "General"  # a count column stays as it is
