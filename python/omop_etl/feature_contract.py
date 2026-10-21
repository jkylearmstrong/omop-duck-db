"""Feature-contract validation (FR-A3): catch drift before a model scores garbage.

A *feature spec* is a dict (or the ``feature_spec.json`` of a model bundle)::

    {"features": [{"name": "creatinine", "dtype": "float", "unit": "mg/dL", "bounds": [0.1, 30.0], "impute": 1.0},
                  ...]}

``bounds`` default to :data:`omop_etl.measurement_filter.DEFAULT_PHYSIOLOGIC_BOUNDS` for the feature name; ``dtype`` is
one of ``float``, ``int``, ``bool``, ``str``. :func:`validate_feature_frame` returns a :class:`FeatureValidationReport`
whose ``frame`` is the (possibly clipped / nullified) data, ordered as the spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from omop_etl.measurement_filter import DEFAULT_PHYSIOLOGIC_BOUNDS

__all__ = ["FeatureContractError", "FeatureValidationReport", "validate_feature_frame", "load_feature_spec"]

_DTYPES = {"float": "f", "int": "iu", "bool": "b", "str": "OUS"}
# feature -> (factor, label): canonical = value / factor, so a frame holding `label` values looks ~factor times too big
# (or too small for factor < 1).
_UNIT_SUSPECTS: dict[str, list[tuple[float, str]]] = {
    "creatinine": [(88.4, "umol/L")],
    "glucose": [(1 / 18.016, "mmol/L")],
    "hemoglobin": [(10.0, "g/L")],
    "wbc": [(1000.0, "cells/uL")],
    "bun": [(0.357, "mmol/L urea")],
}


class FeatureContractError(ValueError):
    """The frame violates the feature contract and ``on_violation='raise'``. ``.report`` holds the full report."""

    report: "FeatureValidationReport | None" = None


def load_feature_spec(spec: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(spec, (str, Path)):
        spec = json.loads(Path(spec).read_text(encoding="utf-8"))
    feats = spec.get("features") if isinstance(spec, Mapping) else None
    if not feats:
        raise ValueError("feature_spec needs a non-empty 'features' list.")
    names = [f["name"] for f in feats]
    if len(set(names)) != len(names):
        raise ValueError("feature_spec has duplicate feature names.")
    return dict(spec)


@dataclass
class FeatureValidationReport:
    """Machine-readable (``to_dict``) and human-readable (``summary``) outcome of a validation."""

    n_rows: int
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    dtype_mismatch: dict[str, dict[str, str]] = field(default_factory=dict)
    out_of_bounds: dict[str, dict[str, Any]] = field(default_factory=dict)
    unit_suspicion: dict[str, str] = field(default_factory=dict)
    all_null: list[str] = field(default_factory=list)
    on_violation: str = "raise"
    n_clipped: int = 0
    n_nullified: int = 0
    frame: pd.DataFrame | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.dtype_mismatch or self.out_of_bounds or self.unit_suspicion)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows, "ok": self.ok, "missing": self.missing, "extra": self.extra,
            "dtype_mismatch": self.dtype_mismatch, "out_of_bounds": self.out_of_bounds,
            "unit_suspicion": self.unit_suspicion, "all_null": self.all_null, "on_violation": self.on_violation,
            "n_clipped": self.n_clipped, "n_nullified": self.n_nullified,
        }

    def to_telemetry(self) -> dict[str, Any]:
        """Aggregate-only counts for the site telemetry packet (no values)."""
        return {"n_rows": self.n_rows, "n_missing_features": len(self.missing), "n_extra_features": len(self.extra),
                "n_dtype_mismatch": len(self.dtype_mismatch), "n_unit_suspicion": len(self.unit_suspicion),
                "n_values_clipped": int(self.n_clipped), "n_values_nullified": int(self.n_nullified),
                "on_violation": self.on_violation}

    def summary(self) -> str:
        if self.ok:
            return f"Feature contract OK ({self.n_rows} rows)."
        lines = [f"Feature contract violations ({self.n_rows} rows):"]
        if self.missing:
            lines.append(f"  missing columns: {', '.join(self.missing)}")
        if self.extra:
            lines.append(f"  unexpected columns: {', '.join(self.extra)}")
        for c, d in self.dtype_mismatch.items():
            lines.append(f"  dtype {c}: expected {d['expected']}, got {d['actual']}")
        for c, d in self.out_of_bounds.items():
            lines.append(f"  out of bounds {c}: {d['n']} values ({d['pct']:.2f}%) outside {d['bounds']}")
        for c, msg in self.unit_suspicion.items():
            lines.append(f"  unit suspicion {c}: {msg}")
        return "\n".join(lines)


def _bounds_for(f: Mapping[str, Any], bounds: Mapping[str, Any] | None):
    b = f.get("bounds")
    if b is None and bounds is not None:
        b = bounds.get(f["name"])
    if b is None:
        b = DEFAULT_PHYSIOLOGIC_BOUNDS.get(f["name"])
    if b is None:
        return None
    lo, hi = b
    return (-np.inf if lo is None else float(lo), np.inf if hi is None else float(hi))


def validate_feature_frame(
    df: pd.DataFrame,
    feature_spec: Mapping[str, Any] | str | Path,
    on_violation: str = "raise",
    *,
    bounds: Mapping[str, Any] | None = None,
    allow_extra: bool = True,
) -> FeatureValidationReport:
    """Checks ``df`` against ``feature_spec``.

    Args:
        df: Frame with one column per feature (extra columns such as ids are allowed unless ``allow_extra=False``).
        feature_spec: Spec dict or path to ``feature_spec.json``.
        on_violation: ``'raise'`` (any violation raises :class:`FeatureContractError`), ``'clip'`` (out-of-bounds values
            clipped to the nearest bound) or ``'nullify'`` (set to NaN). Missing columns, dtype mismatches and unit
            suspicion always raise: they cannot be repaired by clipping.
        bounds: Extra ``{feature: [lo, hi]}`` defaults used when a feature has no ``bounds`` in the spec.
        allow_extra: If False, unexpected columns are violations.

    Returns:
        :class:`FeatureValidationReport`; ``report.frame`` holds the spec-ordered, repaired feature columns (other
        columns are not included).
    """
    if on_violation not in ("raise", "clip", "nullify"):
        raise ValueError("on_violation must be 'raise', 'clip' or 'nullify'.")
    spec = load_feature_spec(feature_spec)
    feats = spec["features"]
    names = [f["name"] for f in feats]
    rep = FeatureValidationReport(n_rows=len(df), on_violation=on_violation)
    rep.missing = [n for n in names if n not in df.columns]
    if not allow_extra:
        rep.extra = [c for c in df.columns if c not in set(names)]
    out = df[[n for n in names if n in df.columns]].copy()
    for f in feats:
        n = f["name"]
        if n not in out.columns:
            continue
        want = f.get("dtype", "float")
        if want not in _DTYPES:
            raise ValueError(f"feature {n!r}: unknown dtype {want!r}.")
        kind = out[n].dtype.kind
        numeric_like = kind in "fiub"
        if want in ("float", "int", "bool") and not numeric_like:
            rep.dtype_mismatch[n] = {"expected": want, "actual": str(out[n].dtype)}
            continue
        if want in ("float", "int") and kind == "b":
            rep.dtype_mismatch[n] = {"expected": want, "actual": "bool"}
            continue
        if want == "str" and kind not in "OUS" and str(out[n].dtype) != "string":
            rep.dtype_mismatch[n] = {"expected": want, "actual": str(out[n].dtype)}
            continue
        if out[n].notna().sum() == 0:
            rep.all_null.append(n)
        if want not in ("float", "int"):
            continue
        b = _bounds_for(f, bounds)
        if b is None:
            continue
        vals = pd.to_numeric(out[n], errors="coerce").astype("float64")
        nn = vals.notna()
        bad = nn & ((vals < b[0]) | (vals > b[1]))
        nbad = int(bad.sum())
        if nbad:
            rep.out_of_bounds[n] = {"n": nbad, "pct": 100.0 * nbad / max(int(nn.sum()), 1), "bounds": [
                None if np.isinf(b[0]) else b[0], None if np.isinf(b[1]) else b[1]]}
        for factor, label in _UNIT_SUSPECTS.get(n, []):
            if nn.sum() and bad.sum() / nn.sum() >= 0.5:
                rescaled = vals[nn] / factor
                if ((rescaled >= b[0]) & (rescaled <= b[1])).mean() >= 0.5:
                    rep.unit_suspicion[n] = (
                        f"{bad.sum()}/{int(nn.sum())} values are out of range but fall in range if divided by "
                        f"{factor:g}: looks like {label} rather than {f.get('unit', 'the canonical unit')}.")
                    break
        if nbad and on_violation in ("clip", "nullify") and n not in rep.unit_suspicion:
            if on_violation == "clip":
                out[n] = vals.clip(lower=b[0], upper=b[1])
                rep.n_clipped += nbad
            else:
                out[n] = vals.mask(bad)
                rep.n_nullified += nbad
    rep.frame = out.reindex(columns=[n for n in names if n in out.columns])

    fatal = rep.missing or rep.extra or rep.dtype_mismatch or rep.unit_suspicion
    unresolved_bounds = rep.out_of_bounds and on_violation == "raise"
    if fatal or unresolved_bounds:
        err = FeatureContractError(rep.summary())
        err.report = rep
        raise err
    return rep
