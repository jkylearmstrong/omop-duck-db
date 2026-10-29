"""Circe definitions of the eDERRI study logic: committed files, compiler semantics, and agreement with the package SQL."""
import json
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.circe import compile_circe_to_duckdb, execute_circe_cohort
from omop_etl.circe_definitions import (PCOS_SETS, diabetes_phenotype_circe, metformin_only_prediabetes_pcos_circe,
                                        write_circe_definitions)
from omop_etl.exclusions import find_metformin_only_prediabetes_pcos

import test_exclusions as fx

COHORTS = REPO_ROOT / "cohorts"
GDM = 4024659


def test_committed_files_match_the_builders(tmp_path):
    for path in write_circe_definitions(tmp_path):
        assert (COHORTS / path.name).read_text(encoding="utf-8") == path.read_text(encoding="utf-8"), path.name
    for p in PCOS_SETS:
        d = json.loads((COHORTS / f"metformin_only_prediabetes_pcos_{p}.circe.json").read_text(encoding="utf-8"))
        assert {"ConceptSets", "PrimaryCriteria", "InclusionRules", "EndStrategy"} <= set(d)


def test_excluded_concept_set_items_are_subtracted():
    d = {"ConceptSets": [{"id": 0, "name": "a10 without a10ba", "expression": {"items": [
        {"concept": {"CONCEPT_ID": 10}, "includeDescendants": True, "isExcluded": False},
        {"concept": {"CONCEPT_ID": 11}, "includeDescendants": True, "isExcluded": True}]}}],
        "PrimaryCriteria": {"CriteriaList": [{"DrugExposure": {"CodesetId": 0}}], "ObservationWindow": {"PriorDays": 0, "PostDays": 0}}}
    c = duckdb.connect()
    c.execute("CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT)")
    c.execute("INSERT INTO concept_ancestor VALUES (10,10),(10,11),(10,12),(11,11),(11,13),(10,13)")
    c.execute("CREATE TABLE drug_exposure (person_id BIGINT, drug_concept_id BIGINT, drug_exposure_start_date DATE, drug_exposure_end_date DATE)")
    c.execute("CREATE TABLE observation_period (person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE)")
    c.execute("INSERT INTO observation_period SELECT unnest([1,2,3,4,5]), DATE '2015-01-01', DATE '2022-01-01'")
    for pid, did in ((1, 10), (2, 11), (3, 12), (4, 13), (5, 99)):
        c.execute(f"INSERT INTO drug_exposure VALUES ({pid}, {did}, DATE '2020-01-01', DATE '2020-01-02')")
    out = c.execute(compile_circe_to_duckdb(d, cdm_schema="main")).df()
    assert sorted(out["subject_id"]) == [1, 3]                     # 11 and its descendant 13 are removed; 99 is not in the set


def _observed(c):
    """Every person observed 2015-2022, which the official engine (and now the native compiler) requires of an index event."""
    c.execute("DELETE FROM observation_period")
    c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
              "observation_period_end_date, period_type_concept_id) SELECT person_id, person_id, DATE '2015-01-01', DATE '2022-01-01', 0 FROM person")
    return c


def _mapped_fixture():
    """The exclusion fixture with every code carried by a standard concept id (what Circe sees)."""
    c = fx._cdm()
    fx._populate(c)
    c.execute(f"UPDATE condition_occurrence SET condition_concept_id = {fx.PCOS_ICD} WHERE person_id = 2")
    c.execute(f"UPDATE condition_occurrence SET condition_concept_id = {fx.PREDIAB} WHERE person_id = 9")
    c.execute(f"UPDATE drug_exposure SET drug_concept_id = {fx.MET} WHERE person_id = 9")
    c.execute(f"UPDATE condition_occurrence SET condition_concept_id = {fx.DM} WHERE person_id = 10")
    c.execute(f"UPDATE drug_exposure SET drug_concept_id = {fx.INSULIN} WHERE person_id = 11 AND drug_concept_id = 0")
    return _observed(c)


