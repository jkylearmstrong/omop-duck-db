"""Iterative horizontal federated learning: logistic regression and Cox proportional hazards (Breslow ties).

Each ``FederatedNode`` keeps its patient-level rows and answers a coordinator only with aggregates: for logistic
regression the log-likelihood, gradient and Hessian at the current coefficients; for Cox the per-event-time risk-set
sums and event totals. The coordinator runs Newton-Raphson on the summed aggregates, so the fit equals the pooled
(one-table) maximum-likelihood fit up to the stopping tolerance. This is the iterative counterpart of the one-shot
``federated_meta_analysis``.

Privacy: no row leaves a node, but aggregates are not anonymous. ``min_site_n`` refuses a node with too few rows, Cox
time grids are optionally coarsened (``time_bin``) so a risk-set sum cannot isolate one patient, and
``noise_scale`` adds Gaussian noise to the shared aggregates: the logistic log-likelihood, gradient and Hessian, and the
Cox event counts per time, covariate sums and risk-set sums (a rough guard, not a formal differential-privacy
guarantee; noisy fits no longer equal the pooled fit). The row count ``n`` (already checked against ``min_site_n``) and
the total event count are bookkeeping figures and are not blurred. A Cox fit whose noisy risk-set sum is not positive
raises instead of silently dropping that time. A real deployment still needs transport security and a
governance review of the shared aggregates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import warnings

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
        ll = float(self._blur(np.asarray(ll)))
        return {"n": len(y), "ll": ll, "grad": self._blur(X.T @ (y - p)), "hess": self._blur((X * w[:, None]).T @ X)}

    def n_events(self, event: str) -> int:
        return int((self._df[event] == 1).sum())

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
        return {"n": len(t), "events": int(ev.sum()), "d": self._blur(d), "sx": self._blur(sx),
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
    def _exp(x):  # separable data -> huge coefficients; clip so exp saturates to a finite bound instead of overflowing
        return np.exp(np.clip(x, -700.0, 700.0))

    return pd.DataFrame({"term": terms, "estimate": beta, "std_error": se, "z": z,
                         "p_value": 2 * stats.norm.sf(np.abs(z)), ratio: _exp(beta),
                         f"{ratio}_lower": _exp(beta - q * se), f"{ratio}_upper": _exp(beta + q * se)})


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
    exact = all(n._noise <= 0 for n in nodes)    # step-halving needs an exact log-likelihood

    def evaluate(b):
        parts = [n.logistic_stats(b, covariates, outcome) for n in nodes]
        return (parts, sum(p["ll"] for p in parts) - 0.5 * float(b @ pen @ b),
                sum(p["grad"] for p in parts) - pen @ b, sum(p["hess"] for p in parts) + pen)

    parts, ll, grad, hess = evaluate(beta)
    trace, converged = [], False
    it = 0
    for it in range(1, max_iter + 1):
        step = np.linalg.solve(hess + 1e-12 * np.eye(len(beta)), grad)
        scale = 1.0
        while True:
            cand = evaluate(beta + scale * step)
            if not exact or (np.isfinite(cand[1]) and cand[1] >= ll - 1e-10):
                break
            scale /= 2
            if scale < 1e-10:
                break
        if scale < 1e-10:        # no step improves the likelihood: at a maximum, or numerically stuck
            converged = bool(np.max(np.abs(step)) < tol)
            break
        beta = beta + scale * step
        parts, ll, grad, hess = cand
        trace.append({"iter": it, "log_likelihood": ll, "max_abs_step": float(np.max(np.abs(scale * step))), "step_scale": scale})
        if np.max(np.abs(scale * step)) < tol:
            converged = True
            break
    if not converged:
        separated = float(l2) == 0.0 and (np.max(np.abs(beta)) > 15 or ll > -1e-6 * max(1, sum(p["n"] for p in parts)))
        warnings.warn("federated logistic regression did not converge in " + str(it) + " iterations"
                      + ("; coefficients are very large, which usually means complete or quasi-complete separation "
                         "(a covariate predicts the outcome perfectly). Drop or combine that covariate or set l2 > 0."
                         if separated else "") + "; estimates and standard errors are unreliable.", UserWarning, stacklevel=2)
    cov = np.linalg.inv(hess)
    return FederatedFitResult("logistic", _summarise(terms, beta, cov, "odds_ratio", alpha), converged, it, ll,
                              sum(p["n"] for p in parts), None, {n.name: len(n) for n in nodes}, trace, cov)


def federated_cox(sites, covariates: Sequence[str], time: str, event: str, *, max_iter: int = 50, tol: float = 1e-8,
                  time_bin: float | None = None, min_site_n: int = 10, min_site_events: int = 0,
                  noise_scale: float = 0.0, seed: int | None = None, alpha: float = 0.05) -> FederatedFitResult:
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
    for n in nodes:
        if n.n_events(event) < min_site_events:
            raise ValueError(f"site {n.name} has {n.n_events(event)} events, below min_site_events={min_site_events}")
    beta = np.zeros(len(covariates))

    def evaluate(b):
        parts = [n.cox_stats(b, covariates, time, event, grid, time_bin) for n in nodes]
        d = sum(p["d"] for p in parts)
        sx = sum(p["sx"] for p in parts)
        s0, s1, s2 = (sum(p[k] for p in parts) for k in ("s0", "s1", "s2"))
        if not (s0 > 0).all():
            raise ValueError("a risk-set sum is not positive after noise; lower noise_scale, raise time_bin or "
                             "min_site_n, or use more sites")
        xbar = s1 / s0[:, None]
        ll = float(sx @ b - np.sum(d * np.log(s0)))
        grad = sx - np.sum(d[:, None] * xbar, axis=0)
        info = np.sum(d[:, None, None] * (s2 / s0[:, None, None] - xbar[:, :, None] * xbar[:, None, :]), axis=0)
        return parts, ll, grad, info

    trace, converged = [], False
    it = 0
    for it in range(1, max_iter + 1):
        _, ll, grad, info = evaluate(beta)
        step = np.linalg.solve(info + 1e-12 * np.eye(len(beta)), grad)
        beta = beta + step
        trace.append({"iter": it, "log_likelihood": ll, "max_abs_step": float(np.max(np.abs(step)))})
        if np.max(np.abs(step)) < tol:
            converged = True
            break
    if not converged and all(n._noise <= 0 for n in nodes):    # noisy aggregates never settle to tol
        warnings.warn(f"federated Cox fit did not converge in {it} iterations; estimates are unreliable.", UserWarning, stacklevel=2)
    parts, ll, _, info = evaluate(beta)      # log-likelihood and covariance at the final coefficients
    cov = np.linalg.inv(info)
    return FederatedFitResult("cox", _summarise(covariates, beta, cov, "hazard_ratio", alpha), converged, it, ll,
                              sum(p["n"] for p in parts), int(sum(p["events"] for p in parts)),
                              {n.name: len(n) for n in nodes}, trace, cov)
