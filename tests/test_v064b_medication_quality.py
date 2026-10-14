"""Unit tests for v0.6.4b Medication & Quality Guards.

Covers:
1. extract_medication_classes guards (on_unresolved, exclude_forms, route_concept_ids, by_type)
2. audit_medication_strategies additions (both_blind, min_prevalence, explain=True)
3. assert_exposure_coverage (silent-zero guard)
4. explain_medication_disagreement (root cause breakdown with privacy cell suppression)
5. export_medication_crosswalk (Athena/RxNorm export with retired mapping and must_contain check)
6. audit_code_list_coverage (code list omission and share auditor)
7. panel_missingness_audit (panel component missingness consistency across sites)
8. plausibility_report (table-level physiological limits and CI gate)
9. detect_redundant_features (constant ratio, difference, and collinearity detector)
10. dqd_summary (data quality dashboard pass-rate consistency validator)
"""

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl.build_omop_cdm import build_schema
from omop_etl.medication_classes import (
    assert_exposure_coverage,
    audit_medication_strategies,
    explain_medication_disagreement,
    export_medication_crosswalk,
    extract_medication_classes,
)
from omop_etl.quality import (
    audit_code_list_coverage,
    detect_redundant_features,
    dqd_summary,
    panel_missingness_audit,
    plausibility_report,
)


@pytest.fixture
def mock_med_cdm():
    """Sets up an in-memory DuckDB CDM with synthetic vocabulary, cohort, and drug exposure."""
    con = duckdb.connect(":memory:")
    build_schema(con)

    # 1. Vocab concepts: ATC class, oral beta blocker, ophthalmic beta blocker, retired code
    con.execute("""
        INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
        VALUES 
        -- ATC class: C07AA Beta blocking agents
        (21601664, 'Beta blocking agents, non-selective', 'Drug', 'ATC', 'ATC 4th', 'C', 'C07AA', DATE '1970-01-01', DATE '2099-12-31'),
        -- Standard Oral Timolol
        (1347384, 'Timolol 10 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '855332', DATE '1970-01-01', DATE '2099-12-31'),
        -- Standard Ophthalmic Timolol
        (1347390, 'Timolol 0.5 % Ophthalmic Solution', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '313299', DATE '1970-01-01', DATE '2099-12-31'),
        -- Retired Timolol (maps to 1347384)
        (9999001, 'Old Timolol Oral Form', 'Drug', 'RxNorm', 'Clinical Drug', NULL, 'OLD-855', DATE '1970-01-01', DATE '2099-12-31'),
        -- Unrelated antibiotic
        (1713332, 'Amoxicillin 500 MG Oral Capsule', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '308182', DATE '1970-01-01', DATE '2099-12-31');
    """)

    # Concept ancestors
    con.execute("""
        INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
        VALUES 
        (21601664, 21601664, 0, 0),
        (21601664, 1347384, 1, 1),
        (21601664, 1347390, 1, 1);
    """)

    # Concept relationships (Maps to)
    con.execute("""
        INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, valid_start_date, valid_end_date)
        VALUES 
        (9999001, 1347384, 'Maps to', DATE '1970-01-01', DATE '2099-12-31');
    """)

    # Persons (20 patients across SiteA and SiteB)
    for pid in range(1, 21):
        site = "SiteA" if pid <= 10 else "SiteB"
        con.execute(f"""
            INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value)
            VALUES ({pid}, 8507, 1960, 8527, 38003563, 'PAT-{site}-{pid}')
        """)

    # Cohort
    for pid in range(1, 21):
        site = "SiteA" if pid <= 10 else "SiteB"
        con.execute(f"""
            INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
            VALUES (1, {pid}, DATE '2023-01-05', DATE '2023-01-10')
        """)

    # Drug exposures:
    # SiteA: patients 1-5 have oral Timolol (drug_concept_id=1347384, source_value='855332')
    # SiteA: patients 6-7 have ophthalmic Timolol (drug_concept_id=1347390, source_value='313299')
    # SiteA: patients 8-10 have amoxicillin
    for pid in range(1, 6):
        con.execute(f"""
            INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
            VALUES ({pid}, {pid}, 1347384, DATE '2023-01-02', DATE '2023-01-05', 38000177, '855332', 4132161)
        """)
    for pid in range(6, 8):
        con.execute(f"""
            INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
            VALUES ({pid}, {pid}, 1347390, DATE '2023-01-02', DATE '2023-01-05', 38000177, '313299', 4184451)
        """)
    for pid in range(8, 11):
        con.execute(f"""
            INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
            VALUES ({pid}, {pid}, 1713332, DATE '2023-01-02', DATE '2023-01-05', 38000177, '308182', 4132161)
        """)
    # SiteB has no beta blockers at all (silent zero in SiteB)
    for pid in range(11, 21):
        con.execute(f"""
            INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
            VALUES ({pid}, {pid}, 1713332, DATE '2023-01-02', DATE '2023-01-05', 38000177, '308182', 4132161)
        """)

    # Create views with site
    con.execute("""
        CREATE OR REPLACE VIEW v_cohort AS 
        SELECT 
            c.subject_id, 
            c.cohort_start_date, 
            c.cohort_end_date, 
            CASE WHEN c.subject_id <= 10 THEN 'SiteA' ELSE 'SiteB' END AS site
        FROM cohort c;
    """)

    con.execute("""
        CREATE OR REPLACE VIEW v_drug_exposure AS 
        SELECT 
            de.*, 
            CASE WHEN de.person_id <= 10 THEN 'SiteA' ELSE 'SiteB' END AS site
        FROM drug_exposure de;
    """)

    yield con
    con.close()


