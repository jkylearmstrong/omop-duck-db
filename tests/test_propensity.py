"""Unit tests for Propensity Score and IPTW Weighting Module."""

import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl.propensity import (
    generate_propensity_weights,
    PropensityResult,
)


@pytest.fixture
def synthetic_confounded_cohort():
    """Generates a synthetic observational cohort with known confounding.
    
    Treated patients (target_id=1) are systematically older, more hypertensive,
    and have higher baseline BMI than comparator patients (comparator_id=2).
    """
    np.random.seed(42)
    n = 2000

    # Covariates
    age = np.random.normal(60, 10, n)
    hypertension = np.random.binomial(1, 0.4, n)
    diabetes = np.random.binomial(1, 0.25, n)
    bmi = np.random.normal(28, 5, n)

    # Treatment assignment model with strong confounding
    # log-odds(Z=1) = -2.0 + 0.05 * age + 0.8 * hypertension + 0.6 * diabetes + 0.04 * (bmi - 28)
    log_odds = -2.5 + 0.04 * (age - 50) + 0.7 * hypertension + 0.5 * diabetes + 0.03 * (bmi - 28)
    prob_treatment = 1.0 / (1.0 + np.exp(-log_odds))
    treatment = np.random.binomial(1, prob_treatment)

    # Map treatment to cohort_definition_id: 1 = target, 2 = comparator
    cohort_def_id = np.where(treatment == 1, 1, 2)

    df = pd.DataFrame({
        "subject_id": np.arange(1, n + 1),
        "cohort_definition_id": cohort_def_id,
        "age": age,
        "hypertension": hypertension,
        "diabetes": diabetes,
        "bmi": bmi,
    })
    return df


def test_propensity_weights_generation(synthetic_confounded_cohort):
    con = duckdb.connect(":memory:")
    con.register("cohort", synthetic_confounded_cohort)

    res = generate_propensity_weights(
        con=con,
        cohort_table="cohort",
        target_id=1,
        comparator_id=2,
        covariate_cols=["age", "hypertension", "diabetes", "bmi"],
    )

    assert isinstance(res, PropensityResult)
    data = res.data
    assert "propensity_score" in data.columns
    assert "iptw_ate" in data.columns
    assert "iptw_att" in data.columns
    assert "iptw_stabilized_ate" in data.columns

    # Valid ranges
    assert (data["propensity_score"] > 0).all()
    assert (data["propensity_score"] < 1).all()
    assert (data["iptw_ate"] >= 1.0).all()
    assert (data["iptw_att"] >= 0.0).all()

    # Pre-weighting has substantial imbalance (pre_SMD > 0.15)
    balance = res.balance
    assert (balance["pre_smd"] > 0.15).any()

    # Acceptance criteria: maximum post-weighting SMD < 0.10
    assert res.max_post_smd < 0.10, f"Max post SMD was {res.max_post_smd}, expected < 0.10"
    for _, row in balance.iterrows():
        assert row["post_smd_ate"] < 0.10, f"Covariate {row['covariate']} post-SMD: {row['post_smd_ate']}"


def test_propensity_dataframe_input(synthetic_confounded_cohort):
    # Test passing pandas DataFrame directly without DuckDB connection
    res = generate_propensity_weights(
        cohort_table=synthetic_confounded_cohort,
        target_id=1,
        comparator_id=2,
    )
    assert res.max_post_smd < 0.10
    assert len(res.data) == len(synthetic_confounded_cohort)


def test_propensity_to_duckdb(synthetic_confounded_cohort):
    con = duckdb.connect(":memory:")
    res = generate_propensity_weights(
        cohort_table=synthetic_confounded_cohort,
        target_id=1,
        comparator_id=2,
    )
    res.to_duckdb(con, "weighted_cohort")

    counts = con.execute("SELECT count(*), avg(iptw_ate) FROM weighted_cohort").fetchone()
    assert counts[0] == len(synthetic_confounded_cohort)
    assert counts[1] > 1.0
