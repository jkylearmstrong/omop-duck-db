"""Capacity-calibrated risk tiers: map continuous probabilities to discrete intervention tiers.

A tier definition is an ordered list (highest risk first) of ``{"name", "top_fraction"}`` entries, where ``top_fraction``
is the CUMULATIVE share of the reference population that falls in that tier or above (the last tier has no
``top_fraction`` and takes the rest). ``[{"name": "high", "top_fraction": 0.10}, {"name": "standard"}]`` means "the
riskiest 10% get the nurse transition call"; adding ``{"name": "elevated", "top_fraction": 0.30}`` between them makes
"elevated" the next 20%.

:func:`fit_capacity_thresholds` turns that into absolute probability cut-points on a reference set (e.g. last year's
scores), in the ``[{"name", "min"}]`` shape that ``write_model_bundle(risk_tiers=...)`` stores. Freeze the cut-points: re-fitting on
each batch would hide drift (a tier that always holds 10% says nothing about whether the model still works).
:func:`map_risk_tiers` applies cut-points; :func:`tier_load` reports the flagged volume against staffing capacity.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["fit_capacity_thresholds", "map_risk_tiers", "tier_load"]


def _clean(prob: Any, what: str = "prob") -> np.ndarray:
    p = np.asarray(prob, dtype="float64").reshape(-1)
    if p.size and (np.isnan(p).any() or p.min() < 0 or p.max() > 1):
        raise ValueError(f"{what} must be probabilities in [0, 1] with no NaN.")
    return p


def fit_capacity_thresholds(reference_prob: Any, tiers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Cut-points so each tier holds its target share of ``reference_prob``.

    Returns tiers sorted lowest risk first as ``[{"name", "min"}]`` (lowest ``min`` is 0.0), with the realised ``fraction``
    of the reference set. Tied probabilities cannot be split, so a tier can be slightly larger than requested.
    """
    ref = _clean(reference_prob, "reference_prob")
    if ref.size == 0:
        raise ValueError("reference_prob is empty.")
    tiers = [dict(t) for t in tiers]
    if len(tiers) < 2 or any("name" not in t for t in tiers):
        raise ValueError("need at least two named tiers.")
    if len({t["name"] for t in tiers}) != len(tiers):
        raise ValueError("tier names must be unique.")
    if "top_fraction" in tiers[-1]:
        raise ValueError("the last (lowest-risk) tier takes the remainder: omit its top_fraction.")
    fracs = []
    for t in tiers[:-1]:
        f = t.get("top_fraction")
        if f is None or not 0 < float(f) < 1:
            raise ValueError(f"tier {t['name']!r}: top_fraction must be in (0, 1).")
        fracs.append(float(f))
    if any(b <= a for a, b in zip(fracs, fracs[1:])):
        raise ValueError("top_fraction is cumulative and must increase from the highest-risk tier down.")
    cuts = [float(np.quantile(ref, 1.0 - f)) for f in fracs]
    if any(b >= a for a, b in zip(cuts, cuts[1:])):
        raise ValueError("reference probabilities are too tied/concentrated to separate the requested tiers.")
    out = []
    mins = [0.0] + cuts[::-1]
    names = [t["name"] for t in tiers][::-1]
    for i, (n, lo) in enumerate(zip(names, mins)):
        hi = mins[i + 1] if i + 1 < len(mins) else np.inf
        out.append({"name": n, "min": lo, "fraction": float(((ref >= lo) & (ref < hi)).mean())})
    return out


def map_risk_tiers(prob: Any, thresholds: Sequence[Mapping[str, Any]], *, as_category: bool = False) -> Any:
    """Tier name for each probability: the tier with the largest ``min`` <= p (tiers are ``[min, next_min)``).

    ``thresholds`` is ``[{"name", "min"}]`` in any order (e.g. from :func:`fit_capacity_thresholds` or a bundle's
    ``risk_tiers``). A scalar in gives a string out.
    """
    scalar = np.ndim(prob) == 0
    p = _clean(prob)
    ts = sorted(thresholds, key=lambda t: float(t["min"]))
    if not ts or float(ts[0]["min"]) > 0:
        raise ValueError("the lowest tier must start at min = 0 so every probability maps to a tier.")
    mins = np.array([float(t["min"]) for t in ts])
    if (np.diff(mins) <= 0).any():
        raise ValueError("tier minimums must be distinct.")
    idx = np.searchsorted(mins, p, side="right") - 1
    names = np.array([t["name"] for t in ts], dtype=object)[idx]
    if scalar:
        return str(names[0])
    return pd.Categorical(names, categories=[t["name"] for t in ts], ordered=True) if as_category else names


def tier_load(prob: Any, thresholds: Sequence[Mapping[str, Any]], *, capacity: Mapping[str, float] | None = None,
              periods: int = 1) -> pd.DataFrame:
    """Volume per tier: n, share, and (when ``capacity`` maps tier -> cases the team can handle per period) the
    ``per_period`` workload, its ``capacity`` and ``over_capacity`` flag. ``periods`` = number of days/weeks in ``prob``."""
    p = _clean(prob)
    names = map_risk_tiers(p, thresholds)
    order = [t["name"] for t in sorted(thresholds, key=lambda t: -float(t["min"]))]
    counts = pd.Series(names).value_counts().reindex(order, fill_value=0)
    df = pd.DataFrame({"tier": order, "n": counts.to_numpy()})
    df["share"] = df["n"] / max(len(p), 1)
    df["per_period"] = df["n"] / max(int(periods), 1)
    if capacity is not None:
        df["capacity"] = df["tier"].map(capacity)
        df["over_capacity"] = df["capacity"].notna() & (df["per_period"] > df["capacity"])
    return df
