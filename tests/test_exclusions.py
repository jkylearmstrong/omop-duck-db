"""Metformin-only prediabetes / PCOS exclusion and the configurable diabetes-medication rule of the eDERRI phenotype."""
import sys
import warnings
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema
from omop_etl.exclusions import find_metformin_only_prediabetes_pcos, metformin_only_sql

MET, MET_BRAND, GLIPIZIDE_MET, INSULIN, SU = 1503297, 1000001, 1000002, 1000003, 1000004
A10, A10A, A10BA, A10BD = 21600712, 21600713, 21600745, 21600765
PREDIAB, PCOS_ICD, PCOS_SNOMED, DM, DM_CHILD = 37018196, 40443308, 36683296, 201820, 1000005


def _cdm(with_vocab=True):
    c = duckdb.connect()
    build_schema(c)
    if with_vocab:
        anc = [(MET, MET), (MET, MET_BRAND), (MET, GLIPIZIDE_MET), (A10, A10), (A10, A10A), (A10, A10BA), (A10, A10BD),
               (A10, MET_BRAND), (A10, GLIPIZIDE_MET), (A10, INSULIN), (A10, SU), (A10A, A10A), (A10A, INSULIN),
               (A10BA, A10BA), (A10BA, MET_BRAND), (A10BD, A10BD), (A10BD, GLIPIZIDE_MET), (DM, DM), (DM, DM_CHILD),
               (PREDIAB, PREDIAB), (PCOS_ICD, PCOS_ICD), (PCOS_SNOMED, PCOS_SNOMED)]
        for a, d in anc:
            c.execute(f"INSERT INTO concept_ancestor VALUES ({a}, {d}, 0, 0)")
    return c


def _person(c, pid, drugs=(), conds=(), drug_src=None):
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
              f"VALUES ({pid}, 8507, 1970, 0, 0)")
    for i, d in enumerate(drugs):
        c.execute("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, "
                  "drug_type_concept_id, drug_source_value) VALUES (?, ?, ?, DATE '2019-01-01', DATE '2019-01-31', 0, ?)",
                  [pid * 100 + i, pid, d, drug_src])
    for i, (cid, src) in enumerate(conds):
        c.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
                  "condition_start_date, condition_type_concept_id, condition_source_value) "
                  f"VALUES (?, ?, ?, DATE '2019-02-01', 0, ?)", [pid * 100 + i, pid, cid, src])


def _populate(c):
    _person(c, 1, [MET_BRAND], [(PREDIAB, None)])                                  # metformin + prediabetes -> excluded
    _person(c, 2, [MET], [(0, "E28.2")])                                           # metformin + PCOS by ICD-10 source value
    _person(c, 3, [MET_BRAND, INSULIN], [(PREDIAB, None)])                         # + insulin -> kept
    _person(c, 4, [MET_BRAND], [(PREDIAB, None), (DM_CHILD, None)])                # + diabetes dx -> kept
    _person(c, 5, [MET_BRAND], [])                                                 # metformin only, no prediabetes/PCOS -> kept
    _person(c, 6, [], [(PREDIAB, None)])                                           # prediabetes, no metformin -> kept
    _person(c, 7, [MET_BRAND], [(PCOS_SNOMED, None)])                              # PCOS through the SNOMED-side concept
    _person(c, 8, [GLIPIZIDE_MET], [(PREDIAB, None)])                              # metformin combination = another drug -> kept
    _person(c, 9, [0], [(0, "R73.03")], drug_src="Metformin HCl 500 mg")           # unmapped drug, source value only -> excluded
    _person(c, 10, [MET_BRAND], [(0, "E11.9")])                                    # diabetes by source value -> kept (and no prediabetes)
    _person(c, 11, [MET_BRAND, 0], [(PREDIAB, None)], drug_src=None)
    c.execute("UPDATE drug_exposure SET drug_source_value = 'insulin glargine' WHERE drug_exposure_id = 1101")  # unmapped insulin -> kept


def test_default_icd10_set():
    c = _cdm()
    _populate(c)
    out = find_metformin_only_prediabetes_pcos(c)
    assert out["person_id"].tolist() == [1, 2, 9]
    row = out.set_index("person_id")
    assert row.loc[1, "has_prediabetes"] and not row.loc[1, "has_pcos"]
    assert row.loc[2, "has_pcos"] and not row.loc[2, "has_prediabetes"]


def test_snomed_and_both_pcos_sets():
    c = _cdm()
    _populate(c)
    assert find_metformin_only_prediabetes_pcos(c, "snomed")["person_id"].tolist() == [1, 7, 9]   # 2 is ICD-only; 7 is SNOMED-only
    assert find_metformin_only_prediabetes_pcos(c, "both")["person_id"].tolist() == [1, 2, 7, 9]


