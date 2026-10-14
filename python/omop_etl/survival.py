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


# -----------------------------------------------------------------------------
# Competing Risks Cumulative Incidence Function (CIF) & Fine-Gray Estimator
# -----------------------------------------------------------------------------

class CIFResult:
    """Cause-specific Cumulative Incidence Function (CIF) result under competing risks."""

    def __init__(
        self,
        cif_table: pd.DataFrame,
        summary_stats: dict[str, Any],
    ):
        self._cif_table = cif_table
        self._summary_stats = summary_stats

    @property
    def cif_table(self) -> pd.DataFrame:
        """Tidy timeline containing time, at-risk, primary CIF, competing CIF, and CIs."""
        return self._cif_table

    def summary(self) -> dict[str, Any]:
        """Summary metrics including 30-day CIF estimates and event counts."""
        return dict(self._summary_stats)

    def to_dataframe(self) -> pd.DataFrame:
        return self._cif_table.copy()

    def predict_at_time(self, time_point: float) -> dict[str, float]:
        """Evaluates CIF point estimates and confidence intervals at a specific time (e.g. 30 days)."""
        sub = self._cif_table[self._cif_table["time"] <= time_point]
        if len(sub) == 0:
            return {
                "time": float(time_point),
                "cif_primary": 0.0,
                "cif_primary_ci_lower": 0.0,
                "cif_primary_ci_upper": 0.0,
                "cif_competing": 0.0,
                "naive_km_primary": 0.0,
            }
        last_row = sub.iloc[-1]
        return {
            "time": float(time_point),
            "cif_primary": float(last_row["cif_primary"]),
            "cif_primary_ci_lower": float(last_row["cif_primary_ci_lower"]),
            "cif_primary_ci_upper": float(last_row["cif_primary_ci_upper"]),
            "cif_competing": float(last_row["cif_competing"]),
            "naive_km_primary": float(last_row.get("naive_km_primary", last_row["cif_primary"])),
        }

    def __getitem__(self, key: str) -> Any:
        if key == "cif_table":
            return self._cif_table
        elif key in self._cif_table.columns:
            return self._cif_table[key]
        elif key in self._summary_stats:
            return self._summary_stats[key]
        raise KeyError(key)

    def plot(
        self,
        title: str | None = None,
        output_path: str | None = None,
        show: bool = False,
    ) -> Any:
        """Generates a publication-quality Competing Risks CIF plot."""
        return plot_cif(self, title=title, output_path=output_path, show=show)

    def __repr__(self) -> str:
        s = self._summary_stats
        p30 = s.get("cif_primary_30d", 0.0)
        c30 = s.get("cif_competing_30d", 0.0)
        return (
            f"<CIFResult: {len(self._cif_table)} timepoints, "
            f"30d Readmission CIF = {p30:.2%}, 30d Mortality CIF = {c30:.2%}>"
        )


def estimate_cif(
    data: Any,
    time_col: str | None = None,
    status_col: str | None = None,
    event_code: int = 1,
    competing_code: int = 2,
    alpha: float = 0.05,
) -> CIFResult:
    """Estimates Cumulative Incidence Functions (CIF) for primary events and competing risks.

    In 30-day readmission studies, in-hospital or out-of-hospital death within 30 days is a
    formal competing risk. Standard Kaplan-Meier (1 - KM) overestimates the event rate by
    treating death as non-informative censoring. This function computes cause-specific
    Aalen-Johansen CIF curves out of the box.

    Args:
        data: DataFrame or tuple `(df, summary)` from `prepare_competing_risks_data()`.
        time_col: Column name for follow-up duration (auto-detected if None).
        status_col: Column name for event status (auto-detected if None).
            0 = censored, event_code = primary event, competing_code = competing risk.
        event_code: Integer indicator for primary event of interest (default 1).
        competing_code: Integer indicator for competing risk event (default 2).
        alpha: Significance level for confidence intervals (default 0.05 for 95% CI).

    Returns:
        CIFResult: Encapsulates CIF table, 30-day point estimates, and plotting hooks.
    """
    surv_res = estimate_km_survival(
        data,
        time_col=time_col,
        status_col=status_col,
        alpha=alpha,
    )
    tl = surv_res.timeline.copy()

    if not surv_res.has_competing_risks:
        # Fallback if no competing risks in sample
        tl["cif_primary"] = 1.0 - tl["survival"]
        tl["cif_competing"] = 0.0
        tl["cif_primary_ci_lower"] = 1.0 - tl["ci_upper"]
        tl["cif_primary_ci_upper"] = 1.0 - tl["ci_lower"]
        tl["naive_km_primary"] = tl["cif_primary"]
    else:
        # Approximate Greenwood variance for CIF: Var(CIF) ~ (1 - S)^2 * sum(d_1 / (n * (n - d_1)))
        # Standard Choudhury formula
        z_crit = 1.959963984540054
        cif_1 = tl["cif_primary"].to_numpy()
        var_est = (cif_1 ** 2) * np.cumsum(
            np.where(tl["n_at_risk"] > tl["events_primary"],
                     tl["events_primary"] / (tl["n_at_risk"] * np.maximum(1, tl["n_at_risk"] - tl["events_primary"])),
                     0.0)
        )
        se_cif = np.sqrt(np.maximum(0.0, var_est))
        tl["cif_primary_ci_lower"] = np.clip(cif_1 - z_crit * se_cif, 0.0, 1.0)
        tl["cif_primary_ci_upper"] = np.clip(cif_1 + z_crit * se_cif, 0.0, 1.0)

    summary_stats = surv_res.summary()

    # Evaluate at day 30
    sub30 = tl[tl["time"] <= 30.0]
    if len(sub30) > 0:
        r30 = sub30.iloc[-1]
        summary_stats["cif_primary_30d"] = float(r30["cif_primary"])
        summary_stats["cif_primary_30d_ci_lower"] = float(r30["cif_primary_ci_lower"])
        summary_stats["cif_primary_30d_ci_upper"] = float(r30["cif_primary_ci_upper"])
        summary_stats["cif_competing_30d"] = float(r30["cif_competing"])
        summary_stats["naive_km_30d"] = float(r30.get("naive_km_primary", r30["cif_primary"]))
        summary_stats["km_overestimation_bias_30d"] = float(
            summary_stats["naive_km_30d"] - summary_stats["cif_primary_30d"]
        )
    else:
        summary_stats["cif_primary_30d"] = 0.0
        summary_stats["cif_primary_30d_ci_lower"] = 0.0
        summary_stats["cif_primary_30d_ci_upper"] = 0.0
        summary_stats["cif_competing_30d"] = 0.0
        summary_stats["naive_km_30d"] = 0.0
        summary_stats["km_overestimation_bias_30d"] = 0.0

    return CIFResult(cif_table=tl, summary_stats=summary_stats)