@pytest.mark.parametrize("pcos", PCOS_SETS)
def test_metformin_only_circe_matches_the_finder_on_mapped_data(pcos):
    c = _mapped_fixture()
    circe = execute_circe_cohort(c, metformin_only_prediabetes_pcos_circe(pcos))
    found = find_metformin_only_prediabetes_pcos(c, pcos)
    assert sorted(circe["subject_id"]) == found["person_id"].tolist()
    assert sorted(circe["subject_id"]) == {"icd10": [1, 2, 9], "snomed": [1, 7, 9], "both": [1, 2, 7, 9]}[pcos]
    assert circe["cohort_start_date"].astype(str).eq("2019-01-01").all()          # entry = first metformin exposure


def test_circe_cannot_see_source_value_only_records():
    c = fx._cdm()
    fx._populate(c)                                                 # persons 2 and 9 carry only source values
    _observed(c)
    circe = set(execute_circe_cohort(c, metformin_only_prediabetes_pcos_circe("icd10"))["subject_id"])
    assert circe == {1, 11}      # 2 and 9 are missed (source values only); 11's insulin is only a name, so it looks metformin-only
    assert set(find_metformin_only_prediabetes_pcos(c)["person_id"]) == {1, 2, 9}


def test_diabetes_phenotype_circe_covers_the_three_pathways():
    c = fx._cdm()
    a1c = [fx.MET]
    for pid, born in ((1, 1970), (2, 1970), (3, 1970), (4, 1970), (5, 1970), (6, 2015), (7, 1970)):
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, 8507, {born}, 0, 0)")
    c.execute(f"INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
              f"condition_type_concept_id) VALUES (1, 1, {fx.DM_CHILD}, DATE '2019-01-01', 0)")           # diagnosis (descendant)
    c.execute(f"INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
              f"drug_exposure_end_date, drug_type_concept_id) VALUES (2, 2, {fx.INSULIN}, DATE '2019-01-01', DATE '2019-01-31', 0)")
    for mid, pid, cid, val in ((3, 3, 3004410, 7.2), (4, 4, 3004410, 5.4), (5, 5, 40758583, 6.5), (6, 6, 3004410, 9.0)):
        c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                  f"measurement_type_concept_id, value_as_number) VALUES ({mid}, {pid}, {cid}, DATE '2019-01-01', 0, {val})")
    _observed(c)
    out = execute_circe_cohort(c, diabetes_phenotype_circe())
    assert sorted(out["subject_id"]) == [1, 2, 3, 5]               # 4 normal HbA1c, 6 is a child, 7 has no evidence
    with pytest.raises(ValueError, match="rxnorm_concept_ids"):
        diabetes_phenotype_circe("rxnorm_concepts")
    d = diabetes_phenotype_circe("rxnorm_concepts", [fx.SU])
    c.execute(f"INSERT INTO concept_ancestor VALUES ({fx.SU}, {fx.SU}, 0, 0)")
    c.execute(f"INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
              f"drug_exposure_end_date, drug_type_concept_id) VALUES (7, 7, {fx.SU}, DATE '2019-01-01', DATE '2019-01-31', 0)")
    assert 7 in set(execute_circe_cohort(c, d, target_cohort_id=2)["subject_id"])
    with pytest.raises(ValueError, match="drug_rule"):
        diabetes_phenotype_circe("nope")


def test_gestational_diabetes_is_excluded_from_the_diagnosis_arm_unless_asked():
    c = fx._cdm()
    c.execute(f"INSERT INTO concept_ancestor VALUES ({fx.DM}, {GDM}, 0, 0), ({GDM}, {GDM}, 0, 0)")
    for pid in (1, 2):
        c.execute(f"INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES ({pid}, 8532, 1990, 0, 0)")
    for cid, pid, cond in ((1, 1, GDM), (2, 2, fx.DM_CHILD)):
        c.execute(f"INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
                  f"condition_type_concept_id) VALUES ({cid}, {pid}, {cond}, DATE '2019-01-01', 0)")
    _observed(c)
    assert sorted(execute_circe_cohort(c, diabetes_phenotype_circe())["subject_id"]) == [2]
    assert sorted(execute_circe_cohort(c, diabetes_phenotype_circe(include_gestational=True))["subject_id"]) == [1, 2]
    with pytest.raises(ValueError, match="pcos"):
        metformin_only_prediabetes_pcos_circe("nope")


