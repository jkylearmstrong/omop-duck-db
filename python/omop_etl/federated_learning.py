"""Iterative horizontal federated learning: logistic regression and Cox proportional hazards (Breslow ties).

Each ``FederatedNode`` keeps its patient-level rows and answers a coordinator only with aggregates: for logistic
regression the log-likelihood, gradient and Hessian at the current coefficients; for Cox the per-event-time risk-set
sums and event totals. The coordinator runs Newton-Raphson on the summed aggregates, so the fit equals the pooled
(one-table) maximum-likelihood fit up to the stopping tolerance. This is the iterative counterpart of the one-shot
``federated_meta_analysis``.

Privacy: no row leaves a node, but aggregates are not anonymous. ``min_site_n`` refuses a node with too few rows, Cox
time grids are optionally coarsened (``time_bin``) so a risk-set sum cannot isolate one patient, and
``noise_scale`` adds Gaussian noise to every shared aggregate (a rough guard, not a formal differential-privacy
guarantee; noisy fits no longer equal the pooled fit). A real deployment still needs transport security and a
governance review of the shared aggregates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats

__all__ = ["FederatedNode", "FederatedFitResult", "federated_logistic_regression", "federated_cox"]


@dataclass
class FederatedFitResult:
    """Coefficients, standard errors, convergence trace and per-site sizes of an iterative federated fit."""

    model: str
    coefficients: pd.DataFrame          # term, estimate, std_error, z, p_value, (hazard_ratio | odds_ratio) + CI
    converged: bool
    n_iter: int
    log_likelihood: float
    n_obs: int
    n_events: int | None
    sites: dict[str, int]
    trace: list[dict[str, float]] = field(default_factory=list)
    covariance: np.ndarray | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "converged": self.converged, "n_iter": self.n_iter,
                "log_likelihood": self.log_likelihood, "n_obs": self.n_obs, "n_events": self.n_events,
                "sites": self.sites, "coefficients": self.coefficients.to_dict("records")}


class FederatedNode:
    """One consortium site. Holds rows locally; ``*_stats`` methods return aggregates only."""

    def __init__(self, name: str, data: pd.DataFrame, *, noise_scale: float = 0.0, seed: int | None = None):
        self.name = str(name)
        self._df = data.reset_index(drop=True)
        self._noise = float(noise_scale)
        self._rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return len(self._df)

    def _blur(self, a: np.ndarray) -> np.ndarray:
        return a if self._noise <= 0 else a + self._rng.normal(0.0, self._noise, size=np.shape(a))

    def _matrix(self, covariates: Sequence[str]) -> np.ndarray:
        missing = [c for c in covariates if c not in self._df.columns]
        if missing:
            raise KeyError(f"site {self.name}: missing columns {missing}")
        return self._df[list(covariates)].to_numpy(dtype=float)

    # -- logistic ----------------------------------------------------------------------------------
    def logistic_stats(self, beta: np.ndarray, covariates: Sequence[str], outcome: str) -> dict[str, Any]:
        X = np.column_stack([np.ones(len(self._df)), self._matrix(covariates)])
        y = self._df[outcome].to_numpy(dtype=float)
        if not np.isin(y, (0.0, 1.0)).all():
            raise ValueError(f"site {self.name}: outcome {outcome!r} must be 0/1")
        eta = np.clip(X @ beta, -35, 35)
        p = 1.0 / (1.0 + np.exp(-eta))
        w = p * (1 - p)
        ll = float(np.sum(y * eta - np.logaddexp(0.0, eta)))
        return {"n": len(y), "ll": ll, "grad": self._blur(X.T @ (y - p)), "hess": self._blur((X * w[:, None]).T @ X)}

    # -- Cox ---------------------------------------------------------------------------------------
    def cox_grid(self, time: str, event: str, time_bin: float | None) -> np.ndarray:
        t = self._df.loc[self._df[event] == 1, time].to_numpy(dtype=float)
        return np.unique(_bin(t, time_bin))

    def cox_stats(self, beta: np.ndarray, covariates: Sequence[str], time: str, event: str,
                  grid: np.ndarray, time_bin: float | None) -> dict[str, Any]:
        X = self._matrix(covariates)
        t = _bin(self._df[time].to_numpy(dtype=float), time_bin)
        e = self._df[event].to_numpy(dtype=int)
        r = np.exp(np.clip(X @ beta, -35, 35))
        k, p = len(grid), X.shape[1]
        s0, s1, s2 = np.zeros(k), np.zeros((k, p)), np.zeros((k, p, p))
        order = np.argsort(-t, kind="stable")        # cumulative sums over the risk set (t_i >= grid time)
        t_s, r_s, X_s = t[order], r[order], X[order]
        c0 = np.cumsum(r_s)
        c1 = np.cumsum(r_s[:, None] * X_s, axis=0)
        c2 = np.cumsum(r_s[:, None, None] * X_s[:, :, None] * X_s[:, None, :], axis=0)
        idx = np.searchsorted(-t_s, -grid, side="right") - 1   # last descending index with t_s >= grid time
        ok = idx >= 0
        s0[ok], s1[ok], s2[ok] = c0[idx[ok]], c1[idx[ok]], c2[idx[ok]]
        d = np.zeros(k)
        sx = np.zeros(p)
        ev = e == 1
        if ev.any():
            np.add.at(d, np.searchsorted(grid, t[ev]), 1)
            sx = X[ev].sum(axis=0)
        return {"n": len(t), "events": int(ev.sum()), "d": d, "sx": self._blur(sx),
                "s0": self._blur(s0), "s1": self._blur(s1), "s2": self._blur(s2)}


def _bin(t: np.ndarray, width: float | None) -> np.ndarray:
    return t if not width else np.ceil(t / width) * width


def _nodes(sites: Mapping[str, Any] | Sequence[Any], min_site_n: int, noise_scale: float, seed: int | None) -> list[FederatedNode]:
    items = list(sites.items()) if isinstance(sites, Mapping) else [(getattr(s, "name", f"site_{i + 1}"), s) for i, s in enumerate(sites)]
    out = []
    for i, (name, s) in enumerate(items):
        node = s if isinstance(s, FederatedNode) else FederatedNode(name, s, noise_scale=noise_scale,
                                                                   seed=None if seed is None else seed + i)
        if len(node) < min_site_n:
            raise ValueError(f"site {node.name} has {len(node)} rows, below min_site_n={min_site_n}")
        out.append(node)
    if len(out) < 2:
        raise ValueError("federated fitting needs at least two sites")
    return out


def _summarise(terms: list[str], beta: np.ndarray, cov: np.ndarray, ratio: str, alpha: float) -> pd.DataFrame:
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    z = np.divide(beta, se, out=np.full_like(beta, np.nan), where=se > 0)
    q = stats.norm.ppf(1 - alpha / 2)
    return pd.DataFrame({"term": terms, "estimate": beta, "std_error": se, "z": z,
                         "p_value": 2 * stats.norm.sf(np.abs(z)), ratio: np.exp(beta),
                         f"{ratio}_lower": np.exp(beta - q * se), f"{ratio}_upper": np.exp(beta + q * se)})


def federated_logistic_regression(sites, covariates: Sequence[str], outcome: str, *, l2: float = 0.0,
                                  max_iter: int = 50, tol: float = 1e-8, min_site_n: int = 10,
                                  noise_scale: float = 0.0, seed: int | None = None, alpha: float = 0.05) -> FederatedFitResult:
    """Newton-Raphson logistic regression across sites; sites share only (log-likelihood, gradient, Hessian).

    ``sites`` is ``{name: DataFrame}`` (or a list of DataFrames / ``FederatedNode``). ``l2`` is a ridge penalty on the
    slopes (never the intercept). Equals the pooled maximum-likelihood fit when ``noise_scale == 0``.
    """
    nodes = _nodes(sites, min_site_n, noise_scale, seed)
    covariates = list(covariates)
    terms = ["(Intercept)", *covariates]
    pen = np.diag([0.0] + [float(l2)] * len(covariates))
    beta = np.zeros(len(terms))
    trace, converged = [], False
    it = 0
    for it in range(1, max_iter + 1):
        parts = [n.logistic_stats(beta, covariates, outcome) for n in nodes]
        ll = sum(p["ll"] for p in parts) - 0.5 * float(beta @ pen @ beta)
        grad = sum(p["grad"] for p in parts) - pen @ beta
        hess = sum(p["hess"] for p in parts) + pen
        step = np.linalg.solve(hess + 1e-12 * np.eye(len(beta)), grad)
        beta = beta + step
        trace.append({"iter": it, "log_likelihood": ll, "max_abs_step": float(np.max(np.abs(step)))})
        if np.max(np.abs(step)) < tol:
            converged = True
            break
    parts = [n.logistic_stats(beta, covariates, outcome) for n in nodes]
    ll = sum(p["ll"] for p in parts) - 0.5 * float(beta @ pen @ beta)
    hess = sum(p["hess"] for p in parts) + pen
    cov = np.linalg.inv(hess)
    return FederatedFitResult("logistic", _summarise(terms, beta, cov, "odds_ratio", alpha), converged, it, ll,
                              sum(p["n"] for p in parts), None, {n.name: len(n) for n in nodes}, trace, cov)


def federated_cox(sites, covariates: Sequence[str], time: str, event: str, *, max_iter: int = 50, tol: float = 1e-8,
                  time_bin: float | None = None, min_site_n: int = 10, noise_scale: float = 0.0,
                  seed: int | None = None, alpha: float = 0.05) -> FederatedFitResult:
    """Cox proportional hazards (Breslow ties) by Newton-Raphson on per-event-time risk-set aggregates.

    Every site first reveals its distinct event times (coarsen with ``time_bin`` to limit this); the coordinator
    unions them and each round sites return risk-set sums at those times. Equals the pooled Breslow partial-likelihood
    fit when ``noise_scale == 0`` and ``time_bin`` is unset.
    """
    nodes = _nodes(sites, min_site_n, noise_scale, seed)
    covariates = list(covariates)
    grid = np.unique(np.concatenate([n.cox_grid(time, event, time_bin) for n in nodes]))
    if grid.size == 0:
        raise ValueError("no events at any site")
    beta = np.zeros(len(covariates))
    trace, converged = [], False
    it = 0
    for it in range(1, max_iter + 1):
        parts = [n.cox_stats(beta, covariates, time, event, grid, time_bin) for n in nodes]
        d = sum(p["d"] for p in parts)
        sx = sum(p["sx"] for p in parts)
        s0, s1, s2 = (sum(p[k] for p in parts) for k in ("s0", "s1", "s2"))
        s0 = np.where(s0 > 0, s0, np.nan)
        ll = float(sx @ beta - np.nansum(d * np.log(s0)))
        xbar = s1 / s0[:, None]
        grad = sx - np.nansum(d[:, None] * xbar, axis=0)
        info = np.nansum(d[:, None, None] * (s2 / s0[:, None, None] - xbar[:, :, None] * xbar[:, None, :]), axis=0)
        step = np.linalg.solve(info + 1e-12 * np.eye(len(beta)), grad)
        beta = beta + step
        trace.append({"iter": it, "log_likelihood": ll, "max_abs_step": float(np.max(np.abs(step)))})
        if np.max(np.abs(step)) < tol:
            converged = True
            break
    cov = np.linalg.inv(info)
    return FederatedFitResult("cox", _summarise(covariates, beta, cov, "hazard_ratio", alpha), converged, it, ll,
                              sum(p["n"] for p in parts), int(sum(p["events"] for p in parts)),
                              {n.name: len(n) for n in nodes}, trace, cov)
