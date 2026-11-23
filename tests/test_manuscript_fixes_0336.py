"""0.33.6 fixes from the audit of the run C8 manuscript (2026-11-22), the draft that goes to Dr. Rubin: counts of 1,000 or more
printed with commas in text cells (Table 1), an estimate and its interval never broken across lines (Table 4, the abstract), and the
text items found by reading every page. These tests keep the table and text rules from coming back."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_document as md  # noqa: E402

# a run of digits of five or more with no separator, outside a date, code or decimal: a count printed without commas
BARE_COUNT = re.compile(r"(?<![\w.,/:\-–])\d{5,}(?![\w,/:\-–])")
SNAKE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")
# names the text prints on purpose: the cohort call's arguments and the scikit-learn / xgboost settings of Section 2.4 and Appendix B
ALLOWED_NAMES = {"define_study_cohort", "concept_ancestor", "random_seed", "n_estimators", "max_depth", "min_samples_split", "max_iter",
                 "learning_rate", "l2_regularization", "colsample_bytree", "random_state"}


# ------------------------------------------------------------------ counts
@pytest.mark.parametrize("cell, printed", [
    ("123456 Unique Patients", "123,456 Unique Patients"), ("12345 (10.00%)", "12,345 (10.00%)"), ("1987 (0.88%)", "1,987 (0.88%)"),
    ("999 (1.2%)", "999 (1.2%)"), ("1234.5 ± 20.1", "1,234.5 ± 20.1"), ("Site A (N = 67890)", "Site A (N = 67,890)"),
    ("Index admission 2017-01-01 to 2022-09-30", "Index admission 2017-01-01 to 2022-09-30"), ("between 2017 and 2022", "between 2017 and 2022"),
    ("2017–2022", "2017–2022"), ("2019", "2019"), ("4.0 [3.0–6.0]", "4.0 [3.0–6.0]"), ("ICD 12345A", "ICD 12345A"), ("<11", "<11")])
def test_text_cells_get_thousands_separators(cell, printed):
    assert md._group_digits(cell) == printed


def test_table1_cells_print_with_commas():
    t = pd.DataFrame({"Feature": ["# Unique Patients", "Age: 18–44 years", "Days Since Prior Encounter"],
                      "Whole cohort": ["123456 Unique Patients", "12345 (10.00%)", "50.00 ± 100.00"],
                      "Subset with readmission": ["12345 Unique Patients", "1500 (6.50%)", "40.00 ± 90.00"]})
    out = md._table_md(t)
    assert "123,456 Unique Patients" in out and "12,345 (10.00%)" in out and "1,500 (6.50%)" in out
    assert not BARE_COUNT.search(out), out


def test_units_with_powers_are_printed_as_superscripts():
    assert md._powers("5 x10^9/L") == "5 ×10⁹/L"
    assert "^" not in md._table_md(pd.DataFrame({"Feature": ["Leukocytes (WBC)"], "Increment": ["5 x10^9/L"]}))


# ------------------------------------------------------------------ intervals
def _table4() -> pd.DataFrame:
    feats = ["Systolic Blood Pressure (mmHg) / Diastolic Blood Pressure (mmHg): not measured", "eGFR (2021)", "HbA1c", "Hematocrit",
             "Number of Secondary Diagnosis CCS Categories", "Discharge Status: Against Medical Advice", "Age: 45–64 years"]
    incs = ["Yes vs. no", "10 mL/min/1.73 m²", "1 percentage point", "5 percentage points", "1 category", "Vs. reference", "Vs. reference"]
    return pd.DataFrame({"Feature": feats, "Increment": incs, "Adjusted OR (95% CI)*": ["0.66 (0.55–0.79)"] * len(feats),
                         "Standardized Beta": [-0.11, 0.07, -0.03, 0.22, 0.04, -0.09, 0.0], "P-Value": ["<0.001", "0.321", "0.045", "<0.001", "0.654", "0.012", "0.987"]})


def test_wide_tables_give_number_columns_their_full_width():
    lines = md._table_md(_table4()).splitlines()
    rule = [len(r.strip(":")) for r in lines[1].strip("|").split("|")]
    assert rule[0] <= md.TEXT_COLUMN_MAX + 1  # the long feature labels wrap
    assert rule[2] >= len("0.66 (0.55–0.79)") + 1 and rule[4] >= len("P-Value") + 1
    # the number columns now take a larger share of the page than the C8 layout gave them
    assert rule[2] / sum(rule) > 0.2, rule


def test_typst_keeps_estimates_and_intervals_together():
    m = re.search(r'#show regex\("(.+)"\): it => box\(it\)', md.TABLE_TYPST)
    assert m, md.TABLE_TYPST
    rx = re.compile(m.group(1).replace("\\\\", "\\"))
    assert rx.fullmatch("1.23 (1.11–1.37)") and rx.fullmatch("2.34–2.56") and rx.fullmatch("0.765 (0.751–0.779)")


# ------------------------------------------------------------------ text
def test_lower_first_keeps_proper_names_and_ranges_collapse():
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx

    assert mtx.lower_first("Elixhauser Score (van Walraven)") == "Elixhauser score (van Walraven)"
    assert mtx.lower_first("Days Since Prior Inpatient / Obs / ED Encounter") == "days since prior inpatient / obs / ED encounter"
    env = mtx._environment([])
    assert env.from_string("{{ r | fmt_range }}").render(r=(0.7771, 0.7769)) == "0.777"
    assert env.from_string("{{ r | fmt_range }}").render(r=(0.701, 0.712)) == "0.701–0.712"


def test_empty_columns_are_named_in_words():
    from omop_etl.manuscript_text import _empty_words

    assert [_empty_words(c) for c in ("vital_heart_rate", "vital_oxygen_saturation", "lab_potassium", "lab_bilirubin_total",
                                      "lab_alkaline_phosphatase")] == ["heart rate", "oxygen saturation", "potassium", "total bilirubin",
                                                                       "alkaline phosphatase"]


def test_v0336_template_has_no_line_that_eats_its_newline():
    """A text line that ends in a block tag must end in "+%}" unless the next line is a tag: with trim_blocks the tag eats the newline,
    so a list item ran into the next one (C8: "manuscript drafting.- Daniel J. Rubin")."""
    from omop_etl.manuscript_text import template_dir

    assert template_dir("0.33.6").name == "v0.33.6"
    lines = (template_dir("0.33.6") / "manuscript.md.j2").read_text(encoding="utf-8").split("\n")
    bad = []
    for i, line in enumerate(lines[:-1]):
        s = line.rstrip()
        text = re.sub(r"\{%.*?%\}|\{#.*?#\}", "", s).strip()
        nxt = lines[i + 1].strip()
        if s.endswith("%}") and not s.endswith("+%}") and text and not s.endswith("-%}") and not nxt.startswith("{%-"):
            bad.append(i + 1)
    assert bad == []


def _render(tmp_path, n=3000, run=None):
    pytest.importorskip("jinja2")
    import test_table6_missing_and_compare as t6
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables

    real = t6.make_features
    t6.make_features = lambda n_=900, seed=4, **kw: real(n=n, seed=seed)
    try:
        wb = t6._full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    finally:
        t6.make_features = real
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    flow = tmp_path / "Study_Figure1_Cohort_Flowchart.png"
    flow.write_bytes(b"\x89PNG")
    params = {"authors": [{"name": "A. Author, PhD", "role": "Study design, manuscript drafting."}, {"name": "B. Author, MD", "role": "Critical revision."}]}
    return wb, mtx.full_manuscript_markdown(tabs, [flow], workbook=wb, run=run if run is not None else {"n_sites": 3, "qualifying_stays": 4100,
                                                                                                   "window": ["2017-01-01", "2022-09-30"]},
                                            summary={"n_features": 95, "n_columns": 110, "empty_columns": ["vital_heart_rate", "lab_potassium"]}, params=params,
                                            version="0.33.6")["markdown"]


def test_rendered_text_follows_the_rules(tmp_path):
    _, out = _render(tmp_path)
    # authors: one per line
    assert re.search(r"(?m)^- A\. Author, PhD: .+\n- B\. Author, MD: ", out), out[out.index("Author Contributions"):][:400]
    # tables: no count without commas
    rows = [ln for ln in out.splitlines() if ln.startswith("| ")]
    assert rows and not [r for r in rows if BARE_COUNT.search(r)]
    # prose: no column names outside the cohort call and file names
    prose = re.sub(r"```.*?```", "", out, flags=re.S)
    prose = re.sub(r"define_study_cohort\(.*?\)", "", prose, flags=re.S)
    prose = re.sub(r"\$\$.*?\$\$|\$[^$\n]+\$", "", prose, flags=re.S)  # formulas
    prose = re.sub(r"\[not in this run: [^\]]*\]", "", prose)  # values the small test run does not have
    hits = {h for h in SNAKE.findall(prose) if not h.endswith((".xlsx", ".csv")) and h not in ALLOWED_NAMES}
    hits -= {h for h in hits if re.search(rf"\\?_?{re.escape(h)}\\?_?\.(xlsx|csv|json)", prose)}
    assert not hits, sorted(hits)
    assert "vital heart rate" not in out and "lab potassium" not in out and "heart rate" in out and "potassium" in out
    for said_twice in ("strictly ran", "standalone Excel", "(stays with unverified follow-up status were excluded)", "protocol period (spanning",
                       "AUPRC, Sensitivity"):
        assert said_twice not in out, said_twice
    assert not re.search(r"(\d\.\d{3})(–| to )\1\b", out)  # no "0.777–0.777"


def test_run_record_next_to_the_workbook_fills_what_the_caller_leaves_out(tmp_path):
    pytest.importorskip("jinja2")
    from omop_etl.manuscript_text import manuscript_context

    wb, _ = _render(tmp_path, n=900)
    (tmp_path / "Study_run.json").write_text(json.dumps({"qualifying_stays": 2500, "n_sites": 3}), encoding="utf-8")
    for run in (None, {}, {"n_sites": 3}):
        ctx = manuscript_context(wb, run=run)
        assert ctx["attrition"]["qualifying_stays"] == 2500, run


@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_rendered_pdf_has_comma_counts_and_unbroken_intervals(tmp_path):
    pytest.importorskip("jinja2")
    pypdf = pytest.importorskip("pypdf")
    import openpyxl

    wb, _ = _render(tmp_path)
    # give Table 4 C8's long labels and increments
    book = openpyxl.load_workbook(wb)
    ws = next(book[s] for s in book.sheetnames if s.startswith("Table 6"))
    long = _table4()
    r0 = next(r for r in range(4, ws.max_row + 1) if ws.cell(r, 2).value not in (None, "", "Reference"))
    for k, row in long.iterrows():
        for j, v in enumerate(row.tolist(), start=1):
            ws.cell(r0 + k, j).value = v
    book.save(wb)
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[], params={"authors": ["A. Author"]}, run={"n_sites": 3},
                                      window=("2017-01-01", "2022-09-30"))
    assert r["missing"] == [], r["note"]
    text = "\n".join(p.extract_text() for p in pypdf.PdfReader(r["files"]["pdf"]).pages)
    assert not re.search(r"\d[–-]\s*\n\s*\d", text), re.findall(r".{30}\d[–-]\s*\n\s*\d.{10}", text)[:5]
    t1 = text[text.index("Table 1:"):text.index("Table 2:")]
    assert not BARE_COUNT.search(t1), BARE_COUNT.findall(t1)[:5]
    assert "×10⁹/L" in text or "x10^9" not in text
