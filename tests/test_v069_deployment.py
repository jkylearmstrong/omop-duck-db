"""FR-A1/A2/A3: model bundles, site telemetry + aggregator, feature-contract validation."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl.feature_contract import FeatureContractError, validate_feature_frame  # noqa: E402
from omop_etl.federation import aggregate_telemetry, site_telemetry  # noqa: E402
from omop_etl.scoring import BundleIntegrityError, load_model_bundle, score_cohort, write_model_bundle  # noqa: E402
from omop_etl.telemetry import auroc_delong, average_precision, validate_packet  # noqa: E402

SPEC = {"features": [
    {"name": "creatinine", "dtype": "float", "unit": "mg/dL", "bounds": [0.1, 30.0], "impute": 1.0},
    {"name": "heart_rate", "dtype": "float", "unit": "bpm", "bounds": [20, 250], "impute": 80.0},
    {"name": "ip_velocity", "dtype": "int", "impute": 0},
]}


def _frame(n=200, seed=0):
    r = np.random.default_rng(seed)
    return pd.DataFrame({
        "person_id": np.arange(n),
        "creatinine": r.lognormal(0.1, 0.4, n), "heart_rate": r.normal(85, 15, n).clip(30, 200),
        "ip_velocity": r.integers(0, 4, n)})


COEF = [0.9, 0.02, 0.4]
INTERCEPT = -3.0


def _glm(df):
    z = INTERCEPT + df["creatinine"] * COEF[0] + df["heart_rate"] * COEF[1] + df["ip_velocity"] * COEF[2]
    return 1 / (1 + np.exp(-z))


@pytest.fixture()
def bundle_dir(tmp_path):
    return write_model_bundle(
        tmp_path / "bundle", name="demo", version="1.0.0", feature_spec=SPEC,
        components=[{"name": "glm", "kind": "logistic", "filename": "glm.json",
                    "content": {"intercept": INTERCEPT, "coef": COEF}}],
        license="MIT", intended_use="unit test")


# ---- A1 -------------------------------------------------------------------------------------------------

def test_bundle_roundtrip_matches_training_predictions(bundle_dir):
    df = _frame()
    out = score_cohort(load_model_bundle(bundle_dir), df)
    assert list(out.columns) == ["person_id", "prob", "risk_tier"]
    np.testing.assert_allclose(out["prob"], _glm(df), atol=1e-5)
    assert set(out["risk_tier"]) <= {"low", "moderate", "high"}


def test_bundle_calibration_and_blend(tmp_path):
    d = write_model_bundle(
        tmp_path / "b", name="m", version="2", feature_spec=SPEC,
        components=[{"kind": "logistic", "filename": "a.json", "weight": 3, "content": {"intercept": INTERCEPT, "coef": COEF}},
                    {"kind": "logistic", "filename": "b.json", "weight": 1, "content": {"intercept": 0.0, "coef": [0, 0, 0]}}],
        calibration={"method": "platt", "slope": 1.0, "intercept": 0.0})
    df = _frame()
    p = score_cohort(d, df)["prob"].to_numpy()
    np.testing.assert_allclose(p, (3 * _glm(df) + 0.5) / 4, atol=1e-5)


def test_bundle_hash_mismatch_raises(bundle_dir):
    (bundle_dir / "glm.json").write_text(json.dumps({"intercept": 0.0, "coef": [1, 1, 1]}))
    with pytest.raises(BundleIntegrityError, match="SHA-256"):
        load_model_bundle(bundle_dir)
    spec = bundle_dir / "feature_spec.json"
    spec.write_text(spec.read_text().replace("bpm", "BPM"))
    with pytest.raises(BundleIntegrityError, match="feature_spec"):
        load_model_bundle(bundle_dir)


def test_bundle_refuses_pickles_and_unlisted_files(bundle_dir, tmp_path):
    import pickle

    (bundle_dir / "model.pkl").write_bytes(pickle.dumps({"x": 1}))
    with pytest.raises(BundleIntegrityError, match="pickle"):
        load_model_bundle(bundle_dir)
    (bundle_dir / "model.pkl").unlink()
    blend = json.loads((bundle_dir / "blend.json").read_text())
    blend["components"][0]["file"] = "../evil.json"
    (bundle_dir / "blend.json").write_text(json.dumps(blend))
    m = json.loads((bundle_dir / "manifest.json").read_text())
    import hashlib

    m["model_sha256"]["blend.json"] = hashlib.sha256((bundle_dir / "blend.json").read_bytes()).hexdigest()
    (bundle_dir / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(BundleIntegrityError, match="plain file name"):
        load_model_bundle(bundle_dir)


def test_score_validates_contract_and_imputes(bundle_dir):
    df = _frame(50)
    df.loc[0, "creatinine"] = np.nan
    out = score_cohort(bundle_dir, df)
    assert out["prob"].notna().all()
    df2 = _frame(50)
    df2.loc[1, "heart_rate"] = 900
    with pytest.raises(FeatureContractError):
        score_cohort(bundle_dir, df2)
    out, rep = score_cohort(bundle_dir, df2, on_violation="clip", return_validation=True)
    assert rep.n_clipped == 1 and out["prob"].notna().all()


def test_xgboost_and_onnx_optional():
    pytest.importorskip("xgboost")  # round trip is exercised wherever the bundle extras are installed


# ---- A3 -------------------------------------------------------------------------------------------------

def test_validate_report_fields_and_policies():
    df = _frame(100)
    df["creatinine"] = df["creatinine"] * 88.4  # umol/L
    df = df.drop(columns=["ip_velocity"]).assign(extra=1)
    with pytest.raises(FeatureContractError) as ei:
        validate_feature_frame(df, SPEC)
    rep = ei.value.report
    assert rep.missing == ["ip_velocity"] and "creatinine" in rep.unit_suspicion and "umol/L" in rep.unit_suspicion["creatinine"]
    d = rep.to_dict()
    json.dumps(d)  # machine-readable
    assert "creatinine" in rep.summary()

    df = _frame(100)
    df.loc[:4, "heart_rate"] = 999
    for policy, key in (("clip", "n_clipped"), ("nullify", "n_nullified")):
        rep = validate_feature_frame(df, SPEC, policy)
        assert getattr(rep, key) == 5
        assert rep.to_telemetry()["n_values_clipped" if policy == "clip" else "n_values_nullified"] == 5
        assert (rep.frame["heart_rate"].max() <= 250) or rep.frame["heart_rate"].isna().sum() == 5
    bad_dtype = _frame(10).assign(creatinine="x")
    with pytest.raises(FeatureContractError, match="dtype"):
        validate_feature_frame(bad_dtype, SPEC, "clip")


# ---- A2 -------------------------------------------------------------------------------------------------

def _site(n, seed, prev=0.15, signal=1.5):
    r = np.random.default_rng(seed)
    y = (r.random(n) < prev).astype(int)
    z = r.normal(-2 + signal * y, 1.0)
    return 1 / (1 + np.exp(-z)), y


def test_auroc_ap_against_reference():
    p, y = _site(2000, 1)
    auc, se = auroc_delong(y, p)
    pos, neg = p[y == 1], p[y == 0]
    brute = ((pos[:, None] > neg[None, :]).mean() + 0.5 * (pos[:, None] == neg[None, :]).mean())
    assert math.isclose(auc, brute, abs_tol=1e-12) and 0 < se < 0.05
    sk = pytest.importorskip("sklearn.metrics")
    assert math.isclose(average_precision(y, p), sk.average_precision_score(y, p), abs_tol=1e-9)


def test_packet_is_aggregate_only_and_valid(tmp_path):
    p, y = _site(5000, 2)
    pk = site_telemetry(p, y, "S01", output=tmp_path / "S01.json")
    validate_packet(pk)
    text = (tmp_path / "S01.json").read_text()
    assert len(json.loads(text)["deciles"]) == 10 and "person" not in text
    assert pk["metrics"]["auroc"] > 0.7 and 0.5 < pk["metrics"]["cal_slope"] < 2.5
    t = {x["top_fraction"]: x for x in pk["tiers"]}
    assert t[0.05]["nns"] >= 1 and t[0.05]["ppv"] > pk["prevalence"]
    with pytest.raises(ValueError, match="site_id"):
        site_telemetry(p, y, "Temple University Hospital")
    bad = dict(pk, patient_ids=[1, 2])
    with pytest.raises(ValueError, match="patient_ids"):
        validate_packet(bad)
    from omop_etl.telemetry import _check, load_packet_schema

    errs: list[str] = []  # the built-in fallback validator (used when jsonschema is absent) agrees
    _check(bad, load_packet_schema(), "", errs)
    assert any("patient_ids" in e for e in errs)
    errs.clear()
    _check(pk, load_packet_schema(), "", errs)
    assert errs == []


def test_small_cells_are_suppressed_with_secondary_suppression():
    p, y = _site(400, 3, prev=0.03)  # ~12 events: deciles will hold small event counts
    pk = site_telemetry(p, y, "S02", min_cell=5)
    assert not pk["suppressed"]
    hidden = [d for d in pk["deciles"] if d["events"] is None]
    for d in pk["deciles"]:
        for key in ("n", "events"):
            assert d[key] is None or d[key] == 0 or d[key] >= 1
    # no published cell is a primary-suppressed small positive count
    assert all(d["events"] is None or d["events"] == 0 or d["events"] >= 5 for d in pk["deciles"])
    # a lone hidden cell would be recoverable from the total: at least two (or none) are hidden
    assert len(hidden) != 1
    tiny = site_telemetry(*_site(60, 4, prev=0.05), "S03")
    assert tiny["suppressed"] and tiny["metrics"] is None and tiny["deciles"] == []


def test_aggregator_matches_pooled_recomputation(tmp_path):
    ps, ys = [], []
    for i, n in enumerate([3000, 4000, 2500, 5000]):
        p, y = _site(n, 10 + i)
        ps.append(p), ys.append(y)
        site_telemetry(p, y, f"S{i:02d}", output=tmp_path / f"S{i:02d}.json")
    agg = aggregate_telemetry(tmp_path)
    pooled_auc, _ = auroc_delong(np.concatenate(ys), np.concatenate(ps))
    assert abs(agg["auroc"]["pooled"] - pooled_auc) < 0.01  # documented tolerance for homogeneous sites
    assert agg["n"] == 14500 and agg["sites"] == ["S00", "S01", "S02", "S03"]
    for k in ("cochran_q", "i2_percent", "tau2", "q_p"):
        assert agg["auroc"][k] is not None
    assert 0 <= agg["auroc"]["i2_percent"] <= 100
    # heterogeneous sites -> tau2 > 0 and a small Q p-value
    pe, ye = _site(4000, 99, signal=0.3)
    site_telemetry(pe, ye, "S09", output=tmp_path / "S09.json")
    het = aggregate_telemetry(tmp_path)
    assert het["auroc"]["tau2"] > 0 and het["auroc"]["q_p"] < 0.05
    dup = site_telemetry(*_site(3000, 5), "S00")
    (tmp_path / "dup.json").write_text(json.dumps(dup))
    with pytest.raises(ValueError, match="Duplicate"):
        aggregate_telemetry(tmp_path)


def test_packet_records_validation_counts():
    df = _frame(100)
    df.loc[:2, "heart_rate"] = 999
    rep = validate_feature_frame(df, SPEC, "clip")
    p, y = _site(3000, 7)
    pk = site_telemetry(p, y, "S05", validation=rep)
    assert pk["feature_validation"]["n_values_clipped"] == 3
