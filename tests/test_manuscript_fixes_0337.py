"""0.33.7: Kyle's C9 list for the final draft (2026-11-23). Figure 1 has one random-stay box (the analytic cohort) and the patients
outside the study's draw branch off the arrow into it; Figure 1 can be drawn again from a finished run's files; the Section 2.2 lab
list leaves out columns with no values; and Kyle's text checks of the PDF (omop_etl.manuscript_checks) run here on a rendered
manuscript. All counts are made up."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_document as md  # noqa: E402
from omop_etl.study_run import RANDOM_DRAW_EXCLUDED, figure1_attrition, mark_random_draw, redraw_cohort_flowchart  # noqa: E402

EMPTY = ["lab_potassium", "lab_chloride", "lab_calcium", "lab_platelets", "lab_bilirubin_total",
         "lab_alkaline_phosphatase", "vital_heart_rate", "vital_respiratory_rate", "vital_body_temperature", "vital_oxygen_saturation"]


def _attrition(pinned: bool = True) -> pd.DataFrame:
    rows = []
    for site, (a, b, c, d) in {"s1": (1000, 800, 600, 590), "s2": (500, 400, 300, 297)}.items():
        rows += [{"site": site, "step_name": "Index visit type in (262, 9201)", "subjects_retained": a},
                 {"site": site, "step_name": "Verified 30-day follow-up", "subjects_retained": b},
                 {"site": site, "step_name": "Index admission 2017-01-01 to 2022-09-30, one stay per patient", "subjects_retained": c}]
        if pinned:
            rows.append({"site": site, "step_name": "Stay selected for the patient (pinned list)", "subjects_retained": d})
    return pd.DataFrame(rows)


def text_pdf(path: Path, pages: list[str]) -> Path:
    """A minimal PDF with one line of Helvetica text per page (pypdf reads it back)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for line in pages:
        body = f"BT /F1 11 Tf 40 760 Td ({line}) Tj ET".encode("latin-1")
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body.decode('latin-1')}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


def _texts(fig) -> list[str]:
    return [" ".join(t.get_text().split()) for t in fig.axes[0].texts]


# ------------------------------------------------------------------ Figure 1
def test_figure1_has_one_random_stay_box_and_the_draw_branches_off_the_last_arrow():
    pytest.importorskip("matplotlib")
    from omop_etl.study_report import flowchart_overlaps, plot_cohort_flowchart

    f = figure1_attrition(_attrition(), "2017-01-01", "2022-09-30")
    assert list(f["into_final"]) == [False, False, False, True]
    assert int(f["subjects_dropped"].iloc[-1]) == 13  # the run record keeps the step's counts
    fig = plot_cohort_flowchart(f, 887, 92)
    texts = _texts(fig)
    assert not any("pinned" in t.lower() for t in texts)
    assert not any(t.startswith("Randomly selected") for t in texts)  # no box of its own
    assert sum("randomly drawn stay per patient" in t for t in texts) == 1
    assert texts[-3] == "Analytic cohort: one randomly drawn stay per patient N = P = 887"
    assert f"{RANDOM_DRAW_EXCLUDED} P = 13 patients" in texts
    assert any(t.startswith("Grant protocol window: index admission January 1, 2017 to September 30, 2022 P = 900 patients") for t in texts)
    assert texts[-2:] == ["30-day readmission N = 92 (10.4%)", "No readmission N = 795 (89.6%)"]
    # every exclusion branches off a down arrow: one horizontal arrow per exclusion note, each starting on the centre line
    arrows = [p for p in fig.axes[0].patches if type(p).__name__ == "FancyArrowPatch"]
    notes = [t for t in texts if t.startswith("Excluded")]
    starts = {round(a._posA_posB[0][0], 3) for a in arrows if a._posA_posB[0][1] == a._posA_posB[1][1] and a._posA_posB[1][0] > 5}
    assert len(notes) == 3 and len([a for a in arrows if a._posA_posB[0][1] == a._posA_posB[1][1] and a._posA_posB[1][0] > 5]) == 3
    assert starts == {round((0.2 + 5.1) / 2, 3)}
    assert flowchart_overlaps(fig) == []


def test_figure1_without_a_pinned_list_ends_at_the_window():
    pytest.importorskip("matplotlib")
    from omop_etl.study_report import plot_cohort_flowchart

    f = figure1_attrition(_attrition(pinned=False), "2017-01-01", "2022-09-30")
    assert not f["into_final"].any()
    texts = _texts(plot_cohort_flowchart(f, 900, 90))
    assert not any(RANDOM_DRAW_EXCLUDED in t for t in texts) and "Analytic cohort: one randomly drawn stay per patient N = P = 900" in texts


def test_into_final_steps_must_come_last():
    pytest.importorskip("matplotlib")
    from omop_etl.study_report import plot_cohort_flowchart

    f = figure1_attrition(_attrition(), "2017-01-01", "2022-09-30")
    f = pd.concat([f.iloc[[0, 3]], f.iloc[[1, 2]]], ignore_index=True)
    with pytest.raises(ValueError, match="into_final"):
        plot_cohort_flowchart(f, 887, 92)


