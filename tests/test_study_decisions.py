"""Decisions that move the manuscript numbers are options with named presets, and Table 6 can be shown per clinical unit."""
import numpy as np
import pandas as pd
import pytest

from omop_etl.manuscript_tables import CLINICAL_INCREMENTS, manuscript_regression
from omop_etl.study_run import STUDY_DECISIONS, STUDY_PRESETS, resolve_decisions


def test_every_preset_is_complete_and_valid():
    for name, preset in STUDY_PRESETS.items():
        assert set(preset) == set(STUDY_DECISIONS), name
        assert resolve_decisions(name) == preset


def test_overrides_win_over_the_preset_and_none_keeps_it():
    d = resolve_decisions("delivered_2026_11_11", address_tie_break="deterministic", sdi_missing_term=None)
    assert d["address_tie_break"] == "deterministic" and d["sdi_missing_term"] is False and d["table6_increments"] == "clinical"


def test_bad_names_and_values_are_refused():
    with pytest.raises(ValueError):
        resolve_decisions("nope")
    with pytest.raises(ValueError):
        resolve_decisions("package", color="red")
    with pytest.raises(ValueError):
        resolve_decisions("package", table5="best")


def test_clinical_increments_rescale_or_but_not_p_or_beta():
    rng = np.random.default_rng(0)
    n = 4000
    sbp = rng.normal(130, 20, n)
    y = (rng.random(n) < 1 / (1 + np.exp(-(-1 + 0.02 * (sbp - 130))))).astype(int)
    df = pd.DataFrame({"readmitted_30d": y, "vital_systolic_bp": sbp})
    sd = manuscript_regression(df)
    cl = manuscript_regression(df, increments="clinical")
    a = sd[sd["Feature"] == "Systolic Blood Pressure (mmHg)"].iloc[0]
    b = cl[cl["Feature"] == "Systolic Blood Pressure (mmHg)"].iloc[0]
    assert a["Increment"] == "Per 1 SD" and b["Increment"] == "10 mmHg"
    assert a["P-Value"] == b["P-Value"] and a["Standardized Beta"] == b["Standardized Beta"]
    per_sd = float(a["Adjusted OR (95% CI)*"].split()[0])
    per_10 = float(b["Adjusted OR (95% CI)*"].split()[0])
    assert per_10 == pytest.approx(per_sd ** (10 / sbp.std()), abs=0.01)
    assert "Systolic Blood Pressure (mmHg)" in CLINICAL_INCREMENTS
    with pytest.raises(ValueError):
        manuscript_regression(df, increments="per-year")


def _geo(tie_break, undated="last"):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_geography import CUTS, RUCA, SDI, _con

    from omop_etl.geography import derive_geography_features

    # person 1: two undated-end addresses with the same start (a tie); person 2: one dated and one undated address in effect
    con = _con([(1, 1, "19104", "2020-01-01", None, None), (1, 2, "15213", "2020-01-01", None, None),
                (2, 3, "19104", "2019-01-01", None, None), (2, 4, "15213", None, None, None)],
               [(10, 1, "2021-06-01"), (11, 2, "2021-06-01")])
    g = derive_geography_features(con, RUCA, SDI, CUTS, address_table="person_address_history", tie_break=tie_break, undated=undated)
    return dict(zip(g["stay_id"], g["zip5"])), dict(zip(g["stay_id"], g["rule"]))


def test_tie_break_options_and_undated_rank():
    assert _geo("deterministic")[0] == {10: "15213", 11: "19104"}
    assert _geo("highest_zip")[0][10] == "19104"
    assert _geo("first_listed")[0] == {10: "19104", 11: "19104"}
    assert _geo("first_listed", undated="first")[0] == {10: "19104", 11: "15213"}
    amb, rule = _geo("ambiguous_missing")
    assert amb[10] is None and rule[10] == "ambiguous"
    assert _geo("arbitrary")[0][10] in ("19104", "15213")
    with pytest.raises(ValueError):
        _geo("first_listed", undated="never")
    with pytest.raises(ValueError):
        _geo("random")


def test_delivered_preset_uses_the_study_pipeline_address_rules():
    d = resolve_decisions("delivered_2026_11_11")
    assert d["address_tie_break"] == "first_listed" and d["address_undated"] == "first"
    assert resolve_decisions("package")["address_undated"] == "last"


def test_assign_site_labels_random_seeded_ordered_and_saved(tmp_path):
    from omop_etl.study_run import assign_site_labels, load_site_labels, save_site_labels

    sites = ["s1", "s2", "s3", "s4", "s5", "s6"]
    a = assign_site_labels(sites, seed=3)
    assert a == assign_site_labels(sites, seed=3) and sorted(a.values()) == [f"Site {c}" for c in "ABCDEF"]
    assert any(assign_site_labels(sites, seed=k) != a for k in range(4, 9))        # different seeds shuffle differently
    assert assign_site_labels(sites, mode="ordered") == {s: f"Site {c}" for s, c in zip(sites, "ABCDEF")}
    assert sorted(assign_site_labels(sites).values()) == sorted(a.values())           # unseeded: still a permutation
    assert assign_site_labels(["x", "x", "y"], mode="ordered") == {"x": "Site A", "y": "Site B"}
    assert load_site_labels(save_site_labels(a, tmp_path / "l.json")) == a
    with pytest.raises(ValueError):
        assign_site_labels(sites, mode="alphabetical")
    with pytest.raises(ValueError):
        assign_site_labels([str(i) for i in range(27)])
