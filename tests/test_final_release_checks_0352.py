"""0.35.2: the PR 108 / C20r polish and Kyle's release gates for the final submission run (2026-11-25). Section 2.3 names a counted
unit once ("10 prior outpatient visits"); the PDF and Excel round a tie the same way; a table caption stays with its table; the
tuning grid is kept on one page; manuscript_checks gains workbook, PDF-versus-workbook, Word and run-record checks; the converter
keeps the operator of a censored lab value; lab_text_profile() says what a lab row without a number holds. All data are made up."""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_document as md  # noqa: E402


# ------------------------------------------------------------------ Section 2.3 increments
def test_increment_phrases_name_a_counted_unit_once():
    from omop_etl.manuscript_checks import _repeats_its_unit, increment_in_table
    from omop_etl.manuscript_text import _increment_phrase

    assert _increment_phrase("10 visits", "prior outpatient visits") == "10 prior outpatient visits"
    assert _increment_phrase("10 encounters", "prior outpatient encounters") == "10 prior outpatient encounters"
    assert _increment_phrase("10 mmHg", "blood pressure") == "10 mmHg of blood pressure"
    assert _increment_phrase("5 mEq/L", "sodium") == "5 mEq/L of sodium"
    t4 = "Systolic Blood Pressure (mmHg) 10 mmHg 0.98 Prior Outpatient Visit Count 10 visits 1.01"
    assert increment_in_table("10 prior outpatient visits", t4) and increment_in_table("10 mmHg of blood pressure", t4)
    assert not increment_in_table("10 outpatient encounters", t4)       # C20: an increment Table 4 does not print
    assert not increment_in_table("5 prior outpatient visits", t4)
    assert _repeats_its_unit("10 visits of prior outpatient visits") and not _repeats_its_unit("10 mmHg of blood pressure")


def _wb(tmp_path, stay_auc=(0.695, 0.7149), grid_auc=0.695):
    """The synthetic manuscript workbook with a stay-type table and a tuning grid whose AUC-ROCs sit on a rounding tie."""
    import test_manuscript_fixes_0337 as t37

    from omop_etl import manuscript_tables as mt

    real = mt.manuscript_workbook

    def with_ties(df, tables, path, **kw):
        t = dict(tables)
        t["By_Stay_Type_AUC"] = pd.DataFrame({"Subset": ["First admission drawn", "Later admission drawn"], "Stays": [1800, 1200],
                                              "Share": [0.6, 0.4], "Readmit rate": [0.08, 0.15], "HistGBM AUC": list(stay_auc)})
        t["Tuning_All_Configs"] = pd.DataFrame({"Model": ["Logistic Regression", "Logistic Regression"], "Config": ["C=1", "C=10"],
                                                "AUC": [grid_auc, 0.701], "F1": [0.291, 0.3], "auc_raw": [grid_auc, 0.70104], "f1_raw": [0.2911, 0.3]})
        return real(df, t, path, **kw)

    mt.manuscript_workbook = with_ties
    try:
        return t37._manuscript(tmp_path)
    finally:
        mt.manuscript_workbook = real


# ------------------------------------------------------------------ rounding: PDF and Excel agree
def test_pdf_and_excel_agree_on_half_ties(tmp_path):
    pytest.importorskip("openpyxl")
    from omop_etl.manuscript_checks import workbook_checks
    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_tables import excel_text
    from omop_etl.manuscript_text import full_manuscript_markdown, metric_cells

    assert f"{0.695:.2f}" == "0.69" and excel_text(0.695) == "0.70"      # the C20r tie: Python vs Excel
    assert excel_text(0.125) == "0.13" and excel_text(-0.0001) == "0.00" and excel_text(2) == "2.00"
    printed = metric_cells(pd.DataFrame({"m": ["A", "B", "C"], "auc": [0.695, 0.7, 0]}))
    assert printed["auc"].tolist() == ["0.70", "0.70", "0.00"]              # an int 0 in a metric column prints 0.00 too
    wb = _wb(tmp_path)
    tabs = read_workbook_tables(wb)
    s1 = next(t for k, (_, t) in tabs.items() if k.startswith("Table 4"))
    stored = s1[[c for c in s1.columns if "AUC" in c][0]].tolist()
    assert all(abs(v - round(v, 2)) < 1e-9 for v in stored)               # stored at METRIC_DECIMALS (was .round(3))
    assert {r["check"]: r["ok"] for r in workbook_checks(wb)}["workbook metrics stored to 2 decimals"]
    text = full_manuscript_markdown(tabs, [], workbook=wb, run={"n_sites": 3})["markdown"]
    sec = text[text.find("Cohort Partition by Whether"):text.find("Missingness and Measurement")]
    for v in stored:                                                        # the PDF prints what Excel shows (0.00 format)
        assert excel_text(v) in sec
    grid = text[text.find("Full Grid Evaluation Metrics"):]
    assert "0.70" in grid and "0.69 " not in grid                           # a 3-decimal grid cell 0.695 prints as Excel rounds it


