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

__all__ = ["REFERENCE_MEDIANS", "check_unit_sanity"]

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
        hint = ""
        if flag:
            near = [msg for target, msg in _SUSPECTS.get(name, []) if abs(np.log(max(ratio, 1e-9) / target)) < np.log(1.6)]
            hint = near[0] if near else "unharmonized unit or scale suspected"
        rows.append({"feature": name, "n": int(len(v)), "site_median": med, "reference_median": float(r),
                     "ratio": float(ratio), "flag": flag, "hint": hint})
    out = pd.DataFrame(rows, columns=["feature", "n", "site_median", "reference_median", "ratio", "flag", "hint"])
    if warn and out["flag"].any():
        bad = out[out["flag"]]
        warnings.warn("unit sanity: " + "; ".join(f"{r.feature} median {r.site_median:.3g} vs reference {r.reference_median:.3g}"
                                                  + (f" ({r.hint})" if r.hint else "") for r in bad.itertuples()),
                      UserWarning, stacklevel=2)
    return out
