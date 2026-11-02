"""N9-N14 of the portable-cohort / all-stays requests."""
import duckdb
import numpy as np
import pandas as pd
import pytest

from omop_etl import consortium_cross_site_duplicates, extract_patient_features
from omop_etl.comorbidity import _resolve_concept_ancestor
from omop_etl.ml import consortium_cross_site_validation, consortium_multivariable_risk_factors


# ---- N9: concept_ancestor in an arbitrarily named attached catalog -----------------------------------

def test_n9_finds_concept_ancestor_in_any_attached_catalog(tmp_path):
    vocab = tmp_path / "vocab.duckdb"
    v = duckdb.connect(str(vocab))
    v.execute("CREATE TABLE concept_ancestor AS SELECT 1 AS ancestor_concept_id, 2 AS descendant_concept_id")
    v.close()
    con = duckdb.connect()
    ref, why = _resolve_concept_ancestor(con)
    assert ref is None and "attached catalog" in why
    con.execute(f"ATTACH '{vocab.as_posix()}' AS my_site_vocab (READ_ONLY)")
    ref, _ = _resolve_concept_ancestor(con)
    assert ref is not None and "my_site_vocab" in ref
    assert con.execute(f"SELECT count(*) FROM {ref}").fetchone()[0] == 1


# ---- N10: features keyed by stay -----------------------------------------------------------------------

def _stay_cohort():
    con = duckdb.connect()
    con.execute("CREATE TABLE person AS SELECT 1 AS person_id, 1960 AS year_of_birth, NULL::INT AS month_of_birth, "
                "NULL::INT AS day_of_birth, 8507 AS gender_concept_id, 0 AS race_concept_id, 0 AS ethnicity_concept_id")
    con.execute("CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES (1, 1, DATE '2020-01-05'), "
                "(2, 1, DATE '2020-03-05')) t(condition_occurrence_id, person_id, condition_start_date)")
    con.execute("CREATE TABLE stays AS SELECT * FROM (VALUES "
                "(1, DATE '2020-01-10', DATE '2020-01-12', 101), (1, DATE '2020-03-10', DATE '2020-03-12', 102)) "
                "t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id)")
    return con


def test_n10_keyed_by_stay():
    con = _stay_cohort()
    df = extract_patient_features(con, cohort_table="stays", index_key="visit_occurrence_id",
                                  include_drugs=False, include_procedures=False, include_measurements=False,
                                  lookback_days=(30,))
    assert list(df["visit_occurrence_id"]) == [101, 102]
    assert df["visit_occurrence_id"].is_unique
    assert list(df["condition_count_30d"]) == [1, 1]


def test_n10_requires_the_column_and_a_valid_key():
    con = _stay_cohort()
    con.execute("CREATE TABLE plain AS SELECT subject_id, cohort_start_date, cohort_end_date FROM stays")
    with pytest.raises(ValueError, match="visit_occurrence_id"):
        extract_patient_features(con, cohort_table="plain", index_key="visit_occurrence_id")
    with pytest.raises(ValueError, match="index_key"):
        extract_patient_features(con, cohort_table="stays", index_key="nonsense")


# ---- N11: cross-site duplicates ------------------------------------------------------------------------

def test_n11_cross_site_duplicates_counts_only():
    ids = {"A": [f"p{i}" for i in range(40)], "B": [f"p{i}" for i in range(20, 60)], "C": ["z1", "z2"]}
    out = consortium_cross_site_duplicates(ids, key="secret", min_cell_count=11)
    ab = out[(out.site_1 == "A") & (out.site_2 == "B")].iloc[0]
    assert ab.shared_patients == "20"
    ac = out[(out.site_1 == "A") & (out.site_2 == "C")].iloc[0]
    assert ac.shared_patients == "0" and not ac.is_suppressed
    total = out[out.site_1 == "ALL"].iloc[0]
    assert total.n_site_1 == 62 and total.shared_patients == "20"
    assert "p1" not in out.to_string()
    small = consortium_cross_site_duplicates({"A": ["x", "y"], "B": ["y"]}, key=b"k")
    assert small[small.site_1 == "A"].iloc[0].shared_patients == "<11"
    with pytest.raises(ValueError):
        consortium_cross_site_duplicates(ids, key="")


# ---- N12 / N13: clustered errors and increments ---------------------------------------------------------

