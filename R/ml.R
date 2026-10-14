#' Cross-Site Model Calibration and Decision Curve Analysis (DCA)
#'
#' Evaluates cross-site predictive model transportability, multi-site calibration drift,
#' Greenwood-Nam-D'Agostino tests, and clinical net benefit via Decision Curve Analysis.

#' Multi-Site Calibration and Decile Drift
#'
#' @description
#' Evaluates whether predictive models maintain calibration across health systems.
#' Computes Greenwood-Nam-D'Agostino multi-site calibration curves, site-specific Brier scores,
#' calibration slope/intercept, expected calibration error (ECE), and decile prediction drift.
#'
#' @param y_true Binary vector of true outcome values (0 or 1).
#' @param y_prob Numeric vector of predicted event probabilities (between 0 and 1).
#' @param site_labels Vector of site names or identifiers.
#' @param n_bins Number of calibration risk quantile bins (default `10`).
#'
#' @return A `consortium_calibration` S3 object containing `$site_metrics`, `$calibration_curves`, and `$decile_drift`.
#' @export
consortium_calibration <- function(y_true, y_prob, site_labels, n_bins = 10L) {
  y <- as.numeric(y_true)
  p <- as.numeric(y_prob)
  sites <- as.character(site_labels)

  if (length(y) != length(p) || length(y) != length(sites)) {
    stop("Lengths of `y_true`, `y_prob`, and `site_labels` must match.", call. = FALSE)
  }
  if (length(y) == 0) {
    stop("Input vectors must not be empty.", call. = FALSE)
  }

  p <- pmin(pmax(p, 1e-7), 1 - 1e-7)

  calc_site <- function(y_sub, p_sub, site_name) {
    n_pts <- length(y_sub)
    n_ev <- sum(y_sub)
    obs_r <- if (n_pts > 0) n_ev / n_pts else 0.0
    mean_p <- if (n_pts > 0) mean(p_sub) else 0.0
    brier <- if (n_pts > 0) mean((y_sub - p_sub)^2) else 0.0

    z <- stats::qlogis(p_sub)
    fit_slope <- tryCatch({
      m <- stats::glm(y_sub ~ z, family = stats::binomial())
      stats::coef(m)
    }, error = function(e) c("(Intercept)" = 0, "z" = 1))

    intercept <- as.numeric(fit_slope[1])
    slope <- as.numeric(fit_slope[2])

    g <- min(as.integer(n_bins), max(2L, as.integer(n_pts / 5)))
    ranks <- rank(p_sub, ties.method = "first")
    bin_ids <- pmin(pmax(as.integer((ranks - 1) * g / n_pts) + 1L, 1L), g)

    ece_acc <- 0.0
    mce_val <- 0.0
    gnd_stat <- 0.0
    crv_list <- list()

    for (b in seq_len(g)) {
      idx <- which(bin_ids == b)
      b_n <- length(idx)
      if (b_n == 0) next
      b_y <- y_sub[idx]
      b_p <- p_sub[idx]
      b_ev <- sum(b_y)
      b_obs <- b_ev / b_n
      b_pred <- mean(b_p)
      b_exp <- sum(b_p)
      drift <- b_obs - b_pred
      d_abs <- abs(drift)

      ece_acc <- ece_acc + (b_n / n_pts) * d_abs
      if (d_abs > mce_val) mce_val <- d_abs

      var_b <- b_n * b_pred * (1.0 - b_pred)
      if (var_b > 1e-6) {
        gnd_stat <- gnd_stat + ((b_ev - b_exp)^2) / var_b
      }

      crv_list[[length(crv_list) + 1]] <- data.frame(
        site = site_name,
        bin = b,
        n = b_n,
        events = b_ev,
        pred_mean = b_pred,
        obs_rate = b_obs,
        drift = drift,
        stringsAsFactors = FALSE
      )
    }

    df_g <- max(1L, g - 2L)
    gnd_p <- stats::pchisq(gnd_stat, df = df_g, lower.tail = FALSE)

    met <- data.frame(
      site = site_name,
      n = n_pts,
      n_events = n_ev,
      observed_event_rate = obs_r,
      mean_predicted_prob = mean_p,
      brier_score = brier,
      calibration_intercept = intercept,
      calibration_slope = slope,
      ece = ece_acc,
      mce = mce_val,
      gnd_statistic = gnd_stat,
      gnd_df = df_g,
      gnd_p_value = gnd_p,
      stringsAsFactors = FALSE
    )
    list(metrics = met, curves = do.call(rbind, crv_list))
  }

  unique_sites <- sort(unique(sites))
  res_sites <- lapply(unique_sites, function(s) {
    m <- sites == s
    calc_site(y[m], p[m], as.character(s))
  })

  pooled <- calc_site(y, p, "Consortium (Pooled)")

  site_metrics_df <- do.call(rbind, c(lapply(res_sites, `[[`, "metrics"), list(pooled$metrics)))
  calibration_curves_df <- do.call(rbind, c(lapply(res_sites, `[[`, "curves"), list(pooled$curves)))

  decile_drift_df <- stats::reshape(
    calibration_curves_df[, c("site", "bin", "drift")],
    idvar = "site",
    timevar = "bin",
    direction = "wide"
  )
  names(decile_drift_df) <- gsub("^drift\\.", "bin_", names(decile_drift_df))

  res <- list(
    site_metrics = site_metrics_df,
    calibration_curves = calibration_curves_df,
    decile_drift = decile_drift_df
  )
  class(res) <- c("consortium_calibration", "list")
  res
}

