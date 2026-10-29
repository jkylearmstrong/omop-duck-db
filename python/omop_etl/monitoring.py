"""Post-deployment drift and calibration monitoring.

* :func:`population_stability_index` - PSI of the predicted-risk distribution against a frozen baseline (bins are the
  baseline's quantiles, so the baseline is ~uniform across bins by construction). Rule of thumb: < 0.10 stable,
  0.10-0.25 watch, >= 0.25 shifted.
* :func:`calibration_summary` - observed vs predicted for a set of scored encounters with known outcomes: O/E ratio,
  Brier, expected calibration error, calibration slope/intercept, AUROC.
* :func:`monitor_scores` - one row per calendar month of scoring: PSI always, calibration only once the month's
  30-day outcomes have matured (month end + ``followup_days`` <= ``as_of``), plus a ``status`` flag.
* :func:`update_monitoring_log` - upserts those rows into a CSV so the monthly job is idempotent and re-runnable.

Output is aggregate-only (counts, rates, statistics); no encounter-level value is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from omop_etl.telemetry import auroc_delong, calibration_fit

__all__ = ["population_stability_index", "calibration_summary", "monitor_scores", "update_monitoring_log"]

PSI_WATCH, PSI_SHIFTED = 0.10, 0.25
_EPS = 1e-4


def _probs(x: Any, what: str) -> np.ndarray:
    a = np.asarray(x, dtype="float64").reshape(-1)
    if np.isnan(a).any():
        raise ValueError(f"{what} contains NaN.")
    return a


def population_stability_index(baseline: Any, current: Any, bins: int = 10) -> float:
    """PSI = sum((cur_i - base_i) * ln(cur_i / base_i)) over ``bins`` baseline-quantile bins (outer bins open-ended).

    Empty bins are floored at a small proportion so the index stays finite."""
    b, c = _probs(baseline, "baseline"), _probs(current, "current")
    if b.size == 0 or c.size == 0:
        raise ValueError("baseline and current must be non-empty.")
    edges = np.unique(np.quantile(b, np.linspace(0, 1, int(bins) + 1)[1:-1]))
    pb = np.bincount(np.searchsorted(edges, b, side="right"), minlength=edges.size + 1) / b.size
    pc = np.bincount(np.searchsorted(edges, c, side="right"), minlength=edges.size + 1) / c.size
    pb, pc = np.clip(pb, _EPS, None), np.clip(pc, _EPS, None)
    return float(np.sum((pc - pb) * np.log(pc / pb)))


def calibration_summary(y: Any, p: Any, bins: int = 10, min_events: int = 30) -> dict[str, Any]:
    """Observed-vs-predicted statistics. Slope/intercept/AUROC are ``None`` when there are fewer than ``min_events``
    events or non-events (too unstable to act on)."""
    y, p = _probs(y, "y"), _probs(p, "p")
    if y.shape != p.shape:
        raise ValueError("y and p must have the same length.")
    n = int(y.size)
    out: dict[str, Any] = {"n": n, "events": int(y.sum()), "observed": None, "mean_predicted": None, "oe_ratio": None,
                           "brier": None, "ece": None, "slope": None, "intercept": None, "auroc": None}
    if n == 0:
        return out
    out["observed"], out["mean_predicted"] = float(y.mean()), float(p.mean())
    out["oe_ratio"] = float(y.mean() / p.mean()) if p.mean() > 0 else None
    out["brier"] = float(np.mean((p - y) ** 2))
    edges = np.unique(np.quantile(p, np.linspace(0, 1, int(bins) + 1)[1:-1]))
    idx = np.searchsorted(edges, p, side="right")
    out["ece"] = float(sum(abs(y[idx == k].mean() - p[idx == k].mean()) * (idx == k).sum() / n for k in np.unique(idx)))
    if out["events"] >= min_events and n - out["events"] >= min_events:
        fit = calibration_fit(y, p)
        out["slope"], out["intercept"] = fit["slope"], fit["intercept"]
        out["auroc"] = float(auroc_delong(y, p)[0])
    return out


def monitor_scores(scores: pd.DataFrame, baseline_prob: Any, *, date_col: str = "score_date", prob_col: str = "prob",
                   outcome_col: str | None = "outcome_flag", as_of: Any = None, followup_days: int = 30,
                   min_events: int = 30, psi_bins: int = 10, oe_range: tuple[float, float] = (0.8, 1.25),
                   slope_range: tuple[float, float] = (0.8, 1.2)) -> pd.DataFrame:
    """Monthly monitoring table from live scores (``date_col`` + ``prob_col``, optional ``outcome_col``).

    ``baseline_prob`` is the frozen reference distribution (validation-period scores). Columns: ``month``, ``n``,
    ``mean_prob``, ``psi``, ``outcomes_matured`` (outcomes complete?), the :func:`calibration_summary` fields, and
    ``status``: ``ok`` | ``watch`` (PSI 0.10-0.25) | ``drift`` (PSI >= 0.25) | ``miscalibrated`` (O/E or slope outside
    its range; needs >= ``min_events`` events) | ``insufficient_outcomes`` (calibration not yet assessable).
    ``miscalibrated`` takes precedence over drift flags. Rows with a missing outcome are excluded from calibration.
    """
    for col in (date_col, prob_col):
        if col not in scores.columns:
            raise KeyError(f"scores needs a {col!r} column.")
    df = scores.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df["_month"] = df[date_col].dt.to_period("M")
    now = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today().normalize()
    has_outcome = outcome_col is not None and outcome_col in df.columns
    rows = []
    for month, g in df.groupby("_month", sort=True):
        p = g[prob_col].to_numpy(dtype="float64")
        matured = bool(has_outcome and month.end_time.normalize() + pd.Timedelta(days=int(followup_days)) <= now)
        row: dict[str, Any] = {"month": str(month), "n": int(len(g)), "mean_prob": float(p.mean()),
                               "psi": population_stability_index(baseline_prob, p, psi_bins), "outcomes_matured": matured}
        known = g[outcome_col].notna().to_numpy() if has_outcome else np.zeros(len(g), dtype=bool)
        cal = calibration_summary(g.loc[known, outcome_col].to_numpy(dtype="float64"), p[known], min_events=min_events) \
            if matured and known.any() else calibration_summary([], [])
        row.update({k: v for k, v in cal.items() if k != "n"})
        row["n_with_outcome"] = cal["n"]
        row["status"] = _status(row, matured, min_events, oe_range, slope_range)
        rows.append(row)
    cols = ["month", "n", "mean_prob", "psi", "outcomes_matured", "n_with_outcome", "events", "observed",
            "mean_predicted", "oe_ratio", "brier", "ece", "slope", "intercept", "auroc", "status"]
    return pd.DataFrame(rows, columns=cols)


def _status(row: dict[str, Any], matured: bool, min_events: int, oe: tuple[float, float], slope: tuple[float, float]) -> str:
    if matured and row["events"] is not None and row["events"] >= min_events:
        bad_oe = row["oe_ratio"] is not None and not oe[0] <= row["oe_ratio"] <= oe[1]
        bad_slope = row["slope"] is not None and not slope[0] <= row["slope"] <= slope[1]
        if bad_oe or bad_slope:
            return "miscalibrated"
    if row["psi"] >= PSI_SHIFTED:
        return "drift"
    if row["psi"] >= PSI_WATCH:
        return "watch"
    return "ok" if matured and row["events"] is not None and row["events"] >= min_events else "insufficient_outcomes"


def update_monitoring_log(report: pd.DataFrame, path: str | Path, *, model_version: str = "") -> pd.DataFrame:
    """Upserts ``report`` into the CSV at ``path`` keyed on (``model_version``, ``month``); a later run replaces a month
    (e.g. once its outcomes mature). Returns the full log, sorted."""
    path = Path(path)
    new = report.copy()
    new.insert(0, "model_version", str(model_version))
    if path.is_file():
        old = pd.read_csv(path, dtype={"model_version": str}, keep_default_na=False, na_values=[""])
        old["model_version"] = old["model_version"].fillna("").astype(str)
        old = old[~old.set_index(["model_version", "month"]).index.isin(new.set_index(["model_version", "month"]).index)]
        new = pd.concat([old, new], ignore_index=True) if len(old) else new
    new = new.sort_values(["model_version", "month"]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(path, index=False)
    return new
