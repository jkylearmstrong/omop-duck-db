"""Unit tests covering the 5 feature requests from tasks/requests1.txt:
1. Consortium-Level Scoring Wrapper on FederatedConsortium
2. Cross-Site Model Calibration & Decile Drift (ConsortiumCalibration)
3. Clinical Decision Curve Analysis (DecisionCurveAnalysis)
4. Automated European & IFCC Metric Unit Scaling (Hemoglobin mmol/L, HbA1c IFCC mmol/mol, Creatinine umol/L)
5. Competing Risks Cumulative Incidence Function (estimate_cif, plot_cif, fine_gray_estimator)
"""

import os
import sys
import tempfile
from unittest.mock import patch
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    FederatedConsortium,
    build_schema,
    harmonize_measurement_units,
)
from omop_etl.ml import (
    ConsortiumCalibration,
    DecisionCurveAnalysis,
    consortium_calibration,
    decision_curve_analysis,
)
from omop_etl.survival import (
    CIFResult,
    FineGrayResult,
    estimate_cif,
    fine_gray_estimator,
    plot_cif,
)
from omop_duck_db.ml import (
    consortium_calibration as dd_consortium_calibration,
    decision_curve_analysis as dd_decision_curve_analysis,
)
from omop_duck_db.survival import (
    estimate_cif as dd_estimate_cif,
    fine_gray_estimator as dd_fine_gray_estimator,
    plot_cif as dd_plot_cif,
)


# ---------------------------------------------------------------------------
# Feature 1: Consortium-Level Scoring Wrapper on FederatedConsortium
# ---------------------------------------------------------------------------
def test_consortium_scoring_wrapper():
    temp_dir = tempfile.mkdtemp()
    site_a_path = os.path.join(temp_dir, "site_a.duckdb")
    site_b_path = os.path.join(temp_dir, "site_b.duckdb")

    for db_path, person_ids in [(site_a_path, [1, 2]), (site_b_path, [3, 4])]:
        con = duckdb.connect(db_path)
        build_schema(con)
        for pid in person_ids:
            con.execute(
                f"INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                f"VALUES ({pid}, 8507, 1960, 8527, 38003563)"
            )
        con.execute(
            "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES "
            "(9201, 'Inpatient Visit', 'Visit', 'Visit', 'Visit', 'IP', DATE '1970-01-01', DATE '2099-12-31'), "
            "(9203, 'Emergency Room Visit', 'Visit', 'Visit', 'Visit', 'ER', DATE '1970-01-01', DATE '2099-12-31'), "
            "(3000963, 'Hemoglobin', 'Measurement', 'LOINC', 'Lab Test', '718-7', DATE '1970-01-01', DATE '2099-12-31'), "
            "(3019550, 'Sodium', 'Measurement', 'LOINC', 'Lab Test', '2951-2', DATE '1970-01-01', DATE '2099-12-31')"
        )
        for i, pid in enumerate(person_ids):
            con.execute(
                f"INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) "
                f"VALUES ({100 + pid}, {pid}, 9201, DATE '2023-01-01', DATE '2023-01-05', 32817)"
            )
            con.execute(
                f"INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number) "
                f"VALUES ({200 + pid}, {pid}, 3000963, DATE '2023-01-04', 32817, 11.0)"
            )
        # Populate standard CDM cohort table
        con.execute(
            "INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date) "
            "SELECT 1, person_id, visit_start_date, visit_end_date FROM visit_occurrence"
        )
        con.close()

    consortium = FederatedConsortium(sites={"SiteA": site_a_path, "SiteB": site_b_path})
    try:
        res = consortium.calculate_scores(cohort_table="cohort")
        assert res is not None
        assert not res.scores.empty
        assert not res.site_summary.empty
        assert not res.consortium_summary.empty
        assert "SiteA" in res.site_summary["site"].values
        assert "SiteB" in res.site_summary["site"].values

        df = res.to_dataframe()
        assert not df.empty
        assert "site" in df.columns
        assert "subject_id" in df.columns or "person_id" in df.columns
        assert "lace_score" in df.columns
        assert "hospital_score" in df.columns

        summary = res.summary()
        assert not summary.empty
        assert "site" in summary.columns
        assert "mean_lace_score" in summary.columns
        assert "mean_hospital_score" in summary.columns
    finally:
        consortium.close()


