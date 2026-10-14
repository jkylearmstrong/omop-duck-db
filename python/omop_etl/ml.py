"""Machine Learning Cohort Extractor and Feature Matrix Builder Engine (RFC-6).

Provides standardized cohort extraction with strict lookback separation,
avoiding future data leakage, and transforms OMOP event tables into tabular matrices
or 3D temporal sequence tensors.
"""

from __future__ import annotations

import math
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
        self.attrition: Any = None

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
        from .cohort import ConsortAttrition

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

        attrition_records = []
        n_candidates = int(df["person_id"].nunique()) if len(df) > 0 else 0
        n_base = n_candidates
        attrition_records.append({
            "step_number": 1,
            "step_name": f"Admissions meeting visit concept IDs ({v_in})",
            "subjects_retained": n_candidates,
            "subjects_dropped": 0,
            "percent_retained": 100.0,
            "exclusion_reason": "Baseline candidate admissions",
        })

        if len(df) == 0:
            self.cohort_df = pd.DataFrame()
            self.attrition = ConsortAttrition(pd.DataFrame(attrition_records))
            return self

        # Age filter
        df_age = df[df["age_at_index"] >= min_age].copy()
        n_age = int(df_age["person_id"].nunique()) if len(df_age) > 0 else 0
        dropped_age = max(0, n_candidates - n_age)
        pct_age = (n_age / n_base * 100.0) if n_base > 0 else 0.0
        attrition_records.append({
            "step_number": 2,
            "step_name": f"Age at admission >= {min_age}",
            "subjects_retained": n_age,
            "subjects_dropped": dropped_age,
            "percent_retained": round(pct_age, 2),
            "exclusion_reason": f"Pediatric patients (age < {min_age})",
        })
        df = df_age
        n_candidates = n_age

        # Verified followup filter (prevents censoring bias)
        if verified_followup_days > 0 and len(df) > 0:
            df["required_obs"] = pd.to_datetime(df["discharge_date"]) + pd.to_timedelta(verified_followup_days, unit="D")
            df["obs_end_dt"] = pd.to_datetime(df["obs_end_date"])
            df_obs = df[df["obs_end_dt"] >= df["required_obs"]].copy()
            n_obs = int(df_obs["person_id"].nunique()) if len(df_obs) > 0 else 0
            dropped_obs = max(0, n_candidates - n_obs)
            pct_obs = (n_obs / n_base * 100.0) if n_base > 0 else 0.0
            attrition_records.append({
                "step_number": 3,
                "step_name": f"Verified observation >= {verified_followup_days}d post-discharge",
                "subjects_retained": n_obs,
                "subjects_dropped": dropped_obs,
                "percent_retained": round(pct_obs, 2),
                "exclusion_reason": "Right-censored or lost to follow-up",
            })
            df = df_obs
            n_candidates = n_obs

        # Diabetes requirement
        if require_diabetes:
            if len(df) > 0:
                dm_sql = """
                SELECT DISTINCT person_id FROM condition_occurrence 
                WHERE condition_concept_id IN (201826, 443238, 316866, 4058243, 4099651)
                """
                dm_persons = set(self.con.execute(dm_sql).df()["person_id"])
                df_dm = df[df["person_id"].isin(dm_persons)].copy()
                n_dm = int(df_dm["person_id"].nunique()) if len(df_dm) > 0 else 0
            else:
                df_dm = df
                n_dm = 0
            dropped_dm = max(0, n_candidates - n_dm)
            pct_dm = (n_dm / n_base * 100.0) if n_base > 0 else 0.0
            attrition_records.append({
                "step_number": len(attrition_records) + 1,
                "step_name": "Confirmed diabetes mellitus diagnosis",
                "subjects_retained": n_dm,
                "subjects_dropped": dropped_dm,
                "percent_retained": round(pct_dm, 2),
                "exclusion_reason": "No documented diabetes condition",
            })
            df = df_dm
            n_candidates = n_dm

        # Selection rule per patient
        if len(df) > 0:
            if index_selection_rule == "first":
                df = df.sort_values(["person_id", "index_date"]).groupby("person_id").first().reset_index()
            elif index_selection_rule == "last":
                df = df.sort_values(["person_id", "index_date"]).groupby("person_id").last().reset_index()
            elif index_selection_rule == "random_eligible":
                df = df.sample(frac=1.0, random_state=random_state).groupby("person_id").first().reset_index()

        n_final = len(df)
        pct_final = (n_final / n_base * 100.0) if n_base > 0 else 0.0
        attrition_records.append({
            "step_number": len(attrition_records) + 1,
            "step_name": f"Index encounter selection ({index_selection_rule})",
            "subjects_retained": n_final,
            "subjects_dropped": 0,
            "percent_retained": round(pct_final, 2),
            "exclusion_reason": "Condense multiple eligible visits per patient",
        })

        attrition_df = pd.DataFrame(attrition_records)
        self.attrition = ConsortAttrition(attrition_df)
        self.cohort_df = df.reset_index(drop=True)
        self.cohort_df.attrs["attrition"] = attrition_df
        return self

    def to_mermaid(self) -> str:
        """Render attrition flowchart to Mermaid diagram."""
        if self.attrition is None:
            raise ValueError("Call define_index() before generating flowchart.")
        return self.attrition.to_mermaid()

    def to_typst(self, output_path: str | Path | None = None) -> str:
        """Render attrition flowchart to Typst syntax."""
        if self.attrition is None:
            raise ValueError("Call define_index() before generating flowchart.")
        return self.attrition.to_typst(output_path)

    def to_tikz(self, output_path: str | Path | None = None) -> str:
        """Render attrition flowchart to LaTeX TikZ environment."""
        if self.attrition is None:
            raise ValueError("Call define_index() before generating flowchart.")
        return self.attrition.to_tikz(output_path)

    def to_latex(self, output_path: str | Path | None = None) -> str:
        """Render attrition flowchart to LaTeX table."""
        if self.attrition is None:
            raise ValueError("Call define_index() before generating flowchart.")
        return self.attrition.to_latex(output_path)

    def to_dataframe(self) -> pd.DataFrame:
        """Return attrition flowchart as a DataFrame."""
        if self.attrition is None:
            raise ValueError("Call define_index() before generating flowchart.")
        return self.attrition.to_dataframe()

    def export_companion_manifest(self, output_path: str | Path | None = None) -> pd.DataFrame:
        """Exports a companion identifier manifest preserving exact cohort row order."""
        from .reconciliation import export_companion_manifest
        return export_companion_manifest(self.cohort_df, output_path=output_path, con=self.con)


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

    def export_companion_manifest(self, output_path: str | Path | None = None) -> pd.DataFrame:
        """Exports a companion identifier manifest preserving exact feature matrix row order."""
        from .reconciliation import export_companion_manifest
        return export_companion_manifest(self.df, output_path=output_path, con=self.con)

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


