"""In-memory FHIR -> risk score adapter for CDS Hooks / EHR point-of-care alerts.

``score_fhir_bundle(fhir_json, model_bundle)`` reads the Patient, Encounter, Observation, Condition and
MedicationRequest resources of a FHIR R4 Bundle into a throw-away in-memory DuckDB OMOP CDM and runs the same
:func:`omop_etl.encounter.score_encounter` path as the on-disk database - nothing is written to disk.

Observations are matched on LOINC (``http://loinc.org``) to the model's measurement concepts and converted to the
units the feature contract expects (creatinine / BUN from SI units). Conditions and medications are loaded only when
``concept_map`` ({"system|code": omop_concept_id}) maps their codes; unmapped codes are listed under
``fhir["unmapped_codes"]`` rather than guessed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import duckdb
import pandas as pd

from omop_etl.velocity import VELOCITY_CONCEPTS

__all__ = ["score_fhir_bundle", "LOINC_TO_FEATURE", "resolve_codes", "resolve_fhir_codes", "VOCABULARY_BY_SYSTEM"]

LOINC_TO_FEATURE = {
    "8867-4": "heart_rate", "8480-6": "systolic_bp", "8462-4": "diastolic_bp", "3094-0": "lab_bun", "2160-0": "lab_creatinine",
    "38483-4": "lab_creatinine", "2951-2": "lab_sodium", "2823-3": "lab_potassium", "2075-0": "lab_chloride",
    "1963-8": "lab_bicarbonate", "2028-9": "lab_bicarbonate", "33037-3": "lab_anion_gap", "62238-1": "lab_egfr",
    "33914-3": "lab_egfr", "2345-7": "lab_glucose", "2339-0": "lab_glucose",
}
# (feature, source unit lower-case) -> multiplier into the contract unit (mg/dL; mmol/L == mEq/L)
_UNIT_FACTORS = {("lab_creatinine", "umol/l"): 1 / 88.4, ("lab_creatinine", "µmol/l"): 1 / 88.4,
                 ("lab_bun", "mmol/l"): 2.801, ("lab_glucose", "mmol/l"): 18.016}
VOCABULARY_BY_SYSTEM = {"http://snomed.info/sct": "SNOMED", "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
                        "http://hl7.org/fhir/sid/icd-10-cm": "ICD10CM", "http://hl7.org/fhir/sid/icd-10": "ICD10",
                        "http://hl7.org/fhir/sid/icd-9-cm": "ICD9CM", "http://hl7.org/fhir/sid/ndc": "NDC",
                        "http://www.ama-assn.org/go/cpt": "CPT4"}
_ENC_CLASS = {"IMP": 9201, "ACUTE": 9201, "NONAC": 9201, "EMER": 9203, "AMB": 9202, "OBSENC": 581385}
_GENDER = {"female": 8532, "male": 8507}


def resolve_codes(vocab_con: duckdb.DuckDBPyConnection, codes: Any) -> dict[str, int]:
    """Resolve ``"system|code"`` strings to standard OMOP concept ids using the ``concept`` tables of ``vocab_con``.

    The FHIR system URI selects the vocabulary (:data:`VOCABULARY_BY_SYSTEM`); a standard concept is used as is,
    otherwise the valid ``Maps to`` relationship is followed (when ``concept_relationship`` exists). Systems that are
    not in the table, and codes with no standard target, are omitted.
    """
    pairs = []
    for k in dict.fromkeys(codes):
        system, _, code = str(k).partition("|")
        if system in VOCABULARY_BY_SYSTEM and code:
            pairs.append((k, VOCABULARY_BY_SYSTEM[system], code))
    if not pairs:
        return {}
    tables = {r[0].lower() for r in vocab_con.execute("SHOW TABLES").fetchall()}
    if "concept" not in tables:
        raise ValueError("The vocabulary connection has no 'concept' table.")
    vocab_con.register("_fhir_codes", pd.DataFrame(pairs, columns=["k", "vocabulary_id", "concept_code"]))
    try:
        if "concept_relationship" in tables:
            join = ("LEFT JOIN concept_relationship r ON r.concept_id_1 = c.concept_id AND r.relationship_id = 'Maps to' "
                    "AND r.invalid_reason IS NULL")
            target = "CASE WHEN c.standard_concept = 'S' THEN c.concept_id ELSE r.concept_id_2 END"
        else:
            join, target = "", "CASE WHEN c.standard_concept = 'S' THEN c.concept_id END"
        rows = vocab_con.execute(
            f"SELECT q.k, min({target}) AS cid FROM _fhir_codes q JOIN concept c ON c.vocabulary_id = q.vocabulary_id "
            f"AND c.concept_code = q.concept_code AND c.invalid_reason IS NULL {join} GROUP BY q.k").fetchall()
    finally:
        vocab_con.unregister("_fhir_codes")
    return {k: int(cid) for k, cid in rows if cid is not None}


def _load(fhir_json: Any) -> dict[str, Any]:
    if isinstance(fhir_json, (str, Path)):
        text = str(fhir_json)
        fhir_json = json.loads(text) if text.lstrip().startswith("{") else json.loads(Path(text).read_text(encoding="utf-8"))
    if not isinstance(fhir_json, Mapping) or fhir_json.get("resourceType") != "Bundle":
        raise ValueError("score_fhir_bundle expects a FHIR Bundle (resourceType 'Bundle').")
    return dict(fhir_json)


def _date(s: Any) -> pd.Timestamp | None:
    return pd.Timestamp(str(s)[:10]) if s else None


def _codings(res: Mapping[str, Any], field: str = "code") -> list[tuple[str, str]]:
    block = res.get(field) or {}
    block = block[0] if isinstance(block, list) and block else block
    return [(c.get("system", ""), c.get("code", "")) for c in (block.get("coding") or [])]


resolve_fhir_codes = resolve_codes  # name parity with the R package


def score_fhir_bundle(fhir_json: Any, model_bundle: Any, *, encounter_id: str | None = None, as_of: Any = None,
                      concept_map: Mapping[str, int] | None = None,
                      vocab_con: Any = None, vocab_db_path: Any = None, explain: bool = False, top_k: int = 5,
                      **score_kwargs: Any) -> dict[str, Any]:
    """Score the latest (or ``encounter_id``) Encounter of a FHIR Bundle. Extra keywords go to ``score_encounter``.

    Condition and MedicationRequest codes are mapped through ``concept_map`` first; the rest are resolved against the
    OMOP vocabulary in ``vocab_con`` (an open DuckDB connection) or ``vocab_db_path`` (opened read-only) when given.
    """
    from omop_etl.build_omop_cdm import build_schema
    from omop_etl.encounter import score_encounter

    bundle = _load(fhir_json)
    res: dict[str, list[dict]] = {}
    for e in bundle.get("entry") or []:
        r = e.get("resource") or {}
        res.setdefault(r.get("resourceType", ""), []).append(r)
    if not res.get("Patient"):
        raise ValueError("Bundle has no Patient resource.")
    if not res.get("Encounter"):
        raise ValueError("Bundle has no Encounter resource.")
    patient = res["Patient"][0]
    encs = res["Encounter"]
    if encounter_id is not None:
        encs = [x for x in encs if x.get("id") == encounter_id]
        if not encs:
            raise ValueError(f"Encounter {encounter_id!r} not in the bundle.")
    enc = max(encs, key=lambda x: str((x.get("period") or {}).get("start") or ""))
    cmap = dict(concept_map or {})
    if vocab_con is not None or vocab_db_path is not None:
        wanted = [f"{s}|{k}" for r in res.get("Condition", []) for s, k in _codings(r)] + \
                 [f"{s}|{k}" for r in res.get("MedicationRequest", []) for s, k in _codings(r, "medicationCodeableConcept")]
        wanted = [w for w in wanted if w not in cmap]
        if vocab_con is not None:
            cmap = {**resolve_codes(vocab_con, wanted), **cmap}
        else:
            vc = duckdb.connect(str(vocab_db_path), read_only=True)
            try:
                cmap = {**resolve_codes(vc, wanted), **cmap}
            finally:
                vc.close()
    unmapped: set[str] = set()
    skipped_obs = 0

    con = duckdb.connect(":memory:")
    try:
        build_schema(con)
        birth = _date(patient.get("birthDate"))
        con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, birth_datetime, race_concept_id, "
                    "ethnicity_concept_id) VALUES (1, ?, ?, ?, 0, 0)",
                    [_GENDER.get(str(patient.get("gender", "")).lower(), 0), None if birth is None else birth.year, birth])
        period = enc.get("period") or {}
        start = _date(period.get("start"))
        if start is None:
            raise ValueError("Encounter has no period.start.")
        today = pd.Timestamp.today().normalize()
        end = _date(period.get("end")) or (pd.Timestamp(as_of).normalize() if as_of is not None else today)
        cls = (enc.get("class") or {}).get("code", "IMP")
        con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                    "visit_end_date, visit_type_concept_id) VALUES (1, 1, ?, ?, ?, 0)", [_ENC_CLASS.get(cls, 9201), start, end])
        # Other Encounters are the longitudinal history behind prior_ip_count, prior_ed_count, days_since_prior_encounter.
        first = start
        vid = 1
        for other in res["Encounter"]:
            if other is enc:
                continue
            p_o = other.get("period") or {}
            s_o = _date(p_o.get("start"))
            if s_o is None:
                continue
            vid += 1
            first = min(first, s_o)
            con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                        "visit_end_date, visit_type_concept_id) VALUES (?, 1, ?, ?, ?, 0)",
                        [vid, _ENC_CLASS.get((other.get("class") or {}).get("code", "IMP"), 9201), s_o, _date(p_o.get("end")) or s_o])
        con.execute("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, "
                    "observation_period_end_date, period_type_concept_id) VALUES (1, 1, ?, ?, 0)",
                    [min(first, birth or first), max(end, today)])
        mid = 0
        for o in res.get("Observation", []):
            when = _date(o.get("effectiveDateTime") or (o.get("effectivePeriod") or {}).get("start") or o.get("issued"))
            # A panel (e.g. blood pressure 85354-9 / 55284-4) has no valueQuantity; its values sit in component[].
            parts = [o] if o.get("valueQuantity") else [c for c in (o.get("component") or []) if isinstance(c, Mapping)] or [o]
            for part in parts:
                feat = next((LOINC_TO_FEATURE[c] for s, c in _codings(part) if s.endswith("loinc.org") and c in LOINC_TO_FEATURE), None)
                q = part.get("valueQuantity") or {}
                if feat is None or q.get("value") is None or when is None:
                    skipped_obs += 1
                    continue
                unit = str(q.get("unit") or q.get("code") or "").lower()
                value = float(q["value"]) * _UNIT_FACTORS.get((feat, unit), 1.0)
                mid += 1
                con.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                            "measurement_type_concept_id, value_as_number, visit_occurrence_id) VALUES (?, 1, ?, ?, 0, ?, 1)",
                            [mid, int(VELOCITY_CONCEPTS[feat][0]), when, value])
        for i, c in enumerate(res.get("Condition", []), 1):
            cid = next((cmap[f"{s}|{k}"] for s, k in _codings(c) if f"{s}|{k}" in cmap), None)
            if cid is None:
                unmapped.update(f"{s}|{k}" for s, k in _codings(c))
                continue
            when = _date(c.get("onsetDateTime") or c.get("recordedDate")) or start
            con.execute("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, "
                        "condition_start_date, condition_type_concept_id, visit_occurrence_id) VALUES (?, 1, ?, ?, 0, 1)",
                        [i, int(cid), when])
        for i, m in enumerate(res.get("MedicationRequest", []), 1):
            cid = next((cmap[f"{s}|{k}"] for s, k in _codings(m, "medicationCodeableConcept") if f"{s}|{k}" in cmap), None)
            if cid is None:
                unmapped.update(f"{s}|{k}" for s, k in _codings(m, "medicationCodeableConcept"))
                continue
            when = _date(m.get("authoredOn")) or start
            con.execute("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, "
                        "drug_exposure_end_date, drug_type_concept_id, visit_occurrence_id) VALUES (?, 1, ?, ?, ?, 0, 1)",
                        [i, int(cid), when, when])
        out = score_encounter(con, 1, model_bundle, as_of=as_of, explain=explain, top_k=top_k, **score_kwargs)
    finally:
        con.close()
    out["fhir"] = {"encounter_id": enc.get("id"), "patient_id": patient.get("id"), "observations_skipped": skipped_obs,
                   "unmapped_codes": sorted(unmapped)}
    return out