# ---------------------------------------------------------------------------
# Feature 2: Cross-Site Model Calibration & Decile Drift
# ---------------------------------------------------------------------------
def test_consortium_calibration():
    np.random.seed(42)
    n = 300
    y_true = np.random.binomial(1, 0.25, size=n)
    y_prob = np.clip(np.random.beta(2, 5, size=n), 0.01, 0.99)
    site_labels = np.random.choice(["SiteA", "SiteB", "SiteC"], size=n)

    cal = consortium_calibration(
        y_true=y_true,
        y_prob=y_prob,
        site_labels=site_labels,
        n_bins=5,
    )

    assert isinstance(cal, ConsortiumCalibration)
    metrics = cal.site_metrics
    assert not metrics.empty
    assert "site" in metrics.columns
    assert "brier_score" in metrics.columns
    assert "ece" in metrics.columns
    assert "mce" in metrics.columns
    assert "gnd_statistic" in metrics.columns
    assert "gnd_p_value" in metrics.columns

    curves = cal.calibration_curves
    assert not curves.empty
    assert "pred_mean" in curves.columns
    assert "obs_rate" in curves.columns

    drift = cal.decile_drift
    assert not drift.empty
    assert "site" in drift.columns

    summary_df = cal.summary()
    assert not summary_df.empty

    fig = cal.plot()
    try:
        import matplotlib.pyplot as plt
        assert fig is not None
        plt.close(fig)
    except ImportError:
        assert fig is None

    # Test re-export
    cal2 = dd_consortium_calibration(y_true, y_prob, site_labels, n_bins=5)
    assert isinstance(cal2, ConsortiumCalibration)


# ---------------------------------------------------------------------------
# Feature 3: Clinical Decision Curve Analysis (DCA)
# ---------------------------------------------------------------------------
def test_decision_curve_analysis():
    np.random.seed(42)
    n = 400
    y_true = np.random.binomial(1, 0.20, size=n)
    y_prob = np.clip(np.random.beta(2, 6, size=n), 0.01, 0.99)

    dca = decision_curve_analysis(
        y_true=y_true,
        y_prob=y_prob,
        threshold_range=(0.05, 0.35),
        step=0.05,
    )

    assert isinstance(dca, DecisionCurveAnalysis)
    df = dca.net_benefit
    assert not df.empty
    assert "threshold" in df.columns
    assert "model" in df.columns
    assert "all" in df.columns
    assert "none" in df.columns
    assert "standardized_net_benefit" in df.columns
    assert "net_reduction_interventions" in df.columns

    # Treat none must always be 0.0
    assert (df["none"] == 0.0).all()

    summary_dict = dca.summary()
    assert isinstance(summary_dict, dict)
    assert "prevalence" in summary_dict
    assert "max_net_benefit" in summary_dict
    assert "clinical_benefit_range" in summary_dict

    df_out = dca.to_dataframe()
    assert not df_out.empty

    fig = dca.plot()
    try:
        import matplotlib.pyplot as plt
        assert fig is not None
        plt.close(fig)
    except ImportError:
        assert fig is None

    # Test re-export
    dca2 = dd_decision_curve_analysis(y_true, y_prob, threshold_range=(0.05, 0.35))
    assert isinstance(dca2, DecisionCurveAnalysis)


# ---------------------------------------------------------------------------
# Feature 4: Automated European & IFCC Metric Unit Scaling
# ---------------------------------------------------------------------------
def test_european_ifcc_unit_scaling():
    con = duckdb.connect(":memory:")
    build_schema(con)

    con.execute(
        "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
        "VALUES (1, 8507, 1970, 8527, 38003563)"
    )
    con.execute(
        "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES "
        "(3000963, 'Hemoglobin', 'Measurement', 'LOINC', 'Lab Test', '59260-0', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3004410, 'HbA1c IFCC', 'Measurement', 'LOINC', 'Lab Test', '59261-8', DATE '1970-01-01', DATE '2099-12-31'), "
        "(3016723, 'Creatinine', 'Measurement', 'LOINC', 'Lab Test', '2160-0', DATE '1970-01-01', DATE '2099-12-31')"
    )

    # 1. Hemoglobin in mmol/L (LOINC 59260-0) value = 10.0 -> should scale to 10.0 * 1.611 = 16.11 g/dL
    # 2. HbA1c in IFCC mmol/mol (LOINC 59261-8) value = 50.0 -> should scale to (0.09148 * 50) + 2.152 = 6.726 %
    # 3. Serum Creatinine in umol/L value = 176.8 -> should scale to 176.8 / 88.4 = 2.0 mg/dL
    con.execute(
        """
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_source_value)
        VALUES
        (1, 1, 3000963, DATE '2023-01-01', 32817, 10.0, 'mmol/L'),
        (2, 1, 3004410, DATE '2023-01-01', 32817, 50.0, 'mmol/mol'),
        (3, 1, 3016723, DATE '2023-01-01', 32817, 176.8, 'µmol/l')
        """
    )

    report = harmonize_measurement_units(con, create_view=True)
    assert report is not None

    res = con.execute("SELECT measurement_id, value_as_number FROM measurement_harmonized ORDER BY measurement_id").fetchall()
    val_map = {row[0]: row[1] for row in res}

    # Hemoglobin 10.0 * 1.611 = 16.11
    assert pytest.approx(16.11, abs=0.01) == val_map[1]
    # HbA1c 50.0 -> 6.726
    assert pytest.approx(6.726, abs=0.01) == val_map[2]
    # Creatinine 176.8 / 88.4 = 2.0
    assert pytest.approx(2.0, abs=0.01) == val_map[3]

    # Also test when unit_source_value is NULL for European LOINCs 59260-0 and 59261-8
    con.execute(
        """
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_source_value)
        VALUES
        (4, 1, 3000963, DATE '2023-02-01', 32817, 9.0, NULL),
        (5, 1, 3004410, DATE '2023-02-01', 32817, 60.0, NULL)
        """
    )
    harmonize_measurement_units(con, create_view=True)
    res_null = con.execute("SELECT measurement_id, value_as_number FROM measurement_harmonized WHERE measurement_id IN (4, 5)").fetchall()
    val_null = {row[0]: row[1] for row in res_null}
    # 9.0 * 1.611 = 14.499
    assert pytest.approx(14.499, abs=0.01) == val_null[4]
    # (0.09148 * 60) + 2.152 = 7.6408
    assert pytest.approx(7.6408, abs=0.01) == val_null[5]


