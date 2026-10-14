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

#' Cause-Specific Cumulative Incidence Function (CIF) Estimator
#'
#' @description
#' Estimates non-parametric Cumulative Incidence Functions (CIF) for primary clinical events
#' (such as 30-day hospital readmission) in the presence of competing events (such as mortality).
#'
#' @param df_or_list A `data.frame` or output list from `prepare_competing_risks_data()`.
#' @param time_col Name of time column (auto-detected if `NULL`).
#' @param status_col Name of status column (auto-detected if `NULL`).
#' @param event_code Indicator integer for primary event (default `1L`).
#' @param competing_code Indicator integer for competing risk (default `2L`).
#' @param alpha Significance level for confidence intervals (default `0.05`).
#'
#' @return A `cif_result` S3 object containing `$cif_table` and `$summary`.
#' @export
estimate_cif <- function(df_or_list,
                         time_col = NULL,
                         status_col = NULL,
                         event_code = 1L,
                         competing_code = 2L,
                         alpha = 0.05) {
  surv_res <- estimate_km_survival(
    df_or_list,
    time_col = time_col,
    status_col = status_col,
    alpha = alpha
  )
  tl <- surv_res$timeline

  if (!isTRUE(surv_res$has_competing_risks)) {
    tl$cif_primary <- 1.0 - tl$survival
    tl$cif_competing <- 0.0
    tl$cif_primary_ci_lower <- pmax(0.0, 1.0 - tl$ci_upper)
    tl$cif_primary_ci_upper <- pmin(1.0, 1.0 - tl$ci_lower)
    tl$naive_km_primary <- tl$cif_primary
  } else {
    z_crit <- stats::qnorm(1 - alpha / 2)
    ev1 <- tl$events_primary
    nar <- tl$n_at_risk
    denom <- nar * pmax(1L, nar - ev1)
    var_terms <- ifelse(nar > ev1, ev1 / denom, 0.0)
    var_est <- (tl$cif_primary^2) * cumsum(var_terms)
    se_cif <- sqrt(pmax(0.0, var_est))
    tl$cif_primary_ci_lower <- pmax(0.0, tl$cif_primary - z_crit * se_cif)
    tl$cif_primary_ci_upper <- pmin(1.0, tl$cif_primary + z_crit * se_cif)
  }

  summary_stats <- surv_res$summary
  sub30 <- tl[tl$time <= 30.0, ]
  if (nrow(sub30) > 0) {
    last_r <- sub30[nrow(sub30), ]
    summary_stats$cif_primary_30d <- last_r$cif_primary
    summary_stats$cif_primary_30d_ci_lower <- last_r$cif_primary_ci_lower
    summary_stats$cif_primary_30d_ci_upper <- last_r$cif_primary_ci_upper
    summary_stats$cif_competing_30d <- last_r$cif_competing
    summary_stats$naive_km_30d <- if ("naive_km_primary" %in% names(last_r)) last_r$naive_km_primary else last_r$cif_primary
    summary_stats$km_overestimation_bias_30d <- summary_stats$naive_km_30d - summary_stats$cif_primary_30d
  } else {
    summary_stats$cif_primary_30d <- 0.0
    summary_stats$cif_primary_30d_ci_lower <- 0.0
    summary_stats$cif_primary_30d_ci_upper <- 0.0
    summary_stats$cif_competing_30d <- 0.0
    summary_stats$naive_km_30d <- 0.0
    summary_stats$km_overestimation_bias_30d <- 0.0
  }

  res <- list(
    cif_table = tl,
    summary = summary_stats
  )
  class(res) <- c("cif_result", "list")
  res
}

#' @export
as.data.frame.cif_result <- function(x, ...) {
  x$cif_table
}

#' @export
summary.cif_result <- function(object, ...) {
  object$summary
}