#' @export
as.data.frame.consortium_calibration <- function(x, ...) {
  x$site_metrics
}

#' @export
print.consortium_calibration <- function(x, ...) {
  cat(sprintf("=== Cross-Site Model Calibration Audit [%d site(s)] ===\n", nrow(x$site_metrics)))
  print(x$site_metrics, row.names = FALSE)
  invisible(x)
}

#' @export
plot.consortium_calibration <- function(x, ...) {
  crv <- x$calibration_curves
  graphics::plot(
    c(0, 1), c(0, 1), type = "l", lty = 2, col = "gray50",
    xlab = "Mean Predicted Probability", ylab = "Observed Event Rate",
    main = "Cross-Site Calibration Curves", xlim = c(0, 1), ylim = c(0, 1), ...
  )

  sites <- unique(crv$site)
  cols <- grDevices::rainbow(length(sites))
  for (i in seq_along(sites)) {
    sub <- crv[crv$site == sites[i], ]
    graphics::lines(sub$pred_mean, sub$obs_rate, type = "b", col = cols[i], pch = 16, lwd = 1.8)
  }
  graphics::legend("topleft", legend = sites, col = cols, pch = 16, lwd = 1.8, bty = "n")
}


#' Clinical Decision Curve Analysis (DCA)
#'
#' @description
#' Evaluates whether using a predictive model to guide clinical interventions yields higher Net Benefit
#' than default strategies ("treat all" or "treat none") across decision thresholds (Vickers et al., 2006).
#'
#' @param y_true Binary vector of true outcome values (0 or 1).
#' @param y_prob Numeric vector of predicted event probabilities (between 0 and 1).
#' @param threshold_range Numeric vector of length 2 `c(min, max)` thresholds (default `c(0.05, 0.35)`).
#' @param step Numeric step increment (default `0.01`).
#' @param harm Numeric test/intervention harm (default `0.0`).
#'
#' @return A `decision_curve_analysis` S3 object containing `$net_benefit` and `$summary`.
#' @export
decision_curve_analysis <- function(y_true, y_prob, threshold_range = c(0.05, 0.35), step = 0.01, harm = 0.0) {
  y <- as.numeric(y_true)
  p <- as.numeric(y_prob)

  if (length(y) != length(p)) {
    stop("Lengths of `y_true` and `y_prob` must match.", call. = FALSE)
  }
  if (length(y) == 0) {
    stop("Input vectors must not be empty.", call. = FALSE)
  }

  n <- length(y)
  events <- sum(y)
  prevalence <- events / n

  thresholds <- seq(threshold_range[1], threshold_range[2], by = step)
  rows <- list()
  beneficial_thresh <- c()

  for (pt in thresholds) {
    if (pt <= 0 || pt >= 1) next
    w <- pt / (1.0 - pt)
    pred_pos <- p >= pt
    tp <- sum(pred_pos & (y == 1))
    fp <- sum(pred_pos & (y == 0))

    nb_model <- (tp / n) - (fp / n) * w - harm
    nb_all <- prevalence - ((1.0 - prevalence) * w) - harm
    nb_none <- 0.0

    reduction <- if (w > 0) max(0.0, ((nb_model - nb_all) / w) * 100.0) else 0.0
    snb <- if (prevalence > 0) nb_model / prevalence else 0.0

    if (nb_model > max(nb_all, nb_none)) {
      beneficial_thresh <- c(beneficial_thresh, pt)
    }

    rows[[length(rows) + 1]] <- data.frame(
      threshold = pt,
      model = nb_model,
      all = nb_all,
      none = 0.0,
      net_reduction_interventions = reduction,
      standardized_net_benefit = snb,
      tp = tp,
      fp = fp,
      n = n,
      stringsAsFactors = FALSE
    )
  }

  nb_df <- do.call(rbind, rows)
  max_idx <- which.max(nb_df$model)

  sum_stats <- list(
    n_samples = n,
    n_events = events,
    prevalence = prevalence,
    max_net_benefit = if (length(max_idx) > 0) nb_df$model[max_idx] else 0.0,
    threshold_at_max = if (length(max_idx) > 0) nb_df$threshold[max_idx] else 0.0,
    clinical_benefit_range = if (length(beneficial_thresh) > 0) c(min(beneficial_thresh), max(beneficial_thresh)) else NULL,
    max_reduction_interventions = if (nrow(nb_df) > 0) max(nb_df$net_reduction_interventions) else 0.0
  )

  res <- list(
    net_benefit = nb_df,
    summary = sum_stats
  )
  class(res) <- c("decision_curve_analysis", "list")
  res
}

