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
    assert full["profile"] == "full" and full["template"] == "0.32.0" and "## Methods" in full["qmd"].read_text(encoding="utf-8")
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
    (["Site C", "Site A", "Site Q"], "Site A, Site C and Site Q", "three"),
])
def test_any_number_of_sites_and_labels(workbook, labels, span, word):
    tabs = {k: v for k, v in read_workbook_tables(workbook).items() if not k.startswith("Manifest")}
    run = {"n_sites": len(labels), "labels": labels, "window": ["2017-01-01", "2022-09-30"], "decisions": {}}
    md = mtx.full_manuscript_markdown(tabs, [], workbook=workbook, run=run, summary={})["markdown"]
    assert f"anonymized as {span}" in md and f"designated {span}" in md
    assert f"across {word} academic health systems" in md
