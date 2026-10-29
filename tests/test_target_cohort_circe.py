"""The index-stay cohort as Circe JSON: committed file, native compiler agreement with the official engine, and what stays different
from the authoritative SQL (inst/cohorts/target_cohort.sql)."""
import copy
import json
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.build_omop_cdm import build_schema
from omop_etl.circe import compile_circe_to_duckdb, execute_circe_cohort
from omop_etl.circe_definitions import target_cohort_circe

COHORTS = REPO_ROOT / "inst" / "cohorts"

# person -> (year of birth, end of observation)
PEOPLE = {1: (1980, "2020-12-31"), 2: (1980, "2020-12-31"), 3: (2015, "2020-12-31"), 4: (1980, "2020-02-01"), 5: (1980, "2020-12-31"),
          6: (2002, "2020-12-31"), 7: (1980, "2020-03-20"), 8: (1980, "2020-12-31"), 9: (1980, "2020-12-31")}
# visit id, person, concept, start, end, discharged_to
VISITS = [
    (1, 1, 9201, "2020-03-01", "2020-03-05", None),
    (2, 1, 9201, "2020-02-01", "2020-02-04", None),      # earlier stay: the index
    (3, 2, 9201, "2020-03-01", "2020-03-05", 4216643),   # discharged dead, no death record
    (4, 3, 9201, "2020-03-01", "2020-03-05", None),      # a child
    (5, 4, 9201, "2020-01-10", "2020-01-20", None),      # observation ends before discharge + 30 days
    (6, 5, 262, "2020-03-01", "2020-03-05", None),       # ED-to-IP
    (7, 1, 9203, "2020-01-01", "2020-01-01", None),      # not an inpatient stay
    (8, 6, 9201, "2019-12-28", "2020-01-03", None),      # 17 at admission, 18 at discharge: age is taken at discharge
    (9, 7, 9201, "2020-03-01", "2020-03-05", None),      # died 15 days after discharge, observation ends at death
    (10, 8, 9201, "2020-03-01", "2020-03-05", None),     # death before discharge
    (11, 9, 9201, "2020-01-10", "2020-01-20", None),     # death on the discharge day, then another stay after it
    (12, 9, 9201, "2020-05-01", "2020-05-05", None),
]
DEATHS = {7: "2020-03-20", 8: "2020-03-03", 9: "2020-01-20"}


def _cdm():
    c = duckdb.connect()
    build_schema(c)
    for pid, (yob, end) in PEOPLE.items():
        c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                  f"VALUES ({pid}, 8507, {yob}, 0, 0)")
        c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                  f"observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '{end}', 0)")
    for vid, pid, cid, s, e, d in VISITS:
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                  f"visit_type_concept_id, discharged_to_concept_id) VALUES ({vid}, {pid}, {cid}, DATE '{s}', DATE '{e}', 0, {'NULL' if d is None else d})")
    for pid, d in DEATHS.items():
        c.execute(f"INSERT INTO death (person_id, death_date, death_type_concept_id) VALUES ({pid}, DATE '{d}', 0)")
    return c


def _pairs(df):
    return sorted((int(r.subject_id), str(r.cohort_start_date)[:10]) for r in df.itertuples())


# what CirceR + CohortGenerator return for target_cohort_circe() on this CDM (scripts/ohdsi_parity_target_cohort.R repeats the run)
OFFICIAL = [(1, "2020-02-04"), (2, "2020-03-05"), (5, "2020-03-05"), (6, "2020-01-03")]


def test_committed_json_is_the_builder_output():
    assert json.loads((COHORTS / "target_cohort.circe.json").read_text(encoding="utf-8")) == target_cohort_circe()


def test_native_compiler_agrees_with_the_official_engine():
    assert _pairs(execute_circe_cohort(_cdm(), target_cohort_circe())) == OFFICIAL


def test_differences_from_the_authoritative_sql_are_exactly_the_two_documented_ones():
    pytest.importorskip("sqlglot")
    from omop_etl.cohort_sql import run_cohort_sql
    c = _cdm()
    run_cohort_sql(c, COHORTS / "target_cohort.sql", target_cohort_id=1, cdm_database_schema="main", target_database_schema="main",
                   target_cohort_table="cohort", followup_days=30, min_age=18)
    sql = {(int(r[0]), str(r[1])) for r in c.execute("SELECT subject_id, cohort_start_date FROM cohort WHERE cohort_definition_id = 1").fetchall()}
    circe = set(OFFICIAL)
    assert sql == {(1, "2020-02-04"), (5, "2020-03-05"), (6, "2020-01-03"), (7, "2020-03-05")}
    assert sql - circe == {(7, "2020-03-05")}      # died within 30 days of discharge: the SQL accepts the death, Circe wants observation
    assert circe - sql == {(2, "2020-03-05")}      # discharged dead without a death record: Circe has no discharge-disposition criterion