# -----------------------------------------------------------------------------
# 1. Silent-zero guard & extract_medication_classes options
# -----------------------------------------------------------------------------
def test_extract_medication_classes_unresolved_guard(mock_med_cdm):
    # Missing ATC code with on_unresolved='raise' must fail loudly
    with pytest.raises(ValueError, match="ATC codes.*resolved to no concept"):
        extract_medication_classes(
            mock_med_cdm,
            "v_cohort",
            {"missing_class": {"atc_codes": ["NONEXISTENT_ATC"]}},
            strategy="standard",
            on_unresolved="raise",
        )

    # Missing ATC code with on_unresolved='warn' issues warning
    with pytest.warns(UserWarning, match="resolved to no concept"):
        df = extract_medication_classes(
            mock_med_cdm,
            "v_cohort",
            {"missing_class": {"atc_codes": ["NONEXISTENT_ATC"]}},
            strategy="standard",
            on_unresolved="warn",
        )
        assert "missing_class" in df.columns
        assert df["missing_class"].sum() == 0


def test_extract_medication_classes_exclude_forms_and_route(mock_med_cdm):
    specs = {"beta_blocker": {"atc_codes": ["C07AA"]}}

    # Without exclude_forms, patients 1-7 are standard positive (oral + ophthalmic)
    df_all = extract_medication_classes(mock_med_cdm, "v_cohort", specs, strategy="standard")
    site_a_all = df_all[df_all["site"] == "SiteA"]["beta_blocker"].sum()
    assert site_a_all == 7

    # With exclude_forms=['Ophthalmic'], patients 6-7 are excluded -> 5 positive
    df_no_eye = extract_medication_classes(
        mock_med_cdm, "v_cohort", specs, strategy="standard", exclude_forms=["Ophthalmic"]
    )
    site_a_no_eye = df_no_eye[df_no_eye["site"] == "SiteA"]["beta_blocker"].sum()
    assert site_a_no_eye == 5

    # With route filter for oral (4132161)
    df_oral_route = extract_medication_classes(
        mock_med_cdm, "v_cohort", specs, strategy="standard", route_concept_ids=[4132161]
    )
    assert df_oral_route[df_oral_route["site"] == "SiteA"]["beta_blocker"].sum() == 5


def test_extract_medication_classes_by_type(mock_med_cdm):
    specs = {"beta_blocker": {"atc_codes": ["C07AA"]}}
    df = extract_medication_classes(
        mock_med_cdm, "v_cohort", specs, strategy="standard", by_type=True
    )
    assert "beta_blocker_type_38000177" in df.columns
    assert df[df["site"] == "SiteA"]["beta_blocker_type_38000177"].sum() == 7


