"""The manuscript text from the versioned template: numbers from the run, numbering from the layout, nothing private in the package."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("jinja2")

from omop_etl import manuscript_text as mtx  # noqa: E402
from omop_etl.manuscript_report import read_workbook_tables  # noqa: E402
from test_table6_missing_and_compare import _full_workbook  # noqa: E402

TEMPLATE = mtx.TEMPLATES / "v0.32.0"


@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    d = tmp_path_factory.mktemp("wb")
    return _full_workbook(d / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})


def _render(workbook, **kw):
    tabs = {k: v for k, v in read_workbook_tables(workbook).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"], "decisions": {"table6_missing": "indicator", "table6_increments": "clinical"},
           "bootstraps": 100, "environment": {"python": "3.13.17", "omop-duck-db": "0.32.0"}}
    return mtx.full_manuscript_markdown(tabs, [], workbook=workbook, run=run, summary={"n_features": 95}, **kw)


def test_template_selection_follows_the_installed_version():
    assert "0.32.0" in mtx.available_templates()
    assert mtx.template_dir("0.32.0").name == "v0.32.0" and mtx.template_dir("9.0.0").name == f"v{mtx.available_templates()[-1]}"
    assert mtx.template_dir("0.1.0").name == f"v{mtx.available_templates()[0]}"  # older than every template: the oldest


def test_numbers_come_from_the_workbook_and_tables_are_numbered_in_reading_order(workbook):
    out = _render(workbook)
    md = out["markdown"]
    assert "AUC-ROC 0.731 (0.728–0.735)" in md or "HistGBM 0.731 (0.728–0.735)" in md
    # the workbook's Table 6 (regression) is the fifth table of the text, and its references say so
    assert out["numbers"]["lr"] == "Table 5" and "presented in Table 5" in md and "**Table 5:" in md
    assert out["numbers"]["loso"] == "Table 4" and "(mean AUC-ROC" in md
    assert "January 1, 2017" in md and "September 30, 2022" in md and "95 features entered the models" in md
    assert "Site A through Site C" in md and "three" in md


def test_missing_values_are_marked_and_listed_not_guessed(workbook):
    tabs = {k: v for k, v in read_workbook_tables(workbook).items() if k.startswith("Table 1")}
    out = mtx.full_manuscript_markdown(tabs, [], workbook=workbook, run={}, summary={})
    assert out["missing"] and all(isinstance(m, str) for m in out["missing"])
    assert mtx.MISSING_MARK.format(out["missing"][0]) in out["markdown"]


def test_private_details_come_only_from_params(workbook):
    text = (TEMPLATE / "manuscript.md.j2").read_text(encoding="utf-8")
    assert "IRB Protocol #" not in text and "University" not in text  # no review board or institution in the package
    plain = _render(workbook)["markdown"]
    assert "supplied by the study team" in plain
    own = _render(workbook, params={"irb_statement": "Approved by the review board of the coordinating center.",
                                   "authors": [{"name": "A. Author", "role": "Design"}]})["markdown"]
    assert "Approved by the review board of the coordinating center." in own and "- A. Author: Design" in own


def test_compare_prose_counts_identical_numbers_only_reworded_and_absent():
    ref = ("Abstract\nThe cohort comprised 225,939 adult patients with diabetes at five health systems in the network.\n"
           "The models were trained on balanced folds and scored on the natural distribution of outcomes.\n"
           "This sentence about something else entirely is not in the rendered text at all today.\n")
    md = ("## Abstract\n\nThe cohort comprised 1,500 adult patients with diabetes at five health systems in the network.\n\n"
          "The models were trained on balanced folds and then scored on the natural distribution of all outcomes.\n")
    r = mtx.compare_prose(ref, md)["answers"]
    assert (r["identical"], r["numbers_only"], r["reworded"], r["absent"]) == (0, 1, 1, 1)
    r2 = mtx.compare_prose(ref.replace("225,939", "1,500"), md)["answers"]
    assert r2["identical"] == 1


def test_document_full_profile_and_tables_only(workbook, tmp_path):
    from omop_etl.manuscript_document import render_manuscript_document

    full = render_manuscript_document(workbook, tmp_path / "full", formats=[], cite_software=False, run={"n_sites": 3})
    assert full["profile"] == "full" and full["template"] == "0.32.0" and "## 2 Methods" in full["qmd"].read_text(encoding="utf-8")
    tab = render_manuscript_document(workbook, tmp_path / "tab", formats=[], cite_software=False, profile="tables_only")
    assert "## Tables" in tab["qmd"].read_text(encoding="utf-8") and tab["template"] is None
    with pytest.raises(ValueError):
        render_manuscript_document(workbook, tmp_path / "x", formats=[], profile="poster")


def test_check_script_runs_on_a_pdf(workbook, tmp_path):
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    pdf = tmp_path / "delivered.pdf"
    with PdfPages(pdf) as pp:
        fig = plt.figure()
        fig.text(0.05, 0.5, "Abstract\nHospital readmission within 30 days of inpatient discharge is a primary metric of healthcare quality.",
                 fontsize=6)
        pp.savefig(fig)
        plt.close(fig)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import check_manuscript_text

    ans = tmp_path / "a.json"
    assert check_manuscript_text.main(["--workbook", str(workbook), "--pdf", str(pdf), "--answers", str(ans),
                                       "--detail", str(tmp_path / "d.md"), "--rendered", str(tmp_path / "r.md")]) == 0
    a = json.loads(ans.read_text())
    assert a["template"] == "0.32.0" and a["reference_sentences"] >= 1 and "by_section" in a


@pytest.mark.parametrize("labels, span, word", [
    ([f"Site {c}" for c in "ABCDEFGH"], "Site A through Site H", "eight"),
    ([f"Site {c}" for c in "ABCDEFGHIJKL"], "Site A through Site L", "12"),
    (["Site C", "Site A", "Site Q"], "Site A, Site C, and Site Q", "three"),
])
def test_any_number_of_sites_and_labels(workbook, labels, span, word):
    tabs = {k: v for k, v in read_workbook_tables(workbook).items() if not k.startswith("Manifest")}
    run = {"n_sites": len(labels), "labels": labels, "window": ["2017-01-01", "2022-09-30"], "decisions": {}}
    md = mtx.full_manuscript_markdown(tabs, [], workbook=workbook, run=run, summary={})["markdown"]
    assert f"anonymized as {span}" in md and f"designated {span}" in md
    assert f"across {word} academic health systems" in md


def test_rendered_text_is_laid_out_like_the_pdf_text():
    """A sub-heading runs into its first paragraph and list items run on from the paragraph above, as in the delivered PDF's text."""
    md = ("## 2 Methods\n\n### 2.1 Cohort Derivation\n\nThe five systems were loaded into the databases here. Criteria were applied in this order:\n\n"
          "1. Index stay: inpatient or emergency stays of adults only.\n\n- Vital Signs: mean blood pressure during the stay.\n\n"
          '![Cohort flowchart caption here.](f.png){#fig-flowchart fig-alt="Figure 1: Cohort flowchart caption here." width="100%"}\n')
    plain = mtx._plain_from_markdown(md)
    assert "2.1 Cohort Derivation\nThe five systems" in plain
    assert "order:\n1. Index stay" in plain and "\n• Vital Signs" in plain and "Figure 1: Cohort flowchart caption here." in plain
    sents = [s for _, s in mtx._sentences_of(plain)]
    assert "Criteria were applied in this order: 1." in sents[1] or any(s.endswith("order: 1.") for s in sents)


