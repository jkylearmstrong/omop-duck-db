"""Make a model bundle refuse to run against custom concept ids that mean something else at this site.

A model that reads a custom feature (a site's Charlson index, an area deprivation index) by concept id is only portable if the id means the
same feature everywhere. Custom concept ids are per database (:func:`omop_etl.custom_covariates.register_custom_concepts`), so a bundle
trained at site A and scored at site B can silently read the wrong column. The fix is to record the id in the bundle and check it:

* :func:`stamp_feature_spec_concepts` adds ``concept_id`` to the features of a feature spec that are registered custom concepts, so the
  bundle records which id each feature had when the model was trained.
* :func:`validate_bundle_concepts` compares those ids with this site's registry (a database connection, a registry mapping, or the JSON
  file from :func:`omop_etl.custom_covariates.export_custom_concept_registry`) and reports every feature whose id differs, is missing, or
  is used by another feature here.
* ``score_cohort(..., check_concepts=...)`` runs the check before scoring.

A feature is matched by name (or by ``concept_key`` when the name differs from the registered concept code). Features without a
``concept_id`` in the spec are not checked.
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from omop_etl.custom_covariates import _load_registry, custom_concept_registry

__all__ = ["stamp_feature_spec_concepts", "validate_bundle_concepts", "BUNDLE_CONCEPT_STATUSES"]

BUNDLE_CONCEPT_STATUSES = ("ok", "different_id", "not_registered_here", "id_used_by_other_feature")


def _local_ids(source: Any) -> dict[str, int]:
    """``{feature key: concept_id}`` from a DuckDB connection (its registered custom concepts), a mapping, or a registry JSON file."""
    if hasattr(source, "execute"):
        return {k: int(v["concept_id"]) for k, v in custom_concept_registry(source).items()}
    return _load_registry(source)


def _spec(bundle: Any) -> Mapping[str, Any]:
    if isinstance(bundle, Mapping):
        return bundle
    if hasattr(bundle, "feature_spec"):
        return bundle.feature_spec
    from omop_etl.model_bundle import load_model_bundle
    return load_model_bundle(bundle).feature_spec


def stamp_feature_spec_concepts(feature_spec: Mapping[str, Any], source: Any) -> dict[str, Any]:
    """Returns a copy of ``feature_spec`` in which every feature that is a registered custom concept in ``source`` (a connection, a
    registry mapping or a registry file) carries its ``concept_id``. Use it when writing a bundle so the ids are recorded."""
    ids = _local_ids(source)
    out = dict(feature_spec)
    feats = []
    for f in feature_spec["features"]:
        f = dict(f)
        key = f.get("concept_key", f["name"])
        if key in ids:
            f["concept_id"] = int(ids[key])
        feats.append(f)
    out["features"] = feats
    return out


def validate_bundle_concepts(bundle: Any, source: Any, *, raise_on_conflict: bool = True) -> pd.DataFrame:
    """Compares the custom concept ids recorded in a bundle with this site's.

    ``bundle`` is a :class:`ModelBundle`, a bundle directory, or a feature spec dict; ``source`` is a DuckDB connection, a registry
    mapping ``{feature: id}`` or a registry JSON file. Returns one row per feature that records a ``concept_id``:
    ``feature``, ``key``, ``bundle_concept_id``, ``local_concept_id``, ``status`` (one of :data:`BUNDLE_CONCEPT_STATUSES`) and ``detail``.
    With ``raise_on_conflict`` (default) any status other than ``ok`` raises ``ValueError`` listing them, so a bundle never scores a column
    that means another feature here."""
    ids = _local_ids(source)
    by_id = {v: k for k, v in ids.items()}
    rows = []
    for f in _spec(bundle)["features"]:
        declared = f.get("concept_id")
        if declared is None:
            continue
        declared = int(declared)
        key = f.get("concept_key", f["name"])
        local = ids.get(key)
        if local is None:
            other = by_id.get(declared)
            if other is not None:
                status, detail = "id_used_by_other_feature", f"id {declared} is {other!r} at this site, and {key!r} is not registered here"
            else:
                status, detail = "not_registered_here", f"{key!r} is not a registered custom concept at this site"
        elif local != declared:
            status, detail = "different_id", f"the bundle was trained with {key!r} = {declared}, this site has {local}"
        else:
            status, detail = "ok", ""
        rows.append({"feature": f["name"], "key": key, "bundle_concept_id": declared, "local_concept_id": local, "status": status, "detail": detail})
    out = pd.DataFrame(rows, columns=["feature", "key", "bundle_concept_id", "local_concept_id", "status", "detail"])
    bad = out[out["status"] != "ok"]
    if raise_on_conflict and len(bad):
        raise ValueError("bundle custom concept ids do not match this site: "
                         + "; ".join(f"{r.feature} ({r.status}: {r.detail})" for r in bad.itertuples()))
    return out
