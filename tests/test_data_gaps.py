"""Silent data gaps fail loudly: address-table detection, feature-group coverage, and the study run's gate and run record.

Synthetic data only; every test runs in seconds. These check how the checks work, not how they scale.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl.data_gaps import DataGapError, DataGapWarning, assess, feature_coverage  # noqa: E402
from omop_etl.geography import find_address_tables, resolve_address_table  # noqa: E402

COLS = "person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR"


def _con(*tables, location_zip=None):
    c = duckdb.connect()
    c.execute("CREATE TABLE person (person_id BIGINT, location_id BIGINT)")
    c.execute("INSERT INTO person VALUES (1, 1)")
    c.execute("CREATE TABLE location (location_id BIGINT, zip VARCHAR)")
    if location_zip:
        c.execute("INSERT INTO location VALUES (1, ?)", [location_zip])
    for name, rows in tables:
        c.execute(f"CREATE TABLE {name} ({COLS})")
        for _ in range(rows):
            c.execute(f"INSERT INTO {name} VALUES (1, 1, '19101', DATE '2019-01-01', NULL, 'Y')")
    return c


# -- address table ----------------------------------------------------------------------------------------------------------

def test_auto_prefers_the_one_non_empty_address_table_whatever_its_name():
    assert resolve_address_table(_con(("person_address_history", 1))) == "person_address_history"
    # the standard table exists but is empty; the study's own table has the rows: auto used to pick neither
    assert resolve_address_table(_con(("person_address_history", 0), ("study_address_history", 2))) == "study_address_history"


def test_auto_refuses_to_guess_between_two_tables():
    with pytest.raises(ValueError, match="more than one.*person_address_history, study_address_history"):
        resolve_address_table(_con(("person_address_history", 1), ("study_address_history", 1)))


def test_auto_without_any_address_names_what_it_saw():
    c = _con(("person_address_history", 0))
    c.execute("CREATE TABLE address_notes (person_id BIGINT, note VARCHAR)")
    with pytest.raises(ValueError, match=r"address_notes \(lacks zip5.*person_address_history \(empty\)"):
        resolve_address_table(c)
    assert {t["table"] for t in find_address_tables(c)} == {"address_notes", "person_address_history"}
    # person.location_id with a ZIP is still a legitimate fallback; None always means it
    assert resolve_address_table(_con(location_zip="19101-1234")) is None
    assert resolve_address_table(c, None) is None


def test_an_explicit_name_must_exist_and_have_the_columns():
    c = _con(("study_address_history", 1))
    with pytest.raises(ValueError, match="'ederri_address_history' does not exist.*study_address_history \\(usable\\)"):
        resolve_address_table(c, "ederri_address_history")
    c.execute("CREATE TABLE addr (person_id BIGINT)")
    with pytest.raises(ValueError, match="lacks column"):
        resolve_address_table(c, "addr")
    with pytest.raises(ValueError, match="invalid"):
        resolve_address_table(c, "x; DROP TABLE person")


# -- feature coverage -------------------------------------------------------------------------------------------------------

def _frame(n=100):
    rng = np.random.default_rng(0)
    return pd.DataFrame({"visit_occurrence_id": np.arange(n), "lab_albumin": rng.normal(3, 1, n), "lab_ldl": np.nan,
                         "med_insulins": rng.integers(0, 2, n), "sdi_quintile": "SDI Quintile 2", "sdoh_missing": 0})


GROUPS = {"labs": ["lab_albumin", "lab_ldl"], "medications": ["med_insulins"], "sdoh": ["sdi_quintile", "sdoh_missing"]}


def test_one_lab_missing_everywhere_at_a_site_fails_and_is_accepted_by_name():
    d = _frame().assign(extra_note=np.nan, med_statins=0)
    f = feature_coverage(d, site="Site A", groups=GROUPS, skipped=["lab_ferritin"])
    by = {(x["check"], x.get("column")): x["severity"] for x in f}
    assert by == {("feature_skipped", "lab_ferritin"): "error", ("column_missing", "lab_ldl"): "error",
                  ("column_missing", "extra_note"): "warning"}            # outside every feature group: a warning only
    assert all(x["check"] != "column_all_zero" for x in f)                # 100 stays: too few to call an all-zero class suspicious
    with pytest.raises(DataGapError, match="--allow-data-gaps lab_ferritin lab_ldl"):
        assess(f)
    assert all(x["allowed"] for x in assess(f, ["lab_ferritin", "lab_ldl"]) if x["severity"] == "error")
    big = pd.concat([d] * 3, ignore_index=True)
    assert any(x["check"] == "column_all_zero" and x["column"] == "med_statins" for x in feature_coverage(big, site="Site A", groups=GROUPS))


def test_all_missing_sdoh_all_zero_meds_and_missing_labs_are_errors():
    d = _frame().assign(sdoh_missing=1, med_insulins=0, lab_albumin=np.nan)
    d.loc[:2, "sdoh_missing"] = 0                                # 97% missing is still "close to all"
    f = feature_coverage(d, site="Site B", groups=GROUPS)
    errors = {x.get("group"): x for x in f if x["severity"] == "error" and x.get("group")}
    assert set(errors) == {"sdoh", "medications", "labs"} and errors["sdoh"]["check"] == "sdoh"
    assert "97.0%" in errors["sdoh"]["message"] and "Site B" not in errors["sdoh"]["message"]
    with pytest.raises(DataGapError, match="--allow-data-gaps labs medications sdoh"):
        assess(f)
    assert all(x["allowed"] for x in assess(f, ["sdoh", "group_missing"]) if x["severity"] == "error")
    with pytest.raises(ValueError, match="unknown name"):
        assess(f, ["sdho"])


def test_skipped_group_and_empty_site():
    assert feature_coverage(_frame(), site="Site C", skipped=["protocol_history"])[0]["check"] == "group_skipped"
    assert feature_coverage(_frame().iloc[:0], site="Site C")[0]["check"] == "empty_site"


# -- the study run ----------------------------------------------------------------------------------------------------------

def _gapped_study(tmp_path, monkeypatch, sdoh_missing_at=None):
    """The three-site stand-in study of test_manuscript_tables, with feature groups reported as the real builder does."""
    import omop_etl.benchmark_features as bf
    from test_manuscript_tables import _fake_study, make_features

    feats = make_features()
    db, sites = _fake_study(tmp_path, monkeypatch, feats)
    fake = bf.extract_benchmark_features
    by_id = feats.set_index("visit_occurrence_id")

    def with_groups(con, table, **kw):
        out = fake(con, table, **kw)
        out["sdoh_missing"] = (by_id.loc[out.visit_occurrence_id, "sdi_quintile"] == "SDI: Missing / Unknown").astype(int).to_numpy()
        if sdoh_missing_at and con.execute("SELECT min(visit_occurrence_id) FROM visit_occurrence").fetchone()[0] == 7 * sdoh_missing_at:
            out["sdoh_missing"], out["sdi_quintile"] = 1, "SDI: Missing / Unknown"
        out.attrs["feature_groups"] = {"sdoh": ["sdi_quintile", "ruca_category", "sdoh_missing"], "vitals": ["vital_bmi", "vital_systolic_bp"]}
        out.attrs["skipped"] = []
        out.attrs["address_table"] = (kw["address_table"] or "person.location_id") if "address_table" in kw else "study_address_history"
        return out

    monkeypatch.setattr(bf, "extract_benchmark_features", with_groups)
    return db, sites


def test_run_with_all_missing_sdoh_at_one_site_fails_unless_allowed(tmp_path, monkeypatch):
    from omop_etl.study_run import run_study

    db, sites = _gapped_study(tmp_path, monkeypatch, sdoh_missing_at=1)       # the second site (first visit id 7)
    kw = dict(sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None, document=False)
    with pytest.raises(DataGapError, match=r"\[Site B\] sdoh: feature group 'sdoh' is missing for 100.0% of stays"):
        run_study(db, tmp_path / "o", **kw)
    assert not (tmp_path / "o" / "Study_Manuscript_Tables.xlsx").exists()
    r = run_study(db, tmp_path / "o2", allow_data_gaps=["sdoh"], run_args={"sites": sites}, **kw)
    rec = json.loads((tmp_path / "o2" / "Study_run.json").read_text())
    gap = [f for f in rec["data_checks"]["findings"] if f["severity"] == "error"]
    assert [(f["site"], f["check"], f["allowed"]) for f in gap] == [("Site B", "sdoh", True)]
    assert rec["address_table"] == {"requested": "auto", "used": {"Site A": "study_address_history", "Site B": "study_address_history",
                                                                   "Site C": "study_address_history"}}
    assert rec["data_checks"]["allowed"] == ["sdoh"] and rec["site_labels"] == "ordered"
    shares = rec["data_checks"]["share_missing_by_group"]
    assert shares["Site B"]["sdoh"] == 1.0 and 0 < shares["Site A"]["sdoh"] < 0.5 and shares["Site A"]["vitals"] == 0.0
    # the --sites order and the command line stay with the operating site
    private = json.loads((r["private_dir"] / "Study_run_private.json").read_text())
    assert private["sites"] == list(sites) and private["run_args"] == {"sites": list(sites)}
    # reuse_features re-checks the saved features: still a gap, still needs the opt-in
    with pytest.raises(DataGapError, match="sdoh"):
        run_study(db, tmp_path / "o2", reuse_features=True, private_dir=r["private_dir"], **kw)
    # ... and refuses features that were built with other settings instead of silently reusing them
    with pytest.raises(DataGapError, match=r"stale_features: reused features were built with different settings \(window\)"):
        run_study(db, tmp_path / "o2", reuse_features=True, private_dir=r["private_dir"], allow_data_gaps=["sdoh"], start="2018-01-01", **kw)
    assert run_study(db, tmp_path / "o2", reuse_features=True, private_dir=r["private_dir"], allow_data_gaps=["sdoh"], **kw)["data"] is not None


def test_script_stops_with_the_candidates_when_no_address_table_is_found(tmp_path, monkeypatch, capsys):
    import reproduce_study

    db, sites = _gapped_study(tmp_path, monkeypatch)
    ruca, sdi = tmp_path / "ruca.csv", tmp_path / "sdi.csv"
    pd.DataFrame({"zipcode": ["19101"], "ruca_code": [1]}).to_csv(ruca, index=False)
    pd.DataFrame({"zcta5_fips": ["19101"], "sdi_score": [50.0]}).to_csv(sdi, index=False)
    for s in sites:                                                              # the address history under a study-specific name
        con = duckdb.connect(str(db / f"omop_{s}.duckdb"))
        con.execute(f"CREATE TABLE person_address_history ({COLS}); CREATE TABLE study_address_history ({COLS})")
        con.close()
    args = ["--db-dir", str(db), "--out", str(tmp_path / "o"), "--sites", *sites, "--bootstraps", "3", "--no-loso", "--no-figures",
            "--profile", "none", "--no-document", "--ruca", str(ruca), "--sdi", str(sdi)]
    assert reproduce_study.main(args) == 3
    err = capsys.readouterr().err
    assert "DATA GAP CHECK FAILED" in err and "address_table" in err and "study_address_history (empty)" in err
    # accepted explicitly: the run goes on and says so
    assert reproduce_study.main(args + ["--allow-data-gaps", "address_table"]) == 0
    out = capsys.readouterr()
    assert "address table used: Site A: person.location_id" in out.out and "DATA GAPS ACCEPTED" in out.err


# -- window, observation periods, partial geography, one-pass check ---------------------------------------------------------

def test_window_not_covered_and_build_date_pileup_are_gap_warnings():
    import warnings

    from omop_etl.study_run import _check_coverage_of_window

    c = duckdb.connect()
    c.execute("CREATE TABLE visit_occurrence AS SELECT 9201 AS visit_concept_id, DATE '2018-06-01' + i::INT AS visit_start_date FROM range(400) t(i)")
    c.execute("CREATE TABLE observation_period AS SELECT DATE '2019-01-01' - (i % 60)::INT AS observation_period_end_date FROM range(120) t(i) "
              "UNION ALL SELECT DATE '2019-07-05' FROM range(200)")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_coverage_of_window(c, "2017-01-01", "2022-09-30", {"visit_type": [9201, 262]})
    gaps = [(x.message.check, str(x.message)) for x in w if isinstance(x.message, DataGapWarning)]
    assert sorted(c for c, _ in gaps) == ["observation_period", "window", "window"]
    assert any("end 1183 days before" in m for _, m in gaps) and any("begin 516 days after" in m for _, m in gaps)
    assert not any("2019" in m or "2018" in m for _, m in gaps)                                                    # days and shares, no dates
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _check_coverage_of_window(c, "2018-06-01", "2019-06-30", {})
    assert not [x for x in w if isinstance(x.message, DataGapWarning) and x.message.check == "window"]


def test_partial_geography_loss_is_caught_below_the_group_threshold():
    from omop_etl.data_gaps import geography_findings

    ok = {"zip_not_in_sdi": 0.02, "zip_not_in_ruca": 0.01, "rule_shares": {"in_effect": 0.9, "earliest_after": 0.05}}
    assert geography_findings(ok, site="Site A") == []
    bad = {"zip_not_in_sdi": 0.4, "zip_not_in_ruca": 0.01, "rule_shares": {"earliest_after": 0.7}}
    f = geography_findings(bad, site="Site A")
    assert [(x["check"], x["severity"]) for x in f] == [("sdoh_partial", "error"), ("address_rule", "warning")]


def test_benchmark_features_report_how_addresses_were_matched():
    from test_sdoh_address_table import CUTS, KW, RUCA, SDI

    from omop_etl.benchmark_features import extract_benchmark_features

    c = duckdb.connect()
    c.execute("CREATE TABLE person (person_id BIGINT, location_id BIGINT); CREATE TABLE location (location_id BIGINT, zip VARCHAR)")
    c.execute(f"CREATE TABLE study_address_history ({COLS})")
    c.execute("INSERT INTO study_address_history VALUES (1, 1, '19101', DATE '2019-01-01', NULL, 'N'), (2, 2, '55555', DATE '2019-01-01', NULL, 'N')")
    c.execute("CREATE TABLE cohort_t AS SELECT * FROM (VALUES (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0), "
              "(2, DATE '2020-01-10', DATE '2020-01-15', 102, 0)) t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)")
    df = extract_benchmark_features(c, "cohort_t", include_sdoh=True, ruca_crosswalk=RUCA, sdi_table=SDI, sdi_cut_points=CUTS, **KW)
    g = df.attrs["geography"]
    assert g["address_table"] == "study_address_history" and g["share_with_zip"] == 1.0
    assert g["zip_not_in_sdi"] == 0.5 and g["rule_shares"] == {"in_effect": 1.0}


def test_check_data_reports_every_site_in_one_pass(tmp_path, monkeypatch, capsys):
    import reproduce_study

    from omop_etl.study_run import run_study

    db, sites = _gapped_study(tmp_path, monkeypatch, sdoh_missing_at=1)
    kw = dict(sites=sites, n_bootstraps=3, loso=False, figures=False, profile=None, document=False)
    r = run_study(db, tmp_path / "ok", check_only=True, allow_data_gaps=["sdoh"], **kw)
    assert r["checked_only"] and set(r["share_missing_by_group"]) == {"Site A", "Site B", "Site C"}
    assert not (tmp_path / "ok" / "Study_Manuscript_Tables.xlsx").exists() and (tmp_path / "ok" / "Study_data_gaps.json").exists()
    args = ["--db-dir", str(db), "--out", str(tmp_path / "o"), "--sites", *sites, "--no-document", "--profile", "none", "--check-data"]
    assert reproduce_study.main(args) == 3
    assert "[Site B] sdoh" in capsys.readouterr().err
    assert reproduce_study.main(args + ["--allow-data-gaps", "sdoh"]) == 0
    assert "data check passed" in capsys.readouterr().out


def test_a_gap_warning_can_name_its_column_and_be_accepted_by_it():
    import warnings

    from omop_etl.study_run import _caught_findings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        warnings.warn(DataGapWarning("medication classes: Medication class 'statins' resolved to no descendant concepts", "feature_skipped",
                                     "med_statins"))
    f = _caught_findings(w, "Site A", {})
    assert f[0]["column"] == "med_statins" and assess(f, ["med_statins"])[0]["allowed"]
