"""Compare feature sets and model runs without sharing patient rows.

Two questions come up in every study that adds or changes features: does a feature group help (does the AUROC drop when it is
removed, by more than noise?), and are two model runs really different (0.706 against 0.710 on the same patients)?

* :func:`ablate_feature_groups` refits a model with each named group of columns removed (or kept alone) in cross-validation and
  reports the change in AUROC, AUPRC and Brier score with a paired bootstrap interval. A group is any list of columns: RUCA/SDI, a
  site's Charlson index beside the standard one, velocity features.
* :func:`compare_model_runs` compares two sets of predictions for the same patients: the metrics of each, the paired differences with
  bootstrap intervals, and how much the two rank patients alike.

Both return aggregate tables only (metrics, counts, intervals), so a site can send the result back. The model is whatever
``fit_predict(X_train, y_train, X_test) -> probabilities`` you give it (scikit-learn, xgboost, a PLP model); the default is a small
ridge-regularised logistic regression that needs nothing beyond numpy.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["ablate_feature_groups", "compare_model_runs", "ridge_logistic_fit_predict", "METRICS"]

METRICS = ("auroc", "auprc", "brier")


def _auroc(y: np.ndarray, p: np.ndarray) -> float:
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = pd.Series(p).rank(method="average").to_numpy()
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _auprc(y: np.ndarray, p: np.ndarray) -> float:
    """Average precision: the mean precision at each positive when patients are ranked by score (ties broken by order)."""
    n1 = int(y.sum())
    if n1 == 0 or n1 == len(y):
        return float("nan")
    order = np.argsort(-p, kind="stable")
    ys = y[order]
    precision = np.cumsum(ys) / np.arange(1, len(ys) + 1)
    return float(precision[ys == 1].sum() / n1)


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


_METRIC_FN = {"auroc": _auroc, "auprc": _auprc, "brier": _brier}


def ridge_logistic_fit_predict(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray, l2: float = 1.0, max_iter: int = 50) -> np.ndarray:
    """Ridge-regularised logistic regression (Newton / IRLS) on standardised columns; missing values take the training mean. The
    intercept is not penalised. Returns the predicted probabilities for ``x_test``."""
    xtr = np.asarray(x_train, dtype=float)
    xte = np.asarray(x_test, dtype=float)
    mean = np.nanmean(xtr, axis=0)
    mean = np.where(np.isnan(mean), 0.0, mean)
    xtr = np.where(np.isnan(xtr), mean, xtr)
    xte = np.where(np.isnan(xte), mean, xte)
    sd = xtr.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    xtr = (xtr - mean) / sd
    xte = (xte - mean) / sd
    a = np.hstack([np.ones((len(xtr), 1)), xtr])
    b = np.zeros(a.shape[1])
    pen = np.eye(a.shape[1]) * l2
    pen[0, 0] = 0.0
    y = np.asarray(y_train, dtype=float)
    for _ in range(max_iter):
        z = np.clip(a @ b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        w = np.clip(p * (1 - p), 1e-6, None)
        grad = a.T @ (p - y) + pen @ b
        hess = a.T @ (a * w[:, None]) + pen
        step = np.linalg.solve(hess, grad)
        b = b - step
        if np.max(np.abs(step)) < 1e-8:
            break
    z = np.clip(np.hstack([np.ones((len(xte), 1)), xte]) @ b, -30, 30)
    return 1.0 / (1.0 + np.exp(-z))


def _folds(y: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    fold = np.empty(len(y), dtype=int)
    for cls in (0, 1):
        idx = np.flatnonzero(y == cls)
        rng.shuffle(idx)
        fold[idx] = np.arange(len(idx)) % k
    return fold


def _oof(x: np.ndarray, y: np.ndarray, fold: np.ndarray, fit_predict: Callable) -> np.ndarray:
    out = np.empty(len(y), dtype=float)
    for f in np.unique(fold):
        test = fold == f
        out[test] = np.asarray(fit_predict(x[~test], y[~test], x[test]), dtype=float).ravel()
    return out


def _paired_bootstrap(y: np.ndarray, p_a: np.ndarray, p_b: np.ndarray, metrics: Sequence[str], n_boot: int,
                      rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Differences ``b - a`` of each metric over ``n_boot`` resamples of the patients (the same resample for both)."""
    out = {m: [] for m in metrics}
    n = len(y)
    for _ in range(n_boot):
        i = rng.integers(0, n, n)
        yy = y[i]
        if yy.min() == yy.max():
            continue
        for m in metrics:
            out[m].append(_METRIC_FN[m](yy, p_b[i]) - _METRIC_FN[m](yy, p_a[i]))
    return {m: np.asarray(v, dtype=float) for m, v in out.items()}


