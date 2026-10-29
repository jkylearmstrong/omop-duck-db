"""Feature-group ablation and paired comparison of two model runs."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.model_ablation import ablate_feature_groups, compare_model_runs, ridge_logistic_fit_predict


def _data(n=1500, seed=3):
    rng = np.random.default_rng(seed)
    x = pd.DataFrame({"s1": rng.normal(size=n), "s2": rng.normal(size=n), "n1": rng.normal(size=n), "n2": rng.normal(size=n),
                      "age": rng.normal(size=n)})
    z = 1.2 * x["s1"] + 0.9 * x["s2"] + 0.3 * x["age"] - 0.5
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-z))).astype(int)
    return x, y


GROUPS = {"signal": ["s1", "s2"], "noise": ["n1", "n2"]}


def test_a_useful_group_hurts_when_removed_and_a_noise_group_does_not():
    x, y = _data()
    res = ablate_feature_groups(x, y, GROUPS, n_boot=150, seed=1).set_index(["mode", "group"])
    assert res.loc[("full", "(all features)"), "n_features"] == 5 and res.loc[("full", "(all features)"), "events"] == int(y.sum())
    sig, noise = res.loc[("drop", "signal")], res.loc[("drop", "noise")]
    assert sig["auroc_delta"] < -0.1 and sig["auroc_delta_hi"] < 0 and sig["auroc_p_boot"] < 0.05     # removing the signal costs AUROC
    assert sig["brier_delta"] > 0 and sig["auprc_delta"] < 0
    assert abs(noise["auroc_delta"]) < 0.01 and abs(noise["auroc_delta"]) < abs(sig["auroc_delta"]) / 20   # noise: a tiny change at most
    assert sig["n_features"] == 3 and noise["n_features"] == 3


def test_only_mode_keeps_a_group_alone_and_both_returns_both():
    x, y = _data()
    both = ablate_feature_groups(x, y, GROUPS, mode="both", n_boot=60, seed=1)
    assert set(both["mode"]) == {"full", "drop", "only"}
    only = both[(both["mode"] == "only")].set_index("group")
    assert only.loc["signal", "n_features"] == 3                                  # the group plus the ungrouped column
    assert only.loc["signal", "auroc_variant"] > only.loc["noise", "auroc_variant"]


def test_results_are_reproducible_and_a_custom_model_is_used():
    x, y = _data(600)
    a = ablate_feature_groups(x, y, GROUPS, n_boot=40, seed=7)
    b = ablate_feature_groups(x, y, GROUPS, n_boot=40, seed=7)
    pd.testing.assert_frame_equal(a, b)
    calls = []

    def prior_only(xt, yt, xe):                                                   # a model that ignores the features
        calls.append(xt.shape[1])
        return np.full(len(xe), yt.mean())

    flat = ablate_feature_groups(x, y, GROUPS, fit_predict=prior_only, n_boot=20, seed=7)
    assert calls and abs(flat["auroc_full"].iloc[0] - 0.5) < 0.05


def test_ablation_validates_its_inputs():
    x, y = _data(200)
    with pytest.raises(ValueError, match="not in features"):
        ablate_feature_groups(x, y, {"g": ["nope"]})
    with pytest.raises(ValueError, match="empty"):
        ablate_feature_groups(x, y, {"g": []})
    with pytest.raises(ValueError, match="0/1"):
        ablate_feature_groups(x, y + 1, GROUPS)
    with pytest.raises(ValueError, match="same number"):
        ablate_feature_groups(x, y[:-1], GROUPS)
    with pytest.raises(ValueError, match="mode"):
        ablate_feature_groups(x, y, GROUPS, mode="x")
    with pytest.raises(ValueError, match="events"):
        ablate_feature_groups(x, np.zeros(len(x), dtype=int), GROUPS)


def test_default_model_recovers_a_known_signal_and_handles_missing_values():
    x, y = _data(1200)
    x.loc[::7, "s1"] = np.nan
    cut = 900
    p = ridge_logistic_fit_predict(x.iloc[:cut].to_numpy(), y[:cut], x.iloc[cut:].to_numpy())
    assert p.shape == (300,) and ((p > 0) & (p < 1)).all()
    from omop_etl.model_ablation import _auroc
    assert _auroc(y[cut:], p) > 0.7


def _runs(n=1000, seed=5):
    rng = np.random.default_rng(seed)
    y = (rng.uniform(size=n) < 0.3).astype(int)
    base = 0.3 + 0.25 * (y - 0.3) + rng.normal(0, 0.15, n)
    return y, np.clip(base, 0.01, 0.99)


def test_two_almost_identical_runs_are_not_different():
    y, p = _runs()
    rng = np.random.default_rng(9)
    a = pd.DataFrame({"person_id": np.arange(len(y)), "pred": p, "outcome": y})
    b = pd.DataFrame({"person_id": np.arange(len(y)), "pred": np.clip(p + rng.normal(0, 0.003, len(y)), 0.01, 0.99)})
    out = compare_model_runs(a, b, names=("v706", "v710"), n_boot=200)
    assert out["n"] == {"common": 1000, "only_v706": 0, "only_v710": 0, "events": int(y.sum())}
    d = out["difference"].set_index("metric")
    assert abs(d.loc["auroc", "delta"]) < 0.01 and d.loc["auroc", "delta_lo"] < 0 < d.loc["auroc", "delta_hi"]
    assert d.loc["auroc", "p_boot"] > 0.05
    assert out["agreement"]["spearman"] > 0.99 and out["agreement"]["top_overlap"] > 0.9
    assert list(out["metrics"]["run"]) == ["v706", "v710"]


def test_a_clearly_better_run_is_detected_and_unmatched_patients_are_counted():
    y, p = _runs()
    a = pd.DataFrame({"person_id": np.arange(len(y)), "pred": p, "outcome": y})
    rng = np.random.default_rng(11)
    better = np.clip(0.3 + 0.55 * (y - 0.3) + rng.normal(0, 0.15, len(y)), 0.01, 0.99)
    b = pd.DataFrame({"person_id": np.arange(len(y)), "pred": better, "outcome": y})
    out = compare_model_runs(a, b, n_boot=200)
    d = out["difference"].set_index("metric")
    assert d.loc["auroc", "delta"] > 0.02 and d.loc["auroc", "delta_lo"] > 0 and d.loc["auroc", "p_boot"] < 0.05
    assert d.loc["brier", "delta"] < 0                                                                # lower Brier is better
    # patients in only one run are counted and left out (A has ids 0-999, B has 50-1049; the outcome of a shared id agrees)
    y_all = np.concatenate([y, np.zeros(50, dtype=int)])
    a2 = pd.DataFrame({"person_id": np.arange(1000), "pred": p, "outcome": y_all[:1000]})
    b2 = pd.DataFrame({"person_id": np.arange(50, 1050), "pred": np.concatenate([better[50:], np.full(50, 0.3)]), "outcome": y_all[50:1050]})
    n = compare_model_runs(a2, b2, n_boot=20)["n"]
    assert n["common"] == 950 and n["only_A"] == 50 and n["only_B"] == 50


def test_compare_runs_validates_its_inputs():
    y, p = _runs(100)
    a = pd.DataFrame({"person_id": np.arange(100), "pred": p, "outcome": y})
    with pytest.raises(ValueError, match="duplicate"):
        compare_model_runs(pd.concat([a, a]), a)
    with pytest.raises(ValueError, match="no column"):
        compare_model_runs(a.drop(columns="pred"), a)
    with pytest.raises(ValueError, match="neither run"):
        compare_model_runs(a.drop(columns="outcome"), a.drop(columns="outcome"))
    flipped = a.assign(outcome=1 - a["outcome"])
    with pytest.raises(ValueError, match="disagree"):
        compare_model_runs(a, flipped)
    with pytest.raises(ValueError, match="in common"):
        compare_model_runs(a, a.assign(person_id=a["person_id"] + 1000))
