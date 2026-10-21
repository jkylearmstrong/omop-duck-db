"""Aggregate-only site telemetry packets and the consortium aggregator (FR-A2).

``site_telemetry(scores, y_true, site_id)`` computes discrimination, calibration, a decile table and screening tiers at a
site and returns a JSON-serialisable packet that contains **no row-level data**: only counts (cells under ``min_cell`` are
suppressed, with secondary suppression so they cannot be recovered by subtraction) and summary statistics.
``aggregate_telemetry(dir)`` pools the packets of many sites (DerSimonian-Laird random effects: Cochran's Q, I^2, tau^2).

The packet schema is versioned (``telemetry_packet.schema.json``, ``SCHEMA_VERSION``) and validated by
:func:`validate_packet` (uses ``jsonschema`` when installed, otherwise a small built-in validator that supports the subset
the schema uses).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from omop_etl.jsonio import strict_dumps

__all__ = ["SCHEMA_VERSION", "site_telemetry", "aggregate_telemetry", "validate_packet", "load_packet_schema",
           "TelemetryPrivacyError"]

SCHEMA_VERSION = "1.0"
SITE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_TIER_FRACTIONS = (0.05, 0.10, 0.20)


class TelemetryPrivacyError(ValueError):
    """The inputs cannot be summarised without disclosing small cells."""


# ---- statistics (NumPy only) ----------------------------------------------------------------------------

def _midranks(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    ranks = np.empty(len(x), dtype="float64")
    i = 0
    n = len(x)
    while i < n:
        j = i
        while j < n and xs[j] == xs[i]:
            j += 1
        ranks[order[i:j]] = 0.5 * (i + j - 1) + 1
        i = j
    return ranks


def auroc_delong(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """AUROC and its DeLong standard error."""
    pos, neg = p[y == 1], p[y == 0]
    m, n = len(pos), len(neg)
    tz = _midranks(np.concatenate([pos, neg]))
    tx, ty = _midranks(pos), _midranks(neg)
    v10 = (tz[:m] - tx) / n
    v01 = 1.0 - (tz[m:] - ty) / m
    auc = float(v10.mean())
    var = np.var(v10, ddof=1) / m + np.var(v01, ddof=1) / n
    return auc, float(math.sqrt(max(var, 0.0)))


def average_precision(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(-p, kind="mergesort")
    ys, ps = y[order], p[order]
    tp = np.cumsum(ys)
    fp = np.cumsum(1 - ys)
    last = np.r_[ps[1:] != ps[:-1], True]  # one point per distinct threshold
    tp, fp = tp[last], fp[last]
    precision = tp / (tp + fp)
    recall = tp / ys.sum()
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def calibration_fit(y: np.ndarray, p: np.ndarray) -> dict[str, float | None]:
    """Logistic recalibration ``logit P(y=1) = a + b*logit(p)``: slope b, intercept a (+ SEs), and calibration-in-the-large."""
    z = np.log(np.clip(p, 1e-9, 1 - 1e-9) / (1 - np.clip(p, 1e-9, 1 - 1e-9)))
    X = np.column_stack([np.ones_like(z), z])
    beta = np.zeros(2)
    se = [None, None]
    try:
        for _ in range(50):
            mu = 1 / (1 + np.exp(-np.clip(X @ beta, -30, 30)))
            W = mu * (1 - mu)
            H = X.T @ (X * W[:, None]) + 1e-10 * np.eye(2)
            step = np.linalg.solve(H, X.T @ (y - mu))
            beta = beta + step
            if np.max(np.abs(step)) < 1e-9:
                break
        cov = np.linalg.inv(H)
        se = [float(math.sqrt(cov[0, 0])), float(math.sqrt(cov[1, 1]))]
    except np.linalg.LinAlgError:
        return {"slope": None, "slope_se": None, "intercept": None, "intercept_se": None, "citl": None}
    # calibration-in-the-large: intercept with slope fixed at 1
    a = 0.0
    for _ in range(50):
        mu = 1 / (1 + np.exp(-np.clip(a + z, -30, 30)))
        step = (y - mu).sum() / max((mu * (1 - mu)).sum(), 1e-12)
        a += step
        if abs(step) < 1e-10:
            break
    return {"slope": float(beta[1]), "slope_se": se[1], "intercept": float(beta[0]), "intercept_se": se[0],
            "citl": float(a)}


def _suppress(counts: np.ndarray, min_cell: int) -> np.ndarray:
    """Boolean mask of cells to hide: primary (0 < c < min_cell) plus secondary so a lone hidden cell cannot be
    recovered from the (published) column total."""
    mask = (counts > 0) & (counts < min_cell)
    if mask.sum() == 1:
        rest = np.where(~mask & (counts > 0))[0]
        if len(rest):
            mask[rest[np.argmin(counts[rest])]] = True
        else:
            mask[:] = True
    return mask


# ---- packet ---------------------------------------------------------------------------------------------

def site_telemetry(
    scores: Sequence[float] | np.ndarray,
    y_true: Sequence[int] | np.ndarray,
    site_id: str,
    *,
    min_cell: int = 11,
    tier_fractions: Sequence[float] = DEFAULT_TIER_FRACTIONS,
    validation: Any = None,
    model: Mapping[str, str] | None = None,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Builds (and optionally writes) an aggregate-only telemetry packet.

    Args:
        scores: Predicted probabilities (one per scored unit; never written out).
        y_true: Observed 0/1 outcomes.
        site_id: Opaque consortium-assigned code (``[A-Za-z0-9_-]{1,32}``); do not use a facility name.
        min_cell: Counts in ``1..min_cell-1`` are suppressed (CMS-style default 11), with secondary suppression.
        tier_fractions: Top-k fractions of scores flagged for the screening tiers (NNS = 1/PPV).
        validation: Optional :class:`~omop_etl.feature_contract.FeatureValidationReport` whose clip / nullify counts are
            recorded.
        model: Optional ``{"name":..., "version":...}`` of the scoring bundle.
        output: Path to write the JSON packet to.

    If either outcome class has fewer than ``min_cell`` members every metric is withheld (``suppressed`` is true).
    """
    if not SITE_ID_RE.match(str(site_id)):
        raise ValueError("site_id must be an opaque code matching [A-Za-z0-9_-]{1,32}.")
    min_cell = int(min_cell)
    if min_cell < 2:
        raise ValueError("min_cell must be >= 2.")
    p = np.asarray(scores, dtype="float64").reshape(-1)
    y = np.asarray(y_true).reshape(-1)
    if p.shape != y.shape:
        raise ValueError("scores and y_true must have the same length.")
    keep = np.isfinite(p) & pd_notna(y)
    p, y = p[keep], y[keep].astype("int64")
    if not set(np.unique(y)) <= {0, 1}:
        raise ValueError("y_true must be binary 0/1.")
    if p.size and (p.min() < 0 or p.max() > 1):
        raise ValueError("scores must be probabilities in [0, 1].")
    n, ne = int(p.size), int(y.sum())
    packet: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "site_id": str(site_id), "min_cell": min_cell, "model": dict(model) if model else None,
        "n": n, "n_events": ne, "prevalence": None, "suppressed": False, "metrics": None, "deciles": [], "tiers": [],
        "feature_validation": validation.to_telemetry() if validation is not None else None,
    }
    if n - ne < min_cell or ne < min_cell:
        packet["suppressed"] = True
        packet["n_events"] = None if 0 < ne < min_cell or 0 < n - ne < min_cell else ne
        packet["n"] = n if n >= min_cell else None
    else:
        auc, auc_se = auroc_delong(y, p)
        cal = calibration_fit(y, p)
        packet["prevalence"] = ne / n
        packet["metrics"] = {
            "auroc": auc, "auroc_se": auc_se, "auprc": average_precision(y, p), "brier": float(np.mean((p - y) ** 2)),
            "cal_slope": cal["slope"], "cal_slope_se": cal["slope_se"], "cal_intercept": cal["intercept"],
            "cal_intercept_se": cal["intercept_se"], "cal_in_the_large": cal["citl"], "mean_prediction": float(p.mean()),
        }
        # deciles by rank (ties broken by order) -> equal-sized groups
        order = np.argsort(p, kind="mergesort")
        groups = np.array_split(order, 10)
        ev = np.array([y[g].sum() for g in groups])
        ne_ = np.array([len(g) - y[g].sum() for g in groups])
        hide_e, hide_n = _suppress(ev, min_cell), _suppress(ne_, min_cell)
        for i, g in enumerate(groups):
            hide = bool(hide_e[i] or hide_n[i])
            packet["deciles"].append({
                "decile": i + 1,
                "n": None if hide else int(len(g)),
                "events": None if hide else int(ev[i]),
                "mean_pred": None if hide else float(p[g].mean()),
                "observed_rate": None if hide else float(ev[i] / len(g)),
            })
        for frac in tier_fractions:
            k = max(1, int(round(frac * n)))
            top = np.argsort(-p, kind="mergesort")[:k]
            tp = int(y[top].sum())
            fp = k - tp
            hide = (0 < tp < min_cell) or (0 < fp < min_cell) or (0 < ne - tp < min_cell)
            packet["tiers"].append({
                "top_fraction": float(frac),
                "flagged": None if hide else k,
                "true_positives": None if hide else tp,
                "sensitivity": None if hide else tp / ne,
                "ppv": None if hide or tp == 0 else tp / k,
                "nns": None if hide or tp == 0 else k / tp,
            })
    validate_packet(packet)
    if output is not None:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(strict_dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    return packet


def pd_notna(a: np.ndarray) -> np.ndarray:
    import pandas as pd

    return np.asarray(pd.notna(a))


# ---- schema validation ----------------------------------------------------------------------------------

def load_packet_schema() -> dict[str, Any]:
    from omop_etl.build_omop_cdm import _resource_path

    return json.loads(Path(_resource_path("extdata", "telemetry_packet.schema.json")).read_text(encoding="utf-8"))


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _check(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if t is not None:
        allowed = [t] if isinstance(t, str) else list(t)

        def ok(tt: str) -> bool:
            if tt == "number":
                return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            if tt == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            return isinstance(value, _TYPES[tt])

        if not any(ok(tt) for tt in allowed):
            errors.append(f"{path or '$'}: expected {allowed}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path or '$'}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or '$'}: not in {schema['enum']}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path or '$'}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path or '$'}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path or '$'}: above maximum {schema['maximum']}")
    if isinstance(value, dict):
        for r in schema.get("required", []):
            if r not in value:
                errors.append(f"{path or '$'}: missing required '{r}'")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errors.append(f"{path or '$'}: unexpected property '{k}'")
        for k, v in value.items():
            if k in props:
                _check(v, props[k], f"{path}.{k}", errors)
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path or '$'}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, v in enumerate(value):
                _check(v, schema["items"], f"{path}[{i}]", errors)


def validate_packet(packet: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> None:
    """Raises ``ValueError`` listing every schema violation."""
    schema = schema or load_packet_schema()
    errors: list[str] = []
    try:
        import jsonschema

        errors = [f"{'.'.join(map(str, e.absolute_path)) or '$'}: {e.message}"
                  for e in jsonschema.Draft7Validator(schema).iter_errors(packet)]
    except ImportError:
        _check(packet, schema, "", errors)
    if errors:
        raise ValueError("Invalid telemetry packet:\n  " + "\n  ".join(errors))


# ---- aggregator -----------------------------------------------------------------------------------------

def _chi2_sf(x: float, df: int) -> float:
    """Upper tail of chi-square via the regularised incomplete gamma function."""
    if df <= 0 or x <= 0:
        return 1.0
    a, xx = df / 2.0, x / 2.0
    if xx < a + 1:  # series for P
        term = total = 1.0 / a
        ap = a
        for _ in range(500):
            ap += 1
            term *= xx / ap
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return float(max(0.0, 1.0 - total * math.exp(-xx + a * math.log(xx) - math.lgamma(a))))
    b = xx + 1 - a  # continued fraction for Q
    c, d = 1e300, 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return float(min(1.0, math.exp(-xx + a * math.log(xx) - math.lgamma(a)) * h))


def _random_effects(est: np.ndarray, se: np.ndarray) -> dict[str, Any]:
    k = len(est)
    w = 1 / se**2
    fixed = float(np.sum(w * est) / np.sum(w))
    q = float(np.sum(w * (est - fixed) ** 2))
    df = k - 1
    c = float(np.sum(w) - np.sum(w**2) / np.sum(w))
    tau2 = max(0.0, (q - df) / c) if df > 0 and c > 0 else 0.0
    ws = 1 / (se**2 + tau2)
    pooled = float(np.sum(ws * est) / np.sum(ws))
    pse = float(math.sqrt(1 / np.sum(ws)))
    i2 = max(0.0, (q - df) / q) * 100 if q > 0 and df > 0 else 0.0
    return {"k": k, "fixed_effect": fixed, "pooled": pooled, "se": pse, "ci95": [pooled - 1.96 * pse, pooled + 1.96 * pse],
            "cochran_q": q, "q_df": df, "q_p": _chi2_sf(q, df) if df > 0 else None, "i2_percent": i2, "tau2": tau2}


def aggregate_telemetry(directory: str | Path, *, pattern: str = "*.json") -> dict[str, Any]:
    """Validates every packet in ``directory`` and pools them.

    Returns a dict with ``sites`` (ids), ``n``/``n_events`` totals over unsuppressed sites, ``excluded`` (suppressed sites),
    ``auroc`` / ``cal_slope`` random-effects summaries (pooled estimate, SE, 95% CI, Cochran's Q, df, p, I^2, tau^2),
    n-weighted ``brier`` and ``prevalence``, and ``auprc_weighted`` (an n-weighted mean: AUPRC does not pool exactly).
    """
    d = Path(directory)
    packets = []
    for f in sorted(d.glob(pattern)):
        pk = json.loads(f.read_text(encoding="utf-8"))
        try:
            validate_packet(pk)
        except ValueError as exc:
            raise ValueError(f"{f.name}: {exc}") from None
        packets.append(pk)
    if not packets:
        raise ValueError(f"No telemetry packets found in {directory}.")
    ids = [p["site_id"] for p in packets]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate site_id among packets; each site must contribute one packet.")
    if len({p["schema_version"] for p in packets}) != 1:
        raise ValueError("Packets use different schema versions.")
    good = [p for p in packets if not p["suppressed"]]
    out: dict[str, Any] = {"schema_version": packets[0]["schema_version"], "sites": ids,
                           "excluded": [p["site_id"] for p in packets if p["suppressed"]],
                           "n": int(sum(p["n"] for p in good)), "n_events": int(sum(p["n_events"] for p in good))}
    if len(good) == 0:
        return {**out, "auroc": None, "cal_slope": None, "brier": None, "prevalence": None, "auprc_weighted": None}
    w = np.array([p["n"] for p in good], dtype="float64")

    def col(key):
        return np.array([p["metrics"][key] for p in good], dtype="float64")

    out["auroc"] = _random_effects(col("auroc"), np.maximum(col("auroc_se"), 1e-9)) if len(good) > 1 else {
        "k": 1, "pooled": float(col("auroc")[0]), "se": float(col("auroc_se")[0]), "i2_percent": 0.0, "tau2": 0.0}
    slopes = [p["metrics"]["cal_slope"] for p in good]
    if all(s is not None for s in slopes) and len(good) > 1:
        out["cal_slope"] = _random_effects(np.array(slopes), np.maximum(col("cal_slope_se"), 1e-9))
    else:
        out["cal_slope"] = None
    out["brier"] = float(np.sum(w * col("brier")) / w.sum())
    out["prevalence"] = float(sum(p["n_events"] for p in good) / w.sum())
    out["auprc_weighted"] = float(np.sum(w * col("auprc")) / w.sum())
    return out
