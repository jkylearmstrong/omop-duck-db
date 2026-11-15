"""Unit tests covering the 3 modular features from tasks/request1.txt:
1. Automated Privacy-Preserving Cell Suppression Auditor (audit_cell_suppression / consortium_privacy_audit)
2. Direct Quarto / Typst Publication Styler (Table1Generator.to_quarto(), to_typst(), table1_to_quarto(), table1_to_typst())
3. Built-in Tree Model Explainability (ConsortiumExplainability, consortium_explainability)
"""

import os
import tempfile
import numpy as np
import pandas as pd
import pytest

from omop_etl import (
    FederatedConsortium,
    Table1Generator,
    build_schema,
    audit_cell_suppression,
    consortium_privacy_audit,
    table1_to_quarto,
    table1_to_typst,
    export_table1,
)
from omop_etl.federation import PrivacyAuditResult
from omop_etl.explainability import (
    ConsortiumExplainability,
    consortium_explainability,
)
from omop_duck_db.federation import (
    audit_cell_suppression as dd_audit_cell_suppression,
    consortium_privacy_audit as dd_consortium_privacy_audit,
)
from omop_duck_db.ml import (
    ConsortiumExplainability as DDConsortiumExplainability,
    consortium_explainability as dd_consortium_explainability,
)
from omop_duck_db.table1 import (
    table1_to_quarto as dd_table1_to_quarto,
    table1_to_typst as dd_table1_to_typst,
)


# ---------------------------------------------------------------------------
# Feature A: Automated Privacy-Preserving Cell Suppression Auditor
# ---------------------------------------------------------------------------
def test_privacy_audit_single_dataframe():
    df = pd.DataFrame({
        "characteristic": ["Age mean (SD)", "Female n (%)", "Diabetes n (%)", "Rare Event n (%)", "P-Value"],
        "Overall": ["65.2 (10.1)", "500 (50.0%)", "120 (12.0%)", "8 (0.8%)", "0.042"],
        "Cohort_A": ["64.8 (9.8)", "260 (52.0%)", "65 (13.0%)", "5 (1.0%)", "0.150"],
        "Cohort_B": ["65.6 (10.4)", "240 (48.0%)", "55 (11.0%)", "3 (0.6%)", "0.210"],
        "raw_counts": [1000, 500, 120, 8, 1],
    })

    result = audit_cell_suppression(df, min_cell_count=11, mask_value="<11")
    assert isinstance(result, pd.DataFrame)

    # Rare event cells (8, 5, 3) must be suppressed to "<11"
    assert result.loc[3, "Overall"] == "<11"
    assert result.loc[3, "Cohort_A"] == "<11"
    assert result.loc[3, "Cohort_B"] == "<11"
    assert result.loc[3, "raw_counts"] == "<11"
    assert result.loc[4, "raw_counts"] == "<11"

    # Non-rare cells (>= 11) and continuous metrics must remain intact
    assert result.loc[0, "Overall"] == "65.2 (10.1)"
    assert result.loc[1, "Overall"] == "500 (50.0%)"
    assert result.loc[2, "Cohort_A"] == "65 (13.0%)"
    assert result.loc[4, "Overall"] == "0.042"


def test_privacy_audit_dict_and_privacy_audit_result():
    t1 = pd.DataFrame({
        "var": ["Hypertension", "UltraRareCondition"],
        "site_1": ["45 (45.0%)", "4 (4.0%)"],
        "site_2": ["55 (55.0%)", "2 (2.0%)"],
    })
    t2 = pd.DataFrame({
        "group": ["Arm A", "Arm B"],
        "adverse_events": [15, 6],
    })

    tables = {"table1": t1, "table2": t2}
    audited = consortium_privacy_audit(tables, min_cell_count=10, mask_value="<10")

    assert isinstance(audited, PrivacyAuditResult)
    assert not audited.is_clean
    assert audited.violation_count == 3

    # Direct dictionary subscripting
    clean_t1 = audited["table1"]
    clean_t2 = audited["table2"]

    assert clean_t1.loc[1, "site_1"] == "<10"
    assert clean_t1.loc[1, "site_2"] == "<10"
    assert clean_t1.loc[0, "site_1"] == "45 (45.0%)"

    assert clean_t2.loc[1, "adverse_events"] == "<10"
    assert clean_t2.loc[0, "adverse_events"] == 15

    # Summary table
    sum_df = audited.summary()
    assert isinstance(sum_df, pd.DataFrame)
    assert len(sum_df) == 2
    assert "violations" in sum_df.columns
    assert sum_df[sum_df["table_name"] == "table1"]["violations"].iloc[0] == 2
    assert sum_df[sum_df["table_name"] == "table2"]["violations"].iloc[0] == 1


