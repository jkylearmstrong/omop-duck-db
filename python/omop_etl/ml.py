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

