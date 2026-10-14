"""Unit tests covering the 6 modular forward-looking features from tasks/request2.txt:
1. Secondary Cell Suppression (audit_cell_suppression with secondary_suppression=True)
2. L2 Regularized Multivariable Odds Ratios & Table 5 (consortium_multivariable_risk_factors)
3. Pure DuckDB OHDSI Cohort Diagnostics (consortium_cohort_diagnostics)
4. Internal-External Cross-Validation (IECV) Matrix (consortium_cross_site_validation)
5. Bidirectional OHDSI Circe JSON Exporter (export_to_circe_json / CohortFlow.to_circe_json)
6. End-to-End Manuscript Scaffold Builder (build_manuscript_scaffold)
"""

import json
import os
import tempfile
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    FederatedConsortium,
    audit_cell_suppression,
    consortium_privacy_audit,
    build_schema,
    CohortFlow,
)
from omop_etl.ml import (
    consortium_multivariable_risk_factors,
    consortium_cross_site_validation,
    ConsortiumRiskFactors,
    ConsortiumCrossSiteValidation,
)
from omop_etl.cohort_diagnostics import (
    consortium_cohort_diagnostics,
    ConsortiumCohortDiagnostics,
)
from omop_etl.circe import export_to_circe_json, compile_circe_to_duckdb
from omop_etl.manuscript import build_manuscript_scaffold, ConsortiumManuscript
from omop_duck_db.ml import (
    consortium_multivariable_risk_factors as dd_consortium_multivariable_risk_factors,
    consortium_cross_site_validation as dd_consortium_cross_site_validation,
)
from omop_duck_db.circe import (
    export_to_circe_json as dd_export_to_circe_json,
)


# ---------------------------------------------------------------------------
# Feature 1: Secondary Cell Suppression (Complementary Suppression)
# ---------------------------------------------------------------------------
def test_secondary_cell_suppression_prevents_algebraic_reconstruction():
    # Table with row totals and 1 small count (e.g., 5)
    # If only 5 is masked, subtracting the other cell from Total discloses 5.
    df = pd.DataFrame({
        "Stratum": ["Group A", "Group B", "Total"],
        "Count": [5, 45, 50],
        "Count_Formatted": ["5 (10.0%)", "45 (90.0%)", "50 (100.0%)"],
    })

    # Without secondary suppression: exactly 1 cell is masked
    res_primary = audit_cell_suppression(df, min_cell_count=11, secondary_suppression=False, return_audit=True)
    assert res_primary.primary_count == 2  # 5 and "5 (10.0%)"
    assert res_primary.secondary_count == 0

    # With secondary suppression: second-smallest cell (45) is also masked
    res_sec = audit_cell_suppression(df, min_cell_count=11, secondary_suppression=True, return_audit=True)
    assert res_sec.secondary_count > 0
    t = res_sec.tables["table"]
    # Both 5 and 45 in Count are masked
    assert t.loc[0, "Count"] == "<11"
    assert t.loc[1, "Count"] == "<11"
    assert t.loc[2, "Count"] == 50  # Marginal total preserved


# ---------------------------------------------------------------------------
# Feature 2: L2 Regularized Multivariable Odds Ratios & Table 5
# ---------------------------------------------------------------------------
def test_consortium_multivariable_risk_factors_irls():
    np.random.seed(42)
    n = 200
    age = np.random.normal(65, 10, n)
    diabetes = np.random.binomial(1, 0.3, n)
    chf = np.random.binomial(1, 0.2, n)
    # Collinear feature
    diabetes_dupe = diabetes.copy()
    
    # Outcome with true positive effects
    logits = -2.0 + 0.04 * (age - 65) + 1.2 * diabetes + 1.0 * chf
    prob = 1.0 / (1.0 + np.exp(-logits))
    outcome = (np.random.rand(n) < prob).astype(int)

    df = pd.DataFrame({
        "age": age,
        "diabetes": diabetes,
        "diabetes_dupe": diabetes_dupe,
        "heart_failure": chf,
        "readmission_30d": outcome,
    })

    rf = consortium_multivariable_risk_factors(
        df,
        outcome_col="readmission_30d",
        feature_cols=["age", "diabetes", "diabetes_dupe", "heart_failure"],
        l2_lambda=1.0,
    )

    assert isinstance(rf, ConsortiumRiskFactors)
    tbl = rf.table
    assert len(tbl) > 0
    assert "odds_ratio" in tbl.columns
    assert "ci_95_lower" in tbl.columns
    assert "ci_95_upper" in tbl.columns
    assert "domain" in tbl.columns

    # Check formatting
    assert any("Per 1-SD Increase" in str(u) for u in tbl["unit"])
    assert any("Presence vs. Absence" in str(u) for u in tbl["unit"])

    # Test Quarto and Typst rendering
    qmd = rf.to_quarto()
    assert "{#tbl-risk-factors}" in qmd
    assert "Odds Ratio" in qmd

    typ = rf.to_typst()
    assert "#figure(" in typ
    assert "Table 5" in typ

    # Test Forest Plot generation
    with tempfile.TemporaryDirectory() as td:
        fp_path = os.path.join(td, "forest_plot.png")
        fig = rf.plot_forest(save_path=fp_path)
        try:
            import matplotlib.pyplot as plt
            assert fig is not None
            assert os.path.exists(fp_path)
            plt.close(fig)
        except ImportError:
            assert fig is None


