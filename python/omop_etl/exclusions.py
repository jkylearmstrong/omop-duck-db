"""Patient-level cohort exclusions that need the vocabulary: metformin-only prediabetes / PCOS.

A patient is *metformin-only prediabetes / PCOS* when ALL hold (checklist A2, Rubin 2026-09-30):

1. a prediabetes or PCOS condition,
2. metformin exposure,
3. NO other diabetes drug (insulins, sulfonylureas, ... and every metformin combination product),
4. NO diabetes mellitus condition.

The legacy study excluded these patients (all their encounters) through a per-site file produced upstream; this builds
the same set from OMOP so it can be validated against that file (report overlap / only-legacy / only-OMOP per site).

Concept sets (ids checked against an Athena vocabulary; descendants come from ``concept_ancestor``):

* prediabetes: standard 37018196 and its SNOMED 15777000 (40316773) / ICD-10-CM R73.03 (37201113) source concepts,
  plus ``condition_source_value`` R73.03;
* PCOS, ``pcos='icd10'``: standard 40443308 and ICD-10-CM E28.2 (35206936), plus source value E28.2;
  ``pcos='snomed'``: 36683296 ("Polycystic ovary", the standard target of SNOMED 69878008) and 195501; ``'both'``: all;
* metformin: RxNorm ingredient 6809 (1503297) and descendants, or the words metformin / glucophage in ``drug_source_value``;
* other diabetes drug: ATC A10 (21600712) descendants that are NOT ATC A10BA biguanides (21600745), so a metformin
  combination counts as another drug, or other diabetes drug names in ``drug_source_value``;
* diabetes: SNOMED 73211009 (201820) and descendants, or ICD-9 249/250 / ICD-10 E08-E13 source values.

Without a usable ``concept_ancestor`` the concept sets shrink to the listed ids and the source-value patterns, and a
warning says so.
"""

from __future__ import annotations

import warnings
from typing import Any

import duckdb
import pandas as pd

from omop_etl.comorbidity import _resolve_concept_ancestor

__all__ = ["PCOS_SETS", "find_metformin_only_prediabetes_pcos", "resolve_concept_ancestor_ref", "metformin_only_sql"]

PCOS_SETS = ("icd10", "snomed", "both")

_PREDIABETES = (37018196, 40316773, 37201113)
_PCOS = {"icd10": (40443308, 35206936), "snomed": (36683296, 195501)}
_METFORMIN_INGREDIENT = 1503297
_ATC_A10, _ATC_A10BA = 21600712, 21600745
_DIABETES_DX = 201820
_OTHER_DRUG_WORDS = ("insulin|glipizide|glyburide|glibenclamide|glimepiride|gliclazide|sitagliptin|saxagliptin|linagliptin|"
                     "alogliptin|vildagliptin|empagliflozin|dapagliflozin|canagliflozin|ertugliflozin|liraglutide|semaglutide|"
                     "dulaglutide|exenatide|pioglitazone|rosiglitazone|repaglinide|nateglinide|acarbose|miglitol|pramlintide")


def resolve_concept_ancestor_ref(con: duckdb.DuckDBPyConnection, schema: str | None = None) -> str | None:
    """A reachable, non-empty ``concept_ancestor`` (the given schema first), or None."""
    if schema:
        try:
            if con.execute(f"SELECT 1 FROM {schema}.concept_ancestor LIMIT 1").fetchone() is not None:
                return f"{schema}.concept_ancestor"
        except (duckdb.CatalogException, duckdb.BinderException):
            pass
    ref, _ = _resolve_concept_ancestor(con)
    return ref


def _ids(values) -> str:
    return ", ".join(str(int(v)) for v in values)


def _concept_set(ca: str | None, ancestors, exclude_ancestors=()) -> str:
    """SQL ``SELECT id`` for the ancestors and, when ``ca`` is known, all their descendants (minus ``exclude_ancestors``)."""
    base = f"SELECT unnest([{_ids(ancestors)}]) AS id"
    if ca is None:
        return base
    sql = f"{base} UNION SELECT descendant_concept_id AS id FROM {ca} WHERE ancestor_concept_id IN ({_ids(ancestors)})"
    if exclude_ancestors:
        sql = (f"SELECT id FROM ({sql}) WHERE id NOT IN (SELECT descendant_concept_id FROM {ca} "
               f"WHERE ancestor_concept_id IN ({_ids(exclude_ancestors)}))")
    return sql