# ---------------------------------------------------------------------------
# Feature 5: Competing Risks Cumulative Incidence Function (CIF)
# ---------------------------------------------------------------------------
def test_cif_and_fine_gray_estimator():
    np.random.seed(42)
    n = 200
    times = np.random.exponential(scale=30, size=n) + 1
    # 0: censored, 1: readmission, 2: competing death
    statuses = np.random.choice([0, 1, 2], p=[0.4, 0.4, 0.2], size=n)
    treatment = np.random.choice([0, 1], size=n)

    df = pd.DataFrame({
        "time_days": times,
        "status": statuses,
        "treatment": treatment,
    })

    cif = estimate_cif(df, time_col="time_days", status_col="status")
    assert isinstance(cif, CIFResult)
    tbl = cif.cif_table
    assert not tbl.empty
    assert "time" in tbl.columns
    assert "cif_primary" in tbl.columns
    assert "cif_competing" in tbl.columns

    # Verify cumulative incidence is within [0, 1]
    assert (tbl["cif_primary"] >= 0.0).all()
    assert (tbl["cif_primary"] <= 1.0).all()
    assert (tbl["cif_competing"] >= 0.0).all()
    assert (tbl["cif_competing"] <= 1.0).all()

    summary_dict = cif.summary()
    assert isinstance(summary_dict, dict)
    assert "cif_primary_30d" in summary_dict

    pred30 = cif.predict_at_time(30.0)
    assert "cif_primary" in pred30

    df_out = cif.to_dataframe()
    assert not df_out.empty

    fig = plot_cif(cif)
    try:
        import matplotlib.pyplot as plt
        assert fig is not None
        plt.close(fig)
    except ImportError:
        assert fig is None

    fig_method = cif.plot()
    try:
        import matplotlib.pyplot as plt
        assert fig_method is not None
        plt.close(fig_method)
    except ImportError:
        assert fig_method is None

    # Test Fine-Gray estimator
    fg = fine_gray_estimator(
        df,
        time_col="time_days",
        status_col="status",
        group_col="treatment",
        event_code=1,
    )
    assert isinstance(fg, FineGrayResult)
    assert fg.shr > 0
    assert 0.0 <= fg.p_value <= 1.0
    assert fg.ci_lower <= fg.shr <= fg.ci_upper

    fg_summary = fg.summary()
    assert isinstance(fg_summary, dict)
    assert "subdistribution_hazard_ratio" in fg_summary
    assert "p_value" in fg_summary

    fg_df = fg.to_dataframe()
    assert not fg_df.empty
    assert "subdistribution_hazard_ratio" in fg_df.columns

    # Test re-exports
    cif2 = dd_estimate_cif(df, time_col="time_days", status_col="status")
    assert isinstance(cif2, CIFResult)
    fg2 = dd_fine_gray_estimator(df, time_col="time_days", status_col="status", group_col="treatment")
    assert isinstance(fg2, FineGrayResult)
    fig2 = dd_plot_cif(cif2)
    try:
        import matplotlib.pyplot as plt
        assert fig2 is not None
        plt.close(fig2)
    except ImportError:
        assert fig2 is None


def test_plotting_without_matplotlib_fallback():
    """Verify that when matplotlib is not available, plot() functions gracefully return None."""
    np.random.seed(42)
    cal = consortium_calibration(
        y_true=np.array([0, 1, 0, 1, 0, 1]),
        y_prob=np.array([0.1, 0.9, 0.2, 0.8, 0.3, 0.7]),
        site_labels=np.array(["A", "A", "B", "B", "C", "C"]),
        n_bins=2,
    )
    dca = decision_curve_analysis(
        y_true=np.array([0, 1, 0, 1]),
        y_prob=np.array([0.1, 0.8, 0.2, 0.7]),
        threshold_range=(0.1, 0.5),
    )
    df = pd.DataFrame({
        "time_days": [5, 10, 15, 20],
        "status": [0, 1, 2, 1],
    })
    cif = estimate_cif(df, time_col="time_days", status_col="status")

    with patch.dict(sys.modules, {"matplotlib": None, "matplotlib.pyplot": None}):
        assert cal.plot() is None
        assert dca.plot() is None
        assert cif.plot() is None
        assert plot_cif(cif) is None
        assert dd_consortium_calibration(
            np.array([0, 1, 0, 1]),
            np.array([0.1, 0.8, 0.2, 0.7]),
            np.array(["A", "A", "B", "B"]),
            n_bins=2,
        ).plot() is None
        assert dd_plot_cif(cif) is None