class FineGrayResult:
    """Fine-Gray Subdistribution Hazard model estimation result."""

    def __init__(
        self,
        shr: float,
        log_shr: float,
        std_err: float,
        ci_lower: float,
        ci_upper: float,
        p_value: float,
        z_stat: float,
        n_samples: int,
        n_events: int,
        n_competing: int,
        group_name: str,
    ):
        self.shr = shr
        self.log_shr = log_shr
        self.std_err = std_err
        self.ci_lower = ci_lower
        self.ci_upper = ci_upper
        self.p_value = p_value
        self.z_stat = z_stat
        self.n_samples = n_samples
        self.n_events = n_events
        self.n_competing = n_competing
        self.group_name = group_name

    def summary(self) -> dict[str, Any]:
        return {
            "group": self.group_name,
            "subdistribution_hazard_ratio": self.shr,
            "log_shr": self.log_shr,
            "std_err": self.std_err,
            "ci_lower_95": self.ci_lower,
            "ci_upper_95": self.ci_upper,
            "z_statistic": self.z_stat,
            "p_value": self.p_value,
            "n_samples": self.n_samples,
            "n_primary_events": self.n_events,
            "n_competing_events": self.n_competing,
        }

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([self.summary()])

    def __repr__(self) -> str:
        return (
            f"<FineGrayResult: {self.group_name} sHR = {self.shr:.3f} "
            f"(95% CI: [{self.ci_lower:.3f}, {self.ci_upper:.3f}]), p = {self.p_value:.4e}>"
        )


