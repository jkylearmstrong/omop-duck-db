"""Circe constructs the native compiler evaluates: First, AdditionalCriteria, source-concept and Unit attributes, AT_LEAST / AT_MOST and
nested groups. The expected cohorts are what CirceR + CohortGenerator returned for these definitions on this CDM
(scripts/ohdsi_parity_constructs.R repeats the run)."""
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.build_omop_cdm import build_schema
from omop_etl.circe import execute_circe_cohort


def _cdm():
    c = duckdb.connect()
    build_schema(c)
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
              "SELECT i, 8507, 1970, 0, 0 FROM range(1, 11) t(i)")
    c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, "
              "period_type_concept_id) SELECT person_id, person_id, DATE '2015-01-01', DATE '2022-12-31', 0 FROM person")
    n = {"cond": 0, "proc": 0, "meas": 0}

    def cond(pid, cid, d):
        n["cond"] += 1
        c.execute(f"INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
                  f"condition_type_concept_id) VALUES ({n['cond']}, {pid}, {cid}, DATE '{d}', 0)")

    def proc(pid, cid, src, d):
        n["proc"] += 1
        c.execute(f"INSERT INTO procedure_occurrence (procedure_occurrence_id, person_id, procedure_concept_id, procedure_date, "
                  f"procedure_type_concept_id, procedure_source_concept_id) VALUES ({n['proc']}, {pid}, {cid}, DATE '{d}', 0, {src})")

    def meas(pid, unit, d):
        n["meas"] += 1
        c.execute(f"INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, "
                  f"value_as_number, unit_concept_id) VALUES ({n['meas']}, {pid}, 3004410, DATE '{d}', 0, 7.0, {unit})")

    # 201820 = diabetes (A), 4024659 = gestational (B), procedure 4000001 with source concept 4000002 (S) or 0
    cond(1, 201820, "2019-01-01"); cond(1, 201820, "2020-01-01"); proc(1, 4000001, 4000002, "2019-06-01")
    cond(2, 201820, "2020-01-01"); meas(2, 8554, "2019-12-01")
    cond(3, 4024659, "2019-01-01"); cond(3, 201820, "2020-06-01")
    cond(4, 201820, "2018-01-01"); cond(4, 201820, "2018-02-01"); cond(4, 201820, "2021-01-01"); meas(4, 8753, "2018-01-15")
    proc(5, 4000001, 0, "2019-01-01")
    cond(6, 201820, "2019-03-01"); cond(6, 4024659, "2019-03-10"); proc(6, 4000001, 4000002, "2019-03-05")
    cond(7, 201820, "2019-05-01"); proc(7, 4000001, 0, "2019-05-20"); meas(7, 8554, "2019-05-02")
    cond(8, 4024659, "2019-02-01"); proc(8, 4000001, 4000002, "2019-02-10")
    cond(9, 201820, "2018-01-01"); cond(9, 201820, "2019-08-05"); cond(9, 4024659, "2019-08-01"); proc(9, 4000001, 0, "2019-08-20")
    meas(10, 8753, "2019-01-01"); meas(10, 8554, "2019-02-01")
    return c


def _cs(cs_id, cid):
    return {"id": cs_id, "name": f"cs{cs_id}", "expression": {"items": [{"concept": {"CONCEPT_ID": cid}, "isExcluded": False,
                                                                        "includeDescendants": False, "includeMapped": False}]}}


SETS = [_cs(0, 201820), _cs(1, 4024659), _cs(2, 4000001), _cs(3, 4000002), _cs(4, 3004410)]


def _win(s, e):
    return {"Start": {"Days": abs(s), "Coeff": -1 if s < 0 else 1}, "End": {"Days": abs(e), "Coeff": -1 if e < 0 else 1}}


def _wrap(crit, s, e):
    return {"Criteria": crit, "StartWindow": _win(s, e), "Occurrence": {"Type": 2, "Count": 1}}


def _grp(gtype, crit, groups=(), count=None):
    g = {"Type": gtype, "CriteriaList": list(crit), "Groups": list(groups), "DemographicCriteriaList": []}
    if count is not None:
        g["Count"] = count
    return g


