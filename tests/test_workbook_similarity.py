"""workbook_similarity: named tolerances for "the new run reproduces the delivered manuscript", and the default site labels."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from omop_etl.manuscript_tables import SIMILARITY_TOLERANCES, workbook_similarity  # noqa: E402


def _book(path, auc2=0.731, auc5=(0.712, 0.730), or_=(1.20, 0.80), p=("<0.001", "0.300")):
    def sheet(title, header, rows):
        return pd.DataFrame([[title] + [None] * (len(header) - 1), [None] * len(header), header] + rows)

    sheets = {
        "Table 2 - Model Performance": sheet("T2", ["Model", "AUC-ROC (95% CI)"], [["XGBoost", f"{auc2} (0.72-0.74)"]]),
        "Table 5 - Cross-Site LOSO": sheet("T5", ["Held-out site", "AUC-ROC (95% CI)"],
                                           [["Site A", f"{auc5[0]} (0.70-0.72)"], ["Site B", f"{auc5[1]} (0.71-0.74)"], ["Mean of sites", "0.72"]]),
        "Table 6 - Multivariable LR": sheet("T6", ["Feature", "Increment", "Adjusted OR (95% CI)*", "Standardized Beta", "P-Value"],
                                            [["Age", "x", f"{or_[0]} (1.1-1.3)", 0.1, p[0]], ["Sex", "x", f"{or_[1]} (0.7-0.9)", -0.1, p[1]]]),
        "Table 1 - Descriptive": sheet("T1", ["Variable", "Whole cohort"], [["Age", "60.1 ± 10"], ["LOS", "5.0 ± 3"]]),
    }
    with pd.ExcelWriter(path) as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, header=False, index=False)
    return path


def test_identical_workbooks_pass_every_check(tmp_path):
    a, b = _book(tmp_path / "a.xlsx"), _book(tmp_path / "b.xlsx")
    r = workbook_similarity(a, b)
    assert r["passed"].all() and {"Table 2 AUC within abs tolerance", "Table 5 per-site AUC within abs tolerance"} <= set(r["check"])


def test_drift_and_swapped_site_labels_fail_with_named_checks(tmp_path):
    old = _book(tmp_path / "old.xlsx")
    drift = workbook_similarity(_book(tmp_path / "n1.xlsx", auc2=0.80, or_=(0.8, 1.2), p=("0.5", "<0.001")), old).set_index("check")
    assert not drift.loc["Table 2 AUC within abs tolerance", "passed"] and not drift.loc["Table 6 p < 0.05 verdict agrees", "passed"]
    assert not drift.loc["Table 6 direction agrees (old OR outside 0.97-1.03)", "passed"]
    swapped = workbook_similarity(_book(tmp_path / "n2.xlsx", auc5=(0.65, 0.78)), old).set_index("check")      # sites in a different order
    assert not swapped.loc["Table 5 per-site AUC within abs tolerance", "passed"]
    loose = workbook_similarity(_book(tmp_path / "n3.xlsx", auc2=0.80), old, tolerances={"table2_auc_abs": 0.1}).set_index("check")
    assert loose.loc["Table 2 AUC within abs tolerance", "passed"] and SIMILARITY_TOLERANCES["table2_auc_abs"] == 0.01