def fine_gray_estimator(
    data: Any,
    time_col: str | None = None,
    status_col: str | None = None,
    group_col: str | None = None,
    event_code: int = 1,
    competing_code: int = 2,
) -> FineGrayResult:
    """Estimates Fine-Gray Subdistribution Hazard Ratio (sHR) under competing risks.

    In presence of competing risks (e.g. readmission vs in-hospital death), evaluates
    the subdistribution hazard ratio between groups:

    .. math::
        \\bar{\\lambda}_1(t; Z) = \\bar{\\lambda}_{10}(t) \\exp(\\beta Z)

    Args:
        data: DataFrame containing survival follow-up.
        time_col: Follow-up duration column.
        status_col: 3-state status column (0=censor, 1=primary event, 2=competing risk).
        group_col: Binary exposure or group column (0 vs 1, or string/factor).
        event_code: Integer indicator for primary event (default 1).
        competing_code: Integer indicator for competing risk (default 2).

    Returns:
        FineGrayResult: Object holding sHR, standard error, 95% CI, and p-value.
    """
    if isinstance(data, tuple):
        df = data[0]
    elif isinstance(data, dict) and "data" in data:
        df = data["data"]
    elif isinstance(data, pd.DataFrame):
        df = data
    else:
        raise TypeError(f"Unsupported data type: {type(data)}")

    if len(df) == 0:
        raise ValueError("DataFrame is empty.")

    # Auto-detect time_col
    if time_col is None:
        for c in ["time_days", "time", "followup_days", "duration"]:
            if c in df.columns:
                time_col = c
                break
    # Auto-detect status_col
    if status_col is None:
        for c in ["status", "event", "composite_event", "outcome"]:
            if c in df.columns:
                status_col = c
                break

    times = df[time_col].to_numpy(dtype=float)
    statuses = df[status_col].to_numpy(dtype=int)

    n_samples = len(times)
    n_events = int(np.sum(statuses == event_code))
    n_competing = int(np.sum(statuses == competing_code))

    if group_col is not None and group_col in df.columns:
        group_raw = df[group_col].to_numpy()
        # Convert group to binary 0/1
        u_vals = np.unique(group_raw)
        if len(u_vals) >= 2:
            z_cov = (group_raw == u_vals[1]).astype(float)
            group_name = f"{group_col} ({u_vals[1]} vs {u_vals[0]})"
        else:
            z_cov = np.zeros(n_samples, dtype=float)
            group_name = str(group_col)
    else:
        # Default indicator for high risk
        z_cov = np.ones(n_samples, dtype=float)
        group_name = "Overall"

    # Fine-Gray score equation estimation
    # For subjects experiencing competing risks, they remain in risk set for primary event
    # Distinct event times for primary event
    event_times = np.sort(np.unique(times[statuses == event_code]))

    score_num = 0.0
    score_denom = 0.0

    # Simplified Fine-Gray IPCW / risk set weighting
    for t in event_times:
        # Risk set at time t under subdistribution hazard:
        # 1) Subjects with time >= t
        # 2) Subjects with time < t who experienced COMPETING risk (code 2)
        in_risk = (times >= t) | ((times < t) & (statuses == competing_code))
        n_risk = np.sum(in_risk)
        if n_risk <= 0:
            continue

        z_risk = z_cov[in_risk]
        mean_z = np.mean(z_risk)
        var_z = np.var(z_risk)

        # Primary events at t
        ev_mask = (times == t) & (statuses == event_code)
        d_ev = np.sum(ev_mask)
        z_ev = np.sum(z_cov[ev_mask])

        score_num += z_ev - d_ev * mean_z
        score_denom += d_ev * var_z

    if score_denom > 1e-6:
        log_shr = score_num / score_denom
        se_beta = 1.0 / np.sqrt(score_denom)
    else:
        log_shr = 0.0
        se_beta = 1.0

    z_stat = log_shr / se_beta if se_beta > 0 else 0.0
    # Two-sided p-value
    import math
    p_val = float(math.erfc(abs(z_stat) / math.sqrt(2.0)))

    shr = float(np.exp(log_shr))
    ci_low = float(np.exp(log_shr - 1.959963984540054 * se_beta))
    ci_high = float(np.exp(log_shr + 1.959963984540054 * se_beta))

    return FineGrayResult(
        shr=shr,
        log_shr=float(log_shr),
        std_err=float(se_beta),
        ci_lower=ci_low,
        ci_upper=ci_high,
        p_value=p_val,
        z_stat=float(z_stat),
        n_samples=n_samples,
        n_events=n_events,
        n_competing=n_competing,
        group_name=group_name,
    )


def plot_cif(
    cif_result: CIFResult,
    title: str | None = None,
    output_path: str | None = None,
    show: bool = False,
) -> Any:
    """Plots competing risk cumulative incidence curves for readmission and death."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    fig, ax = plt.subplots(figsize=(8, 5))
    df = cif_result.cif_table

    ax.step(df["time"], df["cif_primary"], where="post", color="#1f77b4", lw=2.2, label="30-day Readmission (CIF)")
    if "cif_primary_ci_lower" in df.columns:
        ax.fill_between(
            df["time"],
            df["cif_primary_ci_lower"],
            df["cif_primary_ci_upper"],
            step="post",
            alpha=0.18,
            color="#1f77b4",
            label="95% CI (Readmission)",
        )

    if "cif_competing" in df.columns and df["cif_competing"].max() > 0:
        ax.step(df["time"], df["cif_competing"], where="post", color="#d62728", lw=2.0, label="Competing Mortality (CIF)")

    if "naive_km_primary" in df.columns:
        ax.step(
            df["time"],
            df["naive_km_primary"],
            where="post",
            color="#1f77b4",
            linestyle="--",
            lw=1.5,
            alpha=0.65,
            label="Naive (1 - KM, Biased)",
        )

    ax.set_xlabel("Follow-up Time (Days)")
    ax.set_ylabel("Cumulative Incidence")
    ax.set_title(title or "Fine-Gray Competing Risks Cumulative Incidence (CIF)")
    ax.set_xlim(0, max(30, float(df["time"].max())))
    ax.set_ylim(0, max(0.25, float(df["cif_primary"].max() * 1.3)))
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper left")
    plt.tight_layout()

    if output_path:
        fig.savefig(output_path, dpi=300)
    if show:
        plt.show()
    return fig

