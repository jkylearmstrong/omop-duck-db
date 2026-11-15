"""Outstanding request 1: inpatient labs, prior utilization, index-diagnosis guard, scoring bundle."""

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    FeatureSchemaError,
    OMOPScoringPipeline,
    build_schema,
    extract_elixhauser_comorbidities,
    extract_inpatient_labs,
    extract_prior_utilization,
)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    build_schema(c)
    for pid in (1, 2):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, "
                  f"ethnicity_concept_id) VALUES ({pid}, 8507, 1960, 8527, 38003563)")
    yield c
    c.close()


def _visit(con, vid, pid, concept, start, end):
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                f"visit_end_date, visit_type_concept_id) VALUES ({vid}, {pid}, {concept}, DATE '{start}', "
                f"DATE '{end}', 32817)")


def _cohort(con, rows):
    con.execute("ALTER TABLE cohort ADD COLUMN visit_occurrence_id BIGINT")
    for r in rows:
        con.execute("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date, "
                    "visit_occurrence_id) VALUES (1, ?, ?, ?, ?)", list(r))


# ---------------------------------------------------------------- prior utilization
def test_prior_utilization_counts_and_leak_free(con):
    _visit(con, 10, 1, 9201, "2020-01-01", "2020-01-05")   # prior IP, 4d
    _visit(con, 11, 1, 9201, "2020-01-20", "2020-01-23")   # prior IP 15d after discharge -> readmission, 3d
    _visit(con, 12, 1, 9203, "2020-05-01", "2020-05-01")   # prior ED
    _visit(con, 13, 1, 9202, "2018-01-01", "2018-01-01")   # outside 365d
    _visit(con, 20, 1, 9201, "2020-06-01", "2020-06-10")   # INDEX
    _visit(con, 21, 1, 9201, "2020-06-20", "2020-06-22")   # after admission: must not count
    _cohort(con, [(1, "2020-06-01", "2020-06-10", 20), (2, "2020-06-01", "2020-06-05", None)])
    df = extract_prior_utilization(con)
    p1 = df[df.subject_id == 1].iloc[0]
    assert (p1.prior_ip_count, p1.prior_ip_los_days, p1.prior_ed_count, p1.prior_os_count) == (2, 7, 1, 0)
    assert p1.prior_readmit_30d_count == 1
    assert p1.days_since_last_encounter == (pd.Timestamp("2020-06-01") - pd.Timestamp("2020-05-01")).days
    p2 = df[df.subject_id == 2].iloc[0]
    assert p2.prior_ip_count == 0 and pd.isna(p2.days_since_last_encounter)
    assert len(df) == 2


def test_prior_utilization_custom_groups_and_lookback(con):
    _visit(con, 30, 1, 9201, "2020-06-01", "2020-06-01")   # same day as admission: not "prior"
    _visit(con, 31, 1, 9201, "2020-05-31", "2020-05-31")
    _visit(con, 32, 1, 9201, "2020-01-01", "2020-01-02")   # outside the 30d look-back
    _cohort(con, [(1, "2020-06-01", "2020-06-03", 30)])
    df = extract_prior_utilization(con, visit_concept_groups={"acute": [9201]}, lookback_days=30,
                                   readmission_group="acute")
    assert df.loc[0, "prior_acute_count"] == 1 and "prior_ip_count" not in df
    assert df.loc[0, "days_since_last_encounter"] == 1


def test_prior_utilization_validation(con):
    _cohort(con, [(1, "2020-06-01", "2020-06-03", None)])
    with pytest.raises(ValueError, match="admission"):
        extract_prior_utilization(con, anchor_date="discharge")
    with pytest.raises(ValueError, match="lookback_days"):
        extract_prior_utilization(con, lookback_days=-1)
    with pytest.raises(ValueError, match="identifier"):
        extract_prior_utilization(con, visit_concept_groups={"a b": [1]})
    with pytest.raises(ValueError, match="readmission_group"):
        extract_prior_utilization(con, visit_concept_groups={"x": [1]})


# ---------------------------------------------------------------- inpatient labs
def _meas(con, mid, pid, concept, ts, val):
    con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                f"measurement_datetime, value_as_number, measurement_type_concept_id) VALUES ({mid}, {pid}, "
                f"{concept}, CAST(TIMESTAMP '{ts}' AS DATE), TIMESTAMP '{ts}', {val}, 32817)")


