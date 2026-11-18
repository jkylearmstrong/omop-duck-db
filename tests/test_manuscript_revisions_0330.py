"""0.33.0 manuscript revisions requested on 2026-11-18: Figure 1 exclusions branch off the down arrow, the tuning-comparison
table is no longer printed, the cross-site table reports F1 instead of Brier, the regression table prints the reference category of
each multilevel feature, and discharge status separates "Other" from "Missing / Unknown"."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_tables as mt  # noqa: E402
from omop_etl import study_report as sr  # noqa: E402
from omop_etl.benchmark_features import extract_benchmark_features  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402

MULTILEVEL_REFERENCES = ["Age: 18–44 years", "Race: White", "SDI Quintile 1 (Least Deprived)", "RUCA: Metropolitan (1.0)",
                         "Discharge Status: Home", "Tobacco: Never user", "BMI: Normal (18.5–24.9 kg/m²)"]


# ------------------------------------------------------------------ Figure 1
def test_flowchart_exclusions_branch_off_the_down_arrow():
    pytest.importorskip("matplotlib")
    from matplotlib.patches import FancyArrowPatch, Rectangle

    att = pd.DataFrame({"step_name": ["Index", "Age >= 18", "Prior observation >= 365 days", "Diabetes"],
                        "subjects_retained": [1000, 900, 700, 600], "subjects_dropped": [0, 100, 200, 100]})
    fig = sr.plot_cohort_flowchart(att, 600, 90)
    ax = fig.axes[0]
    boxes = sorted([p for p in ax.patches if isinstance(p, Rectangle) and p.get_x() < 1], key=lambda r: -r.get_y())
    arrows = [p for p in ax.patches if isinstance(p, FancyArrowPatch)]
    ends = [(tuple(a._posA_posB[0]), tuple(a._posA_posB[1])) for a in arrows]
    down = [(a, b) for a, b in ends if a[0] == b[0] and a[1] > b[1]]
    side = [(a, b) for a, b in ends if a[1] == b[1] and b[0] > a[0]]
    assert len(side) == 3  # one exclusion per step that dropped patients
    for (x0, y), _ in side:
        # starts on a down arrow, at a height between the two boxes that arrow joins
        assert any(da[0] == x0 and db[1] < y < da[1] for da, db in down)
        assert not any(b.get_y() <= y <= b.get_y() + b.get_height() for b in boxes)
    texts = [t.get_text().replace("\n", " ") for t in ax.texts]
    assert any(t.startswith("Excluded: did not meet Prior observation >= 365 days") and "P = 200 patients" in t for t in texts)
    # long names wrap, they are not cut
    long = att.assign(step_name=["Index", "Age >= 18", "A very long eligibility criterion name that goes on and on past the box", "Diabetes"])
    texts = " ".join(t.get_text() for t in sr.plot_cohort_flowchart(long, 600, 90).axes[0].texts).replace("\n", " ")
    assert "goes on and on past the box" in texts and "..." not in texts


# ------------------------------------------------------------------ Table 5 (workbook Table 6): reference rows
@pytest.fixture(scope="module")
def regression():
    return mt.manuscript_regression(make_features(n=3000, seed=7))


def test_every_multilevel_feature_prints_its_reference_row(regression):
    t = regression.set_index("Feature")
    for ref in MULTILEVEL_REFERENCES:
        assert t.loc[ref, "Increment"] == "Reference" and t.loc[ref, "Adjusted OR (95% CI)*"] == mt.REFERENCE_OR
        assert pd.isna(t.loc[ref, "P-Value"]) and pd.isna(t.loc[ref, "Standardized Beta"])
    feats = regression["Feature"].tolist()
    # the reference comes before the levels compared with it; BMI keeps its natural order (underweight, normal, overweight, obese)
    assert feats.index("Age: 18–44 years") < feats.index("Age: 45–64 years")
    assert feats.index("BMI: Underweight (<18.5 kg/m²)") < feats.index("BMI: Normal (18.5–24.9 kg/m²)") < feats.index("BMI: Overweight (25.0–29.9 kg/m²)")
    assert (t.loc["Race: Black / African American", "Increment"] == "Vs. reference" and t.loc["Sex: Male", "Increment"] == "Yes vs. no")
    assert t.loc["Discharge Status: Other", "Increment"] == "Vs. reference"


def test_reference_rows_do_not_change_the_fit(regression):
    fitted = regression[regression["Increment"].notna() & (regression["Increment"] != "Reference")]
    assert fitted["Adjusted OR (95% CI)*"].str.match(r"^\d").all()
    mt.validate_manuscript_sheets({"Table 6": ("", regression)})  # the reference cell is not read as an interval


def test_sdi_reference_names_missing_when_missing_joins_it():
    t = mt.manuscript_regression(make_features(n=3000, seed=7), sdi_missing_term=False)
    assert "SDI Quintile 1 (Least Deprived) and Missing / Unknown" in t["Feature"].tolist()
    assert "SDI Quintile: Missing / Unknown" not in t["Feature"].tolist()


def test_text_context_skips_reference_rows(tmp_path):
    from omop_etl import manuscript_text as mtx

    df = make_features(n=3000, seed=7)
    path = mt.manuscript_workbook(df, {}, tmp_path / "w.xlsx")["path"]
    from omop_etl.manuscript_report import read_workbook_tables

    lr = mtx._regression(read_workbook_tables(path))
    names = [r["feature"] for r in lr["rows"]]
    assert not set(MULTILEVEL_REFERENCES) & set(names) and "Age: 45–64 years" in names
    assert all(r["group"] in ("Demographics", "Encounter Information", "Vital Signs", "Laboratory values", "Other clinical features") for r in lr["rows"])


# ------------------------------------------------------------------ Table 4 (workbook Table 5): F1 instead of Brier
def _loso(with_f1: bool) -> pd.DataFrame:
    t = pd.DataFrame({"site": ["Site A", "Site B"], "n": [500, 400], "auroc": [0.71, 0.72], "auroc_lo": [0.7, 0.7], "auroc_hi": [0.72, 0.74],
                      "brier_uncalibrated": [0.19, 0.2], "sensitivity_050": [0.6, 0.58], "specificity_050": [0.72, 0.73]})
    return t.assign(f1_050=[0.31, 0.29]) if with_f1 else t


def test_cross_site_table_reports_f1_not_brier(tmp_path):
    df = make_features(n=900, seed=4)
    sheet = mt.manuscript_workbook(df, {"LOSO_By_Site": _loso(True)}, tmp_path / "a.xlsx")["sheets"]["Table 5 - Cross-Site LOSO"]
    assert list(sheet.columns[-3:]) == ["F1", "Sensitivity", "Specificity"] and "Brier" not in sheet.columns
    assert sheet["F1"].tolist() == [0.31, 0.29, 0.3]
    # a results table written by an earlier release has no F1 column: Brier is kept rather than failing
    old = mt.manuscript_workbook(df, {"LOSO_By_Site": _loso(False)}, tmp_path / "b.xlsx")["sheets"]["Table 5 - Cross-Site LOSO"]
    assert "Brier" in old.columns


def test_loso_f1_at_half_matches_its_definition():
    y = np.array([1, 1, 0, 0, 0, 1])
    p = np.array([0.9, 0.4, 0.6, 0.1, 0.2, 0.7])
    pred = p >= 0.5
    tp, fp, fn = (pred & (y == 1)).sum(), (pred & (y == 0)).sum(), (~pred & (y == 1)).sum()
    from sklearn.metrics import f1_score

    assert 2 * tp / (2 * tp + fp + fn) == pytest.approx(f1_score(y, pred))


# ------------------------------------------------------------------ manuscript text: Table 3 dropped
def test_v0330_template_drops_the_tuning_table_and_renumbers(tmp_path):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx
    from omop_etl.manuscript_report import read_workbook_tables
    from test_table6_missing_and_compare import _full_workbook

    wb = _full_workbook(tmp_path / "Study_Manuscript_Tables.xlsx", {"s1": "Site A", "s2": "Site B", "s3": "Site C"})
    tabs = {k: v for k, v in read_workbook_tables(wb).items() if not k.startswith("Manifest")}
    run = {"n_sites": 3, "window": ["2017-01-01", "2022-09-30"], "bootstraps": 100}
    out = mtx.full_manuscript_markdown(tabs, [], workbook=wb, run=run, summary={"n_features": 95}, version="0.33.0")
    md = out["markdown"]
    assert out["template"] == "0.33.0"
    assert "tuning" not in out["numbers"] and out["numbers"]["loso"] == "Table 3" and out["numbers"]["lr"] == "Table 4"
    assert "Hyperparameter Selection Comparison" not in md and "Section 3.2.1" not in md and "3.2.1" not in md
    assert "presented in Table 4" in md and "**Table 4:" in md and "**Table 3:" in md
    assert "reference category (OR 1.00)" in md and "F1, sensitivity and specificity are reported at the 0.50 threshold" in md
    assert "None" not in md.replace("Nonetheless", "")
    assert mtx.template_dir("0.33.0").name == "v0.33.0" and mtx.template_dir("0.32.5").name == "v0.32.0"


# ------------------------------------------------------------------ discharge status: other vs missing
@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE person (person_id BIGINT, year_of_birth INT, month_of_birth INT, day_of_birth INT, gender_concept_id INT, "
              "race_concept_id INT, ethnicity_concept_id INT)")
    yield c
    c.close()


def _cohort(con, rows):
    con.execute("CREATE OR REPLACE TABLE visit_occurrence (visit_occurrence_id BIGINT, discharged_to_concept_id INT, discharged_to_source_value VARCHAR)")
    con.executemany("INSERT INTO visit_occurrence VALUES (?, ?, ?)", rows)
    con.execute("CREATE OR REPLACE TABLE cohort_t AS SELECT 1 AS cohort_definition_id, visit_occurrence_id AS subject_id, visit_occurrence_id, "
                "DATE '2020-01-01' AS cohort_start_date, DATE '2020-01-05' AS cohort_end_date FROM visit_occurrence")


KW = dict(include_labs=False, include_vitals=False, include_medications=False, include_elixhauser=False, include_utilization=False,
          include_protocol_history=False, include_demographics=False, vocabulary_db=None)


def test_ederri_rule_separates_other_from_missing_and_reads_padded_source_values(con):
    # what this package's PCORnet ETL writes: HH 32215, RH 32228, AM 32216, HS 8546; NI / UN / blank have concept 0
    _cohort(con, [(1, 32215, "HH"), (2, 32228, "RH"), (3, 32216, "AM"), (4, 8546, "HS"), (5, 0, "OT"), (6, 0, "NI"), (7, 0, "UN"),
                  (8, 0, None), (9, 0, "  "), (10, 0, "HO  "), (11, 8536, "HO")])
    f = extract_benchmark_features(con, "cohort_t", profile="ederri", **KW).sort_values("visit_occurrence_id")
    cat = f[[c for c in f.columns if c.startswith("disp_")]].idxmax(axis=1).str.replace("disp_", "").tolist()
    assert cat == ["home_health", "snf", "ama", "other", "other", "missing", "missing", "missing", "missing", "home", "home"]
    m = mt.manuscript_frame(f)
    assert m["m_disch"].tolist() == ["Home Health", "SNF / Rehab", "Against Medical Advice", "Other", "Other", "Missing / Unknown",
                                     "Missing / Unknown", "Missing / Unknown", "Missing / Unknown", "Home", "Home"]


def test_etl_concepts_alone_classify_without_a_source_value(con):
    _cohort(con, [(1, 32215, None), (2, 32228, None), (3, 32216, None)])
    f = extract_benchmark_features(con, "cohort_t", profile="ederri", **KW).sort_values("visit_occurrence_id")
    assert f["disp_home_health"].tolist() == [1, 0, 0] and f["disp_snf"].tolist() == [0, 1, 0] and f["disp_ama"].tolist() == [0, 0, 1]
    g = extract_benchmark_features(con, "cohort_t", **KW).sort_values("visit_occurrence_id")  # the generic map knows them too
    assert g["disp_home_health"].tolist() == [1, 0, 0] and g["disp_snf"].tolist() == [0, 1, 0] and g["disp_ama"].tolist() == [0, 0, 1]


def test_table1_and_s2_show_other_and_missing_separately():
    df = make_features(n=900, seed=4)
    t1 = mt.manuscript_table1(df).set_index("Feature")
    assert "Discharge Status: Other" in t1.index and "Discharge Status: Missing / Unknown" in t1.index
    s2 = mt.manuscript_completeness(df).set_index("Feature")
    n_other = int(df["disp_other"].sum())
    assert t1.loc["Discharge Status: Other", "Whole cohort"].startswith(f"{n_other:,} ")
    assert s2.loc["Discharge Status: Missing / Unknown", "Whole cohort"].startswith("0 ")


def test_discharge_status_check_script(tmp_path):
    import json
    import subprocess

    for site, rows in (("s1", [(1, 9201, "2020-01-01", 8536, "HO")] * 12 + [(2, 9201, "2020-02-01", 0, "NI ")] * 12),
                       ("s2", [(3, 262, "2021-01-01", 32215, "HH")] * 3 + [(4, 9202, "2021-01-01", 0, None)] * 5)):
        c = duckdb.connect(str(tmp_path / f"omop_{site}.duckdb"))
        c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, visit_concept_id INT, visit_start_date DATE, "
                  "discharged_to_concept_id INT, discharged_to_source_value VARCHAR)")
        c.executemany("INSERT INTO visit_occurrence VALUES (?, ?, CAST(? AS DATE), ?, ?)", rows)
        c.close()
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_discharge_status.py"
    r = subprocess.run([sys.executable, str(script), "--db-dir", str(tmp_path), "--sites", "s1", "s2", "--answers", str(tmp_path / "a.json"),
                        "--detail", str(tmp_path / "d.md")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    a = json.loads((tmp_path / "a.json").read_text())
    assert a["stays"] == [{"n": 27, "pct": None}]  # the outpatient visits are not counted
    new = {row["class_0330"]: row["n"] for row in a["class (0.33.0)"]}
    old = {row["class_before"]: row["n"] for row in a["class (0.31.0 / 0.32.0)"]}
    assert new == {"home": 12, "missing": 12, "home_health": "<11"}
    assert old["other (shown as Missing / Unknown)"] == 12 and old["home"] == 12  # HH 32215 matched via its source value
    assert a["padded source values"][0]["n"] == 12
    d = (tmp_path / "d.md").read_text()
    assert "## Site A" in d and "## Site B" in d and "s1" not in d.replace("class", "")