# -----------------------------------------------------------------------------
# 2. audit_medication_strategies additions & both_blind
# -----------------------------------------------------------------------------
def test_audit_medication_strategies_both_blind_and_min_prev(mock_med_cdm):
    specs = {"beta_blocker": {"atc_codes": ["C07AA"]}}
    # Crosswalk covering only oral timolol 855332
    xw = pd.DataFrame({"source_value": ["855332"], "drug_class": ["beta_blocker"]})

    flags = extract_medication_classes(
        mock_med_cdm, "v_cohort", specs, strategy="both", crosswalk=xw
    )

    audit = audit_medication_strategies(
        flags,
        classes=["beta_blocker"],
        site_col="site",
        min_n=5,
        min_prevalence={"beta_blocker": 0.30},
    )

    # SiteB has 0 under both strategies -> must flag 'both_blind'
    site_b_row = audit[audit["site"] == "SiteB"].iloc[0]
    assert site_b_row["flagged"] is True or site_b_row["flagged"] == 1
    assert "both_blind" in site_b_row["reason"]
    assert "below_min_prevalence" in site_b_row["reason"]


def test_assert_exposure_coverage():
    flags = pd.DataFrame({
        "person_id": range(1, 21),
        "site": ["SiteA"] * 10 + ["SiteB"] * 10,
        "statin": [1] * 5 + [0] * 5 + [0] * 10,
    })

    # Passes when min_n is higher than site size
    assert_exposure_coverage(flags, ["statin"], min_n=50, site_col="site")

    # Fails when SiteB has 0 coverage with min_n=10
    with pytest.raises(AssertionError, match="assert_exposure_coverage failed.*SiteB"):
        assert_exposure_coverage(flags, ["statin"], min_n=10, site_col="site")


# -----------------------------------------------------------------------------
# 3. Disagreement Explainer & Crosswalk Exporter
# -----------------------------------------------------------------------------
def test_explain_medication_disagreement(mock_med_cdm):
    specs = {"beta_blocker": {"atc_codes": ["C07AA"]}}
    # Crosswalk only maps oral (855332). Patients 6 and 7 (ophthalmic 313299) are standard_only!
    xw = pd.DataFrame({"source_value": ["855332"], "drug_class": ["beta_blocker"]})

    flags = extract_medication_classes(
        mock_med_cdm, "v_cohort", specs, strategy="both", crosswalk=xw
    )

    # Explain disagreements with small-cell suppression
    expl = explain_medication_disagreement(
        mock_med_cdm, flags, specs, min_cell_count=5, mask_value="<5"
    )

    assert not expl.empty
    assert "disagreement_type" in expl.columns
    std_only = expl[expl["disagreement_type"] == "standard_only"]
    assert not std_only.empty
    assert (std_only["drug_concept_id"] == 1347390).any()
    # Cell count is 2 (<5), so it should be suppressed
    assert bool(std_only.iloc[0]["is_suppressed"]) is True
    assert std_only.iloc[0]["patient_count"] == "<5"


def test_export_medication_crosswalk(mock_med_cdm, tmp_path):
    specs = {"beta_blocker": {"atc_codes": ["C07AA"]}}
    out_csv = tmp_path / "beta_blocker_crosswalk.csv"

    # Export crosswalk including retired concepts
    xw = export_medication_crosswalk(
        mock_med_cdm,
        specs,
        output_path=out_csv,
        include_retired=True,
        must_contain=["855332", "OLD-855"],
    )

    assert out_csv.exists()
    assert len(xw) >= 3
    assert "source" in xw.columns
    sources = set(xw["source"])
    assert "current" in sources
    assert "maps_to" in sources
    assert "OLD-855" in list(xw["source_value"])

    # Fail fast if required code is missing
    with pytest.raises(ValueError, match="missing required codes"):
        export_medication_crosswalk(
            mock_med_cdm, specs, must_contain=["NON_EXISTENT_CODE"]
        )


# -----------------------------------------------------------------------------
# 4. Code-list coverage audit
# -----------------------------------------------------------------------------
def test_audit_code_list_coverage(mock_med_cdm):
    # Supplied code list only contains oral timolol ('855332'), omitting ophthalmic ('313299')
    rep = audit_code_list_coverage(
        mock_med_cdm,
        domain="drug",
        supplied_codes=["855332"],
        table_name="v_drug_exposure",
        by_site=True,
        min_share=0.10,
    )

    assert not rep.empty
    assert "coverage_share" in rep.columns
    site_a_rep = rep[rep["site"] == "SiteA"]
    # SiteA has 10 total drugs (5 oral, 2 ophthalmic, 3 amoxicillin).
    # Covered is 5/10 = 0.50.
    assert site_a_rep["covered_rows"].iloc[0] == 5
    assert site_a_rep["coverage_share"].iloc[0] == 0.50

    # High frequency omissions flagged
    flagged = site_a_rep[site_a_rep["flagged"]]
    assert not flagged.empty


