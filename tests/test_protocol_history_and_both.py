"""extract_protocol_history_features and the ``profile="both"`` / per-group options of extract_benchmark_features."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from omop_etl.benchmark_features import extract_benchmark_features, extract_protocol_history_features  # noqa: E402


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,"
              " visit_end_date DATE, discharged_to_concept_id INT)")
    c.execute("""INSERT INTO visit_occurrence VALUES
        (1, 1, 9201, DATE '2019-12-01', DATE '2019-12-04', 8536), (2, 1, 9203, DATE '2019-06-01', DATE '2019-06-01', 8536),
        (3, 1, 9202, DATE '2019-11-01', DATE '2019-11-01', 8536), (4, 1, 9201, DATE '2018-12-01', DATE '2018-12-05', 8536),
        (101, 1, 9201, DATE '2020-01-10', DATE '2020-01-15', 8536), (102, 2, 9201, DATE '2020-01-10', DATE '2020-01-15', 8536),
        (103, 3, 9201, DATE '2020-01-10', DATE '2020-01-15', 8536)""")
    c.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,"
              " condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
    c.execute("""INSERT INTO condition_occurrence VALUES
        (1, 11, DATE '2019-05-01', 'I50.9', NULL), (1, 12, DATE '2020-01-12', 'F17.210', NULL), (1, 13, DATE '2020-02-01', 'G40.9', NULL),
        (2, 21, DATE '2015-05-01', 'Z87.891', NULL)""")
    c.execute("CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE)")
    c.execute("INSERT INTO observation VALUES (3, 4052039, DATE '2019-01-01')")
    c.execute("""CREATE TABLE cohort_t AS SELECT * FROM (VALUES
        (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0), (2, DATE '2020-01-10', DATE '2020-01-15', 102, 1),
        (3, DATE '2020-01-10', DATE '2020-01-15', 103, 0)) t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)""")
    yield c
    c.close()


def _cohort():
    return pd.DataFrame({"row_id": [0, 1, 2], "subject_id": [1, 2, 3],
                         "cohort_start_date": pd.to_datetime(["2020-01-10"] * 3), "cohort_end_date": pd.to_datetime(["2020-01-15"] * 3)})


def test_protocol_history_values(con):
    h = extract_protocol_history_features(con, _cohort())
    assert h.loc[0, "prior_encounters_all_count"] == 3 and h.loc[0, "prior_ip_os_ed_count"] == 2   # the 2018 stay is outside 365 days
    assert h.loc[0, "days_since_prior_encounter"] == 40 and h.loc[0, "days_since_prior_ip_ed"] == 40
    assert h.loc[0, "readmission_count_twelve_months"] == 1
    assert h.loc[1, "days_since_prior_encounter"] == 365 and h.loc[1, "prior_encounters_all_count"] == 0   # none: the window length
    assert h.loc[0, "n_secondary_dx_ccs"] == 1 and h.loc[0, "elix_pre_index_conditions_count"] == 1       # a post-discharge diagnosis is ignored
    assert h["tobacco"].tolist() == ["Current user", "Former user", "Never user"]                           # ICD F17 / ICD Z87.891 / observation "never smoker" 4052039
    assert extract_protocol_history_features(con, _cohort(), lookback_days=30).loc[1, "days_since_prior_encounter"] == 30
    with pytest.raises(ValueError, match="non-negative"):
        extract_protocol_history_features(con, _cohort(), lookback_days=-1)


KW = dict(include_medications=False, include_utilization=False, include_vitals=False, include_labs=False, include_disposition=False,
          include_demographics=False, vocabulary_db=None)


def test_profile_both_keeps_generic_and_adds_prefixed_protocol_flags(con):
    generic = extract_benchmark_features(con, "cohort_t", **KW)
    both = extract_benchmark_features(con, "cohort_t", profile="both", **KW)
    assert "tobacco" not in generic and "tobacco" in both                                            # history columns come with profile "both"
    elix = [c for c in generic.columns if c.startswith("elix_")]
    assert all(c in both for c in elix) and all(f"ederri_{c}" in both for c in elix)
    for c in elix:                                                                                    # generic columns are not changed by "both"
        assert both[c].tolist() == generic[c].tolist()
    assert both.attrs["feature_groups"]["elixhauser_ederri"] and "medications_ederri" not in both.attrs["feature_groups"]
    only = extract_benchmark_features(con, "cohort_t", profile="ederri", **KW)
    assert "ederri_elix_chf" not in only and "elix_chf" in only                                       # "ederri" replaces, "both" adds


def test_each_choice_can_be_set_on_its_own(con):
    a = extract_benchmark_features(con, "cohort_t", elixhauser_code_table="both", **KW)
    assert "ederri_elix_chf" in a and "tobacco" not in a                                              # no profile: only the one option
    b = extract_benchmark_features(con, "cohort_t", include_protocol_history=True, **KW)
    assert "tobacco" in b and "ederri_elix_chf" not in b
    c = extract_benchmark_features(con, "cohort_t", profile="both", include_protocol_history=False, **KW)
    assert "tobacco" not in c and "ederri_elix_chf" in c                                              # explicit argument wins over the profile
    for bad in (dict(profile="x"), dict(elixhauser_code_table="x"), dict(medication_set="x")):
        with pytest.raises(ValueError):
            extract_benchmark_features(con, "cohort_t", **bad, **KW)


def test_medication_set_both_runs_generic_and_protocol_classes(con, monkeypatch):
    import omop_etl.medication_classes as mc
    calls = []

    def fake(db_con, cohort_table, specs, **kw):
        calls.append((sorted(specs), kw))
        return pd.DataFrame({"person_id": [1, 2, 3], "index_date": pd.to_datetime(["2020-01-10"] * 3), **{k: [1, 0, 0] for k in specs}})

    monkeypatch.setattr(mc, "extract_medication_classes", fake)
    kw = {**KW, "include_medications": True}
    out = extract_benchmark_features(con, "cohort_t", medication_set="both", **kw)
    assert len(calls) == 2 and "metformin" in calls[0][0] and "biguanides" in calls[1][0]
    assert calls[0][1]["window"] == "pre_admission" and calls[1][1]["window"] is None and calls[1][1]["offset_anchor"] == "visit_end_date"
    assert "med_metformin" in out and "ederri_med_biguanides" in out and "med_biguanides" not in out