def test_without_vocabulary_falls_back_to_ids_and_source_values():
    c = _cdm(with_vocab=False)
    _populate(c)
    with pytest.warns(UserWarning, match="concept_ancestor"):
        out = find_metformin_only_prediabetes_pcos(c)
    # no descendants: brand metformin ids (1000001) are unknown, so only source-value / listed-id matches remain
    assert out["person_id"].tolist() == [2, 9]


def test_arguments_and_sql():
    with pytest.raises(ValueError, match="pcos"):
        metformin_only_sql("nope")
    assert "R7303" in metformin_only_sql("icd10") and "E282" in metformin_only_sql("both") and "E282" not in metformin_only_sql("snomed")


# ---------------------------------------------------------------------------------------- cohort integration
def _stay_cdm(with_vocab=True):
    c = _cdm(with_vocab)
    _populate(c)
    for pid in range(1, 12):
        c.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                  f"observation_period_end_date, period_type_concept_id) VALUES ({pid}, {pid}, DATE '2015-01-01', DATE '2022-01-01', 0)")
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
                  f"visit_type_concept_id) VALUES ({pid}, {pid}, 9201, DATE '2020-03-01', DATE '2020-03-05', 0)")
    return c


def _ederri(c, **kw):
    from omop_etl import build_ederri_diabetes_cohort
    return build_ederri_diabetes_cohort(c, transfer_discharge_concept_ids=(), **kw)


def test_metformin_only_exclusion_in_the_cohort_and_attrition():
    # give the metformin concepts a drug name so they qualify through the 'names' medication arm
    c = _stay_cdm()
    c.execute("UPDATE drug_exposure SET drug_source_value = 'metformin' WHERE drug_concept_id IN (1000001, 1503297, 1000002)")
    base = _ederri(c)
    assert {1, 2, 5, 7, 8}.issubset(set(base["subject_id"]))                   # metformin users qualify through the drug arm
    out = _ederri(c, exclude_metformin_only_prediabetes_pcos=True)
    assert set(base["subject_id"]) - set(out["subject_id"]) == {1, 2, 9} & set(base["subject_id"])
    assert "Not metformin-only prediabetes / PCOS (patient-level)" in out.attrs["attrition"].step_name.tolist()
    both = _ederri(c, exclude_metformin_only_prediabetes_pcos="both")
    assert 7 not in set(both["subject_id"]) and 7 in set(out["subject_id"])
    assert c.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = '_study_metformin_only'").fetchone()[0] == 0
    with pytest.raises(ValueError, match="exclude_metformin_only"):
        _ederri(c, exclude_metformin_only_prediabetes_pcos="maybe")


def test_diabetes_medication_rules(tmp_path):
    c = _stay_cdm()
    names = set(_ederri(c)["subject_id"])
    assert names == {9, 10, 11}                                                 # names rule: metformin source name (9), E11 source code (10), insulin name (11)
    # the ATC rule adds everyone with a drug concept under A10 (brands and combinations included)
    atc = set(_ederri(c, diabetes_drug_rule="atc_a10")["subject_id"])
    assert {1, 3, 5, 7, 8, 11}.issubset(atc) and 6 not in atc                   # metformin brands (A10BA) and combos (A10BD) qualify
    (tmp_path / "cuis.txt").write_text("'1000004'\n'999999'\n", encoding="utf-8")
    c.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, "
              "valid_start_date, valid_end_date) VALUES (1000004, 'x', 'Drug', 'RxNorm', 'Ingredient', '1000004', DATE '1970-01-01', DATE '2099-12-31')")
    _person(c, 12, [SU], [])
    c.execute("INSERT INTO observation_period VALUES (12, 12, DATE '2015-01-01', DATE '2022-01-01', 0)")
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, "
              "visit_type_concept_id) VALUES (12, 12, 9201, DATE '2020-03-01', DATE '2020-03-05', 0)")
    with pytest.warns(UserWarning, match="not RxNorm concepts"):
        cuis = set(_ederri(c, diabetes_drug_rule="rxnorm_cuis", diabetes_rxnorm_cuis=tmp_path / "cuis.txt")["subject_id"])
    assert 12 not in cuis                                                       # SU has no ancestor row for the ingredient (1000004 -> SU only if listed)
    c.execute(f"INSERT INTO concept_ancestor VALUES (1000004, {SU}, 0, 0)")
    cuis = set(_ederri(c, diabetes_drug_rule="rxnorm_cuis", diabetes_rxnorm_cuis=[1000004])["subject_id"])
    assert 12 in cuis and 1 not in cuis
    for kw, msg in (({"diabetes_drug_rule": "x"}, "diabetes_drug_rule must"),
                    ({"diabetes_drug_rule": "rxnorm_cuis"}, "needs diabetes_rxnorm_cuis"),
                    ({"diabetes_rxnorm_cuis": [1]}, "only used with")):
        with pytest.raises(ValueError, match=msg):
            _ederri(c, **kw)
    empty = _stay_cdm(with_vocab=False)
    with pytest.raises(ValueError, match="concept_ancestor"):
        _ederri(empty, diabetes_drug_rule="atc_a10")
