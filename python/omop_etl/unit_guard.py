"""Pre-scoring unit sanity guard: flag labs whose site median is far from the consortium reference median.

A feed in mmol/L (or umol/L) where mg/dL is expected is clipped silently by the physiologic bounds; comparing each
feature's site median with a reference median catches it before scoring. A ratio above ``max_ratio`` (default 3) in
either direction is flagged, with the usual suspect conversion named.
"""

from __future__ import annotations

import warnings
from typing import Any, Mapping

import numpy as np
import pandas as pd

__all__ = ["REFERENCE_MEDIANS", "check_unit_sanity", "harmonize_contract_units"]

# Typical adult inpatient medians in the contract units (mg/dL for creatinine / BUN; mmol/L = mEq/L for electrolytes).
# Override with the consortium's own medians via ``reference_medians``.
REFERENCE_MEDIANS = {
    "lab_creatinine": 1.0, "lab_bun": 18.0, "lab_sodium": 139.0, "lab_potassium": 4.1, "lab_chloride": 103.0,
    "lab_bicarbonate": 24.0, "lab_anion_gap": 10.0, "lab_egfr": 70.0, "lab_glucose": 110.0,
    "heart_rate": 80.0, "systolic_bp": 125.0, "diastolic_bp": 70.0,
}
_SUSPECTS = {  # (site/reference ratio is near ..., likely cause)
    "lab_creatinine": [(88.4, "umol/L reported instead of mg/dL"), (1 / 88.4, "mg/dL reported as umol/L-scale inverse")],
    "lab_bun": [(0.357, "mmol/L urea reported instead of mg/dL BUN"), (2.801, "BUN mg/dL reported as mmol/L urea inverse")],
    "lab_glucose": [(0.0555, "mmol/L reported instead of mg/dL"), (18.0, "mg/dL reported as mmol/L inverse")],
}


def check_unit_sanity(features: pd.DataFrame, *, reference_medians: Mapping[str, float] | None = None,
                      max_ratio: float = 3.0, min_n: int = 20, warn: bool = True) -> pd.DataFrame:
    """One row per checked feature: ``site_median``, ``reference_median``, ``ratio``, ``n``, ``flag``, ``hint``.

    Features absent from ``features``, or with fewer than ``min_n`` non-null values, are skipped. With ``warn`` a
    ``UserWarning`` names every flagged feature.
    """
    ref = {**REFERENCE_MEDIANS, **(reference_medians or {})}
    rows = []
    for name, r in ref.items():
        if name not in features.columns or not r:
            continue
        v = pd.to_numeric(features[name], errors="coerce").dropna()
        if len(v) < min_n:
            continue
        med = float(np.median(v))
        ratio = med / float(r) if r else np.nan
        flag = bool(np.isfinite(ratio) and (ratio > max_ratio or (ratio < 1 / max_ratio if ratio > 0 else True)))
        hint, factor = "", np.nan
        if flag:
            near = [(target, msg) for target, msg in _SUSPECTS.get(name, [])
                    if abs(np.log(max(ratio, 1e-9) / target)) < np.log(1.6)]
            hint = near[0][1] if near else "unharmonized unit or scale suspected"
            factor = 1.0 / near[0][0] if near else np.nan  # multiply the column by this to reach the contract unit
        rows.append({"feature": name, "n": int(len(v)), "site_median": med, "reference_median": float(r),
                     "ratio": float(ratio), "flag": flag, "hint": hint, "suspect_factor": factor})
    out = pd.DataFrame(rows, columns=["feature", "n", "site_median", "reference_median", "ratio", "flag", "hint",
                                      "suspect_factor"])
    if warn and out["flag"].any():
        bad = out[out["flag"]]
        warnings.warn("unit sanity: " + "; ".join(f"{r.feature} median {r.site_median:.3g} vs reference {r.reference_median:.3g}"
                                                  + (f" ({r.hint})" if r.hint else "") for r in bad.itertuples()),
                      UserWarning, stacklevel=2)
    return out


def harmonize_contract_units(features: pd.DataFrame, sanity: pd.DataFrame | None = None, *,
                             reference_medians: Mapping[str, float] | None = None,
                             recompute_derived: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rescale features flagged by :func:`check_unit_sanity` to the contract units; returns ``(fixed, applied)``.

    Only features whose median matches a named suspect conversion (umol/L creatinine, mmol/L urea, mmol/L glucose, ...)
    are rescaled, by ``suspect_factor``; features flagged for an unexplained reason are left untouched and listed in
    ``applied`` with ``rescaled == False`` so they can still be reviewed. ``features`` is not modified. Pass the
    ``sanity`` frame from an earlier ``check_unit_sanity`` call, or omit it to run the check here (without a warning).

    With ``recompute_derived`` (default) a rescaled creatinine also refreshes ``lab_egfr`` (CKD-EPI 2021, needs ``age`` and
    ``gender_concept_id``) and a rescaled sodium / chloride / bicarbonate refreshes ``lab_anion_gap``, so they do not keep
    the values computed from the unharmonized inputs. Only rows that have the inputs are overwritten; each refreshed
    column is listed in ``applied`` with ``rescaled == False`` and a hint saying it was recomputed.
    """
    sanity = check_unit_sanity(features, reference_medians=reference_medians, warn=False) if sanity is None else sanity
    flagged = sanity[sanity["flag"].astype(bool)] if len(sanity) else sanity
    fixed = features.copy()
    rows = []
    for r in flagged.itertuples():
        ok = bool(np.isfinite(r.suspect_factor)) and r.feature in fixed.columns
        if ok:
            fixed[r.feature] = pd.to_numeric(fixed[r.feature], errors="coerce") * float(r.suspect_factor)
        rows.append({"feature": r.feature, "factor": float(r.suspect_factor) if np.isfinite(r.suspect_factor) else np.nan,
                     "rescaled": ok, "hint": r.hint})
    if recompute_derived:
        done = {r["feature"] for r in rows if r["rescaled"]}
        for d in _recompute_derived(fixed, done):  # a derived column that was itself flagged is updated, not repeated
            rows = [r for r in rows if r["feature"] != d["feature"]] + [d]
    return fixed, pd.DataFrame(rows, columns=["feature", "factor", "rescaled", "hint"])


def _recompute_derived(fixed: pd.DataFrame, rescaled: set[str]) -> list[dict[str, Any]]:
    """Blank and re-derive ``lab_egfr`` / ``lab_anion_gap`` in place where their inputs were rescaled."""
    from omop_etl.velocity import derive_labs  # lazy: velocity imports heavier modules

    rows = []
    # derive_labs only fills NaN (anion gap) or eGFR >= 60, so a stale value must be cleared first.
    if "lab_creatinine" in rescaled and {"age", "gender_concept_id"} <= set(fixed.columns):
        fixed.loc[fixed["lab_creatinine"].notna(), "lab_egfr"] = np.nan
        rows.append({"feature": "lab_egfr", "factor": np.nan, "rescaled": False,
                     "hint": "recomputed from harmonized lab_creatinine"})
    gap_in = ["lab_sodium", "lab_chloride", "lab_bicarbonate"]
    if rescaled & set(gap_in) and all(c in fixed.columns for c in gap_in):
        fixed.loc[fixed[gap_in].notna().all(axis=1), "lab_anion_gap"] = np.nan
        rows.append({"feature": "lab_anion_gap", "factor": np.nan, "rescaled": False,
                     "hint": "recomputed from harmonized sodium / chloride / bicarbonate"})
    if rows:
        derive_labs(fixed)
    return rows