# ------------------------------------------------------------------ workbook checks
def test_workbook_checks_catch_int_cells_long_values_and_table1_gaps(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from omop_etl.manuscript_checks import workbook_checks

    wb = _wb(tmp_path)
    assert all(r["ok"] for r in workbook_checks(wb)), workbook_checks(wb)
    assert len(workbook_checks(wb)) == 4
    book = openpyxl.load_workbook(wb)
    ws = book["Table 5 - Cross-Site LOSO"]
    cell = next(c for row in ws.iter_rows(min_row=4) for c in row if isinstance(c.value, float) and c.number_format == "0.00")
    cell.value, cell.number_format = 0, "General"                          # an unformatted int 0 read back by openpyxl
    flag = next(c for row in ws.iter_rows(min_row=4) for c in row if isinstance(c.value, str))
    flag.value = True                                                      # a bool is not a metric
    ws4 = book["Table 4 - Stay Type Decomp"]
    ws4.cell(row=4, column=5).value = 0.695                                # stored with more places than shown
    t1 = book["Table 1 - Descriptive"]
    for row in t1.iter_rows(min_row=4):
        if row[0].value == "SDI Quintile: Missing / Unknown":            # a dropped category (C20)
            t1.delete_rows(row[0].row)
            break
    s2 = book["Table S2 - Data Completeness"]
    for row in s2.iter_rows(min_row=4):
        if row[0].value == "BMI: Missing / Unknown":
            row[1].value = "999 (33.3%)"
    book.save(tmp_path / "bad.xlsx")
    res = {r["check"]: r for r in workbook_checks(tmp_path / "bad.xlsx")}
    assert not res["workbook metrics shown to 2 decimals"]["ok"] and "Table 5" in res["workbook metrics shown to 2 decimals"]["detail"]
    assert not res["workbook metrics stored to 2 decimals"]["ok"] and "0.695" in res["workbook metrics stored to 2 decimals"]["detail"]
    assert not res["Table 1 categories sum to 100%"]["ok"] and "SDI" in res["Table 1 categories sum to 100%"]["detail"]
    assert not res["Table 1 Missing / Unknown counts match Table S2"]["ok"]


# ------------------------------------------------------------------ captions and pages
def test_captions_stay_with_their_tables_in_the_markdown(tmp_path):
    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_text import full_manuscript_markdown, template_dir

    layout = json.loads((template_dir("0.35.2") / "layout.json").read_text(encoding="utf-8"))
    assert {"grid", "los", "stay_type", "completeness"} <= set(layout["keep_together"])
    wb = _wb(tmp_path)
    out = full_manuscript_markdown(read_workbook_tables(wb), [], workbook=wb, run={"n_sites": 3, "bootstraps": 1000})
    text = out["markdown"]
    assert out["template"] == "0.35.2"
    t1 = text.find("**Table 1:")
    assert t1 > 0 and "#block(sticky: true)[" in text[t1 - 60:t1]       # a table that may break keeps its caption with its rows
    s4 = text.find(f"**{out['numbers']['grid']}:")
    assert "#block(breakable: false)[" in text[s4 - 60:s4]
    assert "1,000 bootstrap replicates" in text


def _mock_qmd(path: Path, sticky: bool) -> Path:
    rows = "\n".join(f"| row {i} | {i * 1.5:.1f} |" for i in range(30))
    table = "| Feature | Value |\n|---|---|\n" + rows
    secs = []
    for k in range(20, 48):  # the caption lands at every height of the page, once at its foot
        cap = f"**Table {k}: A caption for the table**"
        if sticky:
            cap = f"```{{=typst}}\n#block(sticky: true)[\n```\n\n{cap}\n\n```{{=typst}}\n]\n```"
        filler = "\n\n".join(f"Filler line {j}." for j in range(k))
        secs.append(f"{filler}\n\n{cap}\n\n{table}\n\n```{{=typst}}\n#pagebreak()\n```\n")
    path.write_text("---\nformat: typst\n---\n\n" + "\n".join(secs), encoding="utf-8")
    return path


@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_sticky_caption_never_sits_alone_at_the_foot_of_a_page(tmp_path):
    import subprocess

    pytest.importorskip("pypdf")
    from omop_etl.manuscript_checks import _orphan_captions

    q = md.find_renderer()["quarto"]
    for name, sticky in (("plain", False), ("sticky", True)):
        subprocess.run([q, "render", _mock_qmd(tmp_path / f"{name}.qmd", sticky).name, "--to", "typst"], cwd=tmp_path, check=True,
                       capture_output=True)
    assert _orphan_captions(tmp_path / "plain.pdf")                         # the check sees C20r's orphan caption
    assert _orphan_captions(tmp_path / "sticky.pdf") == []                  # the sticky caption moves with its table


def test_pdf_checks_catch_spilled_grid_orphan_caption_and_placeholders(tmp_path):
    pytest.importorskip("pypdf")
    import test_manuscript_fixes_0337 as t37

    from omop_etl.manuscript_checks import final_draft_checks

    pdf = t37.text_pdf(tmp_path / "c20r.pdf", [
        "increment D (e.g., 10 mmHg of blood pressure, 10 visits of prior outpatient visits) or, for indicator variables. "
        "Table 4: Reference 10 mmHg 10 visits 3.5 SHAP",
        "Table S4: Full Grid Evaluation Metrics for All Evaluated Hyperparameter Configurations Model AUC",
        "Logistic Regression C=10 0.70 0.30 Appendix A seed [not in this run: random_state]"])
    res = {x["check"]: x for x in final_draft_checks(pdf)}
    assert not res["Table S4 on one page"]["ok"]
    assert not res["no table caption alone at the foot of a page"]["ok"]
    bad = res["no placeholders or repeated increment words"]
    assert not bad["ok"] and "[not in this run:" in bad["detail"] and "visits of prior outpatient visits" in bad["detail"]
    assert res["Section 2.3 increments are Table 4's"]["ok"]


# ------------------------------------------------------------------ the full release gate on a rendered manuscript
@pytest.mark.skipif(not md.find_renderer()["quarto"], reason="needs Quarto")
def test_release_checks_pass_together_on_a_rendered_manuscript(tmp_path):
    pytest.importorskip("pypdf")
    from omop_etl.manuscript_checks import TITLE, release_checks

    wb = _wb(tmp_path)
    params = {"title": TITLE, "authors": [{"name": "A. Author, PhD", "role": "Design."}, {"name": "B. Author, MD", "role": "Revision."}],
              "irb_statement": "The review board of the study team approved this study."}
    r = md.render_manuscript_document(wb, tmp_path / "doc", params=params, run={"n_sites": 3, "window": ["2017-01-01", "2022-09-30"],
                                                                                "bootstraps": 1000, "random_state": 42},
                                      window=("2017-01-01", "2022-09-30"))
    assert r["missing"] == [], r["note"]
    res = release_checks(r["files"]["pdf"], wb, docx=r["files"]["docx"], params=params, names=["Nowhere General Hospital"])
    assert len(res) == 30
    # the synthetic fixture records no feature counts or tuned configurations, so its text keeps placeholders, which both the PDF
    # and the Word check must report; everything else passes
    placeholder = {"no placeholders or repeated increment words", "DOCX has no placeholders"}
    assert r.get("text_missing")
    assert {x["check"] for x in res if not x["ok"]} == placeholder, [x for x in res if not x["ok"]]


# ------------------------------------------------------------------ Word and run record
def _docx(path: Path, body: str, creator: str = "A. Author, PhD", custom: str = "") -> Path:
    paras = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in body.split("\n"))
    tables = "<w:tbl></w:tbl>" * body.count("Table ")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{paras}{tables}</w:body></w:document>')
        z.writestr("docProps/core.xml", f"<cp:coreProperties><dc:creator>{creator}</dc:creator><cp:lastModifiedBy>{creator}"
                                        "</cp:lastModifiedBy></cp:coreProperties>")
        if custom:
            z.writestr("docProps/custom.xml", f'<Properties><property name="x">{custom}</property></Properties>')
    return path