def test_the_stay_that_qualifies_is_the_index_not_the_first_stay():
    # person 9: the first stay fails (death on the discharge day) and the second fails too (the death precedes it), so nobody;
    # give them a death after the second stay's window and the later stay must become the index
    c = _cdm()
    c.execute("UPDATE death SET death_date = DATE '2020-06-30' WHERE person_id = 9")
    c.execute("UPDATE observation_period SET observation_period_end_date = DATE '2020-12-31' WHERE person_id = 9")
    got = _pairs(execute_circe_cohort(c, target_cohort_circe()))
    assert (9, "2020-01-20") in got            # the death is now after the first discharge, so the first stay qualifies and is the index
    c.execute("INSERT INTO death (person_id, death_date, death_type_concept_id) VALUES (1, DATE '2020-02-04', 0)")
    assert 1 not in {p for p, _ in _pairs(execute_circe_cohort(c, target_cohort_circe()))}   # death on a discharge day excludes that stay and later ones


def _primary(item):
    """The target definition with a VisitOccurrence entry event made of ``item``, no follow-up requirement."""
    d = copy.deepcopy(target_cohort_circe())
    d["PrimaryCriteria"]["CriteriaList"] = [{"VisitOccurrence": item}]
    d["PrimaryCriteria"]["ObservationWindow"] = {"PriorDays": 0, "PostDays": 0}
    return d


def test_date_adjustment_offsets_and_defaults():
    c = _cdm()
    base = {"CodesetId": 0}
    start = _pairs(execute_circe_cohort(c, _primary(dict(base))))
    assert (2, "2020-03-01") in start and (1, "2020-02-01") in start                       # no adjustment: the admission date
    end_plus2 = _pairs(execute_circe_cohort(c, _primary({**base, "DateAdjustment": {"StartWith": "END_DATE", "StartOffset": 2,
                                                                                     "EndWith": "END_DATE", "EndOffset": 2}})))
    assert (1, "2020-02-06") in end_plus2 and (2, "2020-03-07") in end_plus2
    before = _pairs(execute_circe_cohort(c, _primary({**base, "DateAdjustment": {"StartWith": "START_DATE", "StartOffset": -1,
                                                                                  "EndWith": "END_DATE", "EndOffset": 0}})))
    assert (1, "2020-01-31") in before


def test_correlated_criteria_any_group_and_unsupported_forms():
    c = _cdm()
    item = {"CodesetId": 0, "CorrelatedCriteria": {"Type": "ANY", "CriteriaList": [
        {"Criteria": {"Death": {}}, "StartWindow": {"Start": {"Days": 0, "Coeff": 1}, "End": {"Days": 30, "Coeff": 1}},
         "Occurrence": {"Type": 2, "Count": 1}}], "DemographicCriteriaList": [], "Groups": []}}
    d = _primary(item)
    got = _pairs(execute_circe_cohort(c, d))
    assert {p for p, _ in got} == {7, 8, 9}               # a death 0-30 days after an admission: person 7 (15 days), 8 (2 days), 9 (10 days)
    nested = copy.deepcopy(item)
    nested["CorrelatedCriteria"]["Groups"] = [{"Type": "AT_LEAST", "CriteriaList": []}]                  # a subgroup is checked too
    with pytest.raises(NotImplementedError, match="group 1: a AT_LEAST group needs a Count"):
        compile_circe_to_duckdb(_primary(nested))
    bad = {"CodesetId": 0, "DateAdjustment": {"StartWith": "CODE_DATE"}}
    with pytest.raises(NotImplementedError, match="DateAdjustment"):
        compile_circe_to_duckdb(_primary(bad))
    # CorrelatedCriteria on a criterion inside an inclusion rule is still refused
    rule = copy.deepcopy(target_cohort_circe())
    rule["InclusionRules"] = [{"name": "r", "expression": {"Type": "ALL", "CriteriaList": [
        {"Criteria": {"VisitOccurrence": {"CodesetId": 0, "CorrelatedCriteria": item["CorrelatedCriteria"]}},
         "StartWindow": {"Start": {"Coeff": -1}, "End": {"Days": 0, "Coeff": 1}}, "Occurrence": {"Type": 2, "Count": 1}}]}}]
    with pytest.raises(NotImplementedError, match="CorrelatedCriteria"):
        compile_circe_to_duckdb(rule)


def test_death_is_a_domain_for_entry_events():
    c = _cdm()
    d = _primary({})
    d["PrimaryCriteria"]["CriteriaList"] = [{"Death": {}}]
    assert sorted(execute_circe_cohort(c, d)["subject_id"]) == [7, 8, 9]


def test_shipped_cohort_definitions_are_found_by_the_helper():
    from omop_etl import cohort_definitions_dir
    d = cohort_definitions_dir()
    assert d.is_dir() and (d / "target_cohort.sql").exists() and (d / "target_cohort.circe.json").exists()
    assert d.resolve() == COHORTS.resolve()                      # a source checkout: inst/cohorts
