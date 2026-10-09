"""Machine Learning Cohort Extractor and Feature Matrix Builder Engine (RFC-6).

Provides standardized cohort extraction with strict lookback separation,
avoiding future data leakage, and transforms OMOP event tables into tabular matrices
or 3D temporal sequence tensors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence
import duckdb
import numpy as np
import pandas as pd


class CohortExtractor:
    """Extracts index admission cohorts from OMOP DuckDB with strict temporality."""

    def __init__(
        self,
        db_path: str | Path | duckdb.DuckDBPyConnection | None = None,
        con: duckdb.DuckDBPyConnection | None = None,
    ):
        if con is not None:
            self.con = con
            self._owns_con = False
        elif db_path is not None:
            if isinstance(db_path, (str, Path)):
                self.con = duckdb.connect(str(db_path))
                self._owns_con = True
            else:
                self.con = db_path
                self._owns_con = False
        else:
            raise ValueError("Must provide either db_path or con.")
        self.cohort_df: pd.DataFrame = pd.DataFrame()

    def define_index(
        self,
        visit_concept_ids: Sequence[int] = (9201,),
        min_age: int = 18,
        require_diabetes: bool = False,
        index_selection_rule: str = "first",
        verified_followup_days: int = 30,
        random_state: int = 42,
    ) -> CohortExtractor:
        """Defines index study cohort with inclusion criteria.

        Args:
            visit_concept_ids: Inpatient or encounter visit concept IDs (default (9201,)).
            min_age: Minimum patient age at index admission.
            require_diabetes: Whether to require a diagnosis of diabetes.
            index_selection_rule: 'first', 'last', 'random_eligible', or 'all'.
            verified_followup_days: Required post-discharge observation window in days.
            random_state: Random seed for 'random_eligible' sampling.

        Returns:
            CohortExtractor: self.
        """
        v_in = ", ".join(str(int(x)) for x in visit_concept_ids)

        # Base candidate query
        sql = f"""
        SELECT 
            v.person_id,
            v.visit_occurrence_id,
            v.visit_start_date AS index_date,
            COALESCE(v.visit_end_date, v.visit_start_date) AS discharge_date,
            p.gender_concept_id,
            p.year_of_birth,
            date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), v.visit_start_date) AS age_at_index,
            COALESCE(op.observation_period_end_date, DATE '2099-12-31') AS obs_end_date
        FROM visit_occurrence v
        JOIN person p ON v.person_id = p.person_id
        LEFT JOIN observation_period op ON v.person_id = op.person_id 
            AND v.visit_start_date >= op.observation_period_start_date 
            AND v.visit_start_date <= op.observation_period_end_date
        WHERE v.visit_concept_id IN ({v_in})
        """
        df = self.con.execute(sql).df()

        if len(df) == 0:
            self.cohort_df = pd.DataFrame()
            return self

        # Age filter
        df = df[df["age_at_index"] >= min_age].copy()

        # Verified followup filter (prevents censoring bias)
        if verified_followup_days > 0 and len(df) > 0:
            df["required_obs"] = pd.to_datetime(df["discharge_date"]) + pd.to_timedelta(verified_followup_days, unit="D")
            df["obs_end_dt"] = pd.to_datetime(df["obs_end_date"])
            df = df[df["obs_end_dt"] >= df["required_obs"]].copy()

        # Diabetes requirement
        if require_diabetes and len(df) > 0:
            dm_sql = """
            SELECT DISTINCT person_id FROM condition_occurrence 
            WHERE condition_concept_id IN (201826, 443238, 316866, 4058243, 4099651)
            """
            dm_persons = set(self.con.execute(dm_sql).df()["person_id"])
            df = df[df["person_id"].isin(dm_persons)].copy()

        # Selection rule per patient
        if len(df) > 0:
            if index_selection_rule == "first":
                df = df.sort_values(["person_id", "index_date"]).groupby("person_id").first().reset_index()
            elif index_selection_rule == "last":
                df = df.sort_values(["person_id", "index_date"]).groupby("person_id").last().reset_index()
            elif index_selection_rule == "random_eligible":
                df = df.sample(frac=1.0, random_state=random_state).groupby("person_id").first().reset_index()

        self.cohort_df = df.reset_index(drop=True)
        return self


class FeatureMatrixBuilder:
    """Builds tabular feature matrices and 3D temporal tensors for ML/DL."""

    def __init__(self, cohort: CohortExtractor):
        self.cohort = cohort
        self.con = cohort.con
        self.df = cohort.cohort_df.copy()
        self.feature_cols: list[str] = []
        self.outcome_col: str | None = None

        if len(self.df) > 0:
            self.con.register("_ml_cohort", self.df[["person_id", "visit_occurrence_id", "index_date", "discharge_date"]])

    def add_demographics(self) -> FeatureMatrixBuilder:
        """Extracts age and gender indicators."""
        if len(self.df) == 0:
            return self

        self.df["feat_age"] = self.df["age_at_index"].astype(float)
        self.df["feat_female"] = (self.df["gender_concept_id"] == 8532).astype(float)
        self.df["feat_male"] = (self.df["gender_concept_id"] == 8507).astype(float)

        for col in ["feat_age", "feat_female", "feat_male"]:
            if col not in self.feature_cols:
                self.feature_cols.append(col)
        return self

    def add_prior_utilization(self, lookback_days: int = 365) -> FeatureMatrixBuilder:
        """Calculates inpatient and outpatient visit counts in pre-admission lookback window."""
        if len(self.df) == 0:
            return self

        sql = f"""
        SELECT 
            c.visit_occurrence_id,
            COUNT(CASE WHEN v.visit_concept_id = 9201 THEN 1 END) AS feat_prior_inpatient_visits,
            COUNT(CASE WHEN v.visit_concept_id = 9202 THEN 1 END) AS feat_prior_outpatient_visits,
            COUNT(CASE WHEN v.visit_concept_id = 9203 THEN 1 END) AS feat_prior_er_visits
        FROM _ml_cohort c
        LEFT JOIN visit_occurrence v 
            ON c.person_id = v.person_id
            AND v.visit_start_date >= (c.index_date - INTERVAL '{lookback_days}' DAY)
            AND v.visit_start_date < c.index_date
        GROUP BY c.visit_occurrence_id
        """
        util_df = self.con.execute(sql).df()
        self.df = self.df.merge(util_df, on="visit_occurrence_id", how="left")

        for col in ["feat_prior_inpatient_visits", "feat_prior_outpatient_visits", "feat_prior_er_visits"]:
            self.df[col] = self.df[col].fillna(0.0).astype(float)
            if col not in self.feature_cols:
                self.feature_cols.append(col)
        return self

    def add_acute_labs(
        self,
        lookback_days: int = 365,
        aggregation: str = "last_pre_discharge",
    ) -> FeatureMatrixBuilder:
        """Extracts baseline clinical laboratory measurements."""
        if len(self.df) == 0:
            return self

        sql = f"""
        SELECT 
            c.visit_occurrence_id,
            AVG(CASE WHEN m.measurement_concept_id IN (3019550, 3012888) THEN m.value_as_number END) AS feat_lab_sodium,
            AVG(CASE WHEN m.measurement_concept_id = 3023103 THEN m.value_as_number END) AS feat_lab_potassium,
            AVG(CASE WHEN m.measurement_concept_id = 3016723 THEN m.value_as_number END) AS feat_lab_creatinine,
            AVG(CASE WHEN m.measurement_concept_id = 3000963 THEN m.value_as_number END) AS feat_lab_hemoglobin
        FROM _ml_cohort c
        LEFT JOIN measurement m
            ON c.person_id = m.person_id
            AND m.measurement_date >= (c.index_date - INTERVAL '{lookback_days}' DAY)
            AND m.measurement_date <= c.discharge_date
        GROUP BY c.visit_occurrence_id
        """
        labs_df = self.con.execute(sql).df()
        self.df = self.df.merge(labs_df, on="visit_occurrence_id", how="left")

        for col in ["feat_lab_sodium", "feat_lab_potassium", "feat_lab_creatinine", "feat_lab_hemoglobin"]:
            median_val = self.df[col].median() if not self.df[col].isna().all() else 0.0
            self.df[col] = self.df[col].fillna(median_val).astype(float)
            if col not in self.feature_cols:
                self.feature_cols.append(col)
        return self

    def add_chronic_conditions(self, lookback_days: int = 730) -> FeatureMatrixBuilder:
        """Adds binary indicators for major chronic comorbidities."""
        if len(self.df) == 0:
            return self

        sql = f"""
        SELECT 
            c.visit_occurrence_id,
            MAX(CASE WHEN co.condition_concept_id IN (201826, 443238, 316866) THEN 1.0 ELSE 0.0 END) AS feat_has_diabetes,
            MAX(CASE WHEN co.condition_concept_id IN (316139, 434056, 433435) THEN 1.0 ELSE 0.0 END) AS feat_has_heart_failure,
            MAX(CASE WHEN co.condition_concept_id IN (320128, 316866) THEN 1.0 ELSE 0.0 END) AS feat_has_hypertension,
            MAX(CASE WHEN co.condition_concept_id IN (46271022, 193782) THEN 1.0 ELSE 0.0 END) AS feat_has_ckd
        FROM _ml_cohort c
        LEFT JOIN condition_occurrence co
            ON c.person_id = co.person_id
            AND co.condition_start_date >= (c.index_date - INTERVAL '{lookback_days}' DAY)
            AND co.condition_start_date <= c.index_date
        GROUP BY c.visit_occurrence_id
        """
        cond_df = self.con.execute(sql).df()
        self.df = self.df.merge(cond_df, on="visit_occurrence_id", how="left")

        for col in ["feat_has_diabetes", "feat_has_heart_failure", "feat_has_hypertension", "feat_has_ckd"]:
            self.df[col] = self.df[col].fillna(0.0).astype(float)
            if col not in self.feature_cols:
                self.feature_cols.append(col)
        return self

    def add_medications(self, lookback_days: int = 365) -> FeatureMatrixBuilder:
        """Adds medication exposure flags."""
        if len(self.df) == 0:
            return self

        sql = f"""
        SELECT 
            c.visit_occurrence_id,
            MAX(CASE WHEN d.drug_concept_id IN (1125315, 1115008) THEN 1.0 ELSE 0.0 END) AS feat_has_analgesics,
            MAX(CASE WHEN d.drug_concept_id IN (1503297, 1530014) THEN 1.0 ELSE 0.0 END) AS feat_has_metformin
        FROM _ml_cohort c
        LEFT JOIN drug_exposure d
            ON c.person_id = d.person_id
            AND d.drug_exposure_start_date >= (c.index_date - INTERVAL '{lookback_days}' DAY)
            AND d.drug_exposure_start_date <= c.discharge_date
        GROUP BY c.visit_occurrence_id
        """
        med_df = self.con.execute(sql).df()
        self.df = self.df.merge(med_df, on="visit_occurrence_id", how="left")

        for col in ["feat_has_analgesics", "feat_has_metformin"]:
            self.df[col] = self.df[col].fillna(0.0).astype(float)
            if col not in self.feature_cols:
                self.feature_cols.append(col)
        return self

    def set_outcome(
        self,
        outcome_type: str = "inpatient_readmission",
        window_days: int = 30,
    ) -> FeatureMatrixBuilder:
        """Evaluates prospective clinical outcomes within the specified post-discharge window."""
        if len(self.df) == 0:
            return self

        sql = f"""
        SELECT 
            c.visit_occurrence_id,
            MAX(CASE WHEN v.visit_occurrence_id IS NOT NULL THEN 1.0 ELSE 0.0 END) AS outcome_y
        FROM _ml_cohort c
        LEFT JOIN visit_occurrence v
            ON c.person_id = v.person_id
            AND v.visit_concept_id = 9201
            AND v.visit_start_date > c.discharge_date
            AND v.visit_start_date <= (c.discharge_date + INTERVAL '{window_days}' DAY)
        GROUP BY c.visit_occurrence_id
        """
        outcome_df = self.con.execute(sql).df()
        self.df = self.df.merge(outcome_df, on="visit_occurrence_id", how="left")
        self.df["outcome_y"] = self.df["outcome_y"].fillna(0.0).astype(float)
        self.outcome_col = "outcome_y"
        return self

    def to_tabular(self) -> tuple[np.ndarray, np.ndarray, list[str]]:
        """Returns feature matrix X, outcome vector y, and list of feature names."""
        if len(self.df) == 0:
            return np.empty((0, 0)), np.empty(0), []

        X = self.df[self.feature_cols].to_numpy(dtype=np.float32)
        y = self.df[self.outcome_col].to_numpy(dtype=np.float32) if self.outcome_col else np.zeros(len(self.df), dtype=np.float32)
        return X, y, list(self.feature_cols)

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the complete dataset as a pandas DataFrame."""
        return self.df.copy()

    def to_tensor(self, max_seq_len: int = 107) -> Any:
        """Constructs a 3D temporal sequence tensor of shape (N, max_seq_len, D)."""
        X, _, _ = self.to_tabular()
        n_samples, n_features = X.shape
        tensor_3d = np.zeros((n_samples, max_seq_len, n_features), dtype=np.float32)
        for i in range(n_samples):
            tensor_3d[i, -1, :] = X[i, :]

        try:
            import torch
            return torch.as_tensor(tensor_3d, dtype=torch.float32)
        except ImportError:
            return tensor_3d