def _interval(draws: np.ndarray, level: float) -> tuple[float, float, float]:
    if len(draws) == 0:
        return (float("nan"),) * 3
    lo, hi = np.percentile(draws, [100 * (1 - level) / 2, 100 * (1 + level) / 2])
    # two-sided share of resamples on the other side of zero
    p = 2 * min(float((draws <= 0).mean()), float((draws >= 0).mean()))
    return float(lo), float(hi), min(p, 1.0)


def ablate_feature_groups(
    features: pd.DataFrame,
    outcome: Any,
    groups: Mapping[str, Sequence[str]],
    *,
    fit_predict: Callable[[np.ndarray, np.ndarray, np.ndarray], Any] | None = None,
    mode: str = "drop",
    folds: int = 5,
    n_boot: int = 200,
    ci_level: float = 0.95,
    seed: int = 0,
) -> pd.DataFrame:
    """How much does each group of features contribute?

    ``features`` holds one numeric column per feature, ``outcome`` is 0/1 per row, and ``groups`` maps a name to the columns of that
    group (groups may overlap; columns outside every group stay in all models). The full model is refit with each group removed
    (``mode='drop'``), kept alone together with the ungrouped columns (``mode='only'``), or both (``mode='both'``), using the same
    stratified cross-validation folds, so every difference is paired. Differences are ``without - full`` for ``'drop'`` (negative = the
    group helps for AUROC/AUPRC, positive for Brier) and ``only - full`` for ``'only'``; the interval is a paired bootstrap over
    patients of the out-of-fold predictions and ``p_boot`` is the two-sided share of resamples on the other side of zero.

    Returns one row per ``(mode, group)`` plus the full model (``mode='full'``): the number of features, the number of rows and
    events, and for each metric in :data:`METRICS` the full value, the variant's value, ``delta``, ``delta_lo``, ``delta_hi`` and
    ``p_boot``. No patient-level data is returned."""
    if mode not in ("drop", "only", "both"):
        raise ValueError("mode must be 'drop', 'only' or 'both'")
    y = np.asarray(outcome, dtype=float).ravel()
    if len(y) != len(features):
        raise ValueError("outcome and features must have the same number of rows")
    if not set(np.unique(y[~np.isnan(y)])) <= {0.0, 1.0} or np.isnan(y).any():
        raise ValueError("outcome must be 0/1 with no missing values")
    y = y.astype(int)
    if y.sum() < folds or (len(y) - y.sum()) < folds:
        raise ValueError(f"need at least {folds} events and {folds} non-events for {folds}-fold cross-validation")
    cols = list(features.columns)
    for name, members in groups.items():
        missing = [c for c in members if c not in cols]
        if missing:
            raise ValueError(f"group {name!r} names columns that are not in features: {missing}")
        if not members:
            raise ValueError(f"group {name!r} is empty")
    fit = fit_predict or ridge_logistic_fit_predict
    rng = np.random.default_rng(seed)
    fold = _folds(y, folds, rng)
    grouped = {c for m in groups.values() for c in m}
    base = [c for c in cols if c not in grouped]

    def run(use: Sequence[str]) -> np.ndarray:
        if not len(use):
            return np.full(len(y), y.mean())
        return _oof(features[list(use)].to_numpy(dtype=float), y, fold, fit)

    full_pred = run(cols)
    variants: list[tuple[str, str, list[str]]] = []
    for name, members in groups.items():
        if mode in ("drop", "both"):
            variants.append(("drop", name, [c for c in cols if c not in set(members)]))
        if mode in ("only", "both"):
            variants.append(("only", name, base + [c for c in members if c not in base]))
    rows: list[dict[str, Any]] = [{"mode": "full", "group": "(all features)", "n_features": len(cols), "n": len(y), "events": int(y.sum()),
                                   **{f"{m}_full": _METRIC_FN[m](y, full_pred) for m in METRICS}}]
    for kind, name, use in variants:
        pred = run(use)
        draws = _paired_bootstrap(y, full_pred, pred, METRICS, n_boot, rng)
        row: dict[str, Any] = {"mode": kind, "group": name, "n_features": len(use), "n": len(y), "events": int(y.sum())}
        for m in METRICS:
            lo, hi, p = _interval(draws[m], ci_level)
            full_v, var_v = _METRIC_FN[m](y, full_pred), _METRIC_FN[m](y, pred)
            row.update({f"{m}_full": full_v, f"{m}_variant": var_v, f"{m}_delta": var_v - full_v, f"{m}_delta_lo": lo,
                        f"{m}_delta_hi": hi, f"{m}_p_boot": p})
        rows.append(row)
    return pd.DataFrame(rows)


