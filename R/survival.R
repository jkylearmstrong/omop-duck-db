#' Kaplan-Meier Survival and Fine-Gray Competing Risks Cumulative Incidence (CIF) Estimator
#'
#' @description
#' Estimates non-parametric Kaplan-Meier survival curves, Greenwood standard errors,
#' confidence intervals, and cause-specific Aalen-Johansen / Fine-Gray Cumulative
#' Incidence Functions (CIF) for 3-state competing risks data.
#'
#' @param df_or_list A `data.frame` or output list from `prepare_competing_risks_data()`.
#' @param time_col Character column name for event times (auto-detected if `NULL`).
#' @param status_col Character column name for status indicators (auto-detected if `NULL`).
#' @param ci_type Type of confidence interval: `"log-log"` (default) or `"linear"`.
#' @param alpha Significance level for confidence intervals (default `0.05`).
#'
#' @return An object of class `c("omop_survival_result", "list")` containing:
#' \describe{
#'   \item{timeline}{Tidy `data.frame` with time, n_at_risk, events, survival, std_err, CI bounds, and CIFs.}
#'   \item{summary}{List of summary metrics including total events, censoring, and median survival.}
#'   \item{has_competing_risks}{Logical flag indicating whether competing risks were evaluated.}
#' }
#' @export
estimate_km_survival <- function(df_or_list,
                                 time_col = NULL,
                                 status_col = NULL,
                                 ci_type = c("log-log", "linear"),
                                 alpha = 0.05) {
  ci_type <- match.arg(ci_type)

  if (is.list(df_or_list) && !is.data.frame(df_or_list) && "data" %in% names(df_or_list)) {
    df <- df_or_list$data
  } else if (is.data.frame(df_or_list)) {
    df <- df_or_list
  } else {
    stop("`df_or_list` must be a data.frame or a list with a `$data` element.")
  }

  if (nrow(df) == 0) {
    stop("Input data.frame is empty.")
  }

  if (is.null(time_col)) {
    candidates <- c("time_days", "time", "followup_days", "duration", "days")
    matched <- intersect(candidates, names(df))
    if (length(matched) == 0) {
      stop("Could not auto-detect time column.")
    }
    time_col <- matched[1]
  }

  if (is.null(status_col)) {
    candidates <- c("status", "event", "composite_event", "outcome")
    matched <- intersect(candidates, names(df))
    if (length(matched) == 0) {
      stop("Could not auto-detect status column.")
    }
    status_col <- matched[1]
  }

  times <- as.numeric(df[[time_col]])
  statuses <- as.integer(df[[status_col]])

  unique_statuses <- unique(statuses)
  has_competing <- (1L %in% unique_statuses) && (2L %in% unique_statuses)

  n_total <- length(times)
  unique_times <- sort(unique(times))

  z_crit <- stats::qnorm(1 - alpha / 2)

  t_vec <- c(0.0)
  at_risk_vec <- c(n_total)
  events_vec <- c(0L)
  censored_vec <- c(0L)
  surv_vec <- c(1.0)
  se_vec <- c(0.0)
  ci_low_vec <- c(1.0)
  ci_high_vec <- c(1.0)

  events_1_vec <- c(0L)
  events_2_vec <- c(0L)
  cif_1_vec <- c(0.0)
  cif_2_vec <- c(0.0)
  naive_km_1_vec <- c(0.0)

  current_surv <- 1.0
  greenwood_sum <- 0.0

  current_overall_surv <- 1.0
  cif_1 <- 0.0
  cif_2 <- 0.0
  naive_km_surv_1 <- 1.0

  for (t in unique_times) {
    if (t <= 0) next

    n_at_risk <- sum(times >= t)
    if (n_at_risk <= 0) next

    mask_t <- times == t
    st_at_t <- statuses[mask_t]

    if (has_competing) {
      d_1 <- sum(st_at_t == 1L)
      d_2 <- sum(st_at_t == 2L)
      d_all <- d_1 + d_2
      c_t <- sum(st_at_t == 0L)

      prev_overall <- current_overall_surv
      if (d_all > 0) {
        current_overall_surv <- current_overall_surv * (1.0 - d_all / n_at_risk)
      }

      cif_1 <- cif_1 + prev_overall * (d_1 / n_at_risk)
      cif_2 <- cif_2 + prev_overall * (d_2 / n_at_risk)

      if (d_1 > 0) {
        naive_km_surv_1 <- naive_km_surv_1 * (1.0 - d_1 / n_at_risk)
      }

      d_t <- d_all
    } else {
      d_t <- sum(st_at_t > 0L)
      c_t <- sum(st_at_t == 0L)
      d_1 <- d_t
      d_2 <- 0L
    }

    if (d_t > 0) {
      hazard <- d_t / n_at_risk
      current_surv <- current_surv * (1.0 - hazard)
      denom <- n_at_risk * (n_at_risk - d_t)
      if (denom > 0) {
        greenwood_sum <- greenwood_sum + (d_t / denom)
      }
    }

    var_s <- (current_surv^2) * greenwood_sum
    se_s <- sqrt(max(0.0, var_s))

    if (current_surv >= 1.0) {
      ci_low <- 1.0
      ci_high <- 1.0
    } else if (current_surv <= 0.0) {
      ci_low <- 0.0
      ci_high <- 0.0
    } else if (ci_type == "log-log") {
      log_s <- log(current_surv)
      if (log_s == 0) {
        ci_low <- ci_high <- current_surv
      } else {
        w <- z_crit * se_s / (current_surv * abs(log_s))
        ci_low <- min(max(current_surv^exp(w), 0.0), 1.0)
        ci_high <- min(max(current_surv^exp(-w), 0.0), 1.0)
      }
    } else {
      ci_low <- min(max(current_surv - z_crit * se_s, 0.0), 1.0)
      ci_high <- min(max(current_surv + z_crit * se_s, 0.0), 1.0)
    }

    t_vec <- c(t_vec, t)
    at_risk_vec <- c(at_risk_vec, n_at_risk)
    events_vec <- c(events_vec, d_t)
    censored_vec <- c(censored_vec, c_t)
    surv_vec <- c(surv_vec, current_surv)
    se_vec <- c(se_vec, se_s)
    ci_low_vec <- c(ci_low_vec, ci_low)
    ci_high_vec <- c(ci_high_vec, ci_high)

    if (has_competing) {
      events_1_vec <- c(events_1_vec, d_1)
      events_2_vec <- c(events_2_vec, d_2)
      cif_1_vec <- c(cif_1_vec, cif_1)
      cif_2_vec <- c(cif_2_vec, cif_2)
      naive_km_1_vec <- c(naive_km_1_vec, 1.0 - naive_km_surv_1)
    }
  }

  timeline_df <- data.frame(
    time = t_vec,
    n_at_risk = at_risk_vec,
    n_events = events_vec,
    n_censored = censored_vec,
    survival = surv_vec,
    std_err = se_vec,
    ci_lower = ci_low_vec,
    ci_upper = ci_high_vec,
    stringsAsFactors = FALSE
  )

  if (has_competing) {
    timeline_df$events_primary <- events_1_vec
    timeline_df$events_competing <- events_2_vec
    timeline_df$cif_primary <- cif_1_vec
    timeline_df$cif_competing <- cif_2_vec
    timeline_df$naive_km_primary <- naive_km_1_vec
  }

  under_half <- timeline_df[timeline_df$survival <= 0.5, ]
  median_time <- if (nrow(under_half) > 0) under_half$time[1] else NA_real_

  summary_stats <- list(
    n_total = n_total,
    n_events = sum(events_vec),
    n_censored = sum(censored_vec),
    median_survival_time = median_time,
    has_competing_risks = has_competing,
    final_survival = utils::tail(surv_vec, 1)
  )

  if (has_competing) {
    summary_stats$final_cif_primary <- utils::tail(cif_1_vec, 1)
    summary_stats$final_cif_competing <- utils::tail(cif_2_vec, 1)
  }

  res <- list(
    timeline = timeline_df,
    summary = summary_stats,
    has_competing_risks = has_competing,
    to_dataframe = function() timeline_df
  )
  class(res) <- c("omop_survival_result", "list")
  res
}

