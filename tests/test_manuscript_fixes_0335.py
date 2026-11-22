"""0.33.5 fixes after Kyle's review of run C7 (2026-11-22): the models use the index-stay mean of each laboratory only (no
``lab_<name>_last`` features), Figure 1 names the pinned-list step "Randomly selected qualifying stay per patient" with no exclusion
arrow, and the abstract also names the clinical risk factor with the largest odds ratio when the standardized-beta ranking leaves it out."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl.study_run import STUDY_PRESETS, figure1_attrition, resolve_decisions  # noqa: E402


# ------------------------------------------------------------------ last lab values
def test_both_presets_drop_last_lab_values():
    assert STUDY_PRESETS["package"]["last_lab_values"] == "drop"
    assert STUDY_PRESETS["delivered_2026_11_11"]["last_lab_values"] == "drop"
    assert resolve_decisions("delivered_2026_11_11", last_lab_values="keep")["last_lab_values"] == "keep"
    with pytest.raises(ValueError):
        resolve_decisions("package", last_lab_values="maybe")


@pytest.mark.parametrize("choice", ["drop", "keep"])
def test_models_see_no_last_lab_values(tmp_path, monkeypatch, choice):
    pytest.importorskip("sklearn")
    import test_multisite_anonymous as tma

    real = tma.make_features

    def with_last(*a, **kw):
        f = real(*a, **kw)
        return f.assign(lab_sodium_last=f["lab_sodium"] + 1.0, lab_albumin_last=f["lab_albumin"])

    monkeypatch.setattr(tma, "make_features", with_last)
    from omop_etl.study_run import run_study

    db, keys = tma._study(tmp_path, monkeypatch, 3)
    out = tmp_path / "o"
    run_study(db, out, sites=keys, n_bootstraps=3, loso=False, figures=False, profile=None, document=False,
              preset="delivered_2026_11_11", last_lab_values=None if choice == "drop" else "keep")
    rec = json.loads((out / "Study_run.json").read_text(encoding="utf-8"))
    assert rec["decisions"]["last_lab_values"] == choice
    feats = set(pd.read_csv(out / "Study_Missingness.csv").iloc[:, 0].astype(str))
    has_last = any(c.endswith("_last") for c in feats)
    assert has_last == (choice == "keep"), sorted(feats)[:20]
    # the saved features keep both, so a later run may reuse them whichever way it chooses
    saved = pd.read_parquet(next((tmp_path / "o_private").glob("features_*.parquet")))
    assert "lab_sodium_last" in saved.columns


# ------------------------------------------------------------------ Figure 1
def _attrition() -> pd.DataFrame:
    rows = []
    for site, (a, b, c, d) in {"s1": (100, 80, 60, 58), "s2": (50, 40, 30, 29)}.items():
        rows += [{"site": site, "step_name": "Index visit type in (262, 9201)", "subjects_retained": a},
                 {"site": site, "step_name": "Verified 30-day follow-up", "subjects_retained": b},
                 {"site": site, "step_name": "Index admission 2017-01-01 to 2022-09-30, one stay per patient", "subjects_retained": c},
                 {"site": site, "step_name": "Stay selected for the patient (pinned list)", "subjects_retained": d}]
    return pd.DataFrame(rows)


def test_figure1_names_the_random_stay_and_draws_no_pinned_list_arrow():
    pytest.importorskip("matplotlib")
    from omop_etl.study_report import flowchart_overlaps, plot_cohort_flowchart

    f = figure1_attrition(_attrition(), "2017-01-01", "2022-09-30")
    assert f["step_name"].iloc[-1] == "Randomly selected qualifying stay per patient"
    assert list(f["excluded_shown"]) == [True, True, True, False]
    assert int(f["subjects_retained"].iloc[-1]) == 87 and int(f["subjects_dropped"].iloc[-1]) == 3  # the record keeps the counts
    fig = plot_cohort_flowchart(f, 87, 9)
    texts = [" ".join(t.get_text().split()) for t in fig.axes[0].texts]
    assert not any("pinned" in t.lower() for t in texts)
    assert any(t.startswith("Randomly selected qualifying stay per patient") for t in texts)
    assert not any(t.startswith("Excluded") and "P = 3 patients" in t for t in texts)
    assert any(t.startswith("Excluded: no qualifying index admission in the grant protocol window") for t in texts)
    assert flowchart_overlaps(fig) == []


# ------------------------------------------------------------------ abstract risk factors
def _t6(rows):
    return {"Table 6 - LR": ("Table 6: LR", pd.DataFrame(rows, columns=["Feature", "Increment", "Adjusted OR (95% CI)", "Standardized Beta", "P-Value"]))}


def test_largest_odds_ratio_is_named_when_the_beta_ranking_leaves_it_out():
    from omop_etl.manuscript_text import _regression

    r = _regression(_t6([["Encounter Information", "", "", "", ""],
                         ["Prior Readmissions in Past 12 Months", "Per 1 stay", "1.17 (1.15–1.19)", "0.21", "<0.001"],
                         ["Anion gap", "Per 1 mEq/L", "1.05 (1.04–1.06)", "0.12", "<0.001"],
                         ["Discharge Status: Against Medical Advice", "Vs. reference", "1.88 (1.60–2.21)", "0.05", "<0.001"],
                         ["Albumin: not measured", "Yes vs. no", "2.50 (2.00–3.10)", "0.30", "<0.001"]]))
    assert [x["feature"] for x in r["top_risk"]] == ["Prior Readmissions in Past 12 Months", "Anion gap"]
    assert r["largest_or"]["feature"] == "Discharge Status: Against Medical Advice"  # indicators never headline
    same = _regression(_t6([["Prior Readmissions in Past 12 Months", "Per 1 stay", "1.90 (1.80–2.00)", "0.21", "<0.001"],
                            ["Anion gap", "Per 1 mEq/L", "1.05 (1.04–1.06)", "0.12", "<0.001"]]))
    assert same["largest_or"] is None


def test_abstract_sentence_names_the_largest_odds_ratio(tmp_path):
    pytest.importorskip("jinja2")
    from omop_etl import manuscript_text as mtx

    assert mtx.template_dir("0.33.5").name == "v0.33.5"
    lr = {"top_risk": [{"feature": "Prior Readmissions in Past 12 Months", "or": 1.17, "lo": 1.15, "hi": 1.19, "p": 0.0009, "p_text": "<0.001"},
                       {"feature": "Anion gap", "or": 1.05, "lo": 1.04, "hi": 1.06, "p": 0.0009, "p_text": "<0.001"}],
          "largest_or": {"feature": "Discharge Status: Against Medical Advice", "or": 1.88, "lo": 1.6, "hi": 2.21, "p": 0.0009, "p_text": "<0.001"}}
    tpl = (mtx.template_dir("0.33.5") / "manuscript.md.j2").read_text(encoding="utf-8")
    line = next(x for x in tpl.splitlines() if "strongest independent risk factors" in x)
    env = mtx._environment([])
    out = env.from_string(line).render(lr=lr, ref={"lr": "Table 4"})
    assert "prior readmissions in past 12 months (OR 1.17" in out
    assert "and discharge status: Against Medical Advice had the largest odds ratio (OR 1.88" in out and out.rstrip().endswith("(Table 4).")


# ------------------------------------------------------------------ printed table format (Kyle, 2026-11-22: C7 Table 3 hard to read)
def test_printed_tables_size_columns_by_content_and_align_numbers():
    from omop_etl.manuscript_document import TABLE_TYPST, _table_md

    t = pd.DataFrame({"Held-out site": ["Site A", "Site B", "Mean of five sites"], "Test patients": [14216, 53072, None],
                      "AUC-ROC (95% CI)": ["0.720 (0.707–0.734)", "0.713 (0.707–0.719)", "0.722"], "F1": [0.281, 0.323, 0.302],
                      "Sensitivity": [0.638, 0.52, 0.547], "Configuration": ["depth 12", "depth 8", "C=1"], "Beta": [0.3, 0.12, 0.0]})
    lines = _table_md(t).splitlines()
    rule = lines[1].strip("|").split("|")
    widths = [len(r.strip(":")) for r in rule]
    assert widths[2] > widths[3] and widths[2] >= len("0.720 (0.707–0.734)")  # the CI column is the widest number column
    assert rule[0].startswith(":") and rule[5].startswith(":")  # text columns left-aligned
    assert all(rule[i].endswith(":") for i in (1, 2, 3, 4, 6))  # number columns right-aligned
    assert "| 14,216 |" in lines[2] and "| 0.520 |" in lines[3] and "| 0.30 |" in lines[2] and "| 0.00 |" in lines[4]
    assert "justify: false" in TABLE_TYPST and "hyphenate: false" in TABLE_TYPST


def test_table_cells_are_escaped_once():
    from omop_etl.manuscript_document import _table_md

    md_ = _table_md(pd.DataFrame({"Statistic": ["Median [IQR] (days)"], "Site A": ["4.0 [3.0–6.0]"]}))
    assert "\\\\[" not in md_ and md_.count("\\[") == 2
