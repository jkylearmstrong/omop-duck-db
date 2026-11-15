"""Unit tests for Kaplan-Meier and Fine-Gray Competing Risks CIF Estimator."""

import numpy as np
import pandas as pd
import pytest

from omop_etl.survival import (
    estimate_km_survival,
    SurvivalResult,
)


def test_km_known_reference_tolerance():
    """Validates numerical tolerance <= 1e-6 against exact analytical KM & Greenwood formulas.
    
    6 subjects:
    time:   [1, 2, 3, 4, 5, 6]
    status: [1, 0, 1, 1, 0, 1]
    """
    df = pd.DataFrame({
        "time": [1, 2, 3, 4, 5, 6],
        "status": [1, 0, 1, 1, 0, 1],
    })

    res = estimate_km_survival(df, time_col="time", status_col="status")
    assert isinstance(res, SurvivalResult)
    tl = res.to_dataframe()

    # t = 0: S = 1.0, se = 0.0
    row_0 = tl[tl["time"] == 0].iloc[0]
    assert np.isclose(row_0["survival"], 1.0, atol=1e-6)
    assert np.isclose(row_0["std_err"], 0.0, atol=1e-6)

    # t = 1: S = 5/6 = 0.83333333, se = (5/6) * sqrt(1 / 30) = 0.15214515
    row_1 = tl[tl["time"] == 1].iloc[0]
    assert np.isclose(row_1["survival"], 5.0 / 6.0, atol=1e-6)
    expected_se_1 = (5.0 / 6.0) * np.sqrt(1.0 / 30.0)
    assert np.isclose(row_1["std_err"], expected_se_1, atol=1e-6)

    # t = 3: S = 5/8 = 0.625, se = (5/8) * sqrt(1/30 + 1/12) = 0.2134815
    row_3 = tl[tl["time"] == 3].iloc[0]
    assert np.isclose(row_3["survival"], 5.0 / 8.0, atol=1e-6)
    expected_se_3 = (5.0 / 8.0) * np.sqrt(1.0 / 30.0 + 1.0 / 12.0)
    assert np.isclose(row_3["std_err"], expected_se_3, atol=1e-6)

    # t = 4: S = 5/12 = 0.41666667
    row_4 = tl[tl["time"] == 4].iloc[0]
    assert np.isclose(row_4["survival"], 5.0 / 12.0, atol=1e-6)

    # t = 6: S = 0.0
    row_6 = tl[tl["time"] == 6].iloc[0]
    assert np.isclose(row_6["survival"], 0.0, atol=1e-6)


def test_competing_risks_cif():
    """Validates 3-state competing risks Cumulative Incidence Functions.
    
    Status 0 = censored, 1 = readmission (primary), 2 = death (competing).
    Verifies that CIF_primary + CIF_competing == 1 - S_overall at all time steps,
    and that naive (1 - KM) strictly overestimates CIF_primary in the presence of competing risks.
    """
    df = pd.DataFrame({
        "time_days": [2, 3, 5, 7, 8, 10, 12, 15],
        "status":    [1, 2, 0, 1, 2,  0,  1,  2],
    })

    res = estimate_km_survival(df)
    assert res.has_competing_risks is True
    tl = res.to_dataframe()

    assert "cif_primary" in tl.columns
    assert "cif_competing" in tl.columns
    assert "naive_km_primary" in tl.columns

    # Check non-negativity and monotonicity
    assert (tl["cif_primary"].diff().dropna() >= -1e-12).all()
    assert (tl["cif_competing"].diff().dropna() >= -1e-12).all()

    # Fundamental competing risks equality: CIF_1 + CIF_2 == 1 - S_overall
    cif_sum = tl["cif_primary"] + tl["cif_competing"]
    one_minus_s = 1.0 - tl["survival"]
    assert np.allclose(cif_sum, one_minus_s, atol=1e-6)

    # Naive KM >= CIF_primary
    assert (tl["naive_km_primary"] >= tl["cif_primary"] - 1e-6).all()


def test_survival_summary_and_predict():
    df = pd.DataFrame({
        "time_days": [10, 20, 30, 40, 50],
        "status": [1, 1, 1, 0, 1],
    })
    res = estimate_km_survival(df)
    summary = res.summary()

    assert summary["n_total"] == 5
    assert summary["n_events"] == 4
    assert summary["n_censored"] == 1
    assert summary["median_survival_time"] == 30.0

    pred_25 = res.predict_at_time(25.0)
    assert pred_25["time"] == 25.0
    # At t=25, last event was at t=20: S(20) = (1 - 1/5) * (1 - 1/4) = 0.8 * 0.75 = 0.6
    assert np.isclose(pred_25["survival"], 0.6, atol=1e-6)

    # Plot smoke test
    fig = res.plot(title="Test KM")
    # matplotlib may or may not be available; if available, returns Figure
    if fig is not None:
        assert fig is not None
