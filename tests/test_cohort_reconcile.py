"""Reconcile a reference patient list with a cohort and explain the gaps (on the index-stay fixture of test_target_cohort_circe)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.circe_definitions import target_cohort_circe
from omop_etl.cohort_reconcile import explain_cohort_gaps, reconcile_cohort, suppress_small_cells

import test_target_cohort_circe as tc


def _cdm():
    c = tc._cdm()
    c.execute("UPDATE person SET person_source_value = 'P' || person_id")
    # person 10 exists but was never observed
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) "
              "VALUES (10, 8507, 1980, 0, 0, 'P10')")
    return c


def _gaps(res):
    return {(r.reason, r.detail): int(r.n) for r in res["gaps"].itertuples()}


def test_every_reference_patient_gets_the_first_step_that_excludes_them():
    ref = pd.DataFrame({"person_source_value": [f"P{i}" for i in range(1, 11)] + ["P99"]})
    res = explain_cohort_gaps(_cdm(), ref, definition=target_cohort_circe(), include_person_rows=True)
    why = dict(zip(res["persons"]["ref_id"], res["persons"]["reason"]))
    assert why == {"P1": "in_cohort", "P2": "in_cohort", "P3": "no_entry_event",                # a child: no stay with an adult age
                   "P4": "entry_outside_observation_window", "P5": "in_cohort", "P6": "in_cohort",
                   "P7": "entry_outside_observation_window",                                    # observation ends at death, before the 30 days
                   "P8": "failed_entry_correlated_criteria", "P9": "failed_entry_correlated_criteria",   # a death on or before discharge
                   "P10": "no_observation_period", "P99": "not_in_cdm"}
    assert res["n_reference"] == 11 and res["gaps"]["n"].sum() == 11
    assert res["gaps"]["pct"].sum() == pytest.approx(100, abs=0.1)
    assert explain_cohort_gaps(_cdm(), ref, definition=target_cohort_circe())["persons"] is None      # counts only unless asked


def test_an_inclusion_rule_is_named_when_it_is_the_reason():
    d = target_cohort_circe()
    d["InclusionRules"] = [{"name": "Age 40 or older", "expression": {"Type": "ALL", "CriteriaList": [], "Groups": [],
                                                                       "DemographicCriteriaList": [{"Age": {"Value": 40, "Op": "gte"}}]}}]
    ref = pd.DataFrame({"person_source_value": ["P1", "P6"]})
    res = explain_cohort_gaps(_cdm(), ref, definition=d)
    assert _gaps(res) == {("in_cohort", ""): 1, ("failed_inclusion_rule", "Age 40 or older"): 1}


def test_reconcile_counts_overlap_and_date_agreement():
    ref = pd.DataFrame({"person_source_value": ["P1", "P5", "P6", "P7", "P99"],
                        "index_date": ["2020-02-04", "2020-03-10", "2020-01-03", "2020-03-05", "2020-01-01"]})
    out = reconcile_cohort(_cdm(), ref, definition=target_cohort_circe(), index_date_col="index_date")
    # cohort = patients 1, 2, 5, 6; the reference has 1, 5, 6 (found), 7 (missed) and 99 (not in the CDM)
    assert out["counts"] == {"n_reference": 5, "n_mapped_to_cdm": 4, "n_cohort": 4, "both": 3, "reference_only": 2, "cohort_only": 1,
                             "jaccard": 0.5}
    assert out["dates"] == {"n_compared": 3, "n_same_date": 2, "n_different_date": 1, "median_abs_days": 5.0, "max_abs_days": 5.0}
    assert _gaps(out) == {("in_cohort", ""): 3, ("not_in_cdm", ""): 1, ("entry_outside_observation_window", ""): 1}
    assert out["persons"] is None                                             # counts only unless asked


def test_a_cohort_table_without_a_definition_still_reports_the_basics():
    c = _cdm()
    cohort = pd.DataFrame({"subject_id": [1], "cohort_start_date": ["2020-02-04"]})
    ref = pd.DataFrame({"person_source_value": ["P1", "P5", "P99", "P10"]})
    res = explain_cohort_gaps(c, ref, cohort=cohort)
    assert _gaps(res) == {("in_cohort", ""): 1, ("not_in_cdm", ""): 1, ("no_observation_period", ""): 1,
                          ("not_in_cohort_no_definition_given", ""): 1}
    # with a definition and a cohort that lacks a patient the definition keeps, the difference is named
    res = explain_cohort_gaps(c, ref, definition=target_cohort_circe(), cohort=cohort)
    assert ("in_definition_not_in_cohort", "") in _gaps(res)


def test_person_id_references_and_input_forms(tmp_path):
    c = _cdm()
    path = tmp_path / "ref.csv"
    pd.DataFrame({"pid": [1, 3, 99]}).to_csv(path, index=False)
    res = explain_cohort_gaps(c, str(path), definition=target_cohort_circe(), id_col="pid", id_type="person_id")
    assert _gaps(res) == {("in_cohort", ""): 1, ("not_in_cdm", ""): 1, ("no_entry_event", ""): 1}
    c.execute("CREATE TABLE legacy_ids AS SELECT 'p1' AS person_source_value")          # case-insensitive match from a table
    assert _gaps(explain_cohort_gaps(c, "legacy_ids", definition=target_cohort_circe())) == {("in_cohort", ""): 1}
    with pytest.raises(ValueError, match="no column"):
        explain_cohort_gaps(c, pd.DataFrame({"x": [1]}), definition=target_cohort_circe())
    with pytest.raises(ValueError, match="definition, a cohort"):
        explain_cohort_gaps(c, pd.DataFrame({"person_source_value": ["P1"]}))
    with pytest.raises(ValueError, match="id_type"):
        explain_cohort_gaps(c, pd.DataFrame({"person_source_value": ["P1"]}), definition=target_cohort_circe(), id_type="mrn")


def test_small_cells_are_suppressed_for_sharing():
    ref = pd.DataFrame({"person_source_value": ["P1", "P2", "P5", "P6", "P7"]})
    res = explain_cohort_gaps(_cdm(), ref, definition=target_cohort_circe(), min_cell=3)
    g = res["gaps"].set_index("reason")
    assert g.loc["in_cohort", "n"] == 4 and not g.loc["in_cohort", "suppressed"]
    assert np.isnan(g.loc["entry_outside_observation_window", "n"]) and g.loc["entry_outside_observation_window", "suppressed"]
    out = suppress_small_cells(pd.DataFrame({"n": [0, 1, 5], "pct": [0.0, 10.0, 50.0]}), ["n"], 3, also=["pct"])
    assert out["suppressed"].tolist() == [False, True, False] and np.isnan(out.loc[1, "pct"])


def test_definition_as_json_text_or_path_and_reference_files(tmp_path):
    import json
    d = target_cohort_circe()
    path = tmp_path / "def.json"
    path.write_text(json.dumps(d), encoding="utf-8")
    ref = pd.DataFrame({"person_source_value": ["P1", "P7"], "site": ["a", "b"], "index_date": ["2020-02-04", "2020-03-05"]})
    csv, parquet = tmp_path / "ref.csv", tmp_path / "ref.parquet"
    ref.to_csv(csv, index=False)
    ref.to_parquet(parquet)
    expected = {("in_cohort", ""): 1, ("entry_outside_observation_window", ""): 1}
    for definition in (str(path), json.dumps(d), path):
        for reference in (str(csv), str(parquet)):
            assert _gaps(explain_cohort_gaps(_cdm(), reference, definition=definition)) == expected
    res = explain_cohort_gaps(_cdm(), ref, definition=d, group_col="site", include_person_rows=True)
    assert dict(zip(res["persons"]["ref_id"], res["persons"]["group"])) == {"P1": "a", "P7": "b"}


def test_date_agreement_with_a_cohort_table_and_without_start_dates():
    ref = pd.DataFrame({"person_source_value": ["P1", "P5"], "index_date": ["2020-02-04", "2020-03-01"]})
    cohort = pd.DataFrame({"subject_id": [1, 5], "cohort_start_date": ["2020-02-04", "2020-03-05"]})
    out = reconcile_cohort(_cdm(), ref, cohort=cohort, index_date_col="index_date")
    assert out["counts"]["both"] == 2 and out["dates"]["n_same_date"] == 1 and out["dates"]["max_abs_days"] == 4.0
    nodates = reconcile_cohort(_cdm(), ref, cohort=cohort[["subject_id"]], index_date_col="index_date")
    assert nodates["dates"] is None                                                                   # no cohort_start_date to compare
    empty = reconcile_cohort(_cdm(), pd.DataFrame({"person_source_value": ["P99"], "index_date": ["2020-01-01"]}),
                             cohort=cohort, index_date_col="index_date")
    assert empty["counts"]["both"] == 0 and empty["dates"]["n_compared"] == 0


def test_compile_accepts_long_json_text_not_just_paths():
    import json
    from omop_etl.circe import _is_file, compile_circe_to_duckdb
    text = json.dumps(target_cohort_circe())
    assert len(text) > 300 and not _is_file(text) and not _is_file("[1]") and not _is_file("no/such/file.json")
    assert compile_circe_to_duckdb(text) == compile_circe_to_duckdb(target_cohort_circe())


def test_is_file_ignores_paths_the_os_rejects(monkeypatch, tmp_path):
    from omop_etl import circe
    real = tmp_path / "d.json"
    real.write_text("{}", encoding="utf-8")
    assert circe._is_file(real) and circe._is_file(str(real))

    def boom(self):
        raise OSError(36, "File name too long")
    monkeypatch.setattr(Path, "exists", boom)
    assert circe._is_file("a" * 400) is False


def test_additional_criteria_is_named_when_it_is_the_reason():
    d = target_cohort_circe()
    d["AdditionalCriteria"] = {"Type": "ALL", "Groups": [], "DemographicCriteriaList": [{"Age": {"Value": 40, "Op": "gte"}}], "CriteriaList": []}
    res = explain_cohort_gaps(_cdm(), pd.DataFrame({"person_source_value": ["P1", "P6"]}), definition=d)
    assert _gaps(res) == {("in_cohort", ""): 1, ("failed_additional_criteria", ""): 1}          # P6 was born in 2002
