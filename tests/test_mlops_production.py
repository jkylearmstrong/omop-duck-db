"""Point-of-care scoring, drift/calibration monitoring and capacity-calibrated risk tiers."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "python"))

from omop_etl import build_schema  # noqa: E402
from omop_etl.encounter import score_encounter  # noqa: E402
from omop_etl.model_bundle import write_model_bundle  # noqa: E402
from omop_etl.monitoring import (  # noqa: E402
    calibration_summary, monitor_scores, population_stability_index, update_monitoring_log)
from omop_etl.risk_tiers import fit_capacity_thresholds, map_risk_tiers, tier_load  # noqa: E402

SPEC = {"features": [
    {"name": "lab_creatinine", "dtype": "float", "bounds": [0.1, 30.0], "impute": 1.0},
    {"name": "heart_rate", "dtype": "float", "bounds": [20, 250], "impute": 80.0},
    {"name": "prior_ip_count", "dtype": "int", "impute": 0},
]}
COEF, B0 = [0.8, 0.02, 0.5], -3.5


def _p(creat, hr, prior):
    return 1 / (1 + math.exp(-(B0 + COEF[0] * creat + COEF[1] * hr + COEF[2] * prior)))


@pytest.fixture()
def bundle(tmp_path):
    return write_model_bundle(tmp_path / "b", name="demo", version="1", feature_spec=SPEC,
                              components=[{"kind": "logistic", "filename": "glm.json",
                                           "content": {"intercept": B0, "coef": COEF}}])


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    build_schema(c)
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
              "VALUES (1, 8507, 1950, 8527, 38003564)")
    for vid, concept, s, e in [(10, 9201, "2020-01-01", "2020-01-04"),    # one prior inpatient stay
                               (11, 9201, "2020-06-01", "2020-06-04"),    # index stay
                               (12, 9201, "2020-07-01", "2020-07-01")]:   # open stay (placeholder end)
        c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                  "visit_end_date, visit_type_concept_id) VALUES (?, 1, ?, CAST(? AS DATE), CAST(? AS DATE), 32827)",
                  [vid, concept, s, e])
    mid = 0
    for concept, day, val in [(3016723, "2020-06-01", 1.0), (3016723, "2020-06-03", 3.0),   # creatinine rises
                              (3027018, "2020-06-01", 90.0), (3027018, "2020-06-04", 70.0)]:
        mid += 1
        c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, "
                  "value_as_number, measurement_type_concept_id) VALUES (?, 1, ?, CAST(? AS DATE), ?, 44818702)",
                  [mid, concept, day, val])
    yield c
    c.close()


# ---- score_encounter ---------------------------------------------------------------------------------------

def test_score_encounter_uses_latest_values_before_discharge(con, bundle):
    r = score_encounter(con, 11, bundle)
    assert r["features"]["lab_creatinine"] == 3.0 and r["features"]["heart_rate"] == 70.0
    assert r["measurement_dates"]["lab_creatinine"] == "2020-06-03"
    assert r["prob"] == pytest.approx(_p(3.0, 70.0, 1), abs=1e-6)
    assert r["person_id"] == 1 and r["risk_tier"] in {"low", "moderate", "high"}


def test_score_encounter_as_of_ignores_later_readings(con, bundle):
    r = score_encounter(con, 11, bundle, as_of="2020-06-02")
    assert r["features"]["lab_creatinine"] == 1.0 and r["features"]["heart_rate"] == 90.0
    assert r["prob"] == pytest.approx(_p(1.0, 90.0, 1), abs=1e-6)


def test_score_encounter_open_visit_and_custom_tiers(con, bundle):
    r = score_encounter(con, 12, bundle, as_of="2020-07-02",
                        risk_tiers=[{"name": "standard", "min": 0.0}, {"name": "high", "min": 0.0001}])
    assert r["risk_tier"] == "high" and "lab_creatinine" not in r["measurement_dates"]   # imputed, none recorded


def test_score_encounter_errors(con, bundle):
    with pytest.raises(ValueError, match="not found"):
        score_encounter(con, 999, bundle)
    with pytest.raises(ValueError, match="before the admission"):
        score_encounter(con, 11, bundle, as_of="2020-05-01")
    assert "_score_encounter_cohort" not in [r[0] for r in con.execute("SHOW TABLES").fetchall()]


# ---- monitoring --------------------------------------------------------------------------------------------

def test_psi_zero_for_same_distribution_and_large_for_shift():
    r = np.random.default_rng(0)
    base = r.beta(2, 8, 5000)
    assert population_stability_index(base, r.beta(2, 8, 5000)) < 0.05
    assert population_stability_index(base, r.beta(4, 6, 5000)) > 0.25


def test_calibration_summary_detects_overprediction():
    r = np.random.default_rng(1)
    p = r.uniform(0.05, 0.5, 4000)
    good = calibration_summary(r.binomial(1, p), p)
    bad = calibration_summary(r.binomial(1, p / 2), p)
    assert 0.9 < good["oe_ratio"] < 1.1 and 0.85 < good["slope"] < 1.15 and good["auroc"] > 0.6
    assert bad["oe_ratio"] < 0.6
    assert calibration_summary([0, 1, 0], [0.1, 0.2, 0.3])["slope"] is None      # too few events


def _live(months, shift=1.0, deflate=1.0, n=1500, seed=2):
    r = np.random.default_rng(seed)
    frames = []
    for m in months:
        p = np.clip(r.beta(2, 8, n) * shift, 0, 1)
        frames.append(pd.DataFrame({"score_date": pd.Timestamp(m) + pd.to_timedelta(r.integers(0, 28, n), "D"),
                                    "prob": p, "outcome_flag": r.binomial(1, np.clip(p * deflate, 0, 1)).astype(float)}))
    return pd.concat(frames, ignore_index=True)


def test_monitor_scores_flags_drift_and_miscalibration():
    base = np.random.default_rng(9).beta(2, 8, 5000)
    stable = _live(["2020-01-01"])
    drift = _live(["2020-02-01"], shift=2.0)
    miscal = _live(["2020-03-01"], deflate=0.5)
    rep = monitor_scores(pd.concat([stable, drift, miscal]), base, as_of="2020-06-30").set_index("month")
    assert rep.loc["2020-01", "status"] == "ok"
    assert rep.loc["2020-02", "status"] in {"drift", "miscalibrated"} and rep.loc["2020-02", "psi"] > 0.25
    assert rep.loc["2020-03", "status"] == "miscalibrated" and rep.loc["2020-03", "oe_ratio"] < 0.8


def test_monitor_scores_waits_for_outcomes_to_mature():
    base = np.random.default_rng(9).beta(2, 8, 5000)
    rep = monitor_scores(_live(["2020-06-01"]), base, as_of="2020-06-30")
    row = rep.iloc[0]
    assert not row["outcomes_matured"] and row["status"] == "insufficient_outcomes" and pd.isna(row["oe_ratio"])


def test_monitoring_log_upserts_by_month(tmp_path):
    base = np.random.default_rng(9).beta(2, 8, 5000)
    path = tmp_path / "log.csv"
    first = monitor_scores(_live(["2020-01-01", "2020-02-01"]), base, as_of="2020-02-15")
    update_monitoring_log(first, path, model_version="1.0")
    later = monitor_scores(_live(["2020-02-01"]), base, as_of="2020-06-30")
    log = update_monitoring_log(later, path, model_version="1.0")
    assert list(log["month"]) == ["2020-01", "2020-02"]
    assert bool(log.set_index("month").loc["2020-02", "outcomes_matured"])                 # replaced, not duplicated
    assert len(pd.read_csv(path)) == 2


# ---- risk tiers --------------------------------------------------------------------------------------------

def test_capacity_thresholds_hit_target_shares():
    ref = np.random.default_rng(3).beta(2, 8, 10_000)
    ts = fit_capacity_thresholds(ref, [{"name": "high", "top_fraction": 0.10}, {"name": "elevated", "top_fraction": 0.30},
                                       {"name": "standard"}])
    assert [t["name"] for t in ts] == ["standard", "elevated", "high"] and ts[0]["min"] == 0.0
    share = {t["name"]: t["fraction"] for t in ts}
    assert share["high"] == pytest.approx(0.10, abs=0.005) and share["elevated"] == pytest.approx(0.20, abs=0.005)
    tiers = map_risk_tiers(ref, ts)
    assert (tiers == "high").mean() == pytest.approx(0.10, abs=0.005)


def test_map_risk_tiers_boundaries_scalar_and_validation():
    ts = [{"name": "standard", "min": 0.0}, {"name": "high", "min": 0.2}]
    assert map_risk_tiers(0.2, ts) == "high" and map_risk_tiers(0.1999, ts) == "standard" and map_risk_tiers(1.0, ts) == "high"
    assert map_risk_tiers([0.0, 0.5], ts, as_category=True).tolist() == ["standard", "high"]
    with pytest.raises(ValueError, match="probabilities"):
        map_risk_tiers([1.2], ts)
    with pytest.raises(ValueError, match="min = 0"):
        map_risk_tiers([0.5], [{"name": "high", "min": 0.2}])
    with pytest.raises(ValueError, match="increase"):
        fit_capacity_thresholds([0.1, 0.2, 0.3], [{"name": "a", "top_fraction": 0.3}, {"name": "b", "top_fraction": 0.2},
                                                  {"name": "c"}])


def test_tier_load_flags_over_capacity():
    ts = [{"name": "standard", "min": 0.0}, {"name": "high", "min": 0.5}]
    prob = np.array([0.9] * 30 + [0.1] * 70)
    df = tier_load(prob, ts, capacity={"high": 5}, periods=5).set_index("tier")
    assert df.loc["high", "per_period"] == 6 and bool(df.loc["high", "over_capacity"])
    assert not bool(df.loc["standard", "over_capacity"])


# ---- CLI ---------------------------------------------------------------------------------------------------

def test_cli_score_encounter(tmp_path, bundle, capsys):
    import json
    from omop_etl.cli import main
    db = tmp_path / "cdm.duckdb"
    c = duckdb.connect(str(db))
    build_schema(c)
    c.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1950, 8527, 38003564)")
    c.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) "
              "VALUES (11, 1, 9201, DATE '2020-06-01', DATE '2020-06-04', 32827)")
    c.execute("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number, measurement_type_concept_id) "
              "VALUES (1, 1, 3016723, DATE '2020-06-03', 3.0, 44818702)")
    c.close()
    capsys.readouterr()
    assert main(["score-encounter", "--bundle", str(bundle), "--db", str(db), "--visit-id", "11"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["visit_occurrence_id"] == 11 and out["features"]["lab_creatinine"] == 3.0
    assert main(["score-encounter", "--bundle", str(bundle), "--db", str(db), "--visit-id", "999"]) == 1


def test_cli_risk_tiers_fit_and_apply(tmp_path, capsys):
    from omop_etl.cli import main
    ref = pd.DataFrame({"prob": np.random.default_rng(0).beta(2, 8, 2000)})
    ref.to_csv(tmp_path / "ref.csv", index=False)
    th = tmp_path / "th.json"
    assert main(["risk-tiers", "fit", "--reference", str(tmp_path / "ref.csv"), "--tier", "high=0.1", "-o", str(th)]) == 0
    assert main(["risk-tiers", "apply", "--scores", str(tmp_path / "ref.csv"), "--thresholds", str(th),
                 "-o", str(tmp_path / "out.csv"), "--capacity", "high=10", "--periods", "10"]) == 0
    out = pd.read_csv(tmp_path / "out.csv")
    assert (out["risk_tier"] == "high").mean() == pytest.approx(0.10, abs=0.01)
    assert "over_capacity" in capsys.readouterr().out
    assert main(["risk-tiers", "fit", "--reference", str(tmp_path / "ref.csv"), "--tier", "high", "-o", str(th)]) == 1


def test_cli_monitor_writes_log_and_fails_on_flag(tmp_path, capsys):
    from omop_etl.cli import main
    base = pd.DataFrame({"prob": np.random.default_rng(9).beta(2, 8, 5000)})
    base.to_csv(tmp_path / "base.csv", index=False)
    _live(["2020-02-01"], shift=2.0).to_csv(tmp_path / "live.csv", index=False)
    args = ["monitor", "--scores", str(tmp_path / "live.csv"), "--baseline", str(tmp_path / "base.csv"),
            "--log", str(tmp_path / "log.csv"), "--model-version", "1.0", "--as-of", "2020-06-30"]
    assert main(args) == 0
    assert main(args + ["--fail-on-flag"]) == 2
    log = pd.read_csv(tmp_path / "log.csv")
    assert len(log) == 1 and log["model_version"].iloc[0] == 1.0 and log["psi"].iloc[0] > 0.25
    assert main(["monitor", "--scores", str(tmp_path / "live.csv"), "--baseline", str(tmp_path / "base.csv"),
                 "--log", str(tmp_path / "log.csv"), "--date-col", "nope"]) == 1
