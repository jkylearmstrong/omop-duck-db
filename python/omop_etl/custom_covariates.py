"""Add your own covariates to an OMOP DuckDB so the OHDSI tools read them like any other feature.

OHDSI's FeatureExtraction, PatientLevelPrediction and ATLAS know only OMOP tables. A feature that is not in the CDM (an area
deprivation index, a risk score, an alternative definition of a standard feature) becomes visible to them when it is written as
``measurement`` rows with a site-defined concept id (>= 2,000,000,000 by the OMOP convention). Studies then use the standard
tool to extract it (``createCovariateSettings(useMeasurementValueLongTerm = TRUE, includedCovariateConceptIds = ...)``), and an
alternative definition of an existing feature sits beside the standard one under its own concept id, so the two can be put
side by side (``compare_feature_tables``) before one is chosen.
"""
from __future__ import annotations

from typing import Any, Mapping

import duckdb
import pandas as pd

__all__ = ["write_custom_covariates", "DERIVED_VALUE_TYPE_CONCEPT_ID", "LOCAL_CONCEPT_MIN"]

DERIVED_VALUE_TYPE_CONCEPT_ID = 45754907     # "Derived value" (Meas Type)
LOCAL_CONCEPT_MIN = 2_000_000_000


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE lower(table_name) = ? AND table_catalog = current_database() "
                            "AND table_schema = current_schema()", [name]).fetchone()[0])


def write_custom_covariates(
    con: duckdb.DuckDBPyConnection,
    values: pd.DataFrame,
    concepts: Mapping[str, tuple[int, str]],
    *,
    source: str = "omop_etl.custom_covariates",
    visit_ids: bool = False,
    register_concepts: bool = True,
    type_concept_id: int = DERIVED_VALUE_TYPE_CONCEPT_ID,
) -> int:
    """Writes feature values as ``measurement`` rows and returns the number written.

    ``values`` is long: ``person_id``, ``date`` (the date the value applies to, usually the index or admission date),
    ``feature`` (a key of ``concepts``), ``value`` (numeric), and optionally ``visit_occurrence_id``. ``concepts`` maps each
    feature to ``(concept_id, concept_name)``; ids must be at least 2,000,000,000 so they cannot collide with a vocabulary
    concept. Missing values write no row. Re-running replaces the rows of the same concepts for the same persons.
    ``unit_concept_id`` is 0: FeatureExtraction joins measurement covariates on the unit and skips rows where it is NULL.
    ``register_concepts`` adds the concepts to ``concept`` (Measurement domain, vocabulary ``omop-duck-db``) when that table
    exists so covariate names resolve.
    """
    need = {"person_id", "date", "feature", "value"}
    if need - set(values.columns):
        raise ValueError(f"values is missing column(s) {sorted(need - set(values.columns))}")
    bad = {k: v[0] for k, v in concepts.items() if int(v[0]) < LOCAL_CONCEPT_MIN}
    if bad:
        raise ValueError(f"custom concept ids must be >= {LOCAL_CONCEPT_MIN} (site-defined range): {bad}")
    unknown = set(values["feature"].dropna().unique()) - set(concepts)
    if unknown:
        raise ValueError(f"feature(s) {sorted(unknown)} have no entry in concepts")
    rows = values.loc[values["value"].notna(), ["person_id", "date", "feature", "value"] + (["visit_occurrence_id"] if "visit_occurrence_id" in values.columns else [])].copy()
    rows["measurement_concept_id"] = rows["feature"].map(lambda f: int(concepts[f][0]))
    rows["date"] = pd.to_datetime(rows["date"]).dt.date
    if "visit_occurrence_id" not in rows.columns or not visit_ids:
        rows["visit_occurrence_id"] = None
    if rows.empty:
        return 0
    con.register("_custom_cov", rows)
    try:
        ids = ", ".join(str(int(v[0])) for v in concepts.values())
        con.execute(f"DELETE FROM measurement WHERE measurement_concept_id IN ({ids}) "
                    "AND person_id IN (SELECT person_id FROM _custom_cov)")
        start = con.execute("SELECT COALESCE(MAX(measurement_id), 0) FROM measurement").fetchone()[0]
        con.execute(f"""
            INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime,
                                     measurement_type_concept_id, value_as_number, unit_concept_id, visit_occurrence_id, measurement_source_value)
            SELECT {int(start)} + row_number() OVER (ORDER BY person_id, date, measurement_concept_id),
                   CAST(person_id AS BIGINT), measurement_concept_id, date, CAST(date AS TIMESTAMP),
                   {int(type_concept_id)}, CAST(value AS DOUBLE), 0, CAST(visit_occurrence_id AS BIGINT), ?
            FROM _custom_cov""", [source])
    finally:
        con.unregister("_custom_cov")
    if register_concepts and _has_table(con, "concept"):
        for key, (cid, name) in concepts.items():
            if not con.execute("SELECT count(*) FROM concept WHERE concept_id = ?", [int(cid)]).fetchone()[0]:
                con.execute("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, "
                            "concept_code, valid_start_date, valid_end_date) VALUES (?, ?, 'Measurement', 'omop-duck-db', 'Custom covariate', 'S', ?, "
                            "DATE '1970-01-01', DATE '2099-12-31')", [int(cid), name, key])
    return len(rows)