def test_docx_checks(tmp_path):
    from omop_etl.manuscript_checks import TITLE, _names, docx_checks

    params = {"authors": [{"name": "A. Author, PhD"}], "irb_statement": "Approved by the review board."}
    body = f"{TITLE}\nA. Author, PhD\nApproved by the review board.\nTable 1: x\nTable 2: y\nTable S1: z"
    good = docx_checks(_docx(tmp_path / "good.docx", body), params=params, names=["Nowhere General Hospital"])
    assert all(x["ok"] for x in good), good
    bad = docx_checks(_docx(tmp_path / "bad.docx", "Another title\nTable 1: x\nTable 3: y\n[not in this run: n_binary]",
                            creator="Nowhere General Hospital", custom="secret"), params=params, names=["Nowhere General Hospital"])
    assert {x["check"] for x in bad if not x["ok"]} == {"DOCX title", "DOCX has no placeholders", "DOCX has the authors and review-board statement",
                                                       "DOCX tables numbered and present", "DOCX properties name no site or account"}
    assert "Nowhere" not in json.dumps(bad)                                 # a finding never repeats the private name
    (tmp_path / "names.txt").write_text("# private\nNowhere General Hospital\n", encoding="utf-8")
    (tmp_path / "Study_run_private.json").write_text(json.dumps({"sites": ["Elsewhere Medical Center"]}), encoding="utf-8")
    assert _names([str(tmp_path / "names.txt"), str(tmp_path / "Study_run_private.json")]) == ["Nowhere General Hospital", "Elsewhere Medical Center"]