def _repeated_stays(seed=0, patients=120, per=4):
    rng = np.random.default_rng(seed)
    pid = np.repeat(np.arange(patients), per)
    frailty = np.repeat(rng.normal(size=patients), per)
    sbp = rng.normal(120, 15, size=patients * per)
    eta = -1.0 + 0.04 * (sbp - 120) + 0.9 * frailty
    y = (rng.random(patients * per) < 1 / (1 + np.exp(-eta))).astype(int)
    return pd.DataFrame({"pid": pid, "sbp": sbp, "male": rng.integers(0, 2, patients * per), "y": y})


def test_n12_cluster_robust_errors_are_wider_with_repeated_stays():
    df = _repeated_stays()
    naive = consortium_multivariable_risk_factors(df, outcome_col="y", feature_cols=["sbp", "male"], regularize=False)
    clus = consortium_multivariable_risk_factors(df, outcome_col="y", cluster_col="pid", regularize=False)
    assert clus.metadata["cov_type"] == "cluster" and clus.metadata["n_clusters"] == 120
    assert "pid" not in clus.table["variable"].tolist()
    assert (clus.standard_errors > naive.standard_errors).any()
    assert np.allclose(clus.coefficients.values, naive.coefficients.values)
    # one row per cluster: the sandwich should be close to the model-based variance
    one = df.groupby("pid").head(1).reset_index(drop=True)
    a = consortium_multivariable_risk_factors(one, outcome_col="y", cluster_col="pid", regularize=False)
    b = consortium_multivariable_risk_factors(one, outcome_col="y", feature_cols=["sbp", "male"], regularize=False)
    assert np.allclose(a.standard_errors.values, b.standard_errors.values, rtol=0.25)
    with pytest.raises(ValueError, match="cluster_col"):
        consortium_multivariable_risk_factors(df, outcome_col="y", cluster_col="missing")


def test_n12_cluster_vector_matches_column_name():
    df = _repeated_stays()
    a = consortium_multivariable_risk_factors(df, outcome_col="y", cluster_col="pid")
    b = consortium_multivariable_risk_factors(df.drop(columns="pid"), outcome_col="y", cluster_col=df["pid"].values)
    assert np.allclose(a.standard_errors.values, b.standard_errors.values)


def test_n13_odds_ratio_per_increment_and_standardised_beta():
    df = _repeated_stays()
    res = consortium_multivariable_risk_factors(
        df, outcome_col="y", feature_cols=["sbp", "male"], regularize=False,
        increments={"sbp": (10, "per 10 mmHg")})
    row = res.table.set_index("variable").loc["sbp"]
    sd = df["sbp"].std()
    per_unit = row["beta"] / sd  # fitted per 1 SD
    assert row["increment"] == "per 10 mmHg"
    assert row["or_increment"] == pytest.approx(np.exp(per_unit * 10))
    assert row["std_beta"] == pytest.approx(row["beta"])
    assert row["or_increment_ci_95_lower"] < row["or_increment"] < row["or_increment_ci_95_upper"]
    male = res.table.set_index("variable").loc["male"]
    assert pd.isna(male["or_increment"]) and "std_beta" in res.table.columns
    raw = consortium_multivariable_risk_factors(
        df, outcome_col="y", feature_cols=["sbp"], regularize=False, standardize_continuous=False,
        increments={"sbp": (10, "per 10 mmHg")}).table.iloc[0]
    assert raw["or_increment"] == pytest.approx(row["or_increment"], rel=0.02)
    assert raw["std_beta"] == pytest.approx(row["std_beta"], rel=0.02)
    with pytest.raises(ValueError, match="unknown feature"):
        consortium_multivariable_risk_factors(df, outcome_col="y", feature_cols=["sbp"],
                                              increments={"dbp": (5, "x")})


def test_n12_n13_defaults_leave_the_table_unchanged():
    df = _repeated_stays()
    base = consortium_multivariable_risk_factors(df, outcome_col="y", feature_cols=["sbp", "male"])
    assert "std_beta" not in base.table.columns and base.metadata["cov_type"] == "model"


# ---- N14: patient-grouped cross-validation -------------------------------------------------------------

