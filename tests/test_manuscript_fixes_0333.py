"""0.33.3 fixes after Kyle's review of run C5 (2026-11-21): Figure 1 ends with the grant protocol window and the analysed cohort
whatever Table S3 shows, the text never prints a patient count as "qualifying stays", the printed manuscript leaves out the CONSORT
table and turns the length-of-stay table (sites as columns), the title page comes from the study team's parameters, the
References list the literature the text cites, and both formats have one-inch margins on US-letter pages."""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_document as md  # noqa: E402
from omop_etl.study_run import figure1_attrition  # noqa: E402


def _attrition(window_step: bool = True) -> pd.DataFrame:
    rows = []
    for site, (a, b, c) in {"s1": (100, 80, 60), "s2": (50, 40, 30)}.items():
        rows += [{"site": site, "step_name": "Index visit type in (262, 9201)", "subjects_retained": a},
                 {"site": site, "step_name": "Verified 30-day follow-up", "subjects_retained": b}]
        if window_step:
            rows.append({"site": site, "step_name": "Index admission 2017-01-01 to 2022-09-30, one stay per patient", "subjects_retained": c})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ Figure 1
def test_figure1_ends_with_the_grant_protocol_window():
    f = figure1_attrition(_attrition(), "2017-01-01", "2022-09-30")
    assert list(f["subjects_retained"]) == [150, 120, 90]
    assert list(f["subjects_dropped"]) == [0, 30, 30]
    assert f["step_name"].iloc[-1] == "Grant protocol window: index admission January 1, 2017 to September 30, 2022"
    assert f["excluded_label"].iloc[-1] == "Excluded: no qualifying index admission in the grant protocol window"
    assert f["excluded_label"].iloc[:-1].isna().all()


def test_flowchart_uses_the_exclusion_label():
    pytest.importorskip("matplotlib")
    from omop_etl.study_report import flowchart_overlaps, plot_cohort_flowchart

    fig = plot_cohort_flowchart(figure1_attrition(_attrition(), "2017-01-01", "2022-09-30"), 90, 9)
    texts = [t.get_text() for t in fig.axes[0].texts]
    assert any(t.startswith("Grant protocol window") for t in texts)
    assert any("grant protocol window" in t and t.startswith("Excluded: no qualifying") for t in texts)
    assert not any("did not meet Grant" in t for t in texts)
    assert flowchart_overlaps(fig) == []


def test_run_figure_keeps_the_window_when_table_s3_drops_it(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    from test_multisite_anonymous import _study

    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.study_run import run_study

    db, keys = _study(tmp_path, monkeypatch, 3)
    out = tmp_path / "o"
    run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False,
              preset="delivered_2026_11_11", attrition_mode="full_history")  # consort_final_steps=False
    rec = json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
    fig = pd.read_csv(out / "Study_CONSORT_Attrition.csv")
    assert fig["step_name"].iloc[-1].startswith("Grant protocol window")
    assert int(fig["subjects_retained"].iloc[-1]) == rec["n"]
    assert "excluded_label" not in fig.columns
    s3 = read_workbook_tables(out / "Study_Manuscript_Tables.xlsx")["Table S3 - CONSORT Attrition"][1]
    assert not s3.astype(str).apply(lambda c: c.str.contains("Index admission|Grant protocol")).any().any()  # the delivered Table S3
    assert rec["qualifying_stays"] == rec["n"]  # the stand-in cohort has one stay per patient
    assert not [x for x in rec["data_checks"]["findings"] if x["check"] == "figure1_cohort"]


# ------------------------------------------------------------------ text and printed tables
def _render(tmp_path, run_extra=None):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_table6_missing_and_compare import _full_workbook

    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"], "bootstraps": 100, "decisions": {"sdi_missing_term": True}, **(run_extra or {})}
    flow = tmp_path / "Study_Figure1_Cohort_Flowchart.png"
    flow.write_bytes(b"\x89PNG")
    return mtx.full_manuscript_markdown(tabs, [flow], workbook=wb, run=run, summary={"n_features": 95}, version="0.33.3"), tabs


def test_v0333_template_is_selected_and_drops_the_consort_table(tmp_path):
    from omop_etl import manuscript_text as mtx

    assert mtx.template_dir("0.33.3").name == "v0.33.3" and mtx.template_dir("0.33.2").name == "v0.33.2"
    out, _ = _render(tmp_path)
    md_ = out["markdown"]
    assert out["template"] == "0.33.3"
    assert "attrition" not in out["numbers"] and "CONSORT Stepwise" not in md_
    assert "Figure 1 shows the patients remaining after each eligibility step" in md_
    assert "grant protocol window (January 1, 2017 through September 30, 2022)" in md_


def test_length_of_stay_table_is_printed_with_sites_as_columns(tmp_path):
    out, tabs = _render(tmp_path)
    md_ = out["markdown"]
    key = next(k for k in tabs if k.startswith("Table S1"))
    los = tabs[key][1]
    cap = out["numbers"]["los"]
    block = md_[md_.index(f"**{cap}:"):]
    lines = [line for line in block.splitlines()[1:] if line.strip()]
    table = []
    for line in lines:
        if not line.startswith("|"):
            break
        table.append(line)
    cells = [c.strip() for c in table[0].strip().strip("|").split("|")]
    assert cells[0] == "Statistic" and cells[1:] == [str(x) for x in los.iloc[:, 0]]
    assert [line.split("|")[1].strip() for line in table[2:]] == [str(c) for c in los.columns[1:]]