def test_run_record_checks():
    from omop_etl.manuscript_checks import run_record_checks

    good = {"pinned": False, "privacy_check": {"checked": True, "hits": 0, "unreadable": []}, "data_checks": {"n_unexpected": 0},
            "document": {"missing": [], "text_missing": [], "cites_omop_duck_db": "0.35.2"}, "environment": {"omop-duck-db": "0.35.2"},
            "bootstraps": 1000, "observation_period_cap": {"cap": "2023-12-31", "by_site": {}}}
    assert all(x["ok"] for x in run_record_checks(good, final=True))
    assert len(run_record_checks(good)) == 5 and len(run_record_checks(good, final=True)) == 7
    bad = dict(good, pinned=True, bootstraps=100, privacy_check={"checked": False}, data_checks={"n_unexpected": 3},
               document={"missing": [], "text_missing": ["random_state"], "cites_omop_duck_db": "0.35.1"})
    bad.pop("observation_period_cap")
    assert not any(x["ok"] for x in run_record_checks(bad, final=True))
    assert all(x["ok"] for x in run_record_checks(dict(good, bootstraps=100, observation_period_cap=None)))   # review runs need neither


def test_cli_runs_every_check_given(tmp_path, capsys):
    pytest.importorskip("pypdf")
    import test_manuscript_fixes_0337 as t37

    from omop_etl.manuscript_checks import main

    pdf = t37.text_pdf(tmp_path / "x.pdf", ["A title"])
    run = tmp_path / "Study_run.json"
    run.write_text(json.dumps({"pinned": False}), encoding="utf-8")
    assert main([str(pdf), "--run-json", str(run), "--final"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  run: final bootstraps" in out and re.search(r"\d+ of 27 checks passed", out)


# ------------------------------------------------------------------ unpinned determinism and the bootstrap count
def _stays(n_people: int = 60) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE person AS SELECT i AS person_id, 8507 AS gender_concept_id, 1950 AS year_of_birth, 1 AS month_of_birth, "
                f"1 AS day_of_birth, NULL::TIMESTAMP AS birth_datetime, 0 AS race_concept_id, 0 AS ethnicity_concept_id FROM range(1, {n_people + 1}) t(i)")
    con.execute("CREATE TABLE observation_period AS SELECT person_id AS observation_period_id, person_id, DATE '2015-01-01' AS observation_period_start_date, "
                "DATE '2023-01-01' AS observation_period_end_date, 0 AS period_type_concept_id FROM person")
    con.execute("CREATE TABLE visit_occurrence AS SELECT person_id * 10 + k AS visit_occurrence_id, person_id, 9201 AS visit_concept_id, "
                "DATE '2020-01-01' + CAST(k * 60 AS INTEGER) AS visit_start_date, DATE '2020-01-04' + CAST(k * 60 AS INTEGER) AS visit_end_date, "
                "0 AS discharged_to_concept_id, 'A' AS discharged_to_source_value FROM person, range(0, 3) t(k)")
    con.execute("CREATE TABLE death (person_id BIGINT, death_date DATE)")
    for t in ("condition_occurrence (person_id BIGINT, condition_start_date DATE)", "drug_exposure (person_id BIGINT, drug_exposure_start_date DATE)",
              "measurement (person_id BIGINT, measurement_date DATE)"):
        con.execute(f"CREATE TABLE {t}")
    return con


def test_unpinned_draw_is_deterministic_given_the_seed():
    from omop_etl.cohort import define_study_cohort

    kw = dict(materialise=False, sample_stay_rule="random", washin_days=0, min_admit_date="2019-01-01", max_admit_date="2021-12-31")
    a = define_study_cohort(_stays(), random_seed=42, **kw).sort_values("subject_id")["visit_occurrence_id"].tolist()
    b = define_study_cohort(_stays(), random_seed=42, **kw).sort_values("subject_id")["visit_occurrence_id"].tolist()
    c = define_study_cohort(_stays(), random_seed=7, **kw).sort_values("subject_id")["visit_occurrence_id"].tolist()
    assert len(a) == 60 and a == b
    assert a != c                                                           # the seed, not the order of the rows, picks the stay
    assert len({v % 10 for v in a}) > 1                                    # not always the first stay


def test_run_is_reproducible_and_prints_its_bootstrap_count(tmp_path, monkeypatch):
    from test_multisite_anonymous import _study

    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_text import manuscript_context, render_manuscript_body
    from omop_etl.study_run import run_study

    db, keys = _study(tmp_path, monkeypatch, 3)
    outs, texts, books = [], [], []
    for name in ("a", "b"):
        out = tmp_path / name
        run_study(db, out, sites=keys, n_bootstraps=12, loso=False, figures=False, profile=None, document=False, random_state=42)
        rec = json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
        assert rec["bootstraps"] == 12 and rec["random_state"] == 42 and rec["pinned"] is False
        rec.pop("timings_seconds")
        body = render_manuscript_body(manuscript_context(out / "Study_Manuscript_Tables.xlsx", run=rec))["markdown"]
        texts.append(body)
        books.append({k: t.to_csv(index=False) for k, (_, t) in read_workbook_tables(out / "Study_Manuscript_Tables.xlsx").items()})
        outs.append(out)
    assert books[0] == books[1]                                             # Tables 1 to 6 and the supplement, cell for cell
    assert texts[0] == texts[1]
    flat = " ".join(texts[0].split())
    assert "Confidence intervals come from 12 bootstrap replicates" in flat
    assert "12 replicates in which patients were resampled" in flat         # Appendix A
    for s in keys:
        a = pd.read_parquet(Path(str(outs[0]) + "_private") / f"cohort_{s}.parquet")
        b = pd.read_parquet(Path(str(outs[1]) + "_private") / f"cohort_{s}.parquet")
        pd.testing.assert_frame_equal(a, b)


# ------------------------------------------------------------------ lab rows without a number
def test_censored_lab_strings_and_funnel_counts(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import site_quality_report as sq

    from omop_etl.build_omop_cdm import LAB_OPERATOR_SQL, LAB_VALUE_SQL
    from omop_etl.lab_text import lab_text_profile

    con = duckdb.connect()
    src = [("<0.01", None), (None, ">14.0"), ("", ">= 5"), ("POSITIVE", None), (None, None), ("1,200", None), (None, "7.5 H"), ("6.5", "x")]
    con.execute("CREATE TABLE src (RESULT_NUM VARCHAR, RAW_RESULT VARCHAR)")
    con.executemany("INSERT INTO src VALUES (?, ?)", [list(r) for r in src])
    got = con.execute(f"SELECT {LAB_VALUE_SQL}, {LAB_OPERATOR_SQL} FROM src").fetchall()
    assert got == [(0.01, 4171756), (14.0, 4172704), (5.0, 4171755), (None, 0), (None, 0), (1200.0, 0), (None, 0), (6.5, 0)]

    db = tmp_path / "db"
    db.mkdir()
    con = duckdb.connect(str(db / "omop_s1.duckdb"))
    vals = ["6.5", "7.1", "<4.0", ">14.0", "POSITIVE", "see comment", None, "7.5 H", "", "8.0"]
    num = [6.5, 7.1, 4.0, 14.0, None, None, None, None, None, 8.0]
    rows = ", ".join(f"(3004410, '4548-4', {'NULL' if n is None else n}, {'NULL' if v is None else repr(v)}, DATE '2020-06-01', '%')"
                     for n, v in zip(num, vals))
    con.execute(f"CREATE TABLE measurement AS SELECT * FROM (VALUES {rows}) t(measurement_concept_id, measurement_source_value, value_as_number, "
                "value_source_value, measurement_date, unit_source_value)")
    con.execute("""CREATE TABLE concept AS SELECT * FROM (VALUES
        (3004410, 'Hemoglobin A1c/Hemoglobin.total in Blood', 'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4', DATE '1970-01-01', DATE '2099-12-31', NULL))
        t(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason)""")
    con.execute("CREATE TABLE concept_ancestor AS SELECT concept_id AS ancestor_concept_id, concept_id AS descendant_concept_id, "
                "0 AS min_levels_of_separation, 0 AS max_levels_of_separation FROM concept")
    con.execute("CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR, "
                "valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)")
    p = lab_text_profile(con, ["hba1c"], start="2020-01-01", end="2020-12-31").set_index("name").loc["hba1c"]
    con.close()
    assert p["rows"] == 10 and p["pct_no_number"] == 50.0
    assert (p["pct_blank"], p["pct_qualitative"], p["pct_number_with_flag"], p["pct_parseable"]) == (40.0, 40.0, 20.0, 20.0)
    f = sq.lab_funnel(db, {"s1": "Site A"}, "2020-01-01", "2020-12-31", float("inf")).set_index("name").loc["hba1c"]
    assert f["source_rows"] == 10 and f["pct_no_number"] == 50.0                # the site report's non-numeric share is exact


def test_reproduce_study_merge_episodes_flag(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import reproduce_study as rs

    from omop_etl.study_run import PROTOCOL_COHORT_PARAMS

    seen = []

    def fake(*a, **kw):
        seen.append(kw)
        raise SystemExit(0)

    monkeypatch.setattr(rs, "run_study", fake)
    base = ["--db-dir", str(tmp_path), "--out", str(tmp_path / "o"), "--sites", "s1"]
    for extra in (["--merge-episodes-days", "0"], []):
        with pytest.raises(SystemExit):
            rs.main(base + extra)
    assert seen[0]["cohort_params"] == dict(PROTOCOL_COHORT_PARAMS, merge_episodes_days=0)
    assert seen[1]["cohort_params"] is None                                  # the primary run keeps the protocol cohort


def test_example_manuscript_params_file_reads_and_holds_only_placeholders():
    from omop_etl.manuscript_document import _param_authors
    from omop_etl.manuscript_text import load_params

    p = load_params(Path(__file__).resolve().parents[1] / "inst" / "extdata" / "manuscript_params.example.json")
    assert [a["name"] for a in _param_authors(p)] == ["First Author, MD", "Second Author, PhD"]
    assert "<Institution>" in p["irb_statement"] and p["consortium"].startswith("<")


def _plain_pdf(path: Path, text: str, chunk: int = 2500) -> Path:
    """A text-layer PDF of ``text`` (Latin-1 only, parentheses escaped), one chunk per page."""
    import test_manuscript_fixes_0337 as t37

    t = " ".join(text.replace("±", "+/-").replace("–", "-").replace("−", "-").split())
    t = t.encode("latin-1", "replace").decode("latin-1").replace("\\", "").replace("(", "\\(").replace(")", "\\)")
    pages = [t[i:i + chunk] for i in range(0, len(t), chunk)]
    return t37.text_pdf(path, [p.rstrip("\\") for p in pages])


def test_pdf_numbers_must_match_the_workbook(tmp_path):
    pytest.importorskip("pypdf")
    from omop_etl.manuscript_checks import pdf_workbook_checks, release_checks
    from omop_etl.manuscript_report import read_workbook_tables
    from omop_etl.manuscript_text import full_manuscript_markdown

    wb = _wb(tmp_path)
    md_ = full_manuscript_markdown(read_workbook_tables(wb), [], workbook=wb, run={"n_sites": 3})["markdown"]
    plain = re.sub(r"```\{=typst\}.*?```", " ", md_, flags=re.S).replace("|", " ").replace("*", "")
    good = pdf_workbook_checks(_plain_pdf(tmp_path / "good.pdf", plain), wb)
    assert good[0]["ok"], good
    s1 = plain.find("Cohort Partition by Whether")
    bad_text = plain[:s1] + plain[s1:].replace("0.71", "0.72", 1)        # a stay-type AUC-ROC that differs from the workbook
    bad = pdf_workbook_checks(_plain_pdf(tmp_path / "bad.pdf", bad_text), wb)
    assert not bad[0]["ok"] and "stay-type table 0.71" in bad[0]["detail"]
    run = tmp_path / "Study_run.json"
    run.write_text(json.dumps({"pinned": False}), encoding="utf-8")
    res = release_checks(tmp_path / "good.pdf", wb, run_json=run)
    assert len(res) == 20 + 4 + 1 + 5
