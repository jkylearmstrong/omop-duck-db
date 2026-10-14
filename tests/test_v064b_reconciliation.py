"""Unit tests for Pipeline Reconciliation, Forced Index Stays, and Parity Ledger."""

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl.reconciliation import (
    cohort_from_index_stays,
    cohort_overlap,
    compare_feature_tables,
    run_study_grid,
)


@pytest.fixture
def mock_recon_cdm(tmp_path):
    con = duckdb.connect(":memory:")

    # Minimal schema
    con.execute("""
        CREATE TABLE person (
            person_id BIGINT PRIMARY KEY,
            gender_concept_id INTEGER,
            year_of_birth INTEGER,
            race_concept_id INTEGER,
            ethnicity_concept_id INTEGER,
            person_source_value VARCHAR
        );
        CREATE TABLE visit_occurrence (
            visit_occurrence_id BIGINT PRIMARY KEY,
            person_id BIGINT,
            visit_concept_id INTEGER,
            visit_start_date DATE,
            visit_end_date DATE,
            visit_type_concept_id INTEGER,
            visit_source_value VARCHAR
        );
    """)

    # Populate 10 persons
    for pid in range(1, 11):
        con.execute(f"""
            INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value)
            VALUES ({pid}, 8507, 1970, 8527, 38003563, 'SRC_PAT_{pid}');
        """)
        con.execute(f"""
            INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, visit_source_value)
            VALUES ({100 + pid}, {pid}, 9201, DATE '2023-05-01', DATE '2023-05-05', 44818518, 'ENC_{100 + pid}');
        """)

    yield con
    con.close()


def test_cohort_from_index_stays(mock_recon_cdm):
    # External legacy stay file with 8 matched patients and 2 unmapped patients
    stays = pd.DataFrame({
        "person_source_value": [f"SRC_PAT_{i}" for i in range(1, 9)] + ["UNMAPPED_A", "UNMAPPED_B"],
        "visit_source_value": [f"ENC_{100 + i}" for i in range(1, 9)] + ["ENC_999", "ENC_998"],
        "index_date": ["2023-05-01"] * 10,
        "end_date": ["2023-05-05"] * 10,
        "site": ["SiteA"] * 5 + ["SiteB"] * 5,
    })

    cohort_df, audit = cohort_from_index_stays(
        mock_recon_cdm, stays, id_type="person_source_value", target_table="forced_cohort"
    )

    assert not cohort_df.empty
    assert len(cohort_df) == 8
    assert audit["total_input_stays"] == 10
    assert audit["matched_patients"] == 8
    assert audit["unmatched_patients"] == 2
    assert audit["patient_match_rate"] == 0.8
    assert audit["matched_encounters"] == 8

    # Verify table was created in DuckDB
    db_count = mock_recon_cdm.execute("SELECT count(*) FROM forced_cohort").fetchone()[0]
    assert db_count == 8


def test_compare_feature_tables():
    n = 100
    pids = np.arange(1, n + 1)

    # Identical feature
    f_same = np.random.normal(50.0, 5.0, size=n)

    # Divergent feature (large SMD)
    f_diff_l = np.random.normal(10.0, 2.0, size=n)
    f_diff_r = np.random.normal(15.0, 2.0, size=n)

    # Scale mismatch (10x SD ratio)
    f_scale_l = np.random.normal(100.0, 10.0, size=n)
    f_scale_r = np.random.normal(100.0, 1.0, size=n)

    # Missingness gap
    f_miss_l = np.full(n, np.nan)
    f_miss_l[:80] = 5.0  # 20% missing
    f_miss_r = np.full(n, 5.0)  # 0% missing

    # All zero
    f_zero = np.zeros(n)

    left = pd.DataFrame({
        "person_id": pids,
        "f_same": f_same,
        "f_diff": f_diff_l,
        "f_scale": f_scale_l,
        "f_miss": f_miss_l,
        "f_zero": f_zero,
        "site": ["SiteA"] * 50 + ["SiteB"] * 50,
    })

    right = pd.DataFrame({
        "person_id": pids,
        "f_same": f_same,
        "f_diff": f_diff_r,
        "f_scale": f_scale_r,
        "f_miss": f_miss_r,
        "f_zero": f_zero,
        "site": ["SiteA"] * 50 + ["SiteB"] * 50,
    })

    ledger = compare_feature_tables(left, right, keys="person_id", by="site")
    assert not ledger.empty
    assert "large_smd" in ",".join(ledger["reason"].unique())
    assert "missingness_gap" in ",".join(ledger["reason"].unique())
    assert "scale_mismatch" in ",".join(ledger["reason"].unique())
    assert "all_zero" in ",".join(ledger["reason"].unique())

    # f_same should not be flagged
    same_rows = ledger[ledger["feature"] == "f_same"]
    assert not same_rows.empty
    assert not same_rows["flagged"].any()


def test_cohort_overlap():
    cohort_a = pd.DataFrame({
        "subject_id": list(range(1, 16)),  # 1 to 15
        "site": ["SiteA"] * 10 + ["SiteB"] * 5,
        "outcome": [1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 1],
    })

    cohort_b = pd.DataFrame({
        "subject_id": list(range(10, 21)),  # 10 to 20 (overlap 10..15 = 6 patients)
        "site": ["SiteA"] * 5 + ["SiteB"] * 6,
        "outcome": [1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1],
    })

    ov = cohort_overlap(None, cohort_a, cohort_b, by="site", outcome_col="outcome")
    assert not ov.empty

    all_row = ov[ov["site"] == "ALL"].iloc[0]
    assert all_row["n_cohort_a"] == 15
    assert all_row["n_cohort_b"] == 11
    assert all_row["both_count"] == 6
    assert all_row["a_only_count"] == 9
    assert all_row["b_only_count"] == 5
    assert round(all_row["jaccard_index"], 2) == round(6 / 20, 2)
    assert "both_event_rate" in all_row


def test_run_study_grid(mock_recon_cdm):
    # Dummy study function varying sampling_rule and lookback_days
    def mock_study(sampling_rule="first", lookback_days=365):
        n = 10 if sampling_rule == "first" else 8
        out = pd.DataFrame({
            "subject_id": list(range(1, n + 1)),
            "site": ["SiteA"] * (n // 2) + ["SiteB"] * (n - n // 2),
            "outcome": [1, 0] * (n // 2),
        })
        return out

    variants = {
        "sampling_rule": ["first", "random"],
        "lookback_days": [180, 365],
    }

    grid = run_study_grid(mock_recon_cdm, mock_study, variants)
    assert not grid.empty
    # 2 x 2 combinations = 4 combinations, each with SiteA, SiteB, and ALL (3 rows each) = 12 rows
    assert len(grid) == 12
    assert "sampling_rule" in grid.columns
    assert "lookback_days" in grid.columns
    assert "event_rate" in grid.columns
