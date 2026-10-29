"""Study cohort definitions as OHDSI Circe (ATLAS) JSON.

Study logic belongs in standard OHDSI artifacts, not in package-specific SQL: a Circe definition imports into ATLAS, runs
through CohortGenerator at any site and compiles to DuckDB here (:func:`omop_etl.circe.compile_circe_to_duckdb`).
These builders produce the definitions that the eDERRI replication needs; ``cohorts/*.circe.json`` are their output
(``tests/test_circe_definitions.py`` fails if the committed files drift from the builders).

* :func:`metformin_only_prediabetes_pcos_circe` - patients with prediabetes or PCOS whose only diabetes drug is
  metformin and who never had a diabetes diagnosis (checklist A2; 981 patients in the legacy per-site files).
* :func:`diabetes_phenotype_circe` - the SAS extraction's computable diabetes phenotype (checklist A0): adult with a
  diabetes diagnosis, a diabetes drug, or HbA1c >= 6.5 %.

Differences from the package's own SQL (``find_metformin_only_prediabetes_pcos``, ``define_study_cohort``): Circe works
on standard concept ids only, so records that carry just a source value (concept 0) are invisible to it, which is the
normal OHDSI contract. Concept sets use ``includeDescendants``; the ATC A10 drug set excludes A10BA biguanides so a
metformin combination counts as another diabetes drug.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

__all__ = ["PCOS_SETS", "metformin_only_prediabetes_pcos_circe", "diabetes_phenotype_circe", "target_cohort_circe",
           "write_circe_definitions"]

PCOS_SETS = ("icd10", "snomed", "both")

# concept_id -> (name, domain, vocabulary, class, standard flag, code); checked against an Athena vocabulary
_C = {
    1503297: ("metformin", "Drug", "RxNorm", "Ingredient", "S", "6809"),
    37018196: ("Prediabetes", "Condition", "SNOMED", "Disorder", "S", "714628002"),
    40443308: ("Polycystic ovary syndrome", "Condition", "SNOMED", "Disorder", "S", "237055002"),
    36683296: ("Polycystic ovary", "Condition", "SNOMED", "Disorder", "S", "781067001"),
    21600712: ("DRUGS USED IN DIABETES", "Drug", "ATC", "ATC 2nd", "C", "A10"),
    21600745: ("Biguanides", "Drug", "ATC", "ATC 4th", "C", "A10BA"),
    201820: ("Diabetes mellitus", "Condition", "SNOMED", "Disorder", "S", "73211009"),
    4024659: ("Gestational diabetes mellitus", "Condition", "SNOMED", "Disorder", "S", "11687002"),
    3004410: ("Hemoglobin A1c/Hemoglobin.total in Blood", "Measurement", "LOINC", "Lab Test", "S", "4548-4"),
    3034639: ("Hemoglobin A1c [Mass/volume] in Blood", "Measurement", "LOINC", "Lab Test", "S", "41995-2"),
    40758583: ("Deprecated Hemoglobin A1c in Blood", "Measurement", "LOINC", "Lab Test", None, "55454-3"),
    42869630: ("Hemoglobin A1c/Hemoglobin.total [Pure mass fraction] in Blood", "Measurement", "LOINC", "Lab Test", "S", "71875-9"),
    3003309: ("Hemoglobin A1c/Hemoglobin.total in Blood by Electrophoresis", "Measurement", "LOINC", "Lab Test", "S", "4549-2"),
    3005673: ("Hemoglobin A1c/Hemoglobin.total in Blood by HPLC", "Measurement", "LOINC", "Lab Test", "S", "17856-6"),
    40762352: ("Hemoglobin A1c/Hemoglobin.total standardized per IFCC-RMP for CDT in Blood", "Measurement", "LOINC", "Lab Test", "S", "59261-8"),
    40765129: ("Hemoglobin A1c/Hemoglobin.total in Blood by JDS/JSCC protocol", "Measurement", "LOINC", "Lab Test", "S", "62388-4"),
    3007263: ("Hemoglobin A1c/Hemoglobin.total in Blood by calculation", "Measurement", "LOINC", "Lab Test", "S", "17855-8"),
    9201: ("Inpatient Visit", "Visit", "Visit", "Visit", "S", "IP"),
    262: ("Emergency Room and Inpatient Visit", "Visit", "Visit", "Visit", "S", "ERIP"),
}
# the 9 HbA1c codes of the SAS extraction (its "549-2" is a typo for 4549-2; 55454-3 is deprecated)
_HBA1C = (3004410, 3034639, 40758583, 42869630, 3003309, 3005673, 40762352, 40765129, 3007263)


def _item(cid: int, descendants: bool = False, excluded: bool = False) -> dict[str, Any]:
    name, domain, vocab, cls, std, code = _C[cid]
    return {"concept": {"CONCEPT_ID": cid, "CONCEPT_NAME": name, "DOMAIN_ID": domain, "VOCABULARY_ID": vocab,
                        "CONCEPT_CLASS_ID": cls, "STANDARD_CONCEPT": std, "CONCEPT_CODE": code},
            "isExcluded": excluded, "includeDescendants": descendants, "includeMapped": False}


def _concept_set(cs_id: int, name: str, items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"id": cs_id, "name": name, "expression": {"items": list(items)}}


_ALL_TIME = {"Start": {"Coeff": -1}, "End": {"Coeff": 1}}          # no Days = unbounded in both directions
_UP_TO_INDEX = {"Start": {"Coeff": -1}, "End": {"Days": 0, "Coeff": 1}}


def _rule(name: str, description: str, rule_type: str, entries: Sequence[tuple[dict, dict, int, int]],
          demographics: Sequence[dict] = ()) -> dict[str, Any]:
    return {"name": name, "description": description,
            "expression": {"Type": rule_type,
                           "CriteriaList": [{"Criteria": c, "StartWindow": w, "Occurrence": {"Type": t, "Count": n}}
                                            for c, w, t, n in entries],
                           "DemographicCriteriaList": list(demographics),
                           "Groups": []}}


def _definition(concept_sets, primary, rules) -> dict[str, Any]:
    return {"ConceptSets": concept_sets,
            "PrimaryCriteria": {"CriteriaList": primary, "ObservationWindow": {"PriorDays": 0, "PostDays": 0},
                                "PrimaryCriteriaLimit": {"Type": "First"}},
            "QualifiedLimit": {"Type": "First"}, "ExpressionLimit": {"Type": "First"}, "InclusionRules": list(rules),
            "EndStrategy": {"DateOffset": {"DateField": "StartDate", "Offset": 0}}, "CensoringCriteria": [],
            "CollapseSettings": {"CollapseType": "ERA", "EraPad": 0}, "CensorWindow": {}, "cdmVersionRange": ">=5.3.0"}


def metformin_only_prediabetes_pcos_circe(pcos: str = "icd10") -> dict[str, Any]:
    """Cohort entered at the first metformin exposure; kept when prediabetes or PCOS was ever recorded and no other diabetes
    drug and no diabetes diagnosis ever was. ``pcos``: ``icd10`` = "Polycystic ovary syndrome" (ICD-10 E28.2),
    ``snomed`` = "Polycystic ovary" (SNOMED 69878008), ``both``. Patient-level: use it as an exclusion (cohort entry date
    is the first metformin exposure)."""
    if pcos not in PCOS_SETS:
        raise ValueError(f"pcos must be one of {PCOS_SETS}; got {pcos!r}.")
    pcos_ids = {"icd10": (40443308,), "snomed": (36683296,), "both": (40443308, 36683296)}[pcos]
    sets = [
        _concept_set(0, "Metformin", [_item(1503297, descendants=True)]),
        _concept_set(1, "Prediabetes", [_item(37018196, descendants=True)]),
        _concept_set(2, "Polycystic ovary syndrome", [_item(c, descendants=True) for c in pcos_ids]),
        _concept_set(3, "Diabetes drugs other than metformin alone (ATC A10 without A10BA biguanides)",
                     [_item(21600712, descendants=True), _item(21600745, descendants=True, excluded=True)]),
        _concept_set(4, "Diabetes mellitus", [_item(201820, descendants=True)]),
    ]
    rules = [
        _rule("Prediabetes or PCOS at any time", "At least one prediabetes or PCOS condition, any date.", "ANY",
              [({"ConditionOccurrence": {"CodesetId": 1}}, _ALL_TIME, 2, 1),
               ({"ConditionOccurrence": {"CodesetId": 2}}, _ALL_TIME, 2, 1)]),
        _rule("No other diabetes drug", "No insulin, sulfonylurea or other ATC A10 drug, and no metformin combination, ever.",
              "ALL", [({"DrugExposure": {"CodesetId": 3}}, _ALL_TIME, 0, 0)]),
        _rule("No diabetes diagnosis", "No diabetes mellitus condition, any date.", "ALL",
              [({"ConditionOccurrence": {"CodesetId": 4}}, _ALL_TIME, 0, 0)]),
    ]
    return _definition(sets, [{"DrugExposure": {"CodesetId": 0}}], rules)


def diabetes_phenotype_circe(drug_rule: str = "atc_a10", rxnorm_concept_ids: Sequence[int] | None = None,
                             include_gestational: bool = False) -> dict[str, Any]:
    """The SAS extraction's diabetes phenotype: an adult (>= 18 at entry) with, at entry or earlier, a diabetes diagnosis, a
    diabetes drug, or HbA1c >= 6.5. The entry is the earliest of those events. ``drug_rule='atc_a10'`` uses ATC A10
    descendants; ``'rxnorm_concepts'`` takes ``rxnorm_concept_ids`` (ingredient concepts resolved from the CUI list).

    SNOMED places "Gestational diabetes mellitus" (4024659) under "Diabetes mellitus" (201820), so a plain descendant set
    would admit pregnancy-only diabetes, which the SAS extraction's ICD rule (249, 250, E08-E13; no O24) does not. The default
    ``include_gestational=False`` subtracts it from the diagnosis arm; ``True`` keeps it. A person with gestational diabetes who
    also has a diabetes drug or an HbA1c >= 6.5 still enters through those arms."""
    if drug_rule == "atc_a10":
        drug_items = [_item(21600712, descendants=True)]
    elif drug_rule == "rxnorm_concepts":
        if not rxnorm_concept_ids:
            raise ValueError("drug_rule='rxnorm_concepts' needs rxnorm_concept_ids.")
        drug_items = [{"concept": {"CONCEPT_ID": int(c), "CONCEPT_NAME": "", "DOMAIN_ID": "Drug", "VOCABULARY_ID": "RxNorm",
                                   "CONCEPT_CLASS_ID": "Ingredient", "STANDARD_CONCEPT": "S", "CONCEPT_CODE": ""},
                       "isExcluded": False, "includeDescendants": True, "includeMapped": False}
                      for c in sorted(set(int(x) for x in rxnorm_concept_ids))]
    else:
        raise ValueError("drug_rule must be 'atc_a10' or 'rxnorm_concepts'.")
    sets = [
        _concept_set(0, "Diabetes mellitus", [_item(201820, descendants=True)]
                     + ([] if include_gestational else [_item(4024659, descendants=True, excluded=True)])),
        _concept_set(1, "Diabetes drugs", drug_items),
        _concept_set(2, "Hemoglobin A1c", [_item(c) for c in _HBA1C]),
    ]
    primary = [{"ConditionOccurrence": {"CodesetId": 0}}, {"DrugExposure": {"CodesetId": 1}},
               {"Measurement": {"CodesetId": 2, "ValueAsNumber": {"Value": 6.5, "Op": "gte"}}}]
    rules = [_rule("Adult at entry", "Age 18 or older on the entry date.", "ALL", [],
                   demographics=[{"Age": {"Value": 18, "Op": "gte"}}])]
    return _definition(sets, primary, rules)


def target_cohort_circe(min_age: int = 18, followup_days: int = 30) -> dict[str, Any]:
    """The index-stay cohort of ``cohorts/target_cohort.sql`` (adult inpatient and ED-to-IP stays, one per patient, indexed on the
    discharge date) as Circe JSON.

    The rules that decide whether a stay qualifies are correlated criteria of the entry event, not inclusion rules, because Circe
    applies the primary limit before the inclusion rules: with inclusion rules a patient whose first stay failed would be dropped, where
    the SQL takes the earliest stay that qualifies. The index date is the discharge date (``DateAdjustment`` on the end date), the
    observation window requires ``followup_days`` of observation after it, and a death on or before discharge disqualifies the stay.

    Two rules of the SQL cannot be written in Circe and remain differences of the ATLAS version: ``discharged_to_concept_id``
    (a visit attribute that Circe has no criterion for; a patient who died in hospital is still excluded when the death is recorded in
    ``death``), and the SQL's alternative for follow-up, a death within ``followup_days`` after discharge when the observation period
    ends at death (Circe requires the observation period itself to cover the window)."""
    stay = {"CodesetId": 0, "Age": {"Value": min_age, "Op": "gte"},
            "DateAdjustment": {"StartWith": "END_DATE", "StartOffset": 0, "EndWith": "END_DATE", "EndOffset": 0},
            "CorrelatedCriteria": {"Type": "ALL", "CriteriaList": [
                {"Criteria": {"Death": {}}, "StartWindow": _UP_TO_INDEX, "Occurrence": {"Type": 0, "Count": 0}}],
                "DemographicCriteriaList": [], "Groups": []}}
    out = _definition([_concept_set(0, "Index stay visit types", [_item(9201), _item(262)])], [{"VisitOccurrence": stay}], [])
    out["PrimaryCriteria"]["ObservationWindow"] = {"PriorDays": 0, "PostDays": int(followup_days)}
    return out


def write_circe_definitions(out_dir: str | Path) -> list[Path]:
    """Writes the committed definitions (``cohorts/*.circe.json`` names) and returns their paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {f"metformin_only_prediabetes_pcos_{p}.circe.json": metformin_only_prediabetes_pcos_circe(p) for p in PCOS_SETS}
    files["diabetes_phenotype.circe.json"] = diabetes_phenotype_circe()
    files["target_cohort.circe.json"] = target_cohort_circe()
    paths = []
    for name, data in files.items():
        path = out / name
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        paths.append(path)
    return paths
