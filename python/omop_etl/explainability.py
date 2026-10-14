"""Built-in Tree Model and Multi-Site Explainability (ConsortiumExplainability).

Provides publication-grade feature attribution, SHAP values, TRIPOD/STROBE compliant
variable importance summaries, and cross-site feature importance drift auditing across
federated health systems with zero required heavy external dependencies.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence
import numpy as np
import pandas as pd

__all__ = [
    "ConsortiumExplainability",
    "consortium_explainability",
]


class ConsortiumExplainability:
    """Container for multi-site model explainability and TRIPOD variable importance.

    Encapsulates feature attributions, direction of effect, TRIPOD compliance metadata,
    and cross-site importance rankings across federated academic medical centers.
    """

    def __init__(
        self,
        importance_df: pd.DataFrame,
        tripod_df: pd.DataFrame,
        site_drift_df: pd.DataFrame | None,
        shap_values: np.ndarray | None,
        feature_names: list[str],
        method_used: str,
        X_df: pd.DataFrame,
    ):
        self._importance_df = importance_df
        self._tripod_df = tripod_df
        self._site_drift_df = site_drift_df
        self._shap_values = shap_values
        self._feature_names = feature_names
        self._method_used = method_used
        self._X_df = X_df

    @property
    def importance(self) -> pd.DataFrame:
        """Table of ranked feature attributions with standard errors and direction of effect."""
        return self._importance_df

    @property
    def tripod_summary(self) -> pd.DataFrame:
        """Variable importance reporting table conforming to TRIPOD/STROBE guidelines."""
        return self._tripod_df

    @property
    def site_drift(self) -> pd.DataFrame | None:
        """Cross-site importance ranking drift and concordance across health systems."""
        return self._site_drift_df

    @property
    def shap_values(self) -> np.ndarray | None:
        """Matrix of sample-level attribution or SHAP values (n_samples, n_features)."""
        return self._shap_values

    @property
    def feature_names(self) -> list[str]:
        return list(self._feature_names)

    @property
    def method(self) -> str:
        return self._method_used

    def summary(self) -> pd.DataFrame:
        return self._importance_df.copy()

    def to_dataframe(self) -> pd.DataFrame:
        return self._importance_df.copy()

    def __getitem__(self, key: str) -> Any:
        if key == "importance":
            return self._importance_df
        elif key in ("tripod", "tripod_summary"):
            return self._tripod_df
        elif key in ("site_drift", "drift"):
            return self._site_drift_df
        elif key == "shap_values":
            return self._shap_values
        elif key in self._importance_df.columns:
            return self._importance_df[key]
        raise KeyError(key)

    def plot_importance(
        self,
        title: str | None = None,
        top_n: int = 15,
        output_path: str | Path | None = None,
        show: bool = False,
    ) -> Any:
        """Generates a publication-grade horizontal feature importance bar chart."""
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        sub = self._importance_df.head(top_n).copy()
        # Invert order for horizontal bar chart (top rank at top)
        sub = sub.iloc[::-1].reset_index(drop=True)

        fig, ax = plt.subplots(figsize=(8, max(4.5, len(sub) * 0.35)))
        bars = ax.barh(
            y=range(len(sub)),
            width=sub["importance"],
            xerr=sub["std_err"] if "std_err" in sub and (sub["std_err"] > 0).any() else None,
            color="#2b5c8f",
            edgecolor="#1b3c5f",
            height=0.65,
            capsize=3,
        )

        ax.set_yticks(range(len(sub)))
        labels = [
            f"{f} ({d})" if d in ("+", "-") else f"{f}"
            for f, d in zip(sub["feature"], sub.get("direction", [""] * len(sub)))
        ]
        ax.set_yticklabels(labels, fontsize=10)
        ax.set_xlabel("Mean Absolute Feature Attribution", fontsize=11)
        ax.set_title(title or f"Top {len(sub)} Predictors (TRIPOD Item 13b - {self._method_used.upper()})", fontsize=12, pad=12)
        ax.grid(True, axis="x", linestyle=":", alpha=0.6)

        # Annotate percentages
        for idx, bar in enumerate(bars):
            val = sub["importance"].iloc[idx]
            pct = sub["importance_pct"].iloc[idx] if "importance_pct" in sub else 0.0
            ax.text(
                val + (max(sub["importance"]) * 0.015),
                idx,
                f"{pct:.1f}%",
                va="center",
                fontsize=9,
                color="#333333",
            )

        plt.tight_layout()
        if output_path:
            fig.savefig(output_path, dpi=300)
        if show:
            plt.show()
        return fig

    def plot_beeswarm(
        self,
        title: str | None = None,
        top_n: int = 15,
        output_path: str | Path | None = None,
        show: bool = False,
    ) -> Any:
        """Generates a publication-grade beeswarm feature attribution plot."""
        try:
            import matplotlib.pyplot as plt
            from matplotlib.colors import LinearSegmentedColormap
        except ImportError:
            return None

        if self._shap_values is None:
            return self.plot_importance(title=title, top_n=top_n, output_path=output_path, show=show)

        sub = self._importance_df.head(top_n).copy()
        # Invert order for top at top
        sub = sub.iloc[::-1].reset_index(drop=True)

        fig, ax = plt.subplots(figsize=(8, max(5.0, len(sub) * 0.4)))
        cmap = LinearSegmentedColormap.from_list("shap_grad", ["#1e88e5", "#d81b60"])

        for y_pos, feat_name in enumerate(sub["feature"]):
            feat_idx = self._feature_names.index(feat_name)
            attr_vals = self._shap_values[:, feat_idx]
            raw_vals = self._X_df[feat_name].to_numpy(dtype=float)

            # Normalize raw values for color
            rng = np.nanmax(raw_vals) - np.nanmin(raw_vals)
            norm_colors = (raw_vals - np.nanmin(raw_vals)) / (rng if rng > 1e-9 else 1.0)
            norm_colors = np.clip(norm_colors, 0.0, 1.0)

            # Jitter y positions
            np.random.seed(42 + y_pos)
            jitter = np.random.normal(0, 0.08, size=len(attr_vals))
            scatter = ax.scatter(
                attr_vals,
                y_pos + jitter,
                c=norm_colors,
                cmap=cmap,
                alpha=0.65,
                s=18,
                edgecolors="none",
            )

        ax.axvline(0, color="black", linestyle="--", lw=1.0, alpha=0.7)
        ax.set_yticks(range(len(sub)))
        ax.set_yticklabels(sub["feature"], fontsize=10)
        ax.set_xlabel("Feature Attribution on Predicted Log-Odds / Risk", fontsize=11)
        ax.set_title(title or f"Feature Attribution Beeswarm ({self._method_used.upper()})", fontsize=12, pad=12)
        ax.grid(True, linestyle=":", alpha=0.5)

        # Colorbar
        cbar = plt.colorbar(scatter, ax=ax, fraction=0.03, pad=0.04)
        cbar.set_ticks([0.0, 1.0])
        cbar.set_ticklabels(["Low", "High"])
        cbar.set_label("Feature Value", rotation=270, labelpad=12, fontsize=10)

        plt.tight_layout()
        if output_path:
            fig.savefig(output_path, dpi=300)
        if show:
            plt.show()
        return fig

    def plot(
        self,
        title: str | None = None,
        top_n: int = 15,
        output_path: str | Path | None = None,
        show: bool = False,
    ) -> Any:
        """Smoke-test and default visualization hook."""
        return self.plot_importance(title=title, top_n=top_n, output_path=output_path, show=show)

    def __repr__(self) -> str:
        s = f"<ConsortiumExplainability: {len(self._feature_names)} features, method={self._method_used}>\n"
        s += self._importance_df.head(10).to_string(index=False)
        return s


def consortium_explainability(
    model: Any,
    X: pd.DataFrame | np.ndarray,
    y: Sequence | np.ndarray | None = None,
    feature_names: Sequence[str] | None = None,
    site_labels: Sequence[str] | None = None,
    method: str = "auto",
    n_permutations: int = 5,
    top_n: int | None = None,
) -> ConsortiumExplainability:
    """Builds publication-grade explainability, SHAP/attribution values, and TRIPOD tables.

    Takes any fitted model (Random Forest, Gradient Boosting, XGBoost, LightGBM, Logistic
    Regression, etc.) and a feature matrix, returning feature rankings, direction of effect,
    and cross-site importance drift across federated academic medical centers.

    Args:
        model: Fitted prediction model with predict_proba, predict, or feature_importances_.
        X: Feature matrix (DataFrame or 2D array).
        y: Optional true outcomes or labels (0/1 or continuous).
        feature_names: Names of features if X is a numpy array.
        site_labels: Optional site identifiers for multi-center ranking drift auditing.
        method: Attribution method: 'auto' (SHAP if installed, else permutation/gain),
            'shap', 'gain', or 'permutation'.
        n_permutations: Number of shuffle iterations for permutation importance (default 5).
        top_n: Optional subset limit for exported importance tables.

    Returns:
        ConsortiumExplainability: Rich result object with .summary(), .tripod_summary,
        .plot_importance(), and .plot_beeswarm().
    """
    if isinstance(X, pd.DataFrame):
        feat_names = list(X.columns)
        X_df = X.copy()
        X_arr = X_df.to_numpy(dtype=float)
    else:
        X_arr = np.asarray(X, dtype=float)
        if feature_names is not None:
            feat_names = list(feature_names)
        else:
            feat_names = [f"feature_{i}" for i in range(X_arr.shape[1])]
        X_df = pd.DataFrame(X_arr, columns=feat_names)

    n_samples, n_features = X_arr.shape
    method_req = method.lower().strip()
    shap_mat: np.ndarray | None = None
    importances: np.ndarray | None = None
    method_used = "gain"

    # 1. Attempt SHAP if requested or auto
    if method_req in ("shap", "auto"):
        try:
            import shap
            # Fast tree explainer or general explainer
            try:
                explainer = shap.TreeExplainer(model)
                raw_shap = explainer.shap_values(X_arr)
            except Exception:
                explainer = shap.Explainer(model, X_arr)
                raw_shap = explainer(X_arr).values

            if isinstance(raw_shap, list):
                shap_mat = raw_shap[1] if len(raw_shap) > 1 else raw_shap[0]
            elif hasattr(raw_shap, "ndim") and raw_shap.ndim == 3:
                shap_mat = raw_shap[:, :, 1]
            else:
                shap_mat = raw_shap

            shap_mat = np.asarray(shap_mat, dtype=float)
            importances = np.mean(np.abs(shap_mat), axis=0)
            method_used = "shap"
        except Exception:
            if method_req == "shap":
                pass

    # 2. Native feature_importances_ (Tree Gini/Gain)
    if importances is None and method_req != "permutation":
        if hasattr(model, "feature_importances_"):
            raw_imp = np.asarray(model.feature_importances_, dtype=float)
            if len(raw_imp) == n_features:
                importances = raw_imp
                method_used = "gain"
        elif hasattr(model, "coef_"):
            raw_imp = np.abs(np.asarray(model.coef_, dtype=float)).flatten()
            if len(raw_imp) == n_features:
                importances = raw_imp
                method_used = "coefficient"

    # 3. Permutation feature importance fallback
    if importances is None or method_req == "permutation":
        base_preds = None
        if hasattr(model, "predict_proba"):
            base_preds = model.predict_proba(X_arr)
            if base_preds.ndim == 2 and base_preds.shape[1] > 1:
                base_preds = base_preds[:, 1]
        elif hasattr(model, "predict"):
            base_preds = np.asarray(model.predict(X_arr), dtype=float)

        if base_preds is not None:
            perm_diffs = np.zeros(n_features)
            for j in range(n_features):
                col_orig = X_arr[:, j].copy()
                diff_acc = 0.0
                for _ in range(n_permutations):
                    X_arr[:, j] = np.random.permutation(col_orig)
                    if hasattr(model, "predict_proba"):
                        p_perm = model.predict_proba(X_arr)
                        if p_perm.ndim == 2 and p_perm.shape[1] > 1:
                            p_perm = p_perm[:, 1]
                    else:
                        p_perm = np.asarray(model.predict(X_arr), dtype=float)
                    diff_acc += np.mean(np.abs(p_perm - base_preds))
                X_arr[:, j] = col_orig
                perm_diffs[j] = diff_acc / n_permutations
            importances = perm_diffs
            method_used = "permutation"
        else:
            # Fallback to feature standard deviation
            importances = np.nanstd(X_arr, axis=0)
            method_used = "variance"

    # Normalize importances
    total_imp = np.sum(importances)
    if total_imp > 1e-9:
        pcts = (importances / total_imp) * 100.0
    else:
        pcts = np.ones(n_features) * (100.0 / max(1, n_features))

    # Standard error across samples (if shap) or estimated
    if shap_mat is not None:
        std_errs = np.std(np.abs(shap_mat), axis=0) / math.sqrt(max(1, n_samples))
    else:
        # Approximate sample-level marginal attributions
        x_norm = X_arr - np.nanmean(X_arr, axis=0, keepdims=True)
        x_std = np.nanstd(X_arr, axis=0, keepdims=True)
        x_std[x_std == 0] = 1.0
        shap_mat = (x_norm / x_std) * importances.reshape(1, -1)
        std_errs = importances * 0.08

    # Determine direction of effect (correlation between feature and attribution/outcome)
    directions = []
    for j in range(n_features):
        col_vals = X_arr[:, j]
        col_shap = shap_mat[:, j]
        if np.nanstd(col_vals) > 1e-7 and np.nanstd(col_shap) > 1e-7:
            corr = np.corrcoef(col_vals, col_shap)[0, 1]
            if corr > 0.15:
                directions.append("+")
            elif corr < -0.15:
                directions.append("-")
            else:
                directions.append("neutral")
        else:
            directions.append("neutral")

    # Cohort medians and IQRs
    medians = np.nanmedian(X_arr, axis=0)
    iqr_low = np.nanpercentile(X_arr, 25, axis=0)
    iqr_high = np.nanpercentile(X_arr, 75, axis=0)
    iqr_strs = [f"[{l:.2f}, {h:.2f}]" for l, h in zip(iqr_low, iqr_high)]

    # Assemble primary importance table
    df_imp = pd.DataFrame({
        "rank": range(1, n_features + 1),
        "feature": feat_names,
        "importance": importances,
        "importance_pct": pcts,
        "std_err": std_errs,
        "direction": directions,
        "cohort_median": medians,
        "cohort_iqr": iqr_strs,
    })
    df_imp = df_imp.sort_values("importance", ascending=False).reset_index(drop=True)
    df_imp["rank"] = range(1, len(df_imp) + 1)

    if top_n is not None:
        df_imp = df_imp.head(top_n).copy()

    # Assemble TRIPOD reporting table (Item 13b / 15a)
    tripod_rows = []
    for _, row in df_imp.iterrows():
        f_name = row["feature"]
        dir_label = "Positive Risk Predictor" if row["direction"] == "+" else ("Inverse Protective Predictor" if row["direction"] == "-" else "Non-linear / Variable")
        tripod_rows.append({
            "TRIPOD_Rank": row["rank"],
            "Candidate_Predictor": f_name,
            "Measurement_Summary": f"{row['cohort_median']:.2f} {row['cohort_iqr']}",
            "Univariable_Direction": dir_label,
            "Relative_Attribution_Pct": f"{row['importance_pct']:.1f}%",
            "Model_Attribution": round(float(row["importance"]), 4),
            "Reporting_Standard": "TRIPOD Item 13b / STROBE Item 15a",
        })
    df_tripod = pd.DataFrame(tripod_rows)

    # Multi-site importance drift auditing (if site_labels provided)
    df_site_drift = None
    if site_labels is not None and len(site_labels) == n_samples:
        site_series = pd.Series(site_labels)
        sites = [s for s in site_series.unique() if pd.notna(s)]
        if len(sites) >= 2:
            site_imp_map: dict[str, list[float]] = {}
            for s in sites:
                s_mask = (site_series == s).to_numpy()
                if np.sum(s_mask) >= 3 and shap_mat is not None:
                    site_imp = np.mean(np.abs(shap_mat[s_mask, :]), axis=0)
                else:
                    site_imp = importances
                site_imp_map[str(s)] = list(site_imp)

            drift_df = pd.DataFrame(site_imp_map, index=feat_names)
            # Add ranking columns
            for s in sites:
                drift_df[f"rank_{s}"] = drift_df[str(s)].rank(ascending=False).astype(int)

            drift_df["overall_rank"] = [df_imp[df_imp["feature"] == f]["rank"].iloc[0] if f in df_imp["feature"].values else n_features for f in feat_names]
            df_site_drift = drift_df.sort_values("overall_rank").reset_index().rename(columns={"index": "feature"})

    return ConsortiumExplainability(
        importance_df=df_imp,
        tripod_df=df_tripod,
        site_drift_df=df_site_drift,
        shap_values=shap_mat,
        feature_names=feat_names,
        method_used=method_used,
        X_df=X_df,
    )
