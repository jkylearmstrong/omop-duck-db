"""Kaplan-Meier Survival and Fine-Gray Competing Risks Cumulative Incidence (CIF) Estimator.

Consumes cohort survival datasets (such as prepare_competing_risks_data output)
and computes Kaplan-Meier survival curves, Greenwood standard errors, confidence
intervals, and non-parametric Aalen-Johansen / Fine-Gray CIF for competing risks.
"""

from __future__ import annotations

from typing import Any, Sequence
import numpy as np
import pandas as pd


class SurvivalResult:
    """Encapsulates survival curve and cumulative incidence estimates."""

    def __init__(
        self,
        timeline: pd.DataFrame,
        summary_stats: dict[str, Any],
        has_competing_risks: bool = False,
    ):
        self._timeline = timeline
        self._summary_stats = summary_stats
        self._has_competing_risks = has_competing_risks

    @property
    def timeline(self) -> pd.DataFrame:
        """Tidy DataFrame containing event timeline, survival, and CIF."""
        return self._timeline

    @property
    def has_competing_risks(self) -> bool:
        """Whether competing risks were detected and estimated."""
        return self._has_competing_risks

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the tidy survival timeline DataFrame."""
        return self._timeline.copy()

    def summary(self) -> dict[str, Any]:
        """Summary metrics including median survival, total events, and rates."""
        return dict(self._summary_stats)

    def predict_at_time(self, time_point: float) -> dict[str, float]:
        """Evaluates survival and cumulative incidence at a specific time point."""
        sub = self._timeline[self._timeline["time"] <= time_point]
        if len(sub) == 0:
            res = {
                "time": float(time_point),
                "survival": 1.0,
                "std_err": 0.0,
                "ci_lower": 1.0,
                "ci_upper": 1.0,
            }
            if self._has_competing_risks:
                res["cif_primary"] = 0.0
                res["cif_competing"] = 0.0
            return res

        last_row = sub.iloc[-1]
        res = {
            "time": float(time_point),
            "survival": float(last_row["survival"]),
            "std_err": float(last_row["std_err"]),
            "ci_lower": float(last_row["ci_lower"]),
            "ci_upper": float(last_row["ci_upper"]),
        }
        if self._has_competing_risks:
            res["cif_primary"] = float(last_row["cif_primary"])
            res["cif_competing"] = float(last_row["cif_competing"])
            if "naive_km_primary" in last_row:
                res["naive_km_primary"] = float(last_row["naive_km_primary"])
        return res

    def plot(
        self,
        title: str | None = None,
        output_path: str | None = None,
        show: bool = False,
    ) -> Any:
        """Generates a publication-quality survival / CIF curve plot.

        Uses matplotlib if available. Can save to file or return the figure.
        """
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            # Fallback if matplotlib is not installed
            return None

        fig, ax = plt.subplots(figsize=(8, 5))
        df = self._timeline

        if self._has_competing_risks:
            ax.step(df["time"], df["cif_primary"], where="post", label="Primary Event (CIF)", color="#1f77b4", lw=2)
            ax.step(df["time"], df["cif_competing"], where="post", label="Competing Event (CIF)", color="#d62728", lw=2)
            if "naive_km_primary" in df.columns:
                ax.step(
                    df["time"],
                    df["naive_km_primary"],
                    where="post",
                    label="Naive (1 - KM)",
                    color="#1f77b4",
                    linestyle="--",
                    alpha=0.6,
                )
            ax.set_ylabel("Cumulative Incidence")
            ax.set_title(title or "Fine-Gray Cumulative Incidence Functions (Competing Risks)")
            ax.set_ylim(0, max(1.0, float(df[["cif_primary", "cif_competing"]].max().max() * 1.15)))
        else:
            ax.step(df["time"], df["survival"], where="post", label="Kaplan-Meier Survival S(t)", color="#1f77b4", lw=2)
            ax.fill_between(
                df["time"],
                df["ci_lower"],
                df["ci_upper"],
                step="post",
                alpha=0.2,
                color="#1f77b4",
                label="95% CI",
            )
            ax.set_ylabel("Survival Probability S(t)")
            ax.set_title(title or "Kaplan-Meier Survival Curve")
            ax.set_ylim(0, 1.05)

        ax.set_xlabel("Time (Days)")
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.legend(loc="best")
        plt.tight_layout()

        if output_path:
            fig.savefig(output_path, dpi=300)
        if show:
            plt.show()
        return fig


def estimate_km_survival(
    df_or_tuple: Any,
    time_col: str | None = None,
    status_col: str | None = None,
    ci_type: str = "log-log",
    alpha: float = 0.05,
) -> SurvivalResult:
    r"""Estimates Kaplan-Meier survival curves and Fine-Gray competing risk CIF.

    Computes non-parametric Kaplan-Meier survival:

    .. math::
        \hat{S}(t) = \prod_{t_i \le t} \left(1 - \frac{d_i}{n_i}\right)

    With Greenwood standard error confidence intervals:

    .. math::
        \widehat{\text{Var}}(\hat{S}(t)) = \hat{S}(t)^2 \sum_{t_i \le t} \frac{d_i}{n_i (n_i - d_i)}

    For 3-state competing risks (status 0 = censored, 1 = primary, 2 = competing):
    computes cause-specific Aalen-Johansen / Fine-Gray Cumulative Incidence Functions:

    .. math::
        \hat{I}_k(t) = \sum_{t_i \le t} \hat{S}_{\text{overall}}(t_{i-1}) \frac{d_{ki}}{n_i}

    Args:
        df_or_tuple: DataFrame or tuple `(df, summary)` from `prepare_competing_risks_data()`.
        time_col: Name of time column (auto-detected if None).
        status_col: Name of status column (auto-detected if None).
        ci_type: Confidence interval type ('log-log' or 'linear').
        alpha: Significance level (default 0.05 for 95% CI).

    Returns:
        SurvivalResult: Object with tidy timeline, summary statistics, and plotting hooks.
    """
    if isinstance(df_or_tuple, tuple):
        df = df_or_tuple[0]
    elif isinstance(df_or_tuple, dict) and "data" in df_or_tuple:
        df = df_or_tuple["data"]
    elif isinstance(df_or_tuple, pd.DataFrame):
        df = df_or_tuple
    else:
        raise TypeError(f"Unsupported input type for estimate_km_survival: {type(df_or_tuple)}")

    if len(df) == 0:
        raise ValueError("Input DataFrame is empty.")

    # Auto-detect time_col
    if time_col is None:
        candidates = ["time_days", "time", "followup_days", "duration", "days"]
        for c in candidates:
            if c in df.columns:
                time_col = c
                break
        if time_col is None:
            raise ValueError(f"Could not auto-detect time column. Available columns: {list(df.columns)}")

    # Auto-detect status_col
    if status_col is None:
        candidates = ["status", "event", "composite_event", "outcome"]
        for c in candidates:
            if c in df.columns:
                status_col = c
                break
        if status_col is None:
            raise ValueError(f"Could not auto-detect status column. Available columns: {list(df.columns)}")

    times = df[time_col].to_numpy(dtype=np.float64)
    statuses = df[status_col].to_numpy(dtype=np.int64)

    unique_statuses = set(statuses)
    has_competing = 2 in unique_statuses and 1 in unique_statuses

    n_total = len(times)
    unique_times = np.sort(np.unique(times))

    # Critical z-score for (1 - alpha) CI
    z_crit = 1.959963984540054  # 95% standard normal

    # Initialize at t = 0
    t_list = [0.0]
    at_risk_list = [n_total]
    events_list = [0]
    censored_list = [0]
    surv_list = [1.0]
    se_list = [0.0]
    ci_lower_list = [1.0]
    ci_upper_list = [1.0]

    # For competing risks:
    events_1_list = [0]
    events_2_list = [0]
    cif_1_list = [0.0]
    cif_2_list = [0.0]
    naive_km_1_list = [0.0]

    current_surv = 1.0
    greenwood_sum = 0.0

    current_overall_surv = 1.0
    cif_1 = 0.0
    cif_2 = 0.0
    naive_km_surv_1 = 1.0
    naive_greenwood_1 = 0.0

    for t in unique_times:
        if t <= 0:
            continue

        # Risk set at time t: individuals with time >= t
        n_at_risk = int(np.sum(times >= t))
        if n_at_risk <= 0:
            continue

        # Events at time t
        mask_t = times == t
        st_at_t = statuses[mask_t]

        if has_competing:
            d_1 = int(np.sum(st_at_t == 1))
            d_2 = int(np.sum(st_at_t == 2))
            d_all = d_1 + d_2
            c_t = int(np.sum(st_at_t == 0))

            # Update overall survival
            prev_overall = current_overall_surv
            if d_all > 0:
                current_overall_surv *= (1.0 - d_all / n_at_risk)

            # Update CIFs
            cif_1 += prev_overall * (d_1 / n_at_risk)
            cif_2 += prev_overall * (d_2 / n_at_risk)

            # Update naive 1 - KM for primary
            if d_1 > 0:
                naive_km_surv_1 *= (1.0 - d_1 / n_at_risk)

            # Composite event survival
            d_t = d_all
        else:
            d_t = int(np.sum(st_at_t > 0))
            c_t = int(np.sum(st_at_t == 0))
            d_1 = d_t
            d_2 = 0

        # Update standard KM
        if d_t > 0:
            hazard = d_t / n_at_risk
            current_surv *= (1.0 - hazard)
            denom = n_at_risk * (n_at_risk - d_t)
            if denom > 0:
                greenwood_sum += d_t / denom

        var_s = (current_surv ** 2) * greenwood_sum
        se_s = np.sqrt(max(0.0, var_s))

        # Confidence intervals
        if current_surv >= 1.0:
            ci_low = 1.0
            ci_high = 1.0
        elif current_surv <= 0.0:
            ci_low = 0.0
            ci_high = 0.0
        elif ci_type == "log-log":
            # Kalbfleisch and Prentice log-log transform
            log_s = np.log(current_surv)
            if log_s == 0:
                ci_low = ci_high = current_surv
            else:
                w = z_crit * se_s / (current_surv * np.abs(log_s))
                ci_low = float(np.clip(current_surv ** np.exp(w), 0.0, 1.0))
                ci_high = float(np.clip(current_surv ** np.exp(-w), 0.0, 1.0))
        else:  # Linear Wald
            ci_low = float(np.clip(current_surv - z_crit * se_s, 0.0, 1.0))
            ci_high = float(np.clip(current_surv + z_crit * se_s, 0.0, 1.0))

        t_list.append(float(t))
        at_risk_list.append(n_at_risk)
        events_list.append(d_t)
        censored_list.append(c_t)
        surv_list.append(float(current_surv))
        se_list.append(float(se_s))
        ci_lower_list.append(ci_low)
        ci_upper_list.append(ci_high)

        if has_competing:
            events_1_list.append(d_1)
            events_2_list.append(d_2)
            cif_1_list.append(float(cif_1))
            cif_2_list.append(float(cif_2))
            naive_km_1_list.append(float(1.0 - naive_km_surv_1))

    timeline_data = {
        "time": t_list,
        "n_at_risk": at_risk_list,
        "n_events": events_list,
        "n_censored": censored_list,
        "survival": surv_list,
        "std_err": se_list,
        "ci_lower": ci_lower_list,
        "ci_upper": ci_upper_list,
    }

    if has_competing:
        timeline_data["events_primary"] = events_1_list
        timeline_data["events_competing"] = events_2_list
        timeline_data["cif_primary"] = cif_1_list
        timeline_data["cif_competing"] = cif_2_list
        timeline_data["naive_km_primary"] = naive_km_1_list

    timeline_df = pd.DataFrame(timeline_data)

    # Calculate summary statistics
    # Median survival: first time where survival <= 0.5
    under_half = timeline_df[timeline_df["survival"] <= 0.5]
    median_time = float(under_half.iloc[0]["time"]) if len(under_half) > 0 else None

    summary_stats = {
        "n_total": n_total,
        "n_events": int(np.sum(events_list)),
        "n_censored": int(np.sum(censored_list)),
        "median_survival_time": median_time,
        "has_competing_risks": has_competing,
        "final_survival": float(surv_list[-1]),
    }
    if has_competing:
        summary_stats["final_cif_primary"] = float(cif_1_list[-1])
        summary_stats["final_cif_competing"] = float(cif_2_list[-1])

    return SurvivalResult(
        timeline=timeline_df,
        summary_stats=summary_stats,
        has_competing_risks=has_competing,
    )