def test_inpatient_labs_rules_bounds_and_acute_window(con):
    con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, "
                "standard_concept, concept_code, valid_start_date, valid_end_date) VALUES "
                "(7001,'Creatinine','Measurement','LOINC','Lab Test','S','2160-0',DATE '2000-01-01',DATE '2099-12-31'),"
                "(7002,'Lactate','Measurement','LOINC','Lab Test','S','2524-7',DATE '2000-01-01',DATE '2099-12-31')")
    _cohort(con, [(1, "2020-06-01", "2020-06-10", None)])
    _meas(con, 1, 1, 7001, "2020-05-20 08:00:00", 1.0)    # pre-admission routine
    _meas(con, 2, 1, 7001, "2020-06-02 08:00:00", 3.0)
    _meas(con, 3, 1, 7001, "2020-06-09 08:00:00", 2.0)
    _meas(con, 4, 1, 7001, "2020-06-10 08:00:00", 999.0)  # impossible -> dropped before 'last'
    _meas(con, 5, 1, 7002, "2020-05-25 08:00:00", 9.0)    # acute lab before the stay -> ignored
    specs = {"creatinine": ["2160-0"], "lactate": ["2524-7"]}
    last = extract_inpatient_labs(con, lab_specs=specs, routine_window="all")
    assert last.loc[0, "creatinine"] == 2.0 and pd.isna(last.loc[0, "lactate"])
    worst = extract_inpatient_labs(con, lab_specs=specs, selection_rule="worst", routine_window="stay")
    assert worst.loc[0, "creatinine"] == 3.0
    lookback = extract_inpatient_labs(con, lab_specs=specs, routine_window="lookback")
    assert lookback.loc[0, "creatinine"] == 1.0
    raw = extract_inpatient_labs(con, lab_specs=specs, routine_window="all", outlier_bounds={})
    assert raw.loc[0, "creatinine"] == 999.0
    with pytest.raises(ValueError, match="selection_rule"):
        extract_inpatient_labs(con, lab_specs=specs, selection_rule="nope")
    with pytest.raises(ValueError, match="acute_lab_subset"):
        extract_inpatient_labs(con, lab_specs=specs, acute_lab_subset=["troponin"])


# ---------------------------------------------------------------- index-diagnosis guard
def _dx(con, oid, pid, src, date, vid):
    con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
                "condition_start_date, condition_type_concept_id, condition_source_value, visit_occurrence_id) "
                f"VALUES ({oid}, {pid}, 0, DATE '{date}', 32020, '{src}', {vid})")


def test_exclude_index_diagnoses(con):
    _cohort(con, [(1, "2020-06-01", "2020-06-10", 20)])
    _dx(con, 1, 1, "I50.9", "2020-05-31", 20)   # dated before admission but coded to the index visit
    _dx(con, 2, 1, "E11.9", "2020-01-01", 5)    # genuine history
    guarded = extract_elixhauser_comorbidities(con, source_and_standard=False)
    assert guarded.loc[0, "elix_chf"] == 0 and guarded.loc[0, "elix_dm_uncomp"] == 1
    open_ = extract_elixhauser_comorbidities(con, source_and_standard=False, exclude_index_diagnoses=False)
    assert open_.loc[0, "elix_chf"] == 1
    with pytest.raises(ValueError, match="discharge"):
        extract_elixhauser_comorbidities(con, source_and_standard=False, index_date_col="cohort_end_date")
    ok = extract_elixhauser_comorbidities(con, source_and_standard=False, index_date_col="cohort_end_date",
                                          exclude_index_diagnoses=False)
    assert len(ok) == 1


# ---------------------------------------------------------------- scoring bundle
class _Mean:
    def fit_transform(self, X):
        self.m = np.nanmean(X, axis=0)
        return self.transform(X)

    def transform(self, X):
        X = np.array(X, dtype=float)
        idx = np.where(np.isnan(X))
        X[idx] = np.take(self.m, idx[1])
        return X


class _Threshold:
    def fit(self, X, y):
        self.w = X.mean(axis=0)

    def predict_proba(self, X):
        s = 1 / (1 + np.exp(-(X - self.w).sum(axis=1)))
        return np.column_stack([1 - s, s])


def test_scoring_pipeline_schema_guard_and_roundtrip(tmp_path):
    X = pd.DataFrame({"a": [1.0, 2.0, np.nan, 4.0], "b": [0.0, 1.0, 1.0, 0.0]})
    pipe = OMOPScoringPipeline.fit(X, [0, 1, 1, 0], _Threshold(), imputer=_Mean(), metadata={"cohort": "x"})
    new = pd.DataFrame({"b": [1.0], "a": [np.nan]})       # reordered + missing value -> imputed
    assert pipe.predict_proba(new).shape == (1,)
    with pytest.raises(FeatureSchemaError, match="missing"):
        pipe.predict_proba(new[["a"]])
    with pytest.raises(FeatureSchemaError, match="unexpected"):
        pipe.predict_proba(new.assign(c=1))
    with pytest.raises(FeatureSchemaError, match="non-numeric"):
        pipe.predict_proba(new.assign(a="x"))
    assert pipe.out_of_range(pd.DataFrame({"a": [100.0], "b": [0.0]})) == {"a": 1}
    path = pipe.save(tmp_path / "bundle.pkl")
    again = OMOPScoringPipeline.load(path)
    assert again.feature_names == ["a", "b"] and again.metadata == {"cohort": "x"}
    assert np.allclose(again.predict_proba(new), pipe.predict_proba(new))
    bare = OMOPScoringPipeline(_Threshold(), ["a", "b"])
    with pytest.raises(FeatureSchemaError, match="no imputer"):
        bare.validate(new)
