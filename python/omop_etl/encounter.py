"""Point-of-care scoring of one encounter (``score_encounter``) for discharge-alert hooks.

``score_cohort`` scores a batch table built around first-admission-day measurements. At the bedside the question is
"what is this patient's risk NOW", so :func:`score_encounter` builds the same velocity feature row for a single
``visit_occurrence_id`` and replaces the first-day vitals/labs with the LATEST value recorded between admission and
``as_of`` (default: the visit end date or today, whichever is earlier). For a live, still-open stay whose CDM end date is
only a placeholder, pass ``as_of=today``. Everything else (utilization history, age,
``extra_features``) and the validate -> impute -> blend -> calibrate path are exactly those of ``score_cohort``.
"""

from __future__ import annotations

import datetime as _dt
import re
import warnings
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from omop_etl.model_bundle import ModelBundle, load_model_bundle, score_cohort
from omop_etl.risk_tiers import map_risk_tiers
from omop_etl.velocity import VELOCITY_CONCEPTS, derive_labs, engineer_clinical_features, extract_velocity_features

__all__ = ["score_encounter"]

_LATEST_FEATURES = ("heart_rate", "systolic_bp", "diastolic_bp", "lab_bun", "lab_creatinine", "lab_sodium",
                    "lab_potassium", "lab_chloride", "lab_bicarbonate", "lab_anion_gap", "lab_egfr")
_COHORT_VIEW = "_score_encounter_cohort"


def score_encounter(con: duckdb.DuckDBPyConnection, visit_occurrence_id: int, bundle: ModelBundle | str,
                    *, as_of: Any = None, extra_features: pd.DataFrame | None = None, on_violation: str = "raise",
                    risk_tiers: Sequence[Mapping[str, Any]] | None = None, concepts: Mapping[str, Any] | None = None,
                    ) -> dict[str, Any]:
    """Real-time risk for one encounter.

    Args:
        con: DuckDB connection to the OMOP CDM.
        visit_occurrence_id: the encounter to score.
        bundle: a loaded :class:`ModelBundle` or bundle directory (load once and reuse it in a long-running service).
        as_of: only measurements on or before this date count (default: the earlier of visit end date and today).
        extra_features: optional one-row-per-(subject_id, cohort_start_date) frame (``elix_*``, ``med_*`` ...), as in
            ``extract_velocity_features``.
        on_violation: ``raise`` | ``clip`` | ``nullify`` for out-of-contract values (see ``validate_feature_frame``).
        risk_tiers: ``[{"name", "min"}]`` cut-points (e.g. from ``fit_capacity_thresholds``) replacing the bundle's tiers.
        concepts: override of ``VELOCITY_CONCEPTS`` (feature -> measurement concept ids).

    Returns:
        dict with ``visit_occurrence_id``, ``person_id``, ``prob``, ``risk_tier``, ``as_of``, ``features`` (the values
        scored), ``measurement_dates`` (date of the latest reading behind each lab/vital; absent = none found) and
        ``validation`` (the feature-contract summary).
    """
    vid = int(visit_occurrence_id)
    row = con.execute("SELECT person_id, visit_start_date, visit_end_date FROM visit_occurrence WHERE visit_occurrence_id = ?",
                      [vid]).fetchone()
    if row is None:
        raise ValueError(f"visit_occurrence_id {vid} not found.")
    person_id, start, end = row
    start = pd.Timestamp(start).normalize()
    today = pd.Timestamp(_dt.date.today())
    cutoff = pd.Timestamp(as_of).normalize() if as_of is not None else (
        min(pd.Timestamp(end).normalize(), today) if end is not None else today)
    if cutoff < start:
        raise ValueError(f"as_of {cutoff.date()} is before the admission date {start.date()}.")
    b = bundle if isinstance(bundle, ModelBundle) else load_model_bundle(bundle)

    cohort = pd.DataFrame({"subject_id": [person_id], "cohort_start_date": [start], "cohort_end_date": [cutoff]})
    con.register(_COHORT_VIEW, cohort)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # absent optional inputs are surfaced by the contract check
            raw = extract_velocity_features(con, _COHORT_VIEW, engineer=False, extra_features=extra_features,
                                            concepts=dict(concepts) if concepts else None)
        latest = _latest_measurements(con, person_id, start, cutoff, {**VELOCITY_CONCEPTS, **(concepts or {})})
    finally:
        con.unregister(_COHORT_VIEW)

    # Latest reading replaces the admission-day value; derived labs are recomputed from what is now there.
    for feat, inputs in (("lab_egfr", ("lab_creatinine",)),
                         ("lab_anion_gap", ("lab_sodium", "lab_chloride", "lab_bicarbonate"))):
        if feat in latest or any(f in latest for f in inputs):
            raw[feat] = float("nan")
    for feat, (value, _) in latest.items():
        raw[feat] = value
    raw = derive_labs(raw)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="engineer_clinical_features: input columns absent")
        feats = engineer_clinical_features(raw, warn_missing=False)

    frame = feats.assign(visit_occurrence_id=vid)
    scored, report = score_cohort(b, frame, key_columns=["visit_occurrence_id"], on_violation=on_violation,
                                  return_validation=True)
    prob = float(scored["prob"].iloc[0])
    tier = map_risk_tiers(prob, risk_tiers) if risk_tiers is not None else str(scored["risk_tier"].iloc[0])
    values = {n: (None if pd.isna(feats[n].iloc[0]) else float(feats[n].iloc[0])) for n in b.feature_names if n in feats}
    return {"visit_occurrence_id": vid, "person_id": int(person_id), "prob": prob, "risk_tier": tier,
            "as_of": cutoff.date().isoformat(), "features": values,
            "measurement_dates": {k: d.date().isoformat() for k, (_, d) in latest.items()},
            "validation": report.summary()}


def _latest_measurements(con, person_id, start, cutoff, concepts) -> dict[str, tuple[float, pd.Timestamp]]:
    """feature -> (value, date) of the most recent non-null reading in [start, cutoff]."""
    cols = {r[0].lower() for r in con.execute("DESCRIBE measurement").fetchall()}
    when = "COALESCE(measurement_datetime, CAST(measurement_date AS TIMESTAMP))" if "measurement_datetime" in cols \
        else "CAST(measurement_date AS TIMESTAMP)"
    out: dict[str, tuple[float, pd.Timestamp]] = {}
    for feat in _LATEST_FEATURES:
        ids = [int(c) for c in concepts.get(feat, ())]
        if not ids or not all(re.fullmatch(r"-?\d+", str(c)) for c in ids):
            continue
        r = con.execute(
            f"SELECT value_as_number, measurement_date FROM measurement WHERE person_id = ? "
            f"AND measurement_concept_id IN ({', '.join(map(str, ids))}) AND value_as_number IS NOT NULL "
            f"AND measurement_date BETWEEN ? AND ? ORDER BY {when} DESC, measurement_id DESC LIMIT 1",
            [person_id, start.date(), cutoff.date()]).fetchone()
        if r is not None:
            out[feat] = (float(r[0]), pd.Timestamp(r[1]))
    return out