# --- ATLAS semantics the native compiler must follow (found by running PhenotypeLibrary definitions through CirceR) -------

def _atlas_cdm():
    c = duckdb.connect()
    c.execute("CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT)")
    c.execute("CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id BIGINT, condition_start_date DATE, condition_end_date DATE)")
    c.execute("CREATE TABLE observation (person_id BIGINT, observation_concept_id BIGINT, observation_date DATE)")
    c.execute("CREATE TABLE observation_period (person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE)")
    c.execute("CREATE TABLE person (person_id BIGINT, gender_concept_id INT, year_of_birth INT, month_of_birth INT, day_of_birth INT)")
    for pid in (1, 2, 3):
        c.execute(f"INSERT INTO person VALUES ({pid}, 8507, 1960, 1, 1)")
        c.execute(f"INSERT INTO observation_period VALUES ({pid}, DATE '2010-01-01', DATE '2023-01-01')")
    return c


def _atlas_def(rules=(), primary_limit="All", expression_limit="All", end=None, pad=0, primary=None, extra=None):
    d = {"ConceptSets": [{"id": 0, "name": "a", "expression": {"items": [{"concept": {"CONCEPT_ID": 100}}]}},
                         {"id": 1, "name": "b", "expression": {"items": [{"concept": {"CONCEPT_ID": 200}}]}}],
         "PrimaryCriteria": {"CriteriaList": primary or [{"ConditionOccurrence": {"CodesetId": 0}}],
                             "ObservationWindow": {"PriorDays": 0, "PostDays": 0}, "PrimaryCriteriaLimit": {"Type": primary_limit}},
         "ExpressionLimit": {"Type": expression_limit}, "InclusionRules": list(rules),
         "EndStrategy": end if end is not None else {"DateOffset": {"DateField": "StartDate", "Offset": 0}},
         "CollapseSettings": {"CollapseType": "ERA", "EraPad": pad}}
    d.update(extra or {})
    return d


def _b_within_year():
    return {"name": "b within 365 days", "expression": {"Type": "ALL", "Groups": [], "CriteriaList": [{
        "Criteria": {"ConditionOccurrence": {"CodesetId": 1}},
        "StartWindow": {"Start": {"Days": 0, "Coeff": -1}, "End": {"Days": 365, "Coeff": 1}},
        "Occurrence": {"Type": 2, "Count": 1}}]}}


def _run(c, d):
    return c.execute(compile_circe_to_duckdb(d, cdm_schema="main")).df()


def test_primary_limit_comes_before_the_inclusion_rules():
    c = _atlas_cdm()
    c.execute("INSERT INTO condition_occurrence VALUES (1, 100, DATE '2015-01-01', NULL), (1, 100, DATE '2016-01-01', NULL), (1, 200, DATE '2016-06-01', NULL), "
              "(2, 100, DATE '2015-01-01', NULL), (2, 200, DATE '2015-03-01', NULL)")
    # ATLAS keeps the person's FIRST entry event and then applies the rules: person 1's first entry fails the rule, so person 1 is out
    first = _run(c, _atlas_def([_b_within_year()], primary_limit="First"))
    assert sorted(first["subject_id"]) == [2]
    every = _run(c, _atlas_def([_b_within_year()], primary_limit="All"))
    assert sorted(every["subject_id"]) == [1, 2]                      # all entries are evaluated, the later one of person 1 qualifies