def test_hyperparameters_and_cohort_call_come_from_the_code():
    pytest.importorskip("sklearn")
    h = mtx.benchmark_hyperparameters()
    assert h["Random Forest"]["text"] == "n_estimators=100, max_depth=12, min_samples_split=10" and h["Logistic Regression"]["C"] == 1.0
    call = mtx.cohort_call({"min_age": 18, "washin_days": 365}, ["2017-01-01", "2022-09-30"])
    assert call.startswith("define_study_cohort(\n    con,\n") and "    min_age=18," in call and "max_admit_date='2022-09-30'," in call
    assert "sample_stay_rule='random'," in call and "visit_type" not in call  # the run's own parameters, not the defaults


def test_full_text_has_numbered_sections_hyperparameters_the_call_and_figures_numbered_once(workbook):
    pytest.importorskip("sklearn")
    r = _render(workbook)
    md = r["markdown"]
    assert "## 1 Introduction" in md and "### 2.1 Cohort Derivation & Eligibility Criteria" in md and "(Section 2.1.1)" in md
    assert "#### 2.1.1 Exclusion Criteria" in md and "## 5 Ethical Approval" in md
    assert "max_iter=100, learning_rate=0.1, max_depth=6, l2_regularization=1.0" in md and "```python\ndefine_study_cohort(" in md
    assert "$$\\text{AUROC}" in md and "\n\n### " in md and "\n### " not in md.replace("\n\n### ", "")


def test_figure_caption_carries_no_number_quarto_adds_it():
    folder = mtx.template_dir("0.32.0")
    out = mtx.render_manuscript_body({"run": {"n_sites": 5}, "shap": {"n_sample": 2000}}, tables={},
                                     figures={"flowchart": (mtx._FIGURE_CAPTIONS["flowchart"], "flow.png"),
                                              "shap": (mtx._FIGURE_CAPTIONS["shap"], "shap.png")}, version=folder.name.lstrip("v"))
    md = out["markdown"]
    assert '![Cohort selection flowchart.' in md and '{#fig-flowchart fig-alt="Figure 1: Cohort selection flowchart.' in md
    assert "summed over the five health systems" in md and "(Top 15 Predictors, 2,000 Sampled Patients)" in md
    assert "![Figure" not in md
