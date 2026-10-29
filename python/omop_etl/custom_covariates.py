"""Add your own covariates to an OMOP DuckDB so the OHDSI tools read them like any other feature.

OHDSI's FeatureExtraction, PatientLevelPrediction and ATLAS know only OMOP tables. A feature that is not in the CDM (an area
deprivation index, a risk score, an alternative definition of a standard feature) becomes visible to them when it is written as
``measurement`` rows with a site-defined concept id (>= 2,000,000,000 by the OMOP convention). Studies then use the standard
tool to extract it (``createCovariateSettings(useMeasurementValueLongTerm = TRUE, includedCovariateConceptIds = ...)``), and an
alternative definition of an existing feature sits beside the standard one under its own concept id, so the two can be put
side by side (``compare_feature_tables``) before one is chosen.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import duckdb
import pandas as pd

__all__ = ["write_custom_covariates", "write_custom_features", "register_custom_concepts", "custom_concept_registry",
           "export_custom_concept_registry", "check_custom_concepts", "DERIVED_VALUE_TYPE_CONCEPT_ID", "LOCAL_CONCEPT_MIN"]

DERIVED_VALUE_TYPE_CONCEPT_ID = 45754907     # "Derived value" (Meas Type)
LOCAL_CONCEPT_MIN = 2_000_000_000
CUSTOM_VOCABULARY = "omop-duck-db"


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
    ids = [int(v[0]) for v in concepts.values()]
    if len(set(ids)) != len(ids):
        raise ValueError(f"two features share one concept id: {dict(concepts)}")
    if _has_table(con, "concept"):
        for key, (cid, _) in concepts.items():
            row = con.execute("SELECT concept_code, vocabulary_id FROM concept WHERE concept_id = ?", [int(cid)]).fetchone()
            if row and (row[0], row[1]) != (key, CUSTOM_VOCABULARY):
                raise ValueError(f"concept id {cid} for feature {key} is already used by {row[0]} ({row[1]}); "
                                 "use register_custom_concepts() to pick ids that do not collide")
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


def _load_registry(registry: Any) -> dict[str, int]:
    """``{feature name: concept_id}`` from a mapping, a JSON file written by :func:`export_custom_concept_registry`, or a
    ``{name: {"concept_id": ...}}`` mapping."""
    if registry is None:
        return {}
    if isinstance(registry, (str, Path)):
        registry = json.loads(Path(registry).read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for name, v in dict(registry).items():
        cid = v.get("concept_id") if isinstance(v, Mapping) else v
        if isinstance(v, (tuple, list)):
            cid = v[0]
        out[str(name)] = int(cid)
    return out


def custom_concept_registry(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    """The custom concepts registered in this database: ``{feature name: {"concept_id", "concept_name"}}``. Empty when there is no
    ``concept`` table."""
    if not _has_table(con, "concept"):
        return {}
    return {code: {"concept_id": int(cid), "concept_name": name} for cid, code, name in con.execute(
        "SELECT concept_id, concept_code, concept_name FROM concept WHERE vocabulary_id = ? ORDER BY concept_id", [CUSTOM_VOCABULARY]).fetchall()}


def export_custom_concept_registry(con: duckdb.DuckDBPyConnection, path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Writes (when ``path`` is given) and returns the custom concept registry. A consortium shares one registry file so every site
    gives a feature the same concept id (:func:`register_custom_concepts` with ``registry=``), and :func:`check_custom_concepts` finds
    sites whose ids drifted apart."""
    reg = custom_concept_registry(con)
    if path is not None:
        Path(path).write_text(json.dumps(reg, indent=2, sort_keys=True), encoding="utf-8")
    return reg


def check_custom_concepts(con: duckdb.DuckDBPyConnection, *registries: Any, raise_on_conflict: bool = False) -> list[dict[str, Any]]:
    """Finds problems with the custom concept ids of this database, alone and against other sites' registries.

    Checks, each returned as ``{"kind", "feature", "detail"}``: ``below_local_range`` (an id under 2,000,000,000 in the custom
    vocabulary, which can collide with an Athena concept), ``id_collision`` (a custom id also used by a concept of another
    vocabulary), ``duplicate_id`` (two feature names on one id), and, for each registry passed (a mapping or a JSON file from
    :func:`export_custom_concept_registry`), ``name_different_id`` (this site and the registry give a feature different ids) and
    ``id_different_name`` (the same id means different features). Models trained on feature names are portable across sites; models
    that store the id are not, so these conflicts break an external validation silently. ``raise_on_conflict`` raises ``ValueError``."""
    problems: list[dict[str, Any]] = []
    local = custom_concept_registry(con)
    seen: dict[int, str] = {}
    for name, v in local.items():
        cid = v["concept_id"]
        if cid < LOCAL_CONCEPT_MIN:
            problems.append({"kind": "below_local_range", "feature": name, "detail": f"id {cid} is under {LOCAL_CONCEPT_MIN}"})
        if cid in seen:
            problems.append({"kind": "duplicate_id", "feature": name, "detail": f"id {cid} is also used by {seen[cid]}"})
        seen[cid] = name
    if local:
        for cid, vocab in con.execute("SELECT concept_id, vocabulary_id FROM concept WHERE vocabulary_id <> ? AND concept_id IN ("
                                      + ", ".join(str(v["concept_id"]) for v in local.values()) + ")", [CUSTOM_VOCABULARY]).fetchall():
            problems.append({"kind": "id_collision", "feature": seen[int(cid)], "detail": f"id {cid} is also a {vocab} concept"})
    for i, reg in enumerate(registries, start=1):
        other = _load_registry(reg)
        by_id = {cid: n for n, cid in other.items()}
        for name, v in local.items():
            if name in other and other[name] != v["concept_id"]:
                problems.append({"kind": "name_different_id", "feature": name,
                                 "detail": f"registry {i} has {name} = {other[name]}, this database {v['concept_id']}"})
            elif name not in other and by_id.get(v["concept_id"], name) != name:
                problems.append({"kind": "id_different_name", "feature": name,
                                 "detail": f"id {v['concept_id']} is {by_id[v['concept_id']]} in registry {i}, {name} here"})
    if problems and raise_on_conflict:
        raise ValueError("custom concept conflicts: " + "; ".join(f"{p['kind']} {p['feature']} ({p['detail']})" for p in problems))
    return problems


