"""0.33.4 fixes after Kyle's review of the run C6 manuscript (2026-11-22): the title page and the DOCX metadata carry the study title
when the parameters file has none, Figure 2 and the text name features by their clinical labels instead of column names, paragraphs
that end in a template block tag keep their blank line (the abstract's Results and Conclusions, the Section 2.4 model list, the
Table 1 caption), and the short main tables stay on one page in the PDF."""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_document as md  # noqa: E402
from omop_etl.manuscript_tables import FEATURE_LABELS, feature_label  # noqa: E402

STUDY_TITLE = "Multi-Center Benchmark of Traditional Machine Learning Models for 30-Day Hospital Readmission Risk Prediction"
# the column names the C6 manuscript printed in Figure 2, Section 2.2 and Section 3.5
C6_COLUMNS = ["elix_van_walraven_score", "days_since_prior_ip_ed", "los_days", "prior_ip_los_days", "prior_ip_count", "lab_hemoglobin",
              "lab_anion_gap", "lab_bun_last", "age_at_index", "lab_bun", "elix_mets", "prior_ip_os_ed_count", "prior_IP_OS_ED_count",
              "elix_total_conditions", "vital_systolic_bp", "lab_sodium_last", "readmission_count_twelve_months", "n_secondary_dx_ccs"]


# ------------------------------------------------------------------ feature labels
def test_every_c6_column_gets_a_clinical_label():
    for c in C6_COLUMNS:
        lab = feature_label(c)
        assert "_" not in lab and lab != c, (c, lab)
    assert feature_label("lab_bun") == "BUN" and feature_label("lab_bun_last") == "BUN (last value)"
    assert feature_label("elix_mets") == "Metastatic Cancer" and feature_label("disp_ama") == "Discharge Status: Against Medical Advice"
    assert feature_label("readmission_count_twelve_months") == "Prior Readmissions in Past 12 Months"  # as in Table 4
    assert feature_label("tobacco_Never user") == "Tobacco: Never user"
    assert feature_label("ruca_category_RUCA: Small Town / Rural (3+)") == "RUCA: Small Town / Rural (3+)"
    assert feature_label("sdi_quintile_SDI Quintile 3") == "SDI Quintile 3"
    assert feature_label("some_new_ed_feature") == "Some New ED Feature"  # unknown columns still become words
    assert not [v for v in FEATURE_LABELS.values() if "_" in v]


def test_shap_figure_uses_clinical_labels():
    pytest.importorskip("matplotlib")
    import numpy as np

    from omop_etl.study_report import plot_shap_beeswarm

    data = pd.DataFrame(np.random.default_rng(0).normal(size=(40, 3)), columns=["lab_bun_last", "elix_mets", "age_at_index"])
    fig = plot_shap_beeswarm(data.to_numpy() * 0.1, data, max_display=3)
    ticks = [t.get_text() for t in fig.axes[0].get_yticklabels()]
    assert sorted(ticks) == sorted(["BUN (last value)", "Metastatic Cancer", "Age at Index Admission (years)"])
    raw = plot_shap_beeswarm(data.to_numpy() * 0.1, data, max_display=3, labels=str)
    assert "lab_bun_last" in [t.get_text() for t in raw.axes[0].get_yticklabels()]


# ------------------------------------------------------------------ the text
def _render(tmp_path, shap_rows=None):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_table6_missing_and_compare import _full_workbook

    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    if shap_rows is not None:
        pd.DataFrame({"feature": shap_rows, "mean_abs_shap": range(len(shap_rows), 0, -1)}).to_csv(
            tmp_path / "Study_Table_SHAP_Top_Predictors.csv", index=False)
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"], "bootstraps": 100, "decisions": {"sdi_missing_term": True},
           "qualifying_stays": 2500}
    flow = tmp_path / "Study_Figure1_Cohort_Flowchart.png"
    flow.write_bytes(b"\x89PNG")
    return mtx.full_manuscript_markdown(tabs, [flow], workbook=wb, run=run, summary={"n_features": 95}, version="0.33.4")


def test_v0334_template_is_selected(tmp_path):
    from omop_etl import manuscript_text as mtx

    assert mtx.template_dir("0.33.4").name == "v0.33.4" and mtx.template_dir("0.33.3").name == "v0.33.3"
    assert md.template_title("0.33.4") == STUDY_TITLE and md.template_title("0.33.3") == ""


def test_paragraphs_lists_and_captions_start_on_their_own(tmp_path):
    out = _render(tmp_path)["markdown"]
    for label in ("**Results:**", "**Conclusions:**"):
        i = out.index(label)
        assert out[i - 2:i] == "\n\n", (label, out[i - 80:i])
    i = out.index("**Table 1:")
    assert out[i - 2:i] == "\n\n", out[i - 120:i]
    # Section 2.4: one numbered item per line
    sec = out[out.index("### 2.4"):out.index("### 2.5")]
    items = re.findall(r"(?m)^(\d)\. ", sec)
    assert items == [str(k) for k in range(1, len(items) + 1)] and items, sec[:600]
    assert not re.search(r"\.\d\. [A-Z]", sec)
    # the model loop ends each item with a newline (the test workbook has one model; the rendered check below has four)
    from omop_etl.manuscript_text import template_dir

    assert "{%- endif +%}\n{% endfor %}" in (template_dir("0.33.4") / "manuscript.md.j2").read_text(encoding="utf-8")


