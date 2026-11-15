"""Age in an inclusion rule is the difference of calendar years, as in CirceR / ATLAS (checked against the official engine)."""
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
    # all born in 1980, the event is on 2020-03-01: completed years are 39 for a birthday after March 1, but the calendar-year
    # difference (which Circe uses) is 40 for everyone; person 5 has no month or day of birth
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id) "
              "VALUES (1,8507,1980,1,15,0,0),(2,8507,1980,3,1,0,0),(3,8507,1980,3,2,0,0),(4,8507,1980,12,31,0,0),(5,8532,1980,NULL,NULL,0,0)")
    c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, "
              "period_type_concept_id) SELECT person_id, person_id, DATE '2015-01-01', DATE '2022-12-31', 0 FROM person")
    c.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, "
              "condition_type_concept_id) SELECT person_id, person_id, 201820, DATE '2020-03-01', 0 FROM person")
    return c


def _definition(age):
    return {"ConceptSets": [{"id": 0, "name": "dm", "expression": {"items": [
        {"concept": {"CONCEPT_ID": 201820}, "isExcluded": False, "includeDescendants": False, "includeMapped": False}]}}],
        "PrimaryCriteria": {"CriteriaList": [{"ConditionOccurrence": {"CodesetId": 0}}], "ObservationWindow": {"PriorDays": 0, "PostDays": 0},
                            "PrimaryCriteriaLimit": {"Type": "First"}},
        "InclusionRules": [{"name": "age", "expression": {"Type": "ALL", "CriteriaList": [], "Groups": [],
                                                           "DemographicCriteriaList": [{"Age": age}]}}],
        "EndStrategy": {"DateOffset": {"DateField": "StartDate", "Offset": 0}}, "CollapseSettings": {"CollapseType": "ERA", "EraPad": 0}}


# the answers CirceR + CohortGenerator gave for these definitions on this CDM
@pytest.mark.parametrize("age, expected", [
    ({"Value": 40, "Op": "gte"}, [1, 2, 3, 4, 5]),
    ({"Value": 40, "Op": "gt"}, []),
    ({"Value": 40, "Op": "eq"}, [1, 2, 3, 4, 5]),
    ({"Value": 40, "Op": "lt"}, []),
    ({"Value": 39, "Extent": 40, "Op": "bt"}, [1, 2, 3, 4, 5]),
    ({"Value": 41, "Op": "gte"}, []),
])
def test_inclusion_rule_age_matches_the_official_engine(age, expected):
    out = execute_circe_cohort(_cdm(), _definition(age))
    assert sorted(out["subject_id"]) == expected