# ---------------------------------------------------------------------------
# Feature 3: Pure DuckDB OHDSI Cohort Diagnostics
# ---------------------------------------------------------------------------
def test_consortium_cohort_diagnostics():
    con = duckdb.connect(":memory:")
    build_schema(con)

    # Insert sample population across calendar years
    con.execute("""
        INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
        VALUES 
            (1, 8507, 1955, 1, 1, 8527, 38003563),
            (2, 8532, 1960, 6, 1, 8527, 38003563),
            (3, 8532, 1975, 3, 1, 8527, 38003563),
            (4, 8507, 1980, 8, 1, 8527, 38003563),
            (5, 8532, 1945, 11, 1, 8527, 38003563)
    """)

    con.execute("""
        INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
        VALUES 
            (1, 1, DATE '2018-01-01', DATE '2023-12-31', 32817),
            (2, 2, DATE '2019-01-01', DATE '2023-12-31', 32817),
            (3, 3, DATE '2018-01-01', DATE '2023-12-31', 32817),
            (4, 4, DATE '2019-01-01', DATE '2023-12-31', 32817),
            (5, 5, DATE '2018-01-01', DATE '2023-12-31', 32817)
    """)

    con.execute("""
        DELETE FROM cohort;
        INSERT INTO cohort VALUES
            (101, 1, DATE '2020-03-15', DATE '2020-06-15'),
            (101, 2, DATE '2020-07-20', DATE '2020-10-20'),
            (101, 3, DATE '2021-01-10', DATE '2021-05-10'),
            (101, 4, DATE '2021-04-12', DATE '2021-08-12'),
            (101, 5, DATE '2022-09-01', DATE '2022-12-01')
    """)

    diag = consortium_cohort_diagnostics(
        con,
        cohort_table="cohort",
        cohort_definition_id=101,
        secondary_suppression=True,
    )

    assert isinstance(diag, ConsortiumCohortDiagnostics)
    assert len(diag.incidence) > 0
    assert len(diag.time_to_event) > 0
    assert len(diag.age_sex_pyramid) > 0

    # Test Quarto & Typst output
    qmd = diag.to_quarto()
    assert "Cohort Diagnostics Summary" in qmd
    assert "Calendar-Year Incidence Rates" in qmd

    typ = diag.to_typst()
    assert "#figure(" in typ
    assert "Cohort Diagnostics" in typ

    # Test Population Pyramid Plot
    with tempfile.TemporaryDirectory() as td:
        pyr_path = os.path.join(td, "pyramid.png")
        fig = diag.plot_pyramid(save_path=pyr_path)
        try:
            import matplotlib.pyplot as plt
            assert fig is not None
            assert os.path.exists(pyr_path)
            plt.close(fig)
        except ImportError:
            assert fig is None

    con.close()


# ---------------------------------------------------------------------------
# Feature 4: Internal-External Cross-Validation (IECV) Matrix
# ---------------------------------------------------------------------------
def test_consortium_cross_site_validation():
    np.random.seed(42)
    # 3 clinical sites
    data = []
    for site_id in ["Site_Alpha", "Site_Beta", "Site_Gamma"]:
        n = 100
        age = np.random.normal(65, 8, n)
        cad = np.random.binomial(1, 0.4, n)
        # Shifted coefficients representing domain shift
        b_age = 0.03 if site_id != "Site_Gamma" else 0.01
        logits = -1.5 + b_age * (age - 65) + 1.0 * cad
        prob = 1.0 / (1.0 + np.exp(-logits))
        y = (np.random.rand(n) < prob).astype(int)
        for i in range(n):
            data.append({
                "site_id": site_id,
                "age": age[i],
                "cad": cad[i],
                "outcome": y[i],
            })
    df = pd.DataFrame(data)

    iecv = consortium_cross_site_validation(
        df,
        site_col="site_id",
        outcome_col="outcome",
        feature_cols=["age", "cad"],
    )

    assert isinstance(iecv, ConsortiumCrossSiteValidation)
    assert iecv.auroc_matrix.shape == (3, 3)
    assert iecv.brier_matrix.shape == (3, 3)
    assert len(iecv.summary_table) == 3
    assert "mean_auroc_external" in iecv.summary_table.columns
    assert "transportability_gap" in iecv.summary_table.columns

    # Test Quarto & Typst
    qmd = iecv.to_quarto()
    assert "{#tbl-iecv}" in qmd
    assert "Internal-External Cross-Validation (IECV) AUROC Matrix" in qmd

    typ = iecv.to_typst()
    assert "#figure(" in typ
    assert "IECV" in typ

    # Test Heatmap
    with tempfile.TemporaryDirectory() as td:
        hm_path = os.path.join(td, "iecv_heatmap.png")
        fig = iecv.plot_heatmap(metric="auroc", save_path=hm_path)
        try:
            import matplotlib.pyplot as plt
            assert fig is not None
            assert os.path.exists(hm_path)
            plt.close(fig)
        except ImportError:
            assert fig is None