def test_end_strategy_expression_limit_and_era_collapse():
    c = _atlas_cdm()
    c.execute("INSERT INTO condition_occurrence VALUES (1, 100, DATE '2015-01-01', NULL), (1, 100, DATE '2015-01-20', NULL), (1, 100, DATE '2016-01-01', NULL)")
    plus30 = {"DateOffset": {"DateField": "EndDate", "Offset": 30}}
    out = _run(c, _atlas_def(end=plus30))                              # an event ends the day after it starts; +30 days
    ymd = lambda col: [d.strftime("%Y-%m-%d") for d in col]
    assert list(zip(ymd(out["cohort_start_date"]), ymd(out["cohort_end_date"]))) == [
        ("2015-01-01", "2015-02-20"), ("2016-01-01", "2016-02-01")]                              # first two events overlap and become one era
    assert len(_run(c, _atlas_def(end=plus30, expression_limit="First"))) == 1
    assert len(_run(c, _atlas_def(end=plus30, pad=400))) == 1          # a 400-day pad joins all three
    default_end = _run(c, _atlas_def(end={}))                          # no EndStrategy: the end of the observation period, one era
    assert len(default_end) == 1 and ymd(default_end["cohort_end_date"]) == ["2023-01-01"]


def test_observation_domain_is_supported_and_events_need_an_observation_period():
    c = _atlas_cdm()
    c.execute("INSERT INTO observation VALUES (1, 100, DATE '2015-01-01'), (3, 100, DATE '2005-01-01')")      # person 3 before the observation period
    out = _run(c, _atlas_def(primary=[{"Observation": {"CodesetId": 0}}]))
    assert sorted(out["subject_id"]) == [1]


@pytest.mark.parametrize("bad, match", [
    ({"AdditionalCriteria": {"Type": "ALL", "CriteriaList": [{}]}}, "AdditionalCriteria"),
    ({"PrimaryCriteria": {"CriteriaList": [{"ConditionOccurrence": {"CodesetId": 0, "First": True}}], "ObservationWindow": {}}}, "First"),
    ({"PrimaryCriteria": {"CriteriaList": [{"ConditionOccurrence": {"CodesetId": 0, "Unit": [{"CONCEPT_ID": 1}]}}], "ObservationWindow": {}}}, "Unit"),
    ({"PrimaryCriteria": {"CriteriaList": [{"Death": {}}], "ObservationWindow": {}}}, "Death"),
    ({"ConceptSets": [{"id": 0, "name": "m", "expression": {"items": [{"concept": {"CONCEPT_ID": 1}, "includeMapped": True}]}}]}, "includeMapped"),
])
def test_constructs_that_would_change_the_cohort_are_refused_not_ignored(bad, match):
    d = _atlas_def()
    d.update(bad)
    with pytest.raises(NotImplementedError, match=match):
        compile_circe_to_duckdb(d)


def test_unknown_numeric_operator_is_refused_and_between_is_supported():
    c = _atlas_cdm()
    c.execute("CREATE TABLE measurement (person_id BIGINT, measurement_concept_id BIGINT, measurement_date DATE, value_as_number DOUBLE)")
    c.execute("INSERT INTO measurement VALUES (1, 100, DATE '2015-01-01', 5.0), (2, 100, DATE '2015-01-01', 9.0)")
    def mdef(op):
        return _atlas_def(primary=[{"Measurement": {"CodesetId": 0, "ValueAsNumber": {"Value": 4, "Extent": 6, "Op": op}}}])
    assert sorted(_run(c, mdef("bt"))["subject_id"]) == [1]
    assert sorted(_run(c, mdef("!bt"))["subject_id"]) == [2]
    with pytest.raises(NotImplementedError, match="operator"):
        compile_circe_to_duckdb(mdef("between-ish"))


def test_custom_era_end_strategy_warns():
    with pytest.warns(UserWarning, match="CustomEra"):
        compile_circe_to_duckdb(_atlas_def(end={"CustomEra": {"DrugCodesetId": 1, "GapDays": 0, "Offset": 0}}))
