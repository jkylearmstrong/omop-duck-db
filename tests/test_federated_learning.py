"""v0.8.7: iterative federated logistic regression and Cox PH equal the pooled fit and share only aggregates."""
import numpy as np
import pandas as pd
import pytest

from omop_etl.federated_learning import FederatedNode, federated_cox, federated_logistic_regression

sm = pytest.importorskip("statsmodels.api")
PHReg = pytest.importorskip("statsmodels.duration.hazard_regression").PHReg


def _sites(seed=1):
    rng = np.random.default_rng(seed)

    def site(n, shift):
        x1, x2 = rng.normal(shift, 1, n), rng.binomial(1, 0.4, n)
        y = rng.binomial(1, 1 / (1 + np.exp(-(-0.5 + 0.8 * x1 - 0.6 * x2))))
        t = rng.exponential(1 / np.exp(0.5 * x1 - 0.4 * x2))
        c = rng.exponential(2, n)
        return pd.DataFrame({"x1": x1, "x2": x2, "y": y, "time": np.minimum(t, c), "event": (t <= c).astype(int)})

    return {"A": site(400, 0.0), "B": site(300, 0.5), "C": site(250, -0.3)}


def test_federated_logistic_matches_pooled_mle():
    sites = _sites()
    pooled = pd.concat(sites.values())
    r = federated_logistic_regression(sites, ["x1", "x2"], "y")
    m = sm.Logit(pooled["y"], sm.add_constant(pooled[["x1", "x2"]])).fit(disp=0)
    assert r.converged and r.n_obs == len(pooled)
    np.testing.assert_allclose(r.coefficients["estimate"], m.params.values, atol=1e-6)
    np.testing.assert_allclose(r.coefficients["std_error"], m.bse.values, atol=1e-6)
    assert r.sites == {"A": 400, "B": 300, "C": 250}


def test_federated_cox_matches_pooled_breslow():
    sites = _sites()
    pooled = pd.concat(sites.values())
    r = federated_cox(sites, ["x1", "x2"], "time", "event")
    h = PHReg(pooled["time"].values, pooled[["x1", "x2"]].values, status=pooled["event"].values, ties="breslow").fit()
    assert r.converged and r.n_events == int(pooled["event"].sum())
    np.testing.assert_allclose(r.coefficients["estimate"], h.params, atol=1e-6)
    np.testing.assert_allclose(r.coefficients["std_error"], h.bse, atol=1e-6)
    assert {"hazard_ratio", "hazard_ratio_lower", "hazard_ratio_upper"} <= set(r.coefficients.columns)


def test_cox_time_binning_and_noise_stay_close():
    sites = _sites()
    base = federated_cox(sites, ["x1", "x2"], "time", "event")
    binned = federated_cox(sites, ["x1", "x2"], "time", "event", time_bin=0.05)
    noisy = federated_cox(sites, ["x1", "x2"], "time", "event", noise_scale=0.01, seed=3)
    assert np.abs(binned.coefficients["estimate"] - base.coefficients["estimate"]).max() < 0.05
    assert np.abs(noisy.coefficients["estimate"] - base.coefficients["estimate"]).max() < 0.2


def test_guards():
    sites = _sites()
    with pytest.raises(ValueError, match="at least two sites"):
        federated_logistic_regression({"A": sites["A"]}, ["x1"], "y")
    with pytest.raises(ValueError, match="min_site_n"):
        federated_logistic_regression({"A": sites["A"], "B": sites["B"].head(3)}, ["x1"], "y", min_site_n=10)
    bad = {k: v.assign(y=v["y"] * 2) for k, v in sites.items()}
    with pytest.raises(ValueError, match="0/1"):
        federated_logistic_regression(bad, ["x1"], "y")
    with pytest.raises(KeyError):
        federated_cox(sites, ["nope"], "time", "event")
    node = FederatedNode("A", sites["A"])
    assert set(node.logistic_stats(np.zeros(3), ["x1", "x2"], "y")) == {"n", "ll", "grad", "hess"}   # aggregates only
