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
    for pid, did in ((1, 10), (2, 11), (3, 12), (4, 13), (5, 99)):
        c.execute(f"INSERT INTO drug_exposure VALUES ({pid}, {did}, DATE '2020-01-01', DATE '2020-01-02')")
    out = c.execute(compile_circe_to_duckdb(d, cdm_schema="main")).df()
    assert sorted(out["subject_id"]) == [1, 3]                     # 11 and its descendant 13 are removed; 99 is not in the set


def _mapped_fixture():
    """The exclusion fixture with every code carried by a standard concept id (what Circe sees)."""
    c = fx._cdm()
    fx._populate(c)
    c.execute(f"UPDATE condition_occurrence SET condition_concept_id = {fx.PCOS_ICD} WHERE person_id = 2")
    c.execute(f"UPDATE condition_occurrence SET condition_concept_id = {fx.PREDIAB} WHERE person_id = 9")
    c.execute(f"UPDATE drug_exposure SET drug_concept_id = {fx.MET} WHERE person_id = 9")
    c.execute(f"UPDATE condition_occurrence SET condition_concept_id = {fx.DM} WHERE person_id = 10")
    c.execute(f"UPDATE drug_exposure SET drug_concept_id = {fx.INSULIN} WHERE person_id = 11 AND drug_concept_id = 0")
    return c


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
    with pytest.raises(ValueError, match="pcos"):
        metformin_only_prediabetes_pcos_circe("nope")