def test_n14_group_col_gives_patient_grouped_internal_performance():
    rng = np.random.default_rng(1)
    frames = []
    for site in ("A", "B"):
        d = _repeated_stays(seed=hash(site) % 1000, patients=80, per=4)
        d["site"] = site
        d["subject_id"] = site + d["pid"].astype(str)
        d["noise"] = rng.normal(size=len(d))
        frames.append(d)
    data = pd.concat(frames, ignore_index=True)
    feats = ["sbp", "noise"]
    plain = consortium_cross_site_validation(data, feats, target_col="y")
    grouped = consortium_cross_site_validation(data, feats, target_col="y", group_col="subject_id")
    assert grouped.metadata["group_col"] == "subject_id"
    assert (grouped.auroc_matrix.values[~np.eye(2, dtype=bool)] == plain.auroc_matrix.values[~np.eye(2, dtype=bool)]).all()
    assert np.isfinite(grouped.auroc_matrix.values).all()
    again = consortium_cross_site_validation(data, feats, target_col="y", group_col="subject_id")
    assert grouped.auroc_matrix.equals(again.auroc_matrix)
    with pytest.raises(ValueError, match="group_col"):
        consortium_cross_site_validation(data, feats, target_col="y", group_col="nope")


def test_n14_grouped_folds_keep_patients_together():
    from omop_etl.ml import _grouped_folds
    groups = np.repeat(np.arange(30), 3)
    y = (groups % 3 == 0).astype(float)
    folds = _grouped_folds(y, groups, 5, 42)
    for g in range(30):
        assert len(set(folds[groups == g])) == 1
    assert set(folds) == set(range(5))


# ---- extra coverage for the validation / warning branches ----------------------------------------------

def test_n8_warns_when_ip_discharges_are_unmapped_and_stays_quiet_otherwise():
    import warnings
    from omop_etl import build_schema, define_study_cohort
    con = duckdb.connect(":memory:")
    build_schema(con)
    con.execute("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) "
                "VALUES (1, 8507, 1960, 8527, 38003564)")
    con.execute("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, "
                "visit_end_date, visit_type_concept_id, discharged_to_concept_id, discharged_to_source_value) "
                "VALUES (1, 1, 9201, DATE '2020-01-10', DATE '2020-01-14', 32827, 0, 'IP')")
    kw = dict(washin_days=0, require_verified_followup=False, min_age=None, target_outcome="none",
              attrition=False, materialise=False, transfer_discharge_concept_ids=[8717])
    with pytest.warns(UserWarning, match="predates the ETL mapping"):
        define_study_cohort(con, **kw)
    con.execute("UPDATE visit_occurrence SET discharged_to_concept_id = 8717")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        define_study_cohort(con, **kw)  # a mapped transfer stay: no warning


def test_metformin_exclusion_warns_without_concept_ancestor():
    from omop_etl import build_schema, define_study_cohort
    con = duckdb.connect(":memory:")
    build_schema(con)
    con.execute("DROP TABLE IF EXISTS concept_ancestor")
    with pytest.warns(UserWarning, match="descendant concepts are not resolved"):
        define_study_cohort(con, washin_days=0, require_verified_followup=False, min_age=None, target_outcome="none",
                            attrition=False, materialise=False, exclude_metformin_only_prediabetes_pcos=True)


def test_n12_n13_n14_argument_validation():
    df = _repeated_stays(patients=30)
    with pytest.raises(ValueError, match="Length of cluster_col"):
        consortium_multivariable_risk_factors(df[["sbp", "male"]], y=df["y"].values, cluster_col=np.arange(5))
    with pytest.raises(ValueError, match="increments"):
        consortium_multivariable_risk_factors(df[["sbp"]], y=df["y"].values, increments={"sbp": (0, "bad")})
    plain = consortium_multivariable_risk_factors(df[["pid", "sbp", "male"]], y=df["y"].values, cluster_col=df["pid"].values)
    assert "pid" in plain.table["variable"].tolist() and plain.metadata["cov_type"] == "cluster"
    df["site"] = np.where(df["pid"] % 2 == 0, "A", "B")
    with pytest.raises(ValueError, match="n_splits"):
        consortium_cross_site_validation(df, ["sbp"], target_col="y", group_col="pid", n_splits=1)


def test_discovery_handles_a_connection_without_catalog_functions():
    from omop_etl.comorbidity import _discover_concept_ancestors

    class Broken:
        def execute(self, *a, **k):
            raise duckdb.Error("no catalog functions")

    assert _discover_concept_ancestors(Broken()) == []
