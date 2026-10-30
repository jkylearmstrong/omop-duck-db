"""Table 3: multivariable logistic-regression odds ratios with clinically scaled increments.

Per-1-unit odds ratios of continuous predictors (systolic BP, glucose, eGFR) round to an uninformative
``1.00 (1.00-1.00)``. :func:`generate_table3_odds_ratios` reports them per 1-SD (or a stated increment such as 10 mmHg)
instead, with Wald 95% CIs, standardized betas and p-values, and exports to Markdown / Quarto / Typst.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["generate_table3_odds_ratios", "export_table3"]

_Z95 = 1.959963984540054


def _fit_logistic(Xd: np.ndarray, y: np.ndarray, iters: int = 50) -> np.ndarray:
    """Unpenalised IRLS for ``Xd`` that already has an intercept column."""
    beta = np.zeros(Xd.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Xd @ beta, -30, 30)))
        w = np.clip(p * (1 - p), 1e-9, None)
        step = np.linalg.solve(Xd.T @ (Xd * w[:, None]) + 1e-10 * np.eye(Xd.shape[1]), Xd.T @ (y - p))
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def _coefs(model: Any, Xd: np.ndarray, y: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """(beta, se) for the ``k`` predictors; ``Xd`` is [1, X]."""
    if model is None:
        beta_full = _fit_logistic(Xd, y)
    elif hasattr(model, "coef_"):  # scikit-learn style
        beta_full = np.concatenate([np.ravel(getattr(model, "intercept_", [0.0]))[:1], np.ravel(model.coef_)])
    elif hasattr(model, "params"):  # statsmodels results; its own standard errors are the best ones
        params = np.asarray(model.params, dtype="float64")
        if params.shape[0] == k + 1 and hasattr(model, "bse"):
            return params[1:], np.asarray(model.bse, dtype="float64")[1:]
        beta_full = params
    else:
        raise TypeError("model must be None, a scikit-learn-style estimator (coef_) or a statsmodels result (params).")
    if beta_full.shape[0] != k + 1:
        raise ValueError(f"model has {beta_full.shape[0] - 1} coefficients for {k} predictors.")
    p = 1.0 / (1.0 + np.exp(-np.clip(Xd @ beta_full, -30, 30)))
    w = np.clip(p * (1 - p), 1e-9, None)
    cov = np.linalg.pinv(Xd.T @ (Xd * w[:, None]))
    return beta_full[1:], np.sqrt(np.clip(np.diag(cov)[1:], 0, None))


def _pvalue(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _fmt_p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def _or_text(o: float, lo: float, hi: float, places: int = 2, max_places: int = 4) -> str:
    """``OR (lo-hi)``; decimals grow (up to ``max_places``) until the interval no longer collapses onto the estimate."""
    d = places
    while d < max_places and (round(lo, d) == round(hi, d) or round(o, d) in (round(lo, d), round(hi, d))) and not math.isclose(lo, hi):
        d += 1
    return f"{o:.{d}f} ({lo:.{d}f}–{hi:.{d}f})"


def generate_table3_odds_ratios(
    model: Any,
    X: pd.DataFrame | np.ndarray,
    y: Sequence[float] | np.ndarray,
    feature_names: Sequence[str] | None = None,
    continuous_scale: str | Mapping[str, float] | None = "sd",
    labels: Mapping[str, str] | None = None,
    beta_places: int = 2,
) -> pd.DataFrame:
    """Odds-ratio table for a fitted logistic model.

    Args:
        model: Fitted scikit-learn logistic regression (``coef_``/``intercept_``), a statsmodels result, or ``None`` to
            fit an unpenalised logistic regression here. Standard errors are Wald, from the Fisher information at the
            fitted coefficients (statsmodels' own ``bse`` when available), so for a penalised sklearn fit they are approximate.
        X, y: The design matrix the model was fitted on (columns in coefficient order) and the 0/1 outcome.
        feature_names: Column names (default: ``X.columns``).
        continuous_scale: ``"sd"`` reports non-binary predictors per 1 SD; a mapping ``{name: increment}`` sets the
            increment per predictor (others stay per 1 unit); ``None`` keeps everything per 1 unit. 0/1 predictors are
            always "Yes vs No".
        labels: Optional display names.
        beta_places: Decimals for the standardized beta (default 2).

    Returns:
        DataFrame ``Variable, Increment, OR (95% CI), Std. beta, p_value`` plus numeric ``or``, ``ci_low``, ``ci_high``.
    """
    names = list(feature_names) if feature_names is not None else (list(X.columns) if isinstance(X, pd.DataFrame) else None)
    A = np.asarray(X, dtype="float64")
    if A.ndim != 2:
        raise ValueError("X must be 2-dimensional.")
    if names is None:
        names = [f"x{i}" for i in range(A.shape[1])]
    if len(names) != A.shape[1]:
        raise ValueError("feature_names length does not match the columns of X.")
    yv = np.asarray(y, dtype="float64")
    if yv.shape[0] != A.shape[0] or not set(np.unique(yv)).issubset({0.0, 1.0}):
        raise ValueError("y must be a 0/1 outcome with one value per row of X.")
    if isinstance(continuous_scale, str) and continuous_scale != "sd":
        raise ValueError("continuous_scale must be 'sd', a mapping, or None.")

    Xd = np.column_stack([np.ones(A.shape[0]), A])
    beta, se = _coefs(model, Xd, yv, A.shape[1])
    rows = []
    for j, name in enumerate(names):
        col = A[:, j]
        sd = float(np.nanstd(col, ddof=1))
        binary = set(np.unique(col[~np.isnan(col)])).issubset({0.0, 1.0})
        if binary:
            scale, inc = 1.0, "Yes vs No"
        elif isinstance(continuous_scale, Mapping) and name in continuous_scale:
            scale = float(continuous_scale[name])
            inc = f"per {scale:g}-unit"
        elif continuous_scale == "sd":
            scale, inc = sd, f"per 1 SD ({sd:.3g})"
        else:
            scale, inc = 1.0, "per 1 unit"
        b, s = float(beta[j]), float(se[j])
        o, lo, hi = math.exp(b * scale), math.exp((b - _Z95 * s) * scale), math.exp((b + _Z95 * s) * scale)
        if scale < 0:
            lo, hi = hi, lo
        p = _pvalue(b / s) if s > 0 else float("nan")
        rows.append({"Variable": (labels or {}).get(name, name), "Increment": inc, "OR (95% CI)": _or_text(o, lo, hi),
                     "Std. beta": f"{b * (sd if not binary else 1.0):.{beta_places}f}",
                     "p_value": _fmt_p(p) if not math.isnan(p) else "-", "or": o, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows)


def export_table3(table3: pd.DataFrame, format: str = "markdown", output_path: str | Path | None = None,
                  caption: str | None = None, **kwargs: Any) -> str:
    """Export a :func:`generate_table3_odds_ratios` table as ``markdown``, ``quarto``, ``typst``, ``latex`` or ``csv``."""
    from omop_etl.table1 import export_table1

    public = table3[[c for c in table3.columns if c not in ("or", "ci_low", "ci_high")]]
    fmt = format.lower().strip()
    if fmt in ("qmd", "quarto", "typ", "typst"):
        kwargs.setdefault("caption", caption or "Multivariable Logistic Regression: Odds Ratios")
        kwargs.setdefault("include_loinc_footnotes", False)
        kwargs.setdefault("label", "tbl-table3" if fmt in ("qmd", "quarto") else None)
    return export_table1(public, output_path=output_path, format=format, **kwargs)