#' @export
print.cif_result <- function(x, ...) {
  s <- x$summary
  cat(sprintf("=== Competing Risks Cumulative Incidence Function (CIF) ===\n"))
  cat(sprintf("30-Day Readmission CIF: %.2f%% (95%% CI: [%.2f%%, %.2f%%])\n",
              s$cif_primary_30d * 100, s$cif_primary_30d_ci_lower * 100, s$cif_primary_30d_ci_upper * 100))
  cat(sprintf("30-Day Competing Mortality CIF: %.2f%%\n", s$cif_competing_30d * 100))
  cat(sprintf("Naive 1-KM Rate: %.2f%% (Overestimation Bias: +%.2f%%)\n",
              s$naive_km_30d * 100, s$km_overestimation_bias_30d * 100))
  invisible(x)
}

#' Plot Cumulative Incidence Function (CIF)
#'
#' @param x A `cif_result` object.
#' @param title Optional plot title.
#' @param ... Additional arguments passed to [graphics::plot()].
#' @export
plot_cif <- function(x, title = NULL, ...) {
  tl <- x$cif_table
  y_max <- max(0.25, max(tl$cif_primary) * 1.35)
  graphics::plot(
    tl$time, tl$cif_primary, type = "s", col = "blue", lwd = 2.5,
    xlim = c(0, max(30, max(tl$time))), ylim = c(0, y_max),
    xlab = "Follow-up Time (Days)", ylab = "Cumulative Incidence",
    main = if (!is.null(title)) title else "Fine-Gray Competing Risks Cumulative Incidence (CIF)", ...
  )
  if ("cif_competing" %in% names(tl) && max(tl$cif_competing) > 0) {
    graphics::lines(tl$time, tl$cif_competing, type = "s", col = "red", lwd = 2)
  }
  if ("naive_km_primary" %in% names(tl)) {
    graphics::lines(tl$time, tl$naive_km_primary, type = "s", col = "blue", lty = 2, lwd = 1.5)
  }
  graphics::legend(
    "topleft",
    legend = c("30-day Readmission (CIF)", "Competing Mortality (CIF)", "Naive (1 - KM)"),
    col = c("blue", "red", "blue"),
    lty = c(1, 1, 2),
    lwd = c(2.5, 2, 1.5),
    bty = "n"
  )
}

#' @export
plot.cif_result <- function(x, ...) {
  plot_cif(x, ...)
}