def test_privacy_audit_on_clean_table():
    df = pd.DataFrame({
        "var": ["A", "B"],
        "count": [100, 25],
    })
    audited = audit_cell_suppression({"clean": df}, min_cell_count=11)
    assert audited.is_clean
    assert audited.violation_count == 0


def test_privacy_audit_consortium_integration():
    import duckdb
    temp_dir = tempfile.mkdtemp()
    db_a = os.path.join(temp_dir, "cons_a.duckdb")
    con = duckdb.connect(db_a)
    build_schema(con)
    con.close()

    cons = FederatedConsortium({"SiteA": db_a})
    df = pd.DataFrame({"count": [50, 4, 120]})
    res = cons.audit_cell_suppression({"df": df}, min_cell_count=11)
    assert isinstance(res, PrivacyAuditResult)
    assert res["df"]["count"].iloc[1] == "<11"
    assert res["df"]["count"].iloc[0] == 50
    cons.close()


def test_privacy_audit_omop_duck_db_parity():
    df = pd.DataFrame({"n": [2, 15, 8]})
    res = dd_audit_cell_suppression(df, min_cell_count=10)
    assert res["n"].iloc[0] == "<10"
    assert res["n"].iloc[1] == 15
    assert res["n"].iloc[2] == "<10"


# ---------------------------------------------------------------------------
# Feature B: Direct Quarto / Typst Publication Styler
# ---------------------------------------------------------------------------
def test_table1_to_quarto_and_typst_direct():
    df = pd.DataFrame({
        "Characteristic": ["Age, median [IQR]", "Serum Creatinine (mg/dL)", "Female, n (%)"],
        "Overall (N=500)": ["64.0 [52.0, 75.0]", "1.10 [0.85, 1.45]", "245 (49.0%)"],
        "Cohort 1 (N=250)": ["63.0 [51.0, 74.0]", "1.08 [0.82, 1.40]", "120 (48.0%)"],
        "Cohort 2 (N=250)": ["65.0 [53.0, 76.0]", "1.12 [0.88, 1.50]", "125 (50.0%)"],
        "p-value": ["0.420", "0.185", "0.724"],
    })

    # Quarto export
    qmd = table1_to_quarto(
        df,
        caption="Demographic and Clinical Baseline Characteristics",
        label="tbl-baseline",
        loinc_footnotes={"Serum Creatinine": "2160-0"},
    )
    assert isinstance(qmd, str)
    assert "#| label: tbl-baseline" in qmd
    assert 'tbl-cap: "Demographic and Clinical Baseline Characteristics"' in qmd
    assert "| Characteristic |" in qmd
    assert "Serum Creatinine" in qmd

    # Typst export
    typst = table1_to_typst(
        df,
        caption="Demographic and Clinical Baseline Characteristics",
        label="tbl-baseline",
        loinc_footnotes={"Serum Creatinine": "2160-0"},
    )
    assert isinstance(typst, str)
    assert "#figure(" in typst
    assert "caption: [Demographic and Clinical Baseline Characteristics]" in typst
    assert "<tbl-baseline>" in typst
    assert "#table(" in typst
    assert "2160-0" in typst


def test_table1_generator_quarto_and_typst_methods():
    import duckdb
    con = duckdb.connect(":memory:")
    build_schema(con)
    # Populate minimal dummy person table
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8532, 1960, 8527, 0), (2, 8507, 1975, 8527, 0)")

    gen = Table1Generator(con=con)
    df = pd.DataFrame({
        "Variable": ["Age", "Sex"],
        "Value": ["55 [45, 65]", "1 (50.0%)"],
    })

    qmd = gen.to_quarto(df, caption="Study Cohort", label="tbl-one")
    assert "#| label: tbl-one" in qmd
    assert 'tbl-cap: "Study Cohort"' in qmd

    typ = gen.to_typst(df, caption="Study Cohort", label="tbl-one")
    assert "#table(" in typ
    assert "<tbl-one>" in typ

    # export_table1 wrapper
    qmd_exp = export_table1(df, format="quarto", caption="Exported Cohort")
    assert 'tbl-cap: "Exported Cohort"' in qmd_exp

    typ_exp = export_table1(df, format="typst", caption="Exported Cohort")
    assert "#table(" in typ_exp
    con.close()


def test_table1_duckdb_module_reexport():
    df = pd.DataFrame({"Var": ["A"], "Val": ["10 [5, 15]"]})
    qmd = dd_table1_to_quarto(df)
    typ = dd_table1_to_typst(df)
    assert "| Var |" in qmd
    assert "#table(" in typ