def compare_model_runs(
    run_a: pd.DataFrame,
    run_b: pd.DataFrame,
    *,
    id_col: str = "person_id",
    pred_col: str = "pred",
    outcome_col: str = "outcome",
    names: tuple[str, str] = ("A", "B"),
    n_boot: int = 500,
    ci_level: float = 0.95,
    top_fraction: float = 0.1,
    seed: int = 0,
) -> dict[str, Any]:
    """Are two sets of predictions for the same patients really different?

    Each run is a table with a patient id, a predicted probability and (in at least one of them) the 0/1 outcome. Patients are matched
    on ``id_col``; those in only one run are counted and left out. Returns ``{"n", "metrics", "difference", "agreement"}``:

    * ``n``: patients in both runs, in only one, and events;
    * ``metrics``: AUROC, AUPRC, Brier score and mean predicted against observed risk for each run;
    * ``difference``: ``B - A`` for each metric with a paired bootstrap interval (the same resampled patients for both runs) and
      ``p_boot``, the two-sided share of resamples on the other side of zero. A 0.004 AUROC gap whose interval spans zero is not a
      difference;
    * ``agreement``: Spearman correlation of the two risk scores, the mean absolute difference in predicted risk, and the share of the
      top ``top_fraction`` of patients that both runs flag.

    No patient-level rows are returned."""
    for label, df in (("run_a", run_a), ("run_b", run_b)):
        for c in (id_col, pred_col):
            if c not in df.columns:
                raise ValueError(f"{label} has no column {c!r}")
        if df[id_col].duplicated().any():
            raise ValueError(f"{label} has duplicate values in {id_col!r}; one row per patient")
    a = run_a[[id_col, pred_col] + ([outcome_col] if outcome_col in run_a.columns else [])].rename(columns={pred_col: "pa", outcome_col: "ya"})
    b = run_b[[id_col, pred_col] + ([outcome_col] if outcome_col in run_b.columns else [])].rename(columns={pred_col: "pb", outcome_col: "yb"})
    both = a.merge(b, on=id_col, how="inner")
    if "ya" not in both.columns and "yb" not in both.columns:
        raise ValueError(f"neither run has an outcome column {outcome_col!r}")
    if "ya" in both.columns and "yb" in both.columns:
        if not (both["ya"] == both["yb"]).all():
            raise ValueError("the two runs disagree on the outcome of some patients")
    y = both["ya" if "ya" in both.columns else "yb"].to_numpy(dtype=int)
    pa, pb = both["pa"].to_numpy(dtype=float), both["pb"].to_numpy(dtype=float)
    if len(y) == 0:
        raise ValueError("the two runs have no patient in common")
    rng = np.random.default_rng(seed)
    metrics = pd.DataFrame({"run": list(names), **{m: [_METRIC_FN[m](y, pa), _METRIC_FN[m](y, pb)] for m in METRICS},
                            "mean_predicted": [pa.mean(), pb.mean()], "observed": [y.mean(), y.mean()]})
    draws = _paired_bootstrap(y, pa, pb, METRICS, n_boot, rng)
    diff_rows = []
    for m in METRICS:
        lo, hi, p = _interval(draws[m], ci_level)
        diff_rows.append({"metric": m, names[0]: _METRIC_FN[m](y, pa), names[1]: _METRIC_FN[m](y, pb),
                          "delta": _METRIC_FN[m](y, pb) - _METRIC_FN[m](y, pa), "delta_lo": lo, "delta_hi": hi, "p_boot": p})
    ra, rb = pd.Series(pa).rank().to_numpy(), pd.Series(pb).rank().to_numpy()
    k = max(1, int(round(top_fraction * len(y))))
    top_a, top_b = set(np.argsort(-pa, kind="stable")[:k]), set(np.argsort(-pb, kind="stable")[:k])
    agreement = {"spearman": float(np.corrcoef(ra, rb)[0, 1]) if len(y) > 1 and ra.std() > 0 and rb.std() > 0 else float("nan"),
                 "mean_abs_diff": float(np.mean(np.abs(pa - pb))), "top_fraction": top_fraction,
                 "top_overlap": len(top_a & top_b) / k}
    n = {"common": int(len(both)), f"only_{names[0]}": int(len(a) - len(both)), f"only_{names[1]}": int(len(b) - len(both)),
         "events": int(y.sum())}
    return {"n": n, "metrics": metrics, "difference": pd.DataFrame(diff_rows), "agreement": agreement}