#' Fine-Gray Subdistribution Hazard Estimator
#'
#' @description
#' Evaluates the subdistribution hazard ratio (sHR) between clinical exposure groups
#' under competing risks.
#'
#' @param df_or_list A `data.frame` or output list from `prepare_competing_risks_data()`.
#' @param group_col Character column name for group comparison (e.g. `"high_risk"`).
#' @param time_col Character column name for event times.
#' @param status_col Character column name for 3-state status.
#' @param event_code Primary event code (default `1L`).
#' @param competing_code Competing risk code (default `2L`).
#'
#' @return A `fine_gray_result` S3 object containing `$shr`, `$ci_lower`, `$ci_upper`, `$p_value`.
#' @export
fine_gray_estimator <- function(df_or_list,
                                group_col = NULL,
                                time_col = NULL,
                                status_col = NULL,
                                event_code = 1L,
                                competing_code = 2L) {
  if (is.list(df_or_list) && !is.data.frame(df_or_list) && "data" %in% names(df_or_list)) {
    df <- df_or_list$data
  } else if (is.data.frame(df_or_list)) {
    df <- df_or_list
  } else {
    stop("`df_or_list` must be a data.frame or list with `$data`.", call. = FALSE)
  }

  if (nrow(df) == 0) stop("Input data is empty.", call. = FALSE)

  if (is.null(time_col)) {
    candidates <- c("time_days", "time", "followup_days", "duration", "days")
    matched <- intersect(candidates, names(df))
    if (length(matched) == 0) stop("Could not auto-detect time column.", call. = FALSE)
    time_col <- matched[1]
  }

  if (is.null(status_col)) {
    candidates <- c("status", "event", "composite_event", "outcome")
    matched <- intersect(candidates, names(df))
    if (length(matched) == 0) stop("Could not auto-detect status column.", call. = FALSE)
    status_col <- matched[1]
  }

  times <- as.numeric(df[[time_col]])
  statuses <- as.integer(df[[status_col]])
  n_samples <- length(times)
  n_events <- sum(statuses == event_code)
  n_competing <- sum(statuses == competing_code)

  if (!is.null(group_col) && group_col %in% names(df)) {
    g_raw <- df[[group_col]]
    u_vals <- sort(unique(g_raw))
    if (length(u_vals) >= 2) {
      z_cov <- as.numeric(g_raw == u_vals[2])
      group_name <- sprintf("%s (%s vs %s)", group_col, u_vals[2], u_vals[1])
    } else {
      z_cov <- rep(0, n_samples)
      group_name <- as.character(group_col)
    }
  } else {
    z_cov <- rep(1, n_samples)
    group_name <- "Overall"
  }

  event_times <- sort(unique(times[statuses == event_code]))
  score_num <- 0.0
  score_denom <- 0.0

  for (t in event_times) {
    in_risk <- (times >= t) | ((times < t) & (statuses == competing_code))
    n_risk <- sum(in_risk)
    if (n_risk <= 0) next

    z_risk <- z_cov[in_risk]
    mean_z <- mean(z_risk)
    var_z <- stats::var(z_risk)
    if (is.na(var_z)) var_z <- 0.0

    ev_mask <- (times == t) & (statuses == event_code)
    d_ev <- sum(ev_mask)
    z_ev <- sum(z_cov[ev_mask])

    score_num <- score_num + (z_ev - d_ev * mean_z)
    score_denom <- score_denom + (d_ev * var_z)
  }

  if (score_denom > 1e-6) {
    log_shr <- score_num / score_denom
    se_beta <- 1.0 / sqrt(score_denom)
  } else {
    log_shr <- 0.0
    se_beta <- 1.0
  }

  z_stat <- if (se_beta > 0) log_shr / se_beta else 0.0
  p_val <- 2.0 * stats::pnorm(abs(z_stat), lower.tail = FALSE)
  z_crit <- stats::qnorm(0.975)

  shr <- exp(log_shr)
  ci_low <- exp(log_shr - z_crit * se_beta)
  ci_high <- exp(log_shr + z_crit * se_beta)

  res <- list(
    group_name = group_name,
    subdistribution_hazard_ratio = shr,
    shr = shr,
    log_shr = log_shr,
    std_err = se_beta,
    ci_lower = ci_low,
    ci_upper = ci_high,
    z_statistic = z_stat,
    p_value = p_val,
    n_samples = n_samples,
    n_events = n_events,
    n_competing = n_competing
  )
  class(res) <- c("fine_gray_result", "list")
  res
}

#' @export
as.data.frame.fine_gray_result <- function(x, ...) {
  data.frame(
    group = x$group_name,
    sHR = x$subdistribution_hazard_ratio,
    log_sHR = x$log_shr,
    se = x$std_err,
    ci_lower = x$ci_lower,
    ci_upper = x$ci_upper,
    z = x$z_statistic,
    p_value = x$p_value,
    stringsAsFactors = FALSE
  )
}

#' @export
summary.fine_gray_result <- function(object, ...) {
  as.data.frame(object, ...)
}

#' @export
print.fine_gray_result <- function(x, ...) {
  cat(sprintf("=== Fine-Gray Subdistribution Hazard Model ===\n"))
  cat(sprintf("Exposure: %s\n", x$group_name))
  cat(sprintf("Subdistribution Hazard Ratio (sHR): %.3f (95%% CI: [%.3f, %.3f])\n",
              x$subdistribution_hazard_ratio, x$ci_lower, x$ci_upper))
  cat(sprintf("Test of Effect: Z = %.2f, p = %.4e\n", x$z_statistic, x$p_value))
  cat(sprintf("Primary Events: %d | Competing Risks: %d | Total N: %d\n",
              x$n_events, x$n_competing, x$n_samples))
  invisible(x)
}