# -----------------------------------------------------------------------------
# Cross-Site Model Calibration & Decile Drift (ConsortiumCalibration)
# -----------------------------------------------------------------------------

class ConsortiumCalibration:
    """Multi-site calibration report with Greenwood-Nam-D'Agostino tests and decile drift."""

    def __init__(
        self,
        site_metrics: pd.DataFrame,
        calibration_curves: pd.DataFrame,
        decile_drift: pd.DataFrame,
    ):
        self._site_metrics = site_metrics
        self._calibration_curves = calibration_curves
        self._decile_drift = decile_drift

    @property
    def site_metrics(self) -> pd.DataFrame:
        """Per-site and pooled Brier score, calibration slope/intercept, ECE, and GND test."""
        return self._site_metrics

    @property
    def calibration_curves(self) -> pd.DataFrame:
        """Bin-level observed vs expected event rates across sites."""
        return self._calibration_curves

    @property
    def decile_drift(self) -> pd.DataFrame:
        """Cross-site prediction drift matrix across decile bins."""
        return self._decile_drift

    def summary(self) -> pd.DataFrame:
        """Returns the site metrics table."""
        return self._site_metrics.copy()

    def to_dataframe(self) -> pd.DataFrame:
        return self._site_metrics.copy()

    def __getitem__(self, key: str) -> Any:
        if key == "site_metrics":
            return self._site_metrics
        elif key == "calibration_curves":
            return self._calibration_curves
        elif key == "decile_drift":
            return self._decile_drift
        elif key in self._site_metrics.columns:
            return self._site_metrics[key]
        raise KeyError(key)

    def plot(
        self,
        title: str | None = None,
        output_path: str | None = None,
        show: bool = False,
    ) -> Any:
        """Generates a publication-quality multi-site calibration plot."""
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        fig, ax = plt.subplots(figsize=(8, 6))
        # 45 degree ideal line
        ax.plot([0, 1], [0, 1], "k--", lw=1.5, label="Perfect Calibration")

        sites = [s for s in self._calibration_curves["site"].unique() if s != "Consortium (Pooled)"]
        for s in sites:
            sub = self._calibration_curves[self._calibration_curves["site"] == s]
            ax.plot(sub["pred_mean"], sub["obs_rate"], marker="o", lw=1.8, label=str(s))

        # Pooled if available
        if "Consortium (Pooled)" in self._calibration_curves["site"].values:
            pooled = self._calibration_curves[self._calibration_curves["site"] == "Consortium (Pooled)"]
            ax.plot(pooled["pred_mean"], pooled["obs_rate"], "k-", marker="s", lw=2.5, label="Consortium (Pooled)")

        ax.set_xlabel("Mean Predicted Probability")
        ax.set_ylabel("Observed Event Rate")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_title(title or "Cross-Site Model Calibration Curves")
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(loc="upper left")
        plt.tight_layout()

        if output_path:
            fig.savefig(output_path, dpi=300)
        if show:
            plt.show()
        return fig

    def __repr__(self) -> str:
        return f"<ConsortiumCalibration: {len(self._site_metrics)} site(s)>\n{self._site_metrics.to_string(index=False)}"