#' @export
as.data.frame.omop_survival_result <- function(x, ...) {
  x$timeline
}

#' @export
plot.omop_survival_result <- function(x, ...) {
  tl <- x$timeline
  if (x$has_competing_risks) {
    graphics::plot(
      tl$time, tl$cif_primary, type = "s", col = "blue", lwd = 2,
      ylim = c(0, max(1, max(c(tl$cif_primary, tl$cif_competing)) * 1.15)),
      xlab = "Time (Days)", ylab = "Cumulative Incidence",
      main = "Competing Risks Cumulative Incidence (CIF)", ...
    )
    graphics::lines(tl$time, tl$cif_competing, type = "s", col = "red", lwd = 2)
    graphics::legend("topleft", legend = c("Primary Event", "Competing Event"),
                     col = c("blue", "red"), lwd = 2, bty = "n")
  } else {
    graphics::plot(
      tl$time, tl$survival, type = "s", col = "blue", lwd = 2,
      ylim = c(0, 1.05), xlab = "Time (Days)", ylab = "Survival Probability S(t)",
      main = "Kaplan-Meier Survival Curve", ...
    )
    graphics::lines(tl$time, tl$ci_lower, type = "s", col = "blue", lty = 2)
    graphics::lines(tl$time, tl$ci_upper, type = "s", col = "blue", lty = 2)
    graphics::legend("bottomleft", legend = c("S(t)", "95% CI"),
                     col = "blue", lty = c(1, 2), lwd = c(2, 1), bty = "n")
  }
}