# ---------------------------------------------------------------------------
# Feature C: Built-in Tree Model Explainability
# ---------------------------------------------------------------------------
class DummyTreeModel:
    """Mock fitted tree model providing feature_importances_ and predict_proba."""
    def __init__(self, n_features: int = 4):
        self.n_features = n_features
        if n_features == 4:
            self.feature_importances_ = np.array([0.45, 0.30, 0.15, 0.10])
        elif n_features == 3:
            self.feature_importances_ = np.array([0.50, 0.35, 0.15])
        else:
            self.feature_importances_ = np.ones(n_features) / n_features

    def predict_proba(self, X):
        X = np.asarray(X)
        score = X @ self.feature_importances_
        prob = 1.0 / (1.0 + np.exp(-score))
        return np.column_stack([1.0 - prob, prob])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def test_consortium_explainability_basic():
    np.random.seed(42)
    n = 60
    X = np.random.randn(n, 4)
    df_X = pd.DataFrame(X, columns=["age", "bmi", "creatinine", "glucose"])
    model = DummyTreeModel(4)

    expl = consortium_explainability(
        model=model,
        X=df_X,
        method="gain",
    )

    assert isinstance(expl, ConsortiumExplainability)
    assert len(expl.feature_names) == 4
    assert expl.method in ("gain", "feature_importances_")

    # Importance table
    imp = expl.importance
    assert isinstance(imp, pd.DataFrame)
    assert len(imp) == 4
    assert list(imp["rank"]) == [1, 2, 3, 4]
    assert imp["feature"].iloc[0] == "age"  # Highest importance (0.45)
    assert "direction" in imp.columns
    assert "cohort_median" in imp.columns
    assert "cohort_iqr" in imp.columns

    # TRIPOD Item 13b summary table
    tripod = expl.tripod_summary
    assert isinstance(tripod, pd.DataFrame)
    assert "TRIPOD_Rank" in tripod.columns
    assert "Candidate_Predictor" in tripod.columns
    assert "Univariable_Direction" in tripod.columns
    assert "Reporting_Standard" in tripod.columns
    assert tripod["Reporting_Standard"].iloc[0] == "TRIPOD Item 13b / STROBE Item 15a"


def test_consortium_explainability_multi_site_drift():
    np.random.seed(123)
    n = 80
    X = np.random.randn(n, 3)
    df_X = pd.DataFrame(X, columns=["systolic_bp", "hba1c", "ldl"])
    model = DummyTreeModel(3)
    model.feature_importances_ = np.array([0.5, 0.35, 0.15])

    site_labels = ["SiteA"] * 40 + ["SiteB"] * 40

    expl = consortium_explainability(
        model=model,
        X=df_X,
        site_labels=site_labels,
    )

    drift = expl.site_drift
    assert drift is not None
    assert isinstance(drift, pd.DataFrame)
    assert "feature" in drift.columns
    assert "SiteA" in drift.columns
    assert "SiteB" in drift.columns
    assert "rank_SiteA" in drift.columns
    assert "rank_SiteB" in drift.columns
    assert "overall_rank" in drift.columns


def test_consortium_explainability_permutation_fallback():
    np.random.seed(99)
    X = np.random.randn(40, 3)
    model = DummyTreeModel(3)
    # Force permutation method
    expl = consortium_explainability(
        model=model,
        X=X,
        feature_names=["f1", "f2", "f3"],
        method="permutation",
        n_permutations=3,
    )
    assert expl.method == "permutation"
    assert len(expl.importance) == 3


def test_consortium_explainability_plotting_smoke():
    np.random.seed(42)
    X = np.random.randn(30, 3)
    model = DummyTreeModel(3)
    expl = consortium_explainability(model, X, feature_names=["v1", "v2", "v3"])

    # plot_importance should not raise an error even without display
    fig_imp = expl.plot_importance(show=False)
    # plot_beeswarm should not raise an error
    fig_bee = expl.plot_beeswarm(show=False)
    fig_def = expl.plot(show=False)

    # In environments where matplotlib is installed, fig is returned; otherwise None
    try:
        import matplotlib.pyplot as plt
        assert fig_imp is not None
        assert fig_bee is not None
        plt.close("all")
    except ImportError:
        assert fig_imp is None


def test_explainability_omop_duck_db_parity():
    np.random.seed(10)
    X = np.random.randn(20, 2)
    expl = dd_consortium_explainability(
        model=None,
        X=X,
        feature_names=["var1", "var2"],
    )
    assert isinstance(expl, DDConsortiumExplainability)
    assert len(expl.feature_names) == 2