#' @export
as.data.frame.decision_curve_analysis <- function(x, ...) {
  x$net_benefit
}

#' @export
summary.decision_curve_analysis <- function(object, ...) {
  object$summary
}

#' @export
print.decision_curve_analysis <- function(x, ...) {
  s <- x$summary
  cat(sprintf("=== Clinical Decision Curve Analysis (DCA) ===\n"))
  cat(sprintf("Prevalence: %.2f%% (%d / %d)\n", s$prevalence * 100, s$n_events, s$n_samples))
  cat(sprintf("Maximum Net Benefit: %.4f (at threshold %.2f)\n", s$max_net_benefit, s$threshold_at_max))
  if (!is.null(s$clinical_benefit_range)) {
    cat(sprintf("Clinical Benefit Range: [%.2f - %.2f]\n", s$clinical_benefit_range[1], s$clinical_benefit_range[2]))
  }
  cat(sprintf("Maximum Net Reduction in Interventions: %.1f per 100 patients\n", s$max_reduction_interventions))
  invisible(x)
}

#' @export
plot.decision_curve_analysis <- function(x, ...) {
  df <- x$net_benefit
  y_max <- max(0.05, max(c(df$model, df$all)) * 1.15)
  y_min <- min(-0.02, min(df$all) * 0.5)

  graphics::plot(
    df$threshold, df$model, type = "l", col = "blue", lwd = 2.5,
    ylim = c(y_min, y_max), xlab = "Decision Threshold", ylab = "Net Benefit",
    main = "Clinical Decision Curve Analysis (DCA)", ...
  )
  graphics::lines(df$threshold, df$all, col = "gray50", lty = 2, lwd = 1.8)
  graphics::abline(h = 0, col = "black", lty = 3)
  graphics::legend("topright", legend = c("Prediction Model", "Treat All", "Treat None"),
                   col = c("blue", "gray50", "black"), lty = c(1, 2, 3), lwd = c(2.5, 1.8, 1), bty = "n")
}