# ---------------------------------------------------------------------------
# Feature 5: Bidirectional OHDSI Circe JSON Exporter
# ---------------------------------------------------------------------------
def test_circe_json_export_and_cohort_flow():
    con = duckdb.connect(":memory:")
    build_schema(con)

    flow = (
        CohortFlow(con, anchor="admission", follow_up_days=30)
        .add_step("Adults (>= 18 years)", "EXTRACT(year FROM v.visit_start_date) - p.year_of_birth >= 18")
        .add_step("Type 2 Diabetes Mellitus", "EXISTS (SELECT 1 FROM condition_occurrence co WHERE co.person_id = v.person_id AND co.condition_concept_id IN (201826))")
    )

    circe_json = flow.to_circe_json(
        cohort_name="Adult T2DM Inpatient Cohort",
        author="Consortium Lead",
    )

    circe_dict = json.loads(circe_json)
    assert circe_dict["Title"] == "Adult T2DM Inpatient Cohort"
    assert "PrimaryCriteria" in circe_dict
    assert len(circe_dict["InclusionRules"]) == 2
    assert circe_dict["InclusionRules"][0]["name"] == "Adults (>= 18 years)"
    assert circe_dict["InclusionRules"][1]["name"] == "Type 2 Diabetes Mellitus"

    direct_json = export_to_circe_json(flow, cohort_name="Direct Cohort")
    assert direct_json["Title"] == "Direct Cohort"

    con.close()


# ---------------------------------------------------------------------------
# Feature 6: End-to-End Manuscript Scaffold Builder
# ---------------------------------------------------------------------------
def test_build_manuscript_scaffold():
    tables = {
        "table1": pd.DataFrame({"Var": ["Age", "Female"], "Total": ["65.0 (10.0)", "150 (50.0%)"]}),
        "table2": pd.DataFrame({"Metric": ["Patients", "Readmitted"], "Count": [300, 45]}),
        "table5": pd.DataFrame({"variable": ["Age", "Diabetes"], "odds_ratio": [1.03, 1.85]}),
    }

    # Scaffold for JAMA in Quarto
    scaffold_jama_qmd = build_manuscript_scaffold(
        study_title="Comparative Outcomes in Multicenter Heart Failure",
        target_journal="JAMA",
        format="quarto",
        tables=tables,
    )

    assert isinstance(scaffold_jama_qmd, ConsortiumManuscript)
    qmd_text = scaffold_jama_qmd.document_content
    assert "Comparative Outcomes in Multicenter Heart Failure" in qmd_text
    assert "## Abstract" in qmd_text
    assert "**Importance**:" in qmd_text
    assert "**Objective**:" in qmd_text
    assert "## Methods" in qmd_text
    assert "## Results" in qmd_text
    assert "## Discussion" in qmd_text
    assert "TRIPOD Statement Checklist" in qmd_text

    # Scaffold for Lancet in Typst
    scaffold_lancet_typ = build_manuscript_scaffold(
        study_title="Predictive Transportability across UK Sites",
        target_journal="Lancet",
        format="typst",
        tables=tables,
    )
    typ_text = scaffold_lancet_typ.document_content
    assert "#set document(" in typ_text
    assert "Predictive Transportability across UK Sites" in typ_text
    assert "Interpretation" in typ_text
    assert "TRIPOD" in typ_text

    # Test saving manuscript
    with tempfile.TemporaryDirectory() as td:
        out_file = os.path.join(td, "manuscript.qmd")
        saved = scaffold_jama_qmd.save(out_file)
        assert os.path.exists(saved)
        with open(saved, "r") as f:
            content = f.read()
        assert "Comparative Outcomes" in content