def _base(primary, rules=(), limit="All", **extra):
    d = {"ConceptSets": SETS, "PrimaryCriteria": {"CriteriaList": [primary], "ObservationWindow": {"PriorDays": 0, "PostDays": 0},
                                                  "PrimaryCriteriaLimit": {"Type": limit}},
         "InclusionRules": [{"name": "r", "expression": r} for r in rules],
         "EndStrategy": {"DateOffset": {"DateField": "StartDate", "Offset": 0}}, "CollapseSettings": {"CollapseType": "ERA", "EraPad": 0}}
    d.update(extra)
    return d


A = {"ConditionOccurrence": {"CodesetId": 0}}
B = {"ConditionOccurrence": {"CodesetId": 1}}
P = {"ProcedureOccurrence": {"CodesetId": 2}}
M = {"Measurement": {"CodesetId": 4}}
UNIT = [{"CONCEPT_ID": 8554, "CONCEPT_NAME": "percent"}]

CASES = {
    "first_primary": (_base({"ConditionOccurrence": {"CodesetId": 0, "First": True}}),
                      "1@19-01-01 2@20-01-01 3@20-06-01 4@18-01-01 6@19-03-01 7@19-05-01 9@18-01-01"),
    "first_in_rule": (_base(A, [_grp("ALL", [_wrap({"ConditionOccurrence": {"CodesetId": 1, "First": True}}, -365, 365)])]), "6@19-03-01 9@19-08-05"),
    # the first A of person 9 is in 2018, outside the window, so a later A inside it does not count
    "first_window": (_base(B, [_grp("ALL", [_wrap({"ConditionOccurrence": {"CodesetId": 0, "First": True}}, -30, 30)])]), "6@19-03-10"),
    # the first measurement has the wrong unit, so person 10 does not qualify although a later one has the right unit
    "first_attr": (_base({"Measurement": {"CodesetId": 4, "Unit": UNIT, "First": True}}), "2@19-12-01 7@19-05-02"),
    "unit": (_base({"Measurement": {"CodesetId": 4, "Unit": UNIT}}), "2@19-12-01 7@19-05-02 10@19-02-01"),
    "additional": (_base(A, AdditionalCriteria=_grp("ALL", [_wrap(P, 0, 60)])), "6@19-03-01 7@19-05-01 9@19-08-05"),
    # the primary limit comes first: person 9's first A fails the additional criteria and the later one is never considered
    "additional_limit": (_base(A, limit="First", AdditionalCriteria=_grp("ALL", [_wrap(P, 0, 60)])), "6@19-03-01 7@19-05-01"),
    "src_concept": (_base({"ProcedureOccurrence": {"CodesetId": 2, "ProcedureSourceConcept": 3}}), "1@19-06-01 6@19-03-05 8@19-02-10"),
    "src_concept_only": (_base({"ProcedureOccurrence": {"ProcedureSourceConcept": 3}}), "1@19-06-01 6@19-03-05 8@19-02-10"),
    "any_group": (_base(A, [_grp("ANY", [_wrap(B, -30, 30), _wrap(P, -30, 30)])]), "6@19-03-01 7@19-05-01 9@19-08-05"),
    "at_least_2": (_base(A, [_grp("AT_LEAST", [_wrap(B, -30, 30), _wrap(P, -30, 30), _wrap(M, -30, 30)], count=2)]), "6@19-03-01 7@19-05-01 9@19-08-05"),
    "at_most_1": (_base(A, [_grp("AT_MOST", [_wrap(B, -30, 30), _wrap(P, -30, 30), _wrap(M, -30, 30)], count=1)]),
                  "1@19-01-01 1@20-01-01 2@20-01-01 3@20-06-01 4@18-01-01 4@18-02-01 4@21-01-01 9@18-01-01"),
    "nested": (_base(A, [_grp("ALL", [_wrap(P, -60, 60)], groups=[_grp("ANY", [_wrap(B, -30, 30), _wrap(M, -30, 30)])])]), "6@19-03-01 7@19-05-01 9@19-08-05"),
    "nested_at_least": (_base(A, [_grp("ANY", [_wrap(B, -30, 30)], groups=[_grp("AT_LEAST", [_wrap(P, -60, 60), _wrap(M, -60, 60)], count=2)])]),
                        "6@19-03-01 7@19-05-01 9@19-08-05"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_native_compiler_returns_what_the_official_engine_returns(name):
    definition, expected = CASES[name]
    out = execute_circe_cohort(_cdm(), definition)
    got = sorted(f"{int(r.subject_id)}@{str(r.cohort_start_date)[2:10]}" for r in out.itertuples())
    assert got == sorted(expected.split())