# -----------------------------------------------------------------------------
# 5. Panel missingness audit
# -----------------------------------------------------------------------------
def test_panel_missingness_audit():
    # Synthetic BMP panel where SiteA BUN is 90% missing while creatinine is 5% missing
    n = 100
    df = pd.DataFrame({
        "site": ["SiteA"] * n + ["SiteB"] * n,
        "creatinine": [1.0] * 95 + [np.nan] * 5 + [1.0] * 90 + [np.nan] * 10,
        "bun": [20.0] * 10 + [np.nan] * 90 + [20.0] * 90 + [np.nan] * 10,
        "sodium": [140.0] * 95 + [np.nan] * 5 + [140.0] * 90 + [np.nan] * 10,
        "potassium": [4.0] * 95 + [np.nan] * 5 + [4.0] * 90 + [np.nan] * 10,
    })

    audit = panel_missingness_audit(
        df,
        panels={"BMP": ["creatinine", "bun", "sodium", "potassium"]},
        by="site",
        threshold=0.20,
    )

    assert not audit.empty
    bun_site_a = audit[(audit["site"] == "SiteA") & (audit["analyte"] == "bun")]
    assert not bun_site_a.empty
    assert bool(bun_site_a.iloc[0]["flagged"])
    assert "panel_missingness_gap" in bun_site_a.iloc[0]["reason"]


# -----------------------------------------------------------------------------
# 6. Table-level plausibility report
# -----------------------------------------------------------------------------
def test_plausibility_report_and_ci_gate():
    # Impossible potassium (mean 60) and extreme SD
    df = pd.DataFrame({
        "potassium": [60.0, 62.0, 58.0, 65.0, 59.0],  # normal is 2.5 - 7.0
        "creatinine": [1.0, 1.2, 0.9, 1.1, 1.0],
        "sodium": [140.0, 142.0, 138.0, 141.0, 139.0],
    })

    rep = plausibility_report(df)
    assert not rep.empty
    k_flags = rep[rep["feature"] == "potassium"]
    assert not k_flags.empty
    assert (k_flags["reason"] == "mean_outside_range").any()

    # CI gate raises ValueError on flagged bounds
    with pytest.raises(ValueError, match="Plausibility CI gate failed"):
        plausibility_report(df, ci_gate=True)


# -----------------------------------------------------------------------------
# 7. Redundant feature detector
# -----------------------------------------------------------------------------
def test_detect_redundant_features():
    # Hematocrit exactly 3 * Hemoglobin
    n = 50
    hgb = np.random.uniform(10.0, 16.0, size=n)
    hct = 3.0 * hgb
    bun = np.random.uniform(10.0, 40.0, size=n)

    df = pd.DataFrame({"hemoglobin": hgb, "hematocrit": hct, "bun": bun})
    red = detect_redundant_features(df, tol=1e-3)

    assert not red.empty
    assert "constant_ratio" in list(red["redundancy_type"])
    row = red[red["redundancy_type"] == "constant_ratio"].iloc[0]
    assert np.isclose(row["metric_value"], 3.0, atol=0.01) or np.isclose(row["metric_value"], 1.0 / 3.0, atol=0.01)


# -----------------------------------------------------------------------------
# 8. DQD summary consistency
# -----------------------------------------------------------------------------
def test_dqd_summary_consistency():
    # 3 checks: 2 passed, 1 failed. Actual pass rate is 2/3 = 0.6667.
    # If document reports 0.85, it is inconsistent!
    dqd_records = [
        {"check_id": "C1", "execution_status": "PASSED", "site": "SiteA", "pass_rate": 0.85},
        {"check_id": "C2", "execution_status": "PASSED", "site": "SiteA", "pass_rate": 0.85},
        {"check_id": "C3", "execution_status": "FAILED", "site": "SiteA", "pass_rate": 0.85},
    ]

    summary = dqd_summary(dqd_records, by_site=True)
    assert not summary.empty
    assert summary.iloc[0]["total_checks"] == 3
    assert summary.iloc[0]["passed"] == 2
    assert summary.iloc[0]["failed"] == 1
    assert np.isclose(summary.iloc[0]["pass_rate"], 2.0 / 3.0, atol=1e-3)
    assert bool(summary.iloc[0]["flagged"])
    assert "reported_rate_inconsistent" in summary.iloc[0]["reason"]