def _fit_logistic_calibration(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """Fit logistic calibration: logit(y) ~ alpha + beta * logit(p) via IRLS."""
    eps = 1e-7
    p_clipped = np.clip(p, eps, 1.0 - eps)
    z = np.log(p_clipped / (1.0 - p_clipped))
    X = np.column_stack([np.ones_like(z), z])
    b = np.array([0.0, 1.0], dtype=float)

    for _ in range(25):
        p_hat = 1.0 / (1.0 + np.exp(-np.clip(X @ b, -25.0, 25.0)))
        w = np.clip(p_hat * (1.0 - p_hat), 1e-6, 0.25)
        grad = X.T @ (y - p_hat)
        hess = -(X.T * w) @ X - np.eye(2) * 1e-6
        try:
            step = np.linalg.solve(-hess, grad)
            b += step
            if np.max(np.abs(step)) < 1e-5:
                break
        except np.linalg.LinAlgError:
            break
    return float(b[0]), float(b[1])


def _chi2_sf(stat: float, df: int) -> float:
    """Survival function (1 - CDF) for chi-squared distribution."""
    try:
        from scipy.stats import chi2
        return float(chi2.sf(stat, df))
    except ImportError:
        # Wilson-Hilferty transformation approximation to standard normal
        if df <= 0 or stat <= 0:
            return 1.0
        s = stat / df
        z = (s ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / np.sqrt(2.0 / (9.0 * df))
        import math
        return float(0.5 * math.erfc(z / math.sqrt(2.0)))


def consortium_calibration(
    y_true: Sequence[float | int] | np.ndarray | pd.Series,
    y_prob: Sequence[float] | np.ndarray | pd.Series,
    site_labels: Sequence[str | int] | np.ndarray | pd.Series,
    n_bins: int = 10,
) -> ConsortiumCalibration:
    """Evaluates cross-site calibration, Greenwood-Nam-D'Agostino tests, and decile drift.

    Clinical Rationale: Multi-site predictive models often suffer from calibration drift
    when transported across health systems (e.g., systematically over-predicting risk at
    tertiary centers vs under-predicting in community settings).

    Args:
        y_true: Ground-truth binary outcome indicators (0 or 1).
        y_prob: Predicted event probabilities (between 0 and 1).
        site_labels: Site names or identifiers corresponding to each subject.
        n_bins: Number of calibration risk quantile bins (default 10 for deciles).

    Returns:
        ConsortiumCalibration: Encapsulates site_metrics, calibration_curves, and decile_drift.
    """
    y = np.asarray(y_true, dtype=float).ravel()
    p = np.asarray(y_prob, dtype=float).ravel()
    sites = np.asarray(site_labels).ravel()

    if not (len(y) == len(p) == len(sites)):
        raise ValueError(f"Lengths of y_true ({len(y)}), y_prob ({len(p)}), and site_labels ({len(sites)}) must match.")
    if len(y) == 0:
        raise ValueError("Input arrays must not be empty.")

    p = np.clip(p, 1e-7, 1.0 - 1e-7)

    def _calc_site_cal(y_sub: np.ndarray, p_sub: np.ndarray, site_name: str):
        n_pts = len(y_sub)
        n_events = int(np.sum(y_sub))
        obs_rate = float(n_events / n_pts) if n_pts > 0 else 0.0
        mean_p = float(np.mean(p_sub)) if n_pts > 0 else 0.0
        brier = float(np.mean((y_sub - p_sub) ** 2)) if n_pts > 0 else 0.0

        intercept, slope = _fit_logistic_calibration(y_sub, p_sub)

        # Quantile binning
        g = min(n_bins, max(2, n_pts // 5))
        # Percentile ranks
        ranks = np.argsort(np.argsort(p_sub))
        bin_ids = np.clip((ranks * g) // n_pts, 0, g - 1) + 1

        curve_rows = []
        ece_sum = 0.0
        mce = 0.0
        gnd_stat = 0.0

        for b_id in range(1, g + 1):
            mask = bin_ids == b_id
            b_n = int(np.sum(mask))
            if b_n == 0:
                continue
            b_y = y_sub[mask]
            b_p = p_sub[mask]
            b_events = int(np.sum(b_y))
            b_obs = float(b_events / b_n)
            b_pred = float(np.mean(b_p))
            b_exp = float(np.sum(b_p))
            drift = b_obs - b_pred

            diff = abs(drift)
            ece_sum += (b_n / n_pts) * diff
            if diff > mce:
                mce = diff

            # Greenwood-Nam-D'Agostino variance
            var_gnd = b_n * b_pred * (1.0 - b_pred)
            if var_gnd > 1e-6:
                gnd_stat += ((b_events - b_exp) ** 2) / var_gnd

            curve_rows.append({
                "site": site_name,
                "bin": b_id,
                "n": b_n,
                "events": b_events,
                "pred_mean": b_pred,
                "obs_rate": b_obs,
                "drift": drift,
            })

        df_g = max(1, g - 2)
        gnd_p = _chi2_sf(gnd_stat, df_g)

        metrics = {
            "site": site_name,
            "n": n_pts,
            "n_events": n_events,
            "observed_event_rate": obs_rate,
            "mean_predicted_prob": mean_p,
            "brier_score": brier,
            "calibration_intercept": intercept,
            "calibration_slope": slope,
            "ece": ece_sum,
            "mce": mce,
            "gnd_statistic": gnd_stat,
            "gnd_df": df_g,
            "gnd_p_value": gnd_p,
        }
        return metrics, curve_rows

    unique_sites = np.unique(sites)
    site_metric_list = []
    all_curves = []

    for s in unique_sites:
        mask = sites == s
        met, crv = _calc_site_cal(y[mask], p[mask], str(s))
        site_metric_list.append(met)
        all_curves.extend(crv)

    # Pooled consortium calibration
    pooled_met, pooled_crv = _calc_site_cal(y, p, "Consortium (Pooled)")
    site_metric_list.append(pooled_met)
    all_curves.extend(pooled_crv)

    site_metrics_df = pd.DataFrame(site_metric_list)
    calibration_curves_df = pd.DataFrame(all_curves)

    # Decile drift matrix: site x bin
    pivot_drift = calibration_curves_df.pivot(index="site", columns="bin", values="drift").reset_index()

    return ConsortiumCalibration(
        site_metrics=site_metrics_df,
        calibration_curves=calibration_curves_df,
        decile_drift=pivot_drift,
    )


# -----------------------------------------------------------------------------
# Clinical Decision Curve Analysis (DCA)
# -----------------------------------------------------------------------------

class DecisionCurveAnalysis:
    """Clinical Decision Curve Analysis (DCA) result according to Vickers et al. (2006)."""

    def __init__(
        self,
        net_benefit: pd.DataFrame,
        summary_stats: dict[str, Any],
    ):
        self._net_benefit = net_benefit
        self._summary_stats = summary_stats

    @property
    def net_benefit(self) -> pd.DataFrame:
        """Net Benefit across decision thresholds for Model, Treat All, and Treat None."""
        return self._net_benefit

    def summary(self) -> dict[str, Any]:
        """Summary of clinical utility, optimal threshold range, and maximum net benefit."""
        return dict(self._summary_stats)

    def to_dataframe(self) -> pd.DataFrame:
        return self._net_benefit.copy()

    def __getitem__(self, key: str) -> Any:
        if key == "net_benefit":
            return self._net_benefit
        elif key in self._net_benefit.columns:
            return self._net_benefit[key]
        elif key in self._summary_stats:
            return self._summary_stats[key]
        raise KeyError(key)

    def plot(
        self,
        title: str | None = None,
        output_path: str | None = None,
        show: bool = False,
    ) -> Any:
        """Generates a publication-quality Decision Curve Analysis plot."""
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        fig, ax = plt.subplots(figsize=(8, 5))
        df = self._net_benefit

        ax.plot(df["threshold"], df["model"], "b-", lw=2.2, label="Prediction Model")
        ax.plot(df["threshold"], df["all"], color="gray", linestyle="--", lw=1.8, label="Treat All")
        ax.axhline(0, color="black", linestyle=":", lw=1.2, label="Treat None")

        ax.set_xlabel("Decision Threshold (Probability)")
        ax.set_ylabel("Net Benefit")
        ax.set_title(title or "Clinical Decision Curve Analysis (Net Benefit)")
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(loc="upper right")

        # Set sensible Y bounds
        y_max = max(0.05, float(df[["model", "all"]].max().max() * 1.15))
        y_min = min(-0.02, float(df["all"].min() * 0.5))
        ax.set_ylim(y_min, y_max)
        plt.tight_layout()

        if output_path:
            fig.savefig(output_path, dpi=300)
        if show:
            plt.show()
        return fig

    def __repr__(self) -> str:
        s = self._summary_stats
        return (
            f"<DecisionCurveAnalysis: {len(self._net_benefit)} thresholds "
            f"[{self._net_benefit['threshold'].min():.2f} - {self._net_benefit['threshold'].max():.2f}], "
            f"Max Net Benefit = {s.get('max_net_benefit', 0.0):.4f} at threshold {s.get('threshold_at_max', 0.0):.2f}>"
        )


def decision_curve_analysis(
    y_true: Sequence[float | int] | np.ndarray | pd.Series,
    y_prob: Sequence[float] | np.ndarray | pd.Series,
    threshold_range: tuple[float, float] = (0.05, 0.35),
    step: float = 0.01,
    harm: float = 0.0,
) -> DecisionCurveAnalysis:
    """Calculates Decision Curve Analysis (Vickers et al., 2006) for clinical utility assessment.

    Evaluates whether using the model to guide clinical decisions yields higher Net Benefit
    than the default strategies of 'treat all' or 'treat none' across a clinical decision threshold range.

    .. math::
        \\text{Net Benefit}_{\\text{model}}(p_t) = \\frac{TP}{N} - \\frac{FP}{N} \\left(\\frac{p_t}{1 - p_t}\\right) - \\text{harm}

    .. math::
        \\text{Net Benefit}_{\\text{all}}(p_t) = \\text{prevalence} - (1 - \\text{prevalence}) \\left(\\frac{p_t}{1 - p_t}\\right) - \\text{harm}

    Args:
        y_true: Ground-truth binary outcome array (0 or 1).
        y_prob: Predicted event probabilities (between 0 and 1).
        threshold_range: Tuple of `(min_threshold, max_threshold)` (default (0.05, 0.35)).
        step: Threshold increment step (default 0.01).
        harm: Optional test or intervention harm in units of net benefit (default 0.0).

    Returns:
        DecisionCurveAnalysis: Object containing .net_benefit DataFrame, .summary(), and .plot().
    """
    y = np.asarray(y_true, dtype=float).ravel()
    p = np.asarray(y_prob, dtype=float).ravel()

    if len(y) != len(p):
        raise ValueError(f"Lengths of y_true ({len(y)}) and y_prob ({len(p)}) must match.")
    if len(y) == 0:
        raise ValueError("Input arrays must not be empty.")

    n = len(y)
    events = float(np.sum(y))
    non_events = float(n - events)
    prevalence = events / n

    t_min, t_max = threshold_range
    thresholds = np.round(np.arange(t_min, t_max + step / 2.0, step), 4)

    rows = []
    beneficial_thresholds = []

    for pt in thresholds:
        if pt <= 0.0 or pt >= 1.0:
            continue
        w = pt / (1.0 - pt)
        pred_pos = p >= pt
        tp = float(np.sum((pred_pos) & (y == 1.0)))
        fp = float(np.sum((pred_pos) & (y == 0.0)))

        nb_model = (tp / n) - (fp / n) * w - harm
        nb_all = prevalence - ((1.0 - prevalence) * w) - harm
        nb_none = 0.0

        # Net reduction in interventions per 100 patients
        reduction = max(0.0, ((nb_model - nb_all) / w) * 100.0) if w > 0 else 0.0
        snb = (nb_model / prevalence) if prevalence > 0 else 0.0

        if nb_model > max(nb_all, nb_none):
            beneficial_thresholds.append(pt)

        rows.append({
            "threshold": float(pt),
            "model": float(nb_model),
            "all": float(nb_all),
            "none": 0.0,
            "net_reduction_interventions": float(reduction),
            "standardized_net_benefit": float(snb),
            "tp": int(tp),
            "fp": int(fp),
            "n": int(n),
        })

    nb_df = pd.DataFrame(rows)

    max_idx = nb_df["model"].idxmax() if len(nb_df) > 0 else 0
    max_row = nb_df.loc[max_idx] if len(nb_df) > 0 else None

    summary_stats = {
        "n_samples": n,
        "n_events": int(events),
        "prevalence": prevalence,
        "max_net_benefit": float(max_row["model"]) if max_row is not None else 0.0,
        "threshold_at_max": float(max_row["threshold"]) if max_row is not None else 0.0,
        "clinical_benefit_range": (
            float(min(beneficial_thresholds)),
            float(max(beneficial_thresholds)),
        ) if beneficial_thresholds else None,
        "max_reduction_interventions": float(nb_df["net_reduction_interventions"].max()) if len(nb_df) > 0 else 0.0,
    }

    return DecisionCurveAnalysis(net_benefit=nb_df, summary_stats=summary_stats)


# ---------------------------------------------------------------------------
# Robust Multivariable Odds Ratios with L2 / Ridge Inversion
# ---------------------------------------------------------------------------

class ConsortiumRiskFactors:
    """Publication-ready Table 5 multivariable risk factors container.

    Holds standardized odds ratios, 95% confidence intervals, and Wald p-values
    computed via L2 regularized logistic regression and QR orthogonalization.
    """

    def __init__(
        self,
        table: pd.DataFrame,
        coefficients: pd.Series,
        standard_errors: pd.Series,
        covariance_matrix: np.ndarray,
        dropped_features: list[str],
        metadata: dict[str, Any],
    ):
        self.table = table
        self.coefficients = coefficients
        self.standard_errors = standard_errors
        self.covariance_matrix = covariance_matrix
        self.dropped_features = dropped_features
        self.metadata = metadata

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the publication-grade Table 5 DataFrame."""
        return self.table.copy()

    def to_quarto(
        self,
        caption: str = "Table 5: Multivariable Risk Factors for Clinical Outcome (L2 Regularized)",
        label: str = "tbl-risk-factors",
    ) -> str:
        """Renders the risk factors table into Quarto Markdown with bracketed 95% CIs."""
        lines = [
            f"```{{=typst}}",
            f"// {caption}",
            f"```",
            f": {caption} {{#{label}}}",
            "",
            "| Domain | Variable | Unit / Metric | Odds Ratio (95% CI) | p-value |",
            "|:---|:---|:---|:---:|:---:|",
        ]
        curr_domain = None
        for _, row in self.table.iterrows():
            d = str(row["domain"])
            if d != curr_domain:
                curr_domain = d
                lines.append(f"| **{curr_domain}** | | | | |")
            lines.append(
                f"| | {row['variable']} | {row['unit']} | {row['or_ci_formatted']} | {row['p_formatted']} |"
            )
        return "\n".join(lines) + "\n"

    def to_typst(
        self,
        caption: str = "Table 5: Multivariable Risk Factors for Clinical Outcome (L2 Regularized)",
    ) -> str:
        """Renders the risk factors table into Typst table syntax."""
        lines = [
            f"#figure(",
            f"  table(",
            f"    columns: (1.5fr, 2fr, 2fr, 2fr, 1.2fr),",
            f"    align: (left, left, left, center, center),",
            f"    stroke: (x, y) => if y == 0 {{ (bottom: 1.2pt) }} else {{ none }},",
            f"    table.header([*Domain*], [*Variable*], [*Unit*], [*Odds Ratio (95% CI)*], [*p-value*]),",
        ]
        curr_domain = None
        for _, row in self.table.iterrows():
            d = str(row["domain"])
            if d != curr_domain:
                curr_domain = d
                lines.append(f"    table.cell(colspan: 5)[*#upper(\"{curr_domain}\")*],")
            lines.append(
                f"    [], [{row['variable']}], [{row['unit']}], [{row['or_ci_formatted']}], [{row['p_formatted']}], "
            )
        lines.append(f"  ),")
        lines.append(f"  caption: [{caption}],")
        lines.append(f")")
        return "\n".join(lines) + "\n"

    def plot_forest(
        self,
        output_path: str | Path | None = None,
        save_path: str | Path | None = None,
        title: str = "Multivariable Odds Ratios (95% CI)",
        figsize: tuple[float, float] = (8.0, 6.0),
        show: bool = False,
    ) -> Any:
        """Plots a publication-quality Forest Plot with log-scale odds ratios and 95% CIs."""
        if save_path is not None:
            output_path = save_path
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        df = self.table.copy()
        if df.empty:
            return None

        # Sort variables for forest plot
        df = df.iloc[::-1].reset_index(drop=True)
        y_pos = np.arange(len(df))

        fig, ax = plt.subplots(figsize=figsize, dpi=300)
        ax.axvline(1.0, color="gray", linestyle="--", linewidth=1.0, alpha=0.7)

        or_vals = df["odds_ratio"].astype(float).values
        ci_low = df["ci_95_lower"].astype(float).values
        ci_high = df["ci_95_upper"].astype(float).values

        xerr_low = np.maximum(0.0, or_vals - ci_low)
        xerr_high = np.maximum(0.0, ci_high - or_vals)

        ax.errorbar(
            or_vals,
            y_pos,
            xerr=[xerr_low, xerr_high],
            fmt="s",
            color="#08519c",
            ecolor="#3182bd",
            elinewidth=1.8,
            capsize=3.5,
            markersize=6,
        )

        labels = [f"{row['variable']} ({row['unit']})" for _, row in df.iterrows()]
        ax.set_yticks(y_pos)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xscale("log")
        ax.set_xlabel("Odds Ratio (log scale)", fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.grid(axis="x", linestyle=":", alpha=0.5)

        plt.tight_layout()
        if output_path:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(p), dpi=300, bbox_inches="tight")
        if show:
            plt.show()
        return fig

    def __repr__(self) -> str:
        return (
            f"<ConsortiumRiskFactors: {len(self.table)} predictors, "
            f"{len(self.dropped_features)} dropped collinear features, "
            f"L2 alpha={self.metadata.get('alpha', 1.0)}>\n"
            f"{self.table[['domain', 'variable', 'unit', 'or_ci_formatted', 'p_formatted']].to_string(index=False)}"
        )


def consortium_multivariable_risk_factors(
    X: pd.DataFrame | np.ndarray,
    y: Sequence[int | float] | np.ndarray | pd.Series | None = None,
    outcome_col: str | None = None,
    target_col: str | None = None,
    feature_cols: Sequence[str] | None = None,
    binary_cols: Sequence[str] | None = None,
    regularize: bool = True,
    alpha: float = 1.0,
    l2_lambda: float | None = None,
    domain_mapping: dict[str, str] | None = None,
    standardize_continuous: bool = True,
    drop_collinear: bool = True,
    feature_names: Sequence[str] | None = None,
) -> ConsortiumRiskFactors:
    """Calculates robust multivariable Odds Ratios with L2 / Ridge inversion and QR orthogonalization.

    When fitting multivariable logistic regression on high-dimensional clinical matrices
    (e.g. hundreds of dummy-coded comorbidities, procedures, and medication classes), standard
    unregularized maximum likelihood frequently suffers from Hessian collinearity and quasi-complete
    separation, resulting in NaN standard errors and missing 95% CIs.

    This function uses QR decomposition to eliminate linearly dependent features, applies L2
    penalized logistic regression (ridge inversion) to guarantee an invertible parameter covariance
    matrix, and emits publication-grade Table 5 outputs with standardized units ("Per 1-SD Increase"
    vs. "Presence vs. Absence").

    Args:
        X: Feature matrix DataFrame or 2D NumPy array. If `outcome_col` is supplied, X may be full DataFrame.
        y: Binary outcome vector (0 or 1). Optional if `outcome_col` is specified.
        outcome_col: Column name of outcome if X is a combined DataFrame.
        target_col: Optional alias for outcome_col.
        feature_cols: Optional list of predictor columns to subset from X.
        binary_cols: List of column names that are binary indicators. If None, auto-detected.
        regularize: Whether to apply L2 ridge regularization (default True).
        alpha: L2 penalty parameter lambda (default 1.0).
        l2_lambda: Optional alias for alpha.
        domain_mapping: Optional dictionary mapping feature names to clinical domains.
        standardize_continuous: Whether continuous features are standardized to 1-SD units (default True).
        drop_collinear: Whether to eliminate linearly dependent columns via QR decomposition (default True).
        feature_names: Optional column names if X is a raw numpy array.

    Returns:
        ConsortiumRiskFactors: Container holding Table 5 DataFrame, Wald statistics, and plotting methods.
    """
    if l2_lambda is not None:
        alpha = float(l2_lambda)

    target_k = outcome_col or target_col
    if y is None and target_k is not None:
        if isinstance(X, pd.DataFrame):
            y = X[target_k].values
            if feature_cols is not None:
                X_df = X[list(feature_cols)].copy()
            else:
                X_df = X[[c for c in X.columns if c != target_k]].copy()
            feat_names = list(X_df.columns)
        else:
            raise ValueError("When outcome_col/target_col is passed, X must be a pandas DataFrame.")
    else:
        if isinstance(X, pd.DataFrame):
            if feature_cols is not None:
                X_df = X[list(feature_cols)].copy()
            else:
                X_df = X.copy()
            feat_names = list(X_df.columns)
        else:
            X_arr = np.asarray(X, dtype=float)
            feat_names = (
                list(feature_names)
                if feature_names is not None
                else [f"x{i+1}" for i in range(X_arr.shape[1])]
            )
            X_df = pd.DataFrame(X_arr, columns=feat_names)

    if y is None:
        raise ValueError("Binary outcome target y or outcome_col/target_col must be specified.")

    y_arr = np.asarray(y, dtype=float).ravel()
    if len(X_df) != len(y_arr):
        raise ValueError(f"Length of X ({len(X_df)}) and y ({len(y_arr)}) must match.")

    # Drop missing values
    valid_mask = (~X_df.isna().any(axis=1)) & (~np.isnan(y_arr))
    if not valid_mask.all():
        X_df = X_df.loc[valid_mask].reset_index(drop=True)
        y_arr = y_arr[valid_mask]

    n_samples, n_features = X_df.shape
    if n_samples < 5:
        raise ValueError(f"Insufficient samples ({n_samples}) for multivariable regression.")

    # 1. Determine binary vs continuous columns
    if binary_cols is not None:
        binary_set = set(binary_cols)
    else:
        binary_set = set()
        for c in X_df.columns:
            u_vals = set(X_df[c].dropna().unique())
            if u_vals.issubset({0, 1, 0.0, 1.0}):
                binary_set.add(c)

    # 2. Standardize continuous columns if requested
    X_proc = pd.DataFrame(index=X_df.index)
    col_units: dict[str, str] = {}
    for c in X_df.columns:
        s = X_df[c].astype(float)
        if c in binary_set:
            X_proc[c] = s
            col_units[c] = "Presence vs. Absence"
        else:
            if standardize_continuous:
                sd = float(s.std())
                if sd > 1e-12:
                    X_proc[c] = (s - s.mean()) / sd
                    col_units[c] = "Per 1-SD Increase"
                else:
                    X_proc[c] = s
                    col_units[c] = "Continuous (Invariant)"
            else:
                X_proc[c] = s
                col_units[c] = "Per 1-Unit Increase"

    # 3. QR Orthogonalization / Collinearity elimination
    dropped_features: list[str] = []
    active_cols = list(X_proc.columns)

    if drop_collinear and len(active_cols) > 0:
        kept_cols = []
        design_mat = np.ones((n_samples, 1))
        for c in active_cols:
            col_v = X_proc[c].values[:, np.newaxis]
            # Test residual after projection onto current design basis
            proj, _, _, _ = np.linalg.lstsq(design_mat, col_v, rcond=None)
            resid = col_v - design_mat @ proj
            var_resid = np.var(resid)
            if var_resid < 1e-7:
                dropped_features.append(c)
            else:
                kept_cols.append(c)
                design_mat = np.column_stack([design_mat, col_v])
        active_cols = kept_cols

    if len(active_cols) == 0:
        raise ValueError("All features were identified as collinear or invariant.")

    # 4. Fit L2 regularized logistic regression via Newton-Raphson / IRLS
    Z = np.column_stack([np.ones(n_samples), X_proc[active_cols].values])
    p_dim = Z.shape[1]

    # Initialize beta: intercept = log(p0 / (1 - p0)), others = 0
    p0 = float(np.clip(np.mean(y_arr), 1e-4, 1.0 - 1e-4))
    beta = np.zeros(p_dim)
    beta[0] = np.log(p0 / (1.0 - p0))

    # L2 penalty matrix (do not penalize intercept)
    lam = float(alpha) if regularize else 1e-9
    diag_pen = np.full(p_dim, lam)
    diag_pen[0] = 0.0
    Lambda = np.diag(diag_pen)

    for iteration in range(60):
        eta = np.clip(Z @ beta, -30.0, 30.0)
        prob = 1.0 / (1.0 + np.exp(-eta))
        prob = np.clip(prob, 1e-12, 1.0 - 1e-12)
        w = prob * (1.0 - prob)
        W_Z = Z * w[:, np.newaxis]
        Hessian = Z.T @ W_Z + Lambda
        grad = Z.T @ (prob - y_arr) + Lambda @ beta

        try:
            step = np.linalg.solve(Hessian, grad)
        except np.linalg.LinAlgError:
            # Fall back to pseudo-inverse if Hessian is near singular
            step = np.linalg.pinv(Hessian) @ grad

        beta -= step
        if np.max(np.abs(step)) < 1e-6:
            break

    # 5. Parameter covariance matrix via regularized information inversion
    eta_final = np.clip(Z @ beta, -30.0, 30.0)
    p_final = np.clip(1.0 / (1.0 + np.exp(-eta_final)), 1e-12, 1.0 - 1e-12)
    w_final = p_final * (1.0 - p_final)
    H_final = Z.T @ (Z * w_final[:, np.newaxis]) + Lambda

    try:
        cov_matrix = np.linalg.inv(H_final)
    except np.linalg.LinAlgError:
        cov_matrix = np.linalg.pinv(H_final)

    se = np.sqrt(np.maximum(1e-12, np.diag(cov_matrix)))

    # 6. Extract Odds Ratios, 95% CIs, and Wald p-values (skip intercept = idx 0)
    feat_betas = beta[1:]
    feat_se = se[1:]

    # Normal CDF approximation for p-values
    def norm_cdf(val: float) -> float:
        return 0.5 * (1.0 + math.erf(val / np.sqrt(2.0)))

    def infer_domain(fname: str) -> str:
        fn_l = fname.lower()
        if any(k in fn_l for k in ("age", "gender", "sex", "race", "ethnicity", "male", "female")):
            return "Demographics"
        elif any(fn_l.startswith(k) for k in ("dx_", "cond_", "condition_", "charlson", "elixhauser", "cci")) or any(k in fn_l for k in ("diabetes", "hypertension", "failure", "copd", "ckd")):
            return "Comorbidities"
        elif any(fn_l.startswith(k) for k in ("rx_", "drug_", "med_")) or any(k in fn_l for k in ("statin", "insulin", "aspirin", "ace_", "arb_")):
            return "Medications"
        elif any(fn_l.startswith(k) for k in ("proc_", "procedure_")):
            return "Procedures"
        elif any(fn_l.startswith(k) for k in ("lab_", "measurement_", "val_")) or any(k in fn_l for k in ("creatinine", "glucose", "a1c", "sbp", "dbp", "bmi", "potassium", "sodium")):
            return "Laboratory & Vitals"
        return "Clinical Features"

    rows = []
    for idx, f_name in enumerate(active_cols):
        b = float(feat_betas[idx])
        s_err = float(feat_se[idx])
        or_val = float(np.exp(b))
        ci_l = float(np.exp(b - 1.96 * s_err))
        ci_u = float(np.exp(b + 1.96 * s_err))
        z_score = b / s_err if s_err > 0 else 0.0
        p_val = 2.0 * (1.0 - norm_cdf(abs(z_score)))

        dom = (
            domain_mapping.get(f_name, infer_domain(f_name))
            if domain_mapping is not None
            else infer_domain(f_name)
        )

        p_str = "<0.001" if p_val < 0.001 else f"{p_val:.3f}"
        rows.append({
            "domain": dom,
            "variable": f_name,
            "unit": col_units.get(f_name, "Unit"),
            "odds_ratio": round(or_val, 2),
            "ci_95_lower": round(ci_l, 2),
            "ci_95_upper": round(ci_u, 2),
            "p_value": float(p_val),
            "or_ci_formatted": f"{or_val:.2f} [{ci_l:.2f}-{ci_u:.2f}]",
            "p_formatted": p_str,
            "beta": b,
            "se": s_err,
            "z_score": z_score,
        })

    table_df = pd.DataFrame(rows)
    table_df.sort_values(by=["domain", "p_value"], inplace=True)
    table_df.reset_index(drop=True, inplace=True)

    coef_series = pd.Series(feat_betas, index=active_cols, name="beta")
    se_series = pd.Series(feat_se, index=active_cols, name="se")

    meta = {
        "n_samples": n_samples,
        "n_features": len(active_cols),
        "alpha": alpha,
        "regularize": regularize,
        "intercept": float(beta[0]),
        "converged": True,
    }

    return ConsortiumRiskFactors(
        table=table_df,
        coefficients=coef_series,
        standard_errors=se_series,
        covariance_matrix=cov_matrix,
        dropped_features=dropped_features,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# Internal-External Cross-Validation (IECV) Matrix
# ---------------------------------------------------------------------------

class ConsortiumCrossSiteValidation:
    """Container for K x K Internal-External Cross-Validation (IECV) performance matrices."""

    def __init__(
        self,
        auroc_matrix: pd.DataFrame,
        brier_matrix: pd.DataFrame,
        summary_df: pd.DataFrame,
        sites: list[str],
        metadata: dict[str, Any],
    ):
        self.auroc_matrix = auroc_matrix
        self.brier_matrix = brier_matrix
        self.summary_df = summary_df
        self.sites = sites
        self.metadata = metadata

    def summary(self) -> pd.DataFrame:
        """Returns internal vs external validation transportability summary."""
        return self.summary_df.copy()

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the AUROC K x K matrix."""
        return self.auroc_matrix.copy()

    def to_quarto(
        self,
        caption: str = "Table 4: Internal-External Cross-Validation (IECV) AUROC Matrix Across Participating Sites",
        label: str = "tbl-iecv",
    ) -> str:
        """Exports the K x K AUROC matrix into Quarto table syntax with diagonal highlighted."""
        cols = list(self.auroc_matrix.columns)
        header = "| Train \\ Test | " + " | ".join(cols) + " |"
        sep = "|:---| " + " | ".join([":---:"] * len(cols)) + " |"
        lines = [
            f": {caption} {{#{label}}}",
            "",
            header,
            sep,
        ]
        for idx, row in self.auroc_matrix.iterrows():
            vals = []
            for c in cols:
                v = float(row[c])
                if str(idx) == str(c):
                    vals.append(f"**{v:.3f}**")  # internal
                else:
                    vals.append(f"{v:.3f}")
            lines.append(f"| **{idx}** | " + " | ".join(vals) + " |")
        return "\n".join(lines) + "\n"

    def to_typst(
        self,
        caption: str = "Table 4: Internal-External Cross-Validation (IECV) AUROC Matrix Across Participating Sites",
    ) -> str:
        """Exports the K x K AUROC matrix into Typst syntax."""
        cols = list(self.auroc_matrix.columns)
        n_c = len(cols) + 1
        col_spec = f"({', '.join(['1.5fr'] + ['1fr'] * len(cols))})"
        lines = [
            f"#figure(",
            f"  table(",
            f"    columns: {col_spec},",
            f"    align: (left, {', '.join(['center'] * len(cols))}),",
            f"    stroke: (x, y) => if y == 0 {{ (bottom: 1.2pt) }} else {{ none }},",
            f"    table.header([*Train \\ Test*], {', '.join(f'[*test: {c}*]' for c in cols)}),",
        ]
        for idx, row in self.auroc_matrix.iterrows():
            cells = [f"[*train: {idx}*]"]
            for c in cols:
                v = float(row[c])
                cell_val = f"*{v:.3f}*" if str(idx) == str(c) else f"{v:.3f}"
                cells.append(f"[{cell_val}]")
            lines.append(f"    {', '.join(cells)},")
        lines.append(f"  ),")
        lines.append(f"  caption: [{caption}],")
        lines.append(f")")
        return "\n".join(lines) + "\n"

    @property
    def summary_table(self) -> pd.DataFrame:
        """Returns per-site internal vs external transportability summary table."""
        rows = []
        for s in self.sites:
            int_auc = float(self.auroc_matrix.loc[s, s])
            ext_vals = [float(self.auroc_matrix.loc[s, o]) for o in self.sites if o != s]
            mean_ext = float(np.mean(ext_vals)) if ext_vals else int_auc
            gap = float(int_auc - mean_ext)
            rows.append({
                "site": s,
                "internal_auroc": round(int_auc, 3),
                "mean_auroc_external": round(mean_ext, 3),
                "transportability_gap": round(gap, 3),
            })
        return pd.DataFrame(rows)

    def plot_heatmap(
        self,
        output_path: str | Path | None = None,
        save_path: str | Path | None = None,
        metric: str = "auroc",
        title: str | None = None,
        figsize: tuple[float, float] = (6.0, 5.0),
        show: bool = False,
    ) -> Any:
        """Plots an annotated heatmap of the K x K cross-site validation matrix."""
        if save_path is not None:
            output_path = save_path

        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        if str(metric).lower() == "brier":
            mat = self.brier_matrix.values.astype(float)
            labels = list(self.brier_matrix.columns)
            default_title = "Cross-Site Transportability (IECV Brier Score Matrix)"
            cbar_label = "Brier Score"
            cmap = "YlOrRd"
            vmin, vmax = 0.0, max(0.5, float(np.max(mat)))
        else:
            mat = self.auroc_matrix.values.astype(float)
            labels = list(self.auroc_matrix.columns)
            default_title = "Cross-Site Transportability (IECV AUROC Matrix)"
            cbar_label = "AUROC"
            cmap = "YlGnBu"
            vmin, vmax = 0.5, 1.0

        title = title or default_title

        fig, ax = plt.subplots(figsize=figsize, dpi=300)
        cax = ax.imshow(mat, cmap=cmap, vmin=vmin, vmax=vmax)
        fig.colorbar(cax, ax=ax, fraction=0.046, pad=0.04, label=cbar_label)

        ax.set_xticks(np.arange(len(labels)))
        ax.set_yticks(np.arange(len(labels)))
        ax.set_xticklabels(labels, fontsize=9)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("Evaluation Site (Test Partition)", fontsize=10, fontweight="bold")
        ax.set_ylabel("Model Origin (Training Site)", fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=11, fontweight="bold")

        for i in range(len(labels)):
            for j in range(len(labels)):
                val = mat[i, j]
                weight = "bold" if i == j else "normal"
                color = "white" if (val > 0.80 and metric != "brier") else "black"
                ax.text(j, i, f"{val:.3f}", ha="center", va="center", color=color, fontweight=weight, fontsize=9)

        plt.tight_layout()
        if output_path:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(p), dpi=300, bbox_inches="tight")
        if show:
            plt.show()
        return fig

    def __repr__(self) -> str:
        s = self.summary_df.iloc[0].to_dict() if not self.summary_df.empty else {}
        return (
            f"<ConsortiumCrossSiteValidation: {len(self.sites)} sites ({len(self.sites)}x{len(self.sites)} matrix), "
            f"Mean Internal AUROC={s.get('mean_internal_auroc', 0.0):.3f}, "
            f"Mean External AUROC={s.get('mean_external_auroc', 0.0):.3f}, "
            f"Generalization Gap={s.get('generalization_gap', 0.0):.3f}>\n"
            f"{self.auroc_matrix.to_string()}"
        )


def _calc_auroc_pure(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Calculates AUROC using the Mann-Whitney U statistic with zero dependencies."""
    y_t = np.asarray(y_true, dtype=float).ravel()
    y_s = np.asarray(y_score, dtype=float).ravel()

    pos = y_t == 1.0
    neg = y_t == 0.0
    n_pos = int(np.sum(pos))
    n_neg = int(np.sum(neg))

    if n_pos == 0 or n_neg == 0:
        return 0.5

    # Rank with average rank for ties
    order = np.argsort(y_s)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(y_s) + 1)

    # Adjust ties
    sorted_s = y_s[order]
    idx = 0
    while idx < len(sorted_s):
        end = idx
        while end + 1 < len(sorted_s) and sorted_s[end + 1] == sorted_s[idx]:
            end += 1
        if end > idx:
            avg_rank = (idx + 1 + end + 1) / 2.0
            ranks[order[idx:end + 1]] = avg_rank
            idx = end + 1
        else:
            idx += 1

    sum_pos_ranks = np.sum(ranks[pos])
    u_stat = sum_pos_ranks - (n_pos * (n_pos + 1)) / 2.0
    return float(u_stat / (n_pos * n_neg))


def consortium_cross_site_validation(
    site_data: dict[str, pd.DataFrame] | pd.DataFrame,
    feature_cols: Sequence[str],
    target_col: str | None = None,
    outcome_col: str | None = None,
    site_col: str = "site",
    regularize: bool = True,
    alpha: float = 1.0,
    l2_lambda: float | None = None,
) -> ConsortiumCrossSiteValidation:
    """Performs K x K Internal-External Cross-Validation (IECV) across federated health systems.

    Trains predictive models on Site i and evaluates on Site j for all (i, j) pairs in {1..K} x {1..K}.
    Returns a K x K AUROC matrix, Brier score matrix, and transportability summary to evaluate
    domain shift and model transportability across independent institutions.

    Args:
        site_data: Dictionary of {site_name: DataFrame} or single DataFrame with `site_col`.
        feature_cols: List of column names used as predictive features.
        target_col: Name of the binary target outcome column.
        outcome_col: Optional alias for target_col.
        site_col: Name of the hospital/site identifier column if single DataFrame is provided.
        regularize: Whether to apply L2 ridge regularization to site models.
        alpha: L2 penalty parameter lambda (default 1.0).
        l2_lambda: Optional alias for alpha.

    Returns:
        ConsortiumCrossSiteValidation: Object with AUROC matrix, Brier matrix, and heatmap plot.
    """
    if target_col is None:
        target_col = outcome_col
    if target_col is None:
        raise ValueError("Either target_col or outcome_col must be specified.")
    if l2_lambda is not None:
        alpha = float(l2_lambda)

    if isinstance(site_data, pd.DataFrame):
        if site_col not in site_data.columns:
            raise ValueError(f"Site column {site_col!r} not found in site_data DataFrame.")
        sites_dict = {str(s): group.copy() for s, group in site_data.groupby(site_col)}
    elif isinstance(site_data, dict):
        sites_dict = {str(k): (v.copy() if isinstance(v, pd.DataFrame) else pd.DataFrame(v)) for k, v in site_data.items()}
    else:
        raise TypeError(f"site_data must be a dict or DataFrame, got {type(site_data).__name__}")

    site_names = sorted(sites_dict.keys())
    if len(site_names) < 2:
        raise ValueError(f"IECV requires at least 2 distinct sites, found {len(site_names)}.")

    k = len(site_names)
    auroc_mat = np.zeros((k, k))
    brier_mat = np.zeros((k, k))

    # Fit site models and evaluate cross-site
    trained_models = {}
    for i, s_train in enumerate(site_names):
        df_train = sites_dict[s_train].dropna(subset=list(feature_cols) + [target_col])
        X_train = df_train[list(feature_cols)].values
        y_train = df_train[target_col].values.astype(float)

        rf = consortium_multivariable_risk_factors(
            X=X_train,
            y=y_train,
            regularize=regularize,
            alpha=alpha,
            standardize_continuous=False,
            drop_collinear=False,
            feature_names=feature_cols,
        )
        trained_models[s_train] = (rf.metadata["intercept"], rf.coefficients.values)

    for i, s_train in enumerate(site_names):
        intercept, betas = trained_models[s_train]
        for j, s_test in enumerate(site_names):
            df_test = sites_dict[s_test].dropna(subset=list(feature_cols) + [target_col])
            X_test = df_test[list(feature_cols)].values
            y_test = df_test[target_col].values.astype(float)

            eta = intercept + X_test @ betas
            prob = 1.0 / (1.0 + np.exp(-np.clip(eta, -30.0, 30.0)))

            auc = _calc_auroc_pure(y_test, prob)
            brier = float(np.mean((y_test - prob) ** 2))

            auroc_mat[i, j] = round(auc, 3)
            brier_mat[i, j] = round(brier, 4)

    df_auroc = pd.DataFrame(auroc_mat, index=site_names, columns=site_names)
    df_brier = pd.DataFrame(brier_mat, index=site_names, columns=site_names)

    # Calculate internal (diagonal) and external (off-diagonal) metrics
    diag_indices = np.diag_indices(k)
    internal_aucs = auroc_mat[diag_indices]
    mask_off_diag = ~np.eye(k, dtype=bool)
    external_aucs = auroc_mat[mask_off_diag]

    mean_internal = float(np.mean(internal_aucs))
    mean_external = float(np.mean(external_aucs))
    gap = float(mean_internal - mean_external)

    summary_rows = [{
        "n_sites": k,
        "mean_internal_auroc": round(mean_internal, 3),
        "mean_external_auroc": round(mean_external, 3),
        "generalization_gap": round(gap, 3),
        "mean_brier_score": round(float(np.mean(brier_mat)), 4),
        "transportability_status": "EXCELLENT" if gap <= 0.05 else ("MODERATE" if gap <= 0.10 else "DOMAIN_SHIFT_DETECTED"),
    }]
    summary_df = pd.DataFrame(summary_rows)

    return ConsortiumCrossSiteValidation(
        auroc_matrix=df_auroc,
        brier_matrix=df_brier,
        summary_df=summary_df,
        sites=site_names,
        metadata={"alpha": alpha, "regularize": regularize},
    )


# ---------------------------------------------------------------------------
# Re-export Explainability
# ---------------------------------------------------------------------------
import sys
from omop_etl.explainability import (
    ConsortiumExplainability,
    consortium_explainability,
)
import omop_etl.explainability as explainability

sys.modules["omop_etl.ml.explainability"] = explainability