def metformin_only_sql(pcos: str = "icd10", schema: str | None = None, concept_ancestor: str | None = None) -> str:
    """One SELECT returning ``person_id, has_prediabetes, has_pcos`` for the metformin-only prediabetes / PCOS patients."""
    if pcos not in PCOS_SETS:
        raise ValueError(f"pcos must be one of {PCOS_SETS}; got {pcos!r}.")
    pre = f"{schema}." if schema else ""
    ca = concept_ancestor
    pcos_ids = (_PCOS["icd10"] if pcos == "icd10" else _PCOS["snomed"] if pcos == "snomed" else _PCOS["icd10"] + _PCOS["snomed"])
    pcos_src = "|E282" if pcos in ("icd10", "both") else ""
    return f"""
WITH cond AS (
    SELECT person_id, condition_concept_id AS cid,
           upper(regexp_replace(coalesce(condition_source_value, ''), '[.\\s]', '', 'g')) AS src
    FROM {pre}condition_occurrence
), drug AS (
    SELECT person_id, drug_concept_id AS did, lower(coalesce(drug_source_value, '')) AS s FROM {pre}drug_exposure
), pre_ids AS ({_concept_set(ca, _PREDIABETES)}),
   pcos_ids AS ({_concept_set(ca, pcos_ids)}),
   dm_ids AS ({_concept_set(ca, (_DIABETES_DX,))}),
   met_ids AS ({_concept_set(ca, (_METFORMIN_INGREDIENT,))}),
   other_ids AS ({_concept_set(ca, (_ATC_A10,), (_ATC_A10BA,)) if ca else "SELECT NULL::BIGINT AS id WHERE FALSE"}),
 prediab AS (SELECT DISTINCT person_id FROM cond WHERE cid IN (SELECT id FROM pre_ids) OR regexp_matches(src, '^R7303')),
 pcosp AS (SELECT DISTINCT person_id FROM cond WHERE cid IN (SELECT id FROM pcos_ids)
           {f"OR regexp_matches(src, '^{pcos_src[1:]}')" if pcos_src else ""}),
 dm AS (SELECT DISTINCT person_id FROM cond WHERE cid IN (SELECT id FROM dm_ids)
        OR regexp_matches(src, '^(249|250|E08|E09|E10|E11|E12|E13)')),
 met AS (SELECT DISTINCT person_id FROM drug WHERE did IN (SELECT id FROM met_ids) OR regexp_matches(s, 'metformin|glucophage')),
 other AS (SELECT DISTINCT person_id FROM drug WHERE did IN (SELECT id FROM other_ids)
           OR regexp_matches(s, '{_OTHER_DRUG_WORDS}'))
SELECT m.person_id, (m.person_id IN (SELECT person_id FROM prediab)) AS has_prediabetes,
       (m.person_id IN (SELECT person_id FROM pcosp)) AS has_pcos
FROM met AS m
WHERE (m.person_id IN (SELECT person_id FROM prediab) OR m.person_id IN (SELECT person_id FROM pcosp))
  AND m.person_id NOT IN (SELECT person_id FROM other)
  AND m.person_id NOT IN (SELECT person_id FROM dm)
ORDER BY m.person_id
"""


def find_metformin_only_prediabetes_pcos(con: duckdb.DuckDBPyConnection, pcos: str = "icd10", schema: str | None = None) -> pd.DataFrame:
    """Patients whose only diabetes drug is metformin and who have prediabetes or PCOS but no diabetes diagnosis.

    ``pcos`` picks the PCOS concept set (``'icd10'`` = E28.2 / "Polycystic ovary syndrome", ``'snomed'`` = 69878008 /
    "Polycystic ovary", ``'both'``). Returns ``person_id, has_prediabetes, has_pcos``; the whole history is used (no
    date window), as in a patient-level exclusion.
    """
    ca = resolve_concept_ancestor_ref(con, schema)
    if ca is None:
        warnings.warn("find_metformin_only_prediabetes_pcos(): no usable concept_ancestor on this connection; concept sets "
                      "are limited to the listed concept ids and source-value patterns (attach the vocabulary, e.g. "
                      "omop_connect(), for descendants).", UserWarning, stacklevel=2)
    return con.execute(metformin_only_sql(pcos, schema, ca)).df()