def test_redraw_from_a_finished_runs_files(tmp_path):
    """A C8-style run folder (the 0.33.5 figure step name in its CONSORT csv) gets the new figure with no data access."""
    pytest.importorskip("matplotlib")
    from omop_etl.study_report import plot_cohort_flowchart

    old = figure1_attrition(_attrition(), "2017-01-01", "2022-09-30")
    old["step_name"] = old["step_name"].astype(str)
    old.drop(columns=["excluded_label", "excluded_shown", "into_final"]).to_csv(tmp_path / "Study_CONSORT_Attrition.csv", index=False)
    pd.DataFrame([{"Variable": "N", "Level": "", "Overall": 887, "readmitted_30d=0": 795, "readmitted_30d=1": 92, "p": ""}]).to_csv(
        tmp_path / "Study_Table1.csv", index=False)
    seen = {}
    import omop_etl.study_report as sr

    real = sr.plot_cohort_flowchart

    def spy(att, n, k, **kw):
        seen["fig"] = real(att, n, k, **kw)
        return seen["fig"]

    sr.plot_cohort_flowchart = spy
    try:
        out = redraw_cohort_flowchart(tmp_path)
    finally:
        sr.plot_cohort_flowchart = real
    assert out == tmp_path / "Study_Figure1_Cohort_Flowchart.png" and out.stat().st_size > 1000
    texts = _texts(seen["fig"])
    assert f"{RANDOM_DRAW_EXCLUDED} P = 13 patients" in texts and "Analytic cohort: one randomly drawn stay per patient N = P = 887" in texts
    assert "Excluded: no qualifying index admission in the grant protocol window P = 0 patients" not in texts
    assert any(t.startswith("Excluded: no qualifying index admission in the grant protocol window") for t in texts)
    assert mark_random_draw(pd.read_csv(tmp_path / "Study_CONSORT_Attrition.csv"))["into_final"].sum() == 1
    assert plot_cohort_flowchart is real


# ------------------------------------------------------------------ rendered manuscript and Kyle's checks
def _manuscript(tmp_path) -> Path:
    pytest.importorskip("jinja2")
    import test_table6_missing_and_compare as t6
    from omop_etl import manuscript_tables as mt
    from omop_etl.study_report import plot_cohort_flowchart

    real_wb = mt.manuscript_workbook

    def with_f1(df, tables, path, **kw):  # the delivered LOSO table reports F1 (Dr. Rubin, 0.33.0); the shared fixture predates it
        t = dict(tables)
        t["LOSO_By_Site"] = t["LOSO_By_Site"].assign(f1_050=[0.31, 0.3, 0.29])
        return real_wb(df, t, path, **kw)

    mt.manuscript_workbook = with_f1
    real_feat = t6.make_features
    t6.make_features = lambda n_=900, seed=4, **kw: real_feat(n=3000, seed=seed)
    try:
        wb = t6._full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    finally:
        mt.manuscript_workbook, t6.make_features = real_wb, real_feat
    (tmp_path / "Study_summary.json").write_text(json.dumps({"n_features": 95, "n_columns": 110, "empty_columns": EMPTY}), encoding="utf-8")
    pd.DataFrame({"Variable": EMPTY + ["lab_sodium", "lab_hemoglobin"], "n_missing": [3000] * 10 + [12, 30],
                  "pct_missing": [100.0] * 10 + [0.4, 1.0]}).to_csv(tmp_path / "Study_Missingness.csv", index=False)
    plot_cohort_flowchart(figure1_attrition(_attrition(), "2017-01-01", "2022-09-30"), 887, 92, path=tmp_path / "Study_Figure1_Cohort_Flowchart.png")
    return wb


def test_section_22_lab_list_leaves_out_empty_columns(tmp_path):
    from omop_etl.manuscript_text import manuscript_context

    ctx = manuscript_context(_manuscript(tmp_path), run={"n_sites": 3})
    labs = [x.lower() for x in ctx["lr"]["labs"]]
    assert "hemoglobin" in labs and not {"potassium", "chloride", "calcium", "platelets", "total bilirubin", "alkaline phosphatase"} & set(labs)
    assert len(ctx["features"]["empty"]) == 10


@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_final_draft_checks_pass_on_a_rendered_manuscript(tmp_path):
    pytest.importorskip("pypdf")
    from omop_etl.manuscript_checks import TITLE, final_draft_checks

    wb = _manuscript(tmp_path)
    params = {"title": TITLE, "authors": [{"name": "A. Author, PhD", "role": "Design."}, {"name": "B. Author, MD", "role": "Revision."}]}
    r = md.render_manuscript_document(wb, tmp_path / "doc", params=params, run={"n_sites": 3, "window": ["2017-01-01", "2022-09-30"]},
                                      window=("2017-01-01", "2022-09-30"))
    assert r["missing"] == [], r["note"]
    res = final_draft_checks(r["files"]["pdf"])
    assert [x for x in res if not x["ok"]] == [], res
    assert len(res) == 17


def test_final_draft_checks_catch_each_problem(tmp_path):
    pytest.importorskip("pypdf")
    from omop_etl.manuscript_checks import final_draft_checks

    pdf = text_pdf(tmp_path / "bad.pdf", ["A different title", "Excluded: no stay on the pinned list", "Hemoglobin (last value) lab_bun_last",
                                          "Table 3: LOSO Brier", "3.4 Multivariable", "Table 4: OR Reference", "3.5 SHAP",
                                          "potassium potassium"])
    failed = {x["check"] for x in final_draft_checks(pdf) if not x["ok"]}
    assert failed == {"title on page 1", "no 'pinned'", "no *_last labs", "Table 4 reference rows", "Table 3 has F1, no Brier",
                      "references end on the last page", "empty source columns named once", "no snake_case column names",
                      "Table 1 SDI Missing / Unknown row"}