def test_no_template_paragraph_loses_its_blank_line():
    """A line with text that ends in a block tag must use "+%}": with trim_blocks the tag eats the newline, and the blank line after it
    with it, so the next paragraph ran on (C6: the abstract's Results and Conclusions, the Table 1 caption)."""
    from omop_etl.manuscript_text import template_dir

    lines = (template_dir("0.33.4") / "manuscript.md.j2").read_text(encoding="utf-8").split("\n")
    bad = []
    for i, line in enumerate(lines[:-1]):
        s = line.rstrip()
        text = re.sub(r"\{%.*?%\}|\{#.*?#\}", "", s).strip()
        if s.endswith("%}") and not s.endswith("+%}") and text and not lines[i + 1].strip():
            bad.append(i + 1)
    assert bad == []


def test_section_2_2_and_shap_text_have_no_column_names(tmp_path):
    out = _render(tmp_path, shap_rows=["readmission_count_twelve_months", "disp_ama", "lab_albumin", "lab_bun_last", "elix_mets"])
    md_ = out["markdown"]
    s22 = md_[md_.index("### 2.2"):md_.index("### 2.3")]
    for col in ("prior_IP_OS_ED_count", "readmission_count_twelve_months", "n_secondary_dx_ccs"):
        assert col not in s22
    assert "prior 12-month inpatient readmissions" in s22 and "secondary diagnosis categories" in s22
    s35 = md_[md_.index("SHAP Feature Importance"):]
    assert "Prior Readmissions in Past 12 Months, Discharge Status: Against Medical Advice, Albumin, BUN (last value), and Metastatic Cancer" in s35
    assert not re.search(r"\b[a-z]+_[a-z0-9_]+\b", s35[:s35.index("\n## ")])


def test_short_tables_are_kept_on_one_page(tmp_path):
    md_ = _render(tmp_path)["markdown"]
    i = md_.index("**Table 2:")
    assert md_[:i].rstrip().endswith("```{=typst}\n#block(breakable: false)[\n```")
    loso = md_.index("**Table 3:")
    assert md_[:loso].rstrip().endswith("#block(breakable: false)[\n```")
    assert md_[loso:].index("```{=typst}\n]\n```") < md_[loso:].index("###")
    t1 = md_.index("**Table 1:")
    assert "breakable" not in md_[t1 - 60:t1]  # long tables still break across pages


# ------------------------------------------------------------------ title page
def test_title_defaults_to_the_study_title(tmp_path, monkeypatch):
    pytest.importorskip("jinja2")
    from test_table6_missing_and_compare import _full_workbook

    monkeypatch.setattr(md, "find_renderer", lambda: {"quarto": None, "pandoc": None, "typst": None})
    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[], params={"authors": ["A. Author"]}, run={"n_sites": 3})
    head = r["qmd"].read_text(encoding="utf-8").split("\n---\n")[0]
    assert f'title: "{STUDY_TITLE}"' in head and "Study manuscript" not in head
    r2 = md.render_manuscript_document(wb, tmp_path / "doc2", figures=[], params={"title": "Own title"}, run={"n_sites": 3})
    assert 'title: "Own title"' in r2["qmd"].read_text(encoding="utf-8")  # the parameters still win
    r3 = md.render_manuscript_document(wb, tmp_path / "doc3", figures=[], title="Explicit", params={"title": "Own title"}, run={"n_sites": 3})
    assert 'title: "Explicit"' in r3["qmd"].read_text(encoding="utf-8")


@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_rendered_docx_metadata_and_pdf_page_one_carry_the_title(tmp_path):
    pytest.importorskip("jinja2")
    pypdf = pytest.importorskip("pypdf")
    from test_table6_missing_and_compare import _full_workbook

    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    r = md.render_manuscript_document(wb, tmp_path / "doc", figures=[], params={"authors": ["A. Author"]}, run={"n_sites": 3},
                                      window=("2017-01-01", "2022-09-30"))
    assert r["missing"] == [], r["note"]
    core = zipfile.ZipFile(r["files"]["docx"]).read("docProps/core.xml").decode()
    assert f"<dc:title>{STUDY_TITLE}</dc:title>" in core
    first = pypdf.PdfReader(r["files"]["pdf"]).pages[0].extract_text()
    assert " ".join(first.split()).startswith(STUDY_TITLE)
    import docx

    paras = [p.text for p in docx.Document(str(r["files"]["docx"])).paragraphs]
    assert any(p.startswith("Results:") for p in paras) and any(p.startswith("Conclusions:") for p in paras)
