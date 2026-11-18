"""Table 6 terms that cannot be told apart, and the check that stops a broken table before any document is written."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from omop_etl import manuscript_tables as mt  # noqa: E402
from test_manuscript_tables import make_features  # noqa: E402


def _with_shared_missingness(n=3000):
    df = make_features(n=n, seed=4).copy()
    rng = np.random.default_rng(1)
    miss = rng.random(len(df)) < 0.4
    df["lab_creatinine"] = rng.normal(1.0, 0.3, len(df))
    df["lab_egfr"] = 90 - 30 * df["lab_creatinine"] + rng.normal(0, 5, len(df))  # derived from creatinine: missing together
    df.loc[miss, ["lab_creatinine", "lab_egfr"]] = np.nan
    return df


def test_identical_not_measured_terms_become_one_estimable_term():
    t = mt.manuscript_regression(_with_shared_missingness())
    feats = list(t["Feature"])
    assert "Creatinine / eGFR (2021): not measured" in feats
    assert "Creatinine: not measured" not in feats and "eGFR (2021): not measured" not in feats
    row = t[t["Feature"] == "Creatinine / eGFR (2021): not measured"].iloc[0]
    mt.validate_manuscript_sheets({"Table 6": ("", t)})  # every interval finite and narrow
    pt, lo, hi = mt._numbers(row["Adjusted OR (95% CI)*"])
    assert lo < pt < hi < 10


def test_collinear_terms_stop_the_fit():
    df = make_features(n=600, seed=2).copy()
    df["lab_creatinine"] = np.arange(len(df), dtype=float)
    df["lab_egfr"] = 2 * df["lab_creatinine"] + 1  # exactly collinear values
    with pytest.raises(mt.ManuscriptValidationError, match="collinear"):
        mt.manuscript_regression(df)


@pytest.mark.parametrize("cell, message", [
    ("0.99 (0.00–1533532213571036254429714906451741815665479408562958726332416.00)", "not estimable"),
    ("1.20 (1.30–1.50)", "outside its interval"),
    ("1.20 (1.50–1.10)", "lower bound above"),
    ("nan (nan–nan)", "non-finite"),
])
def test_validator_rejects_broken_or_cells(cell, message):
    t = pd.DataFrame({"Feature": ["Creatinine: not measured"], "Adjusted OR (95% CI)*": [cell]})
    with pytest.raises(mt.ManuscriptValidationError, match=message):
        mt.validate_manuscript_sheets({"Table 6": ("", t)})


def test_validator_bootstrap_interval_check_can_be_relaxed_for_smoke_runs():
    t = pd.DataFrame({"Health System": ["Site A"], "AUC-ROC (95% CI)": ["0.618 (0.636–0.663)"]})
    with pytest.raises(mt.ManuscriptValidationError):
        mt.validate_manuscript_sheets({"Table 5": ("", t)})
    mt.validate_manuscript_sheets({"Table 5": ("", t)}, bootstrap_intervals=False)
    # "ROC" is not an odds-ratio column, and good cells pass
    mt.validate_manuscript_sheets({"Table 5": ("", pd.DataFrame({"Health System": ["Site A"], "AUC-ROC (95% CI)": ["0.731 (0.728–0.735)"]}))})