def register_custom_concepts(con: duckdb.DuckDBPyConnection, names: Mapping[str, str] | list[str], *, registry: Any = None) -> dict[str, tuple[int, str]]:
    """Returns ``{feature: (concept_id, label)}`` for ``write_custom_covariates``, reusing the id already registered for a
    feature name in this database (``concept_code`` = the name, vocabulary ``omop-duck-db``) and otherwise taking the next free
    id from 2,001,000,000 upwards (clear of the geography ids 2,000,100,001-3). ``names`` is a list of feature names or ``{name: label}``.

    Ids are per database unless sites agree on them. Pass ``registry`` (a mapping ``{name: id}`` or the JSON file from
    :func:`export_custom_concept_registry`) to pin the ids a consortium shares: a name in the registry takes the registry's id, and
    a name outside it gets an id above every registry id. A registry id that this database already gives to another feature, or
    that belongs to a vocabulary concept, raises ``ValueError``, so a mismatch is caught before any covariate is written."""
    labels = dict(names) if isinstance(names, Mapping) else {n: n for n in names}
    pinned = _load_registry(registry)
    have = _has_table(con, "concept")
    out: dict[str, tuple[int, str]] = {}
    used = set()
    taken: dict[int, tuple[str, str]] = {}
    if have:
        for cid, code, vocab in con.execute("SELECT concept_id, concept_code, vocabulary_id FROM concept WHERE concept_id >= 2000000000").fetchall():
            taken[int(cid)] = (code, vocab)
        for cid, code in con.execute("SELECT concept_id, concept_code FROM concept WHERE vocabulary_id = ?", [CUSTOM_VOCABULARY]).fetchall():
            if code in labels:
                out[code] = (int(cid), labels[code])
        used = {int(r[0]) for r in con.execute("SELECT concept_id FROM concept WHERE concept_id >= 2001000000").fetchall()}
    for name, cid in pinned.items():
        if name not in labels:
            continue
        if cid < LOCAL_CONCEPT_MIN:
            raise ValueError(f"registry id {cid} for {name} is under {LOCAL_CONCEPT_MIN} (site-defined range)")
        if name in out and out[name][0] != cid:
            raise ValueError(f"{name} is concept {out[name][0]} in this database but {cid} in the registry")
        if cid in taken and taken[cid] != (name, CUSTOM_VOCABULARY):
            raise ValueError(f"registry id {cid} for {name} is already used here by {taken[cid][0]} ({taken[cid][1]})")
        out[name] = (cid, labels[name])
    nxt = max([2_001_000_000 - 1, *used, *pinned.values(), *(v[0] for v in out.values())]) + 1
    for name, label in labels.items():
        if name not in out:
            out[name] = (nxt, label)
            nxt += 1
    return out


def write_custom_features(
    con: duckdb.DuckDBPyConnection,
    features: pd.DataFrame,
    *,
    labels: Mapping[str, str] | None = None,
    date_col: str = "date",
    registry: Any = None,
    **kwargs: Any,
) -> dict[str, tuple[int, str]]:
    """Writes a wide table of your own features (``person_id``, ``date_col`` and one numeric column per feature) so
    FeatureExtraction, PatientLevelPrediction and DeepPatientLevelPrediction read them like any other covariate.

    This is how a study adds its own version of a feature that OMOP already defines (a Charlson index, an Elixhauser score, a
    frailty index) next to the standard one: give it its own name (``charlson_site``), and pull both through the same
    covariate settings. Returns the ``{feature: (concept_id, label)}`` mapping to pass to ``custom_covariate_settings``.
    ``registry`` pins the concept ids a consortium shares (see :func:`register_custom_concepts`). ``kwargs`` go to
    :func:`write_custom_covariates`.
    """
    feats = [c for c in features.columns if c not in ("person_id", date_col, "visit_occurrence_id")]
    concepts = register_custom_concepts(con, {f: (labels or {}).get(f, f) for f in feats}, registry=registry)
    long = features.melt(id_vars=[c for c in ("person_id", date_col, "visit_occurrence_id") if c in features.columns],
                         value_vars=feats, var_name="feature", value_name="value").rename(columns={date_col: "date"})
    write_custom_covariates(con, long, concepts, **kwargs)
    return concepts
