"""Leave-one-site-out transport evaluation, probability recalibration and capacity thresholds (v0.13.0).

A model trained on a balanced (for example 50/50) sample over N-1 sites is scored on the natural-prevalence data of the
held-out site, mapped back to the natural incidence rate, and judged by discrimination, calibration and what happens
when only the riskiest ``k`` percent are flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from omop_etl.ml import _fit_logistic_calibration
from omop_etl.model_ablation import ridge_logistic_fit_predict

__all__ = ["LosoTransportResult", "Recalibrator", "adjust_for_prevalence", "capacity_risk_tiers", "capacity_threshold",
           "evaluate_at_capacity", "evaluate_loso_transport", "fit_recalibrator", "isotonic_fit", "isotonic_apply",
           "platt_scaling_apply", "platt_scaling_fit"]

_EPS = 1e-7


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype="float64"), _EPS, 1 - _EPS)
    return np.log(p / (1 - p))


def _expit(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _binary(y: Any) -> np.ndarray:
    a = np.asarray(y, dtype="float64").reshape(-1)
    if a.size == 0 or np.isnan(a).any() or not np.isin(a, (0.0, 1.0)).all():
        raise ValueError("y must be a non-empty 0/1 vector without missing values.")
    return a


def _prob(p: Any) -> np.ndarray:
    a = np.asarray(p, dtype="float64").reshape(-1)
    if np.isnan(a).any() or (a < 0).any() or (a > 1).any():
        raise ValueError("probabilities must be in [0, 1] with no missing values.")
    return a


# ---------------------------------------------------------------------------------------------------------------------
# Recalibration
# ---------------------------------------------------------------------------------------------------------------------
def platt_scaling_fit(y: Any, p: Any) -> dict[str, float]:
    """Platt scaling ``logit(p') = intercept + slope * logit(p)`` by maximum likelihood (IRLS)."""
    yy, pp = _binary(y), _prob(p)
    if len(yy) != len(pp):
        raise ValueError("y and p must have the same length.")
    if yy.min() == yy.max():
        raise ValueError("Platt scaling needs both outcome classes.")
    a, b = _fit_logistic_calibration(yy, pp)
    return {"intercept": a, "slope": b}


def platt_scaling_apply(p: Any, params: Mapping[str, float]) -> np.ndarray:
    return _expit(params["intercept"] + params["slope"] * _logit(_prob(p)))


def isotonic_fit(y: Any, p: Any) -> dict[str, list[float]]:
    """Isotonic regression of ``y`` on ``p`` (pool-adjacent-violators). Returns the step function as ``x`` / ``y`` knots."""
    yy, pp = _binary(y), _prob(p)
    if len(yy) != len(pp):
        raise ValueError("y and p must have the same length.")
    order = np.argsort(pp, kind="stable")
    x, t = pp[order], yy[order]
    # merge tied x first so the fit is a function of p
    ux, inv = np.unique(x, return_inverse=True)
    w = np.bincount(inv).astype("float64")
    m = np.bincount(inv, weights=t) / w
    blocks: list[list[float]] = []   # [mean, weight, first_x, last_x]
    for xi, mi, wi in zip(ux, m, w):
        blocks.append([mi, wi, xi, xi])
        while len(blocks) > 1 and blocks[-2][0] >= blocks[-1][0]:
            b2, b1 = blocks.pop(), blocks.pop()
            wt = b1[1] + b2[1]
            blocks.append([(b1[0] * b1[1] + b2[0] * b2[1]) / wt, wt, b1[2], b2[3]])
    xs: list[float] = []
    ys: list[float] = []
    for mean, _, lo, hi in blocks:
        xs += [float(lo), float(hi)]
        ys += [float(mean), float(mean)]
    return {"x": xs, "y": ys}


def isotonic_apply(p: Any, params: Mapping[str, Sequence[float]]) -> np.ndarray:
    return np.interp(_prob(p), np.asarray(params["x"]), np.asarray(params["y"]))


def adjust_for_prevalence(p: Any, train_prevalence: float, target_prevalence: float) -> np.ndarray:
    """Prior-shift correction: map probabilities of a model trained at ``train_prevalence`` (for example 0.5 after
    balancing) to a population with ``target_prevalence`` by adding the log odds ratio of the two base rates."""
    for name, v in (("train_prevalence", train_prevalence), ("target_prevalence", target_prevalence)):
        if not 0 < v < 1:
            raise ValueError(f"{name} must be in (0, 1).")
    shift = np.log(target_prevalence / (1 - target_prevalence)) - np.log(train_prevalence / (1 - train_prevalence))
    return _expit(_logit(_prob(p)) + shift)


@dataclass
class Recalibrator:
    """A fitted recalibration map; ``predict(p)`` returns recalibrated probabilities."""
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def predict(self, p: Any) -> np.ndarray:
        if self.method == "none":
            return _prob(p)
        if self.method == "platt":
            return platt_scaling_apply(p, self.params)
        if self.method == "isotonic":
            return isotonic_apply(p, self.params)
        if self.method == "prior_shift":
            return adjust_for_prevalence(p, self.params["train_prevalence"], self.params["target_prevalence"])
        raise ValueError(self.method)


def fit_recalibrator(method: str, *, y: Any = None, p: Any = None, train_prevalence: float | None = None,
                     target_prevalence: float | None = None) -> Recalibrator:
    """``"platt"`` / ``"isotonic"`` need ``y`` and ``p`` from natural-prevalence data; ``"prior_shift"`` needs the two
    prevalences (no outcome data at the target); ``"none"`` leaves probabilities unchanged."""
    method = method.lower()
    if method == "none":
        return Recalibrator("none")
    if method == "platt":
        return Recalibrator("platt", platt_scaling_fit(y, p))
    if method == "isotonic":
        return Recalibrator("isotonic", isotonic_fit(y, p))
    if method == "prior_shift":
        if train_prevalence is None or target_prevalence is None:
            raise ValueError("prior_shift needs train_prevalence and target_prevalence.")
        return Recalibrator("prior_shift", {"train_prevalence": float(train_prevalence), "target_prevalence": float(target_prevalence)})
    raise ValueError("calibration must be 'none', 'prior_shift', 'platt' or 'isotonic'.")


# ---------------------------------------------------------------------------------------------------------------------
# Capacity-driven thresholds
# ---------------------------------------------------------------------------------------------------------------------
def capacity_threshold(prob: Any, top_fraction: float) -> float:
    """Probability cut-off that flags the riskiest ``top_fraction`` (for example 0.10) of ``prob``: ``p >= cutoff``.
    Tied probabilities cannot be split, so slightly more than the requested share may be flagged."""
    if not 0 < top_fraction < 1:
        raise ValueError("top_fraction must be in (0, 1).")
    p = _prob(prob)
    if p.size == 0:
        raise ValueError("prob is empty.")
    return float(np.quantile(p, 1.0 - top_fraction))


def capacity_risk_tiers(reference_prob: Any, top_fraction: float = 0.10, *, high_name: str = "high_risk",
                        low_name: str = "standard") -> list[dict[str, Any]]:
    """Two risk tiers whose cut-point is set by capacity (top ``top_fraction`` of the reference scores), in the format
    of :func:`fit_capacity_thresholds` / :func:`map_risk_tiers`."""
    from omop_etl.risk_tiers import fit_capacity_thresholds
    return fit_capacity_thresholds(reference_prob, [{"name": high_name, "top_fraction": float(top_fraction)}, {"name": low_name}])


def evaluate_at_capacity(y: Any, prob: Any, top_fraction: float = 0.10, *, cutoff: float | None = None) -> dict[str, float]:
    """What flagging the riskiest ``top_fraction`` achieves: ``cutoff``, ``n_flagged``, ``share_flagged``, ``ppv``,
    ``sensitivity``, ``lift`` (PPV / prevalence). A frozen ``cutoff`` (from another cohort) overrides the quantile."""
    yy, pp = _binary(y), _prob(prob)
    cut = capacity_threshold(pp, top_fraction) if cutoff is None else float(cutoff)
    flag = pp >= cut
    tp = float((yy[flag] == 1).sum())
    prev = float(yy.mean())
    ppv = tp / flag.sum() if flag.any() else float("nan")
    return {"cutoff": cut, "n_flagged": int(flag.sum()), "share_flagged": float(flag.mean()), "ppv": ppv,
            "sensitivity": tp / yy.sum() if yy.sum() else float("nan"), "lift": ppv / prev if prev > 0 else float("nan")}


# ---------------------------------------------------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------------------------------------------------
def _auroc(y: np.ndarray, s: np.ndarray) -> float:
    n1 = y.sum()
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = pd.Series(s).rank(method="average").to_numpy()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _auprc(y: np.ndarray, s: np.ndarray) -> float:
    """Average precision (step interpolation)."""
    if y.sum() == 0:
        return float("nan")
    order = np.argsort(-s, kind="stable")
    ys, ss = y[order], s[order]
    tp = np.cumsum(ys)
    last = np.r_[ss[1:] != ss[:-1], True]       # one point per distinct threshold
    tp_t, n_t = tp[last], (np.arange(len(ys)) + 1)[last]
    prec, rec = tp_t / n_t, tp_t / y.sum()
    return float(np.sum(np.diff(np.r_[0.0, rec]) * prec))


def _metrics(y: np.ndarray, p: np.ndarray, fractions: Sequence[float]) -> dict[str, float]:
    out = {"auroc": _auroc(y, p), "auprc": _auprc(y, p), "brier": float(np.mean((p - y) ** 2)),
           "oe_ratio": float(y.sum() / p.sum()) if p.sum() > 0 else float("nan")}
    if 0 < y.sum() < len(y):
        a, b = _fit_logistic_calibration(y, p)
        out["calibration_intercept"], out["calibration_slope"] = a, b
    else:
        out["calibration_intercept"] = out["calibration_slope"] = float("nan")
    for f in fractions:
        r = evaluate_at_capacity(y, p, f)
        tag = f"top{int(round(f * 100))}"
        out[f"{tag}_ppv"], out[f"{tag}_sensitivity"], out[f"{tag}_lift"] = r["ppv"], r["sensitivity"], r["lift"]
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------------------------------------------------
def _make_fit_predict(model_type: str, random_state: int, model_params: Mapping[str, Any] | None) -> Callable:
    mt = model_type.lower()
    if mt in ("logistic", "ridge_logistic"):
        l2 = float((model_params or {}).get("l2", 1.0))
        return lambda xtr, ytr, xte: ridge_logistic_fit_predict(xtr, ytr, xte, l2=l2)
    if mt == "xgboost":
        try:
            import xgboost as xgb
        except ImportError as exc:
            raise ImportError("model_type='xgboost' needs xgboost (pip install xgboost); 'logistic' needs nothing extra.") from exc
        user = dict(model_params or {})
        rounds = int(user.pop("n_estimators", 200))
        params = {"objective": "binary:logistic", "eval_metric": "logloss", "max_depth": 4, "eta": float(user.pop("learning_rate", 0.1)),
                  "subsample": 0.8, "colsample_bytree": 0.8, "seed": random_state, "nthread": 1, **user}

        def fit_predict(xtr, ytr, xte):
            booster = xgb.train(params, xgb.DMatrix(np.asarray(xtr, dtype="float32"), label=np.asarray(ytr, dtype="float32")),
                                num_boost_round=rounds)
            return booster.predict(xgb.DMatrix(np.asarray(xte, dtype="float32")))
        return fit_predict
    raise ValueError("model_type must be 'logistic' or 'xgboost'.")


def _balance(y: np.ndarray, ratio: float | None, rng: np.random.Generator) -> np.ndarray:
    """Row indices of a training sample with ``ratio`` majority rows per minority row (1.0 = 50/50); all rows when None."""
    if ratio is None:
        return np.arange(len(y))
    if ratio <= 0:
        raise ValueError("balance_ratio must be positive (or None to train on the natural sample).")
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    minority, majority = (pos, neg) if len(pos) <= len(neg) else (neg, pos)
    take = min(len(majority), int(round(ratio * len(minority))))
    chosen = rng.choice(majority, size=take, replace=False)
    return np.sort(np.concatenate([minority, chosen]))


@dataclass
class LosoTransportResult:
    """``per_site`` (one row per held-out site, with ``*_lo`` / ``*_hi`` 95% bootstrap limits), ``summary`` (mean and
    range across sites), optional row-level ``predictions`` and the ``settings`` used."""
    per_site: pd.DataFrame
    summary: pd.DataFrame
    predictions: pd.DataFrame | None
    settings: dict[str, Any]


def evaluate_loso_transport(
    features_df: pd.DataFrame,
    site_col: str = "site_anon",
    *,
    model_type: str = "logistic",
    feature_cols: Sequence[str] | None = None,
    label_col: str = "y",
    balance_ratio: float | None = 1.0,
    score_natural: bool = True,
    calibration: str = "prior_shift",
    n_bootstraps: int = 100,
    capacity_fractions: Sequence[float] = (0.10,),
    random_state: int = 20261104,
    model_params: Mapping[str, Any] | None = None,
    keep_predictions: bool = False,
    min_events: int = 1,
) -> LosoTransportResult:
    """Train on every site but one, score the held-out site, repeat.

    Parameters
    ----------
    features_df : DataFrame
        One row per stay with ``site_col``, ``label_col`` (0/1) and the features (default: every numeric column that is not
        the label, the site or an identifier / date column).
    balance_ratio : float or None, default 1.0
        Majority rows per minority row in the training sample (1.0 = 50/50). ``None`` trains on the natural sample.
    score_natural : bool, default True
        Score the held-out site at its natural prevalence (every row). ``False`` scores a balanced subsample of the held-out
        site (diagnostic only: calibration is then not meaningful).
    calibration : {"prior_shift", "platt", "isotonic", "none"}
        Map from the balanced model's probabilities to the natural incidence. ``prior_shift`` shifts the log odds by
        the ratio of the training sites' natural prevalence to the balanced training prevalence and uses no outcome
        data from the held-out site. ``platt`` / ``isotonic`` are fitted on the training sites' natural data scored by the
        model (in-sample for the training rows, so mildly optimistic) and applied to the held-out site.
    n_bootstraps : int, default 100
        Bootstrap resamples of the held-out rows for 95% percentile intervals (0 disables).
    capacity_fractions : sequence of float
        Top-k shares flagged for PPV / sensitivity / lift. The cut-off is the held-out site's own quantile of the
        calibrated scores (a capacity rule: "the riskiest 10%").
    model_type : {"logistic", "xgboost"}
        ``logistic`` is the ridge IRLS fit of :func:`ridge_logistic_fit_predict`; ``xgboost`` needs the package.
    """
    if site_col not in features_df or label_col not in features_df:
        raise ValueError(f"features_df needs the columns {site_col!r} and {label_col!r}.")
    df = features_df.reset_index(drop=True)
    skip = {site_col, label_col, "person_id", "visit_occurrence_id", "index_date", "discharge_date", "site", "site_anon"}
    cols = list(feature_cols) if feature_cols is not None else [c for c in df.columns
                                                               if c not in skip and pd.api.types.is_numeric_dtype(df[c])]
    if not cols:
        raise ValueError("No feature columns.")
    y_all = _binary(df[label_col])
    sites = [s for s in pd.unique(df[site_col].dropna())]
    if len(sites) < 2:
        raise ValueError("Leave-one-site-out needs at least two sites.")
    X = df[cols].to_numpy("float64")
    fit_predict = _make_fit_predict(model_type, random_state, model_params)
    rows, preds = [], []
    for k, site in enumerate(sorted(sites, key=str)):
        test = (df[site_col] == site).to_numpy()
        train = ~test & df[site_col].notna().to_numpy()
        ytr_nat, yte = y_all[train], y_all[test]
        rng = np.random.default_rng([random_state, k])
        if ytr_nat.min() == ytr_nat.max():
            raise ValueError(f"Training sites for held-out site {site!r} contain a single outcome class.")
        idx = _balance(ytr_nat, balance_ratio, rng)
        xtr_nat = X[train]
        xtr, ytr = xtr_nat[idx], ytr_nat[idx]
        xte = X[test]
        if not score_natural:
            sub = _balance(yte, 1.0 if balance_ratio is None else balance_ratio, rng)
            xte, yte, kept = xte[sub], yte[sub], np.flatnonzero(test)[sub]
        else:
            kept = np.flatnonzero(test)
        both = fit_predict(xtr, ytr, np.vstack([xtr_nat, xte]))
        p_train_nat, p_raw = both[: len(xtr_nat)], both[len(xtr_nat):]
        nat_prev, bal_prev = float(ytr_nat.mean()), float(ytr.mean())
        if calibration == "prior_shift":
            recal = fit_recalibrator("prior_shift", train_prevalence=bal_prev, target_prevalence=nat_prev) if abs(nat_prev - bal_prev) > 1e-12 \
                else fit_recalibrator("none")
        elif calibration in ("platt", "isotonic"):
            recal = fit_recalibrator(calibration, y=ytr_nat, p=p_train_nat)
        elif calibration == "none":
            recal = fit_recalibrator("none")
        else:
            raise ValueError("calibration must be 'prior_shift', 'platt', 'isotonic' or 'none'.")
        p = recal.predict(p_raw)
        row = {"site": site, "n": int(len(yte)), "n_events": int(yte.sum()), "prevalence": float(yte.mean()),
               "train_prevalence_natural": nat_prev, "train_prevalence_balanced": bal_prev, **_metrics(yte, p, capacity_fractions)}
        if len(yte) and yte.sum() >= min_events and n_bootstraps and n_bootstraps > 0:
            brng = np.random.default_rng([random_state, 10_000 + k])
            boot = []
            for _ in range(int(n_bootstraps)):
                bi = brng.integers(0, len(yte), len(yte))
                boot.append(_metrics(yte[bi], p[bi], capacity_fractions))
            bdf = pd.DataFrame(boot)
            for c in bdf.columns:
                row[f"{c}_lo"], row[f"{c}_hi"] = (float(np.nanpercentile(bdf[c], 2.5)), float(np.nanpercentile(bdf[c], 97.5)))
        rows.append(row)
        if keep_predictions:
            preds.append(pd.DataFrame({"row": kept, "site": site, "y": yte, "p_raw": p_raw, "p": p}))
    per_site = pd.DataFrame(rows)
    metric_cols = [c for c in per_site.columns if c not in ("site", "n", "n_events") and not c.endswith(("_lo", "_hi"))
                   and not c.startswith("train_")]
    summary = per_site[metric_cols].agg(["mean", "min", "max"]).T.reset_index().rename(columns={"index": "metric"})
    settings = {"model_type": model_type, "balance_ratio": balance_ratio, "score_natural": score_natural, "calibration": calibration,
                "n_bootstraps": n_bootstraps, "capacity_fractions": list(capacity_fractions), "features": cols, "random_state": random_state}
    return LosoTransportResult(per_site, summary, pd.concat(preds, ignore_index=True) if preds else None, settings)