def test_patient_counts_are_not_printed_as_qualifying_stays(tmp_path):
    from omop_etl import manuscript_text as mtx

    s3 = pd.DataFrame({"Site": ["All_Sites_Combined"] * 2, "Stage / Description": ["Index visit", "Follow-up"],
                       "Remaining Stays (N)": [610, 300], "Remaining Patients (P)": [610, 300]})
    assert mtx._attrition({"Table S3 - CONSORT Attrition": ("t", s3)}, 226) == {}
    out, _ = _render(tmp_path, {"qualifying_stays": 2500})
    n = re.search(r"Analytic cohort \(1 random stay per patient\): ([\d,]+) stays", out["markdown"]).group(1)
    assert "Total qualifying stays across 3 sites: 2,500 stays" in out["markdown"]
    assert f"({2500 / int(n.replace(',', '')):.2f} per patient)" in out["markdown"]


# ------------------------------------------------------------------ title page, references, margins
def _workbook(tmp_path):
    from test_table6_missing_and_compare import _full_workbook

    return _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})


def test_title_and_authors_come_from_the_params(tmp_path, monkeypatch):
    pytest.importorskip("jinja2")
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": None, "pandoc": None, "typst": None})
    prm = {"title": 'Benchmarking "30-Day" Readmission', "authors": [{"name": "A. Author", "affiliation": "Dept. X", "role": "Design"}, "B. Author"]}
    r = md.render_manuscript_document(_workbook(tmp_path), tmp_path / "doc", figures=[], params=prm, run={"n_sites": 3},
                                      window=("2017-01-01", "2022-09-30"))
    head = r["qmd"].read_text(encoding="utf-8").split("\n---\n")[0]
    assert 'title: "Benchmarking \\"30-Day\\" Readmission"' in head
    assert '  - name: "A. Author"\n    affiliations:\n      - name: "Dept. X"' in head and '  - name: "B. Author"' in head
    assert "Study manuscript" not in head
    assert re.search(r"margin:\n\s+x: 1in\n\s+y: 1in", head) and "citeproc: true" in head
    # an explicit author still wins, and without params the neutral title stays
    r2 = md.render_manuscript_document(_workbook(tmp_path), tmp_path / "doc2", figures=[], author="Study team", run={"n_sites": 3})
    head2 = r2["qmd"].read_text(encoding="utf-8").split("\n---\n")[0]
    assert 'title: "Study manuscript"' in head2 and '  - name: "Study team"' in head2 and "A. Author" not in head2


def test_references_list_the_cited_literature(tmp_path, monkeypatch):
    pytest.importorskip("grateful_py")
    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": None, "pandoc": None, "typst": None})
    r = md.render_manuscript_document(_workbook(tmp_path), tmp_path / "doc", figures=[], run={"n_sites": 3})
    bib = r["bib"].read_text(encoding="utf-8")
    qmd = r["qmd"].read_text(encoding="utf-8")
    for key in md.LITERATURE_KEYS:
        assert f"{{{key}," in bib and f"@{key}" in qmd.split("\n---\n")[0]
    for cited in ("Pedregosa et al. 2011", "Lundberg and Lee 2017"):  # the others are printed when their model is in the run
        assert cited in qmd


def test_reference_docx_sets_letter_pages_and_one_inch_margins(tmp_path):
    p = tmp_path / "ref.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", '<w:document><w:body><w:p/><w:sectPr><w:footnotePr/></w:sectPr></w:body></w:document>')
        z.writestr("word/styles.xml", "<w:styles/>")
    md._set_docx_page(p)
    xml = zipfile.ZipFile(p).read("word/document.xml").decode()
    assert xml.count("<w:sectPr>") == 1 and '<w:pgSz w:w="12240" w:h="15840" />' in xml
    assert all(f'w:{side}="1440"' in xml for side in ("top", "right", "bottom", "left"))
    assert md.PAGE_MARGIN == "1in"


@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_rendered_documents_have_the_title_page_margins_and_all_references(tmp_path):
    pytest.importorskip("grateful_py")
    pypdf = pytest.importorskip("pypdf")
    prm = {"title": "Benchmarking Readmission Prediction", "authors": [{"name": "A. Author", "affiliation": "Dept. X"}]}
    r = md.render_manuscript_document(_workbook(tmp_path), tmp_path / "doc", figures=[], params=prm, run={"n_sites": 3},
                                      window=("2017-01-01", "2022-09-30"))
    assert r["missing"] == [], r["note"]
    text = "\n".join(pg.extract_text() for pg in pypdf.PdfReader(r["files"]["pdf"]).pages)
    assert text.lstrip().startswith("Benchmarking Readmission Prediction") and "A. Author" in text
    tail = text[text.rindex("References"):]
    assert "Bibliography" not in tail and "Yuan, Guo-Xun" in tail and "omop-duck-db" in tail
    xml = zipfile.ZipFile(r["files"]["docx"]).read("word/document.xml").decode()
    assert re.search(r'<w:pgMar[^>]*w:left="1440"', xml) and re.search(r'<w:pgSz[^>]*w:w="12240"', xml)
    assert not (tmp_path / "doc" / "Study_reference.docx").exists()
