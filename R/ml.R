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
    main = "Clinical Decision Curve Analysis (DCA)"
  )
  graphics::lines(df$threshold, df$all, col = "gray50", lty = 2, lwd = 1.8)
  graphics::abline(h = 0, col = "black", lty = 3)
  graphics::legend("topright", legend = c("Prediction Model", "Treat All", "Treat None"),
                   col = c("blue", "gray50", "black"), lty = c(1, 2, 3), lwd = c(2.5, 1.8, 1), bty = "n")
}


# ---------------------------------------------------------------------------
# Robust Multivariable Odds Ratios with L2 / Ridge Inversion
# ---------------------------------------------------------------------------

#' Robust Multivariable Odds Ratios with L2 / Ridge Inversion
#'
#' @description
#' Fits multivariable logistic regression using L2 ridge penalization and QR
#' orthogonalization to eliminate collinear columns and prevent Hessian singularity
#' on high-dimensional clinical feature matrices.
#'
#' @param X Data frame or numeric matrix of predictors.
#' @param y Binary numeric vector of outcome indicators (0 or 1).
#' @param binary_cols Optional character vector of binary column names. If `NULL`, auto-detected.
#' @param regularize Logical; whether to apply L2 ridge regularization (default `TRUE`).
#' @param alpha Numeric; L2 regularization penalty parameter lambda (default `1.0`).
#' @param domain_mapping Optional named character vector mapping feature names to clinical domains.
#' @param standardize_continuous Logical; whether continuous features are scaled to 1-SD units (default `TRUE`).
#' @param drop_collinear Logical; whether linearly dependent columns are dropped via QR decomposition (default `TRUE`).
#'
#' @return An S3 object of class `consortium_risk_factors` with Table 5 data frame, coefficients, standard errors, and methods.
#' @export
consortium_multivariable_risk_factors <- function(X,
                                                  y,
                                                  binary_cols = NULL,
                                                  regularize = TRUE,
                                                  alpha = 1.0,
                                                  domain_mapping = NULL,
                                                  standardize_continuous = TRUE,
                                                  drop_collinear = TRUE) {
  X_df <- as.data.frame(X, stringsAsFactors = FALSE)
  y_vec <- as.numeric(y)

  if (nrow(X_df) != length(y_vec)) {
    stop("Number of rows in `X` must match length of `y`.", call. = FALSE)
  }

  valid_rows <- stats::complete.cases(X_df) & !is.na(y_vec)
  X_df <- X_df[valid_rows, , drop = FALSE]
  y_vec <- y_vec[valid_rows]
  n_samples <- nrow(X_df)

  if (n_samples < 5) {
    stop("Insufficient complete samples for multivariable regression.", call. = FALSE)
  }

  # 1. Detect binary vs continuous columns
  if (!is.null(binary_cols)) {
    bin_set <- as.character(binary_cols)
  } else {
    bin_set <- character(0)
    for (nm in names(X_df)) {
      u_vals <- unique(stats::na.omit(X_df[[nm]]))
      if (all(u_vals %in% c(0, 1, 0.0, 1.0))) {
        bin_set <- c(bin_set, nm)
      }
    }
  }

  # 2. Standardize continuous columns
  X_proc <- data.frame(row.names = rownames(X_df))
  col_units <- character(0)

  for (nm in names(X_df)) {
    v <- as.numeric(X_df[[nm]])
    if (nm %in% bin_set) {
      X_proc[[nm]] <- v
      col_units[nm] <- "Presence vs. Absence"
    } else {
      if (standardize_continuous) {
        sd_v <- stats::sd(v, na.rm = TRUE)
        if (!is.na(sd_v) && sd_v > 1e-12) {
          X_proc[[nm]] <- (v - mean(v, na.rm = TRUE)) / sd_v
          col_units[nm] <- "Per 1-SD Increase"
        } else {
          X_proc[[nm]] <- v
          col_units[nm] <- "Continuous (Invariant)"
        }
      } else {
        X_proc[[nm]] <- v
        col_units[nm] <- "Per 1-Unit Increase"
      }
    }
  }

  # 3. QR Orthogonalization to drop collinear features
  dropped_features <- character(0)
  active_cols <- names(X_proc)

  if (drop_collinear && length(active_cols) > 0) {
    kept_cols <- character(0)
    design_mat <- matrix(1, nrow = n_samples, ncol = 1)

    for (nm in active_cols) {
      col_v <- X_proc[[nm]]
      fit_proj <- stats::lm.fit(design_mat, col_v)
      resid_v <- fit_proj$residuals
      if (stats::var(resid_v) < 1e-7) {
        dropped_features <- c(dropped_features, nm)
      } else {
        kept_cols <- c(kept_cols, nm)
        design_mat <- cbind(design_mat, col_v)
      }
    }
    active_cols <- kept_cols
  }

  if (length(active_cols) == 0) {
    stop("All predictor columns were identified as collinear or invariant.", call. = FALSE)
  }

  # 4. IRLS Newton-Raphson with L2 Ridge Penalty
  Z <- cbind(1, as.matrix(X_proc[, active_cols, drop = FALSE]))
  p_dim <- ncol(Z)

  p0 <- min(max(mean(y_vec), 1e-4), 1.0 - 1e-4)
  beta <- rep(0.0, p_dim)
  beta[1] <- log(p0 / (1.0 - p0))

  lam <- if (regularize) as.numeric(alpha) else 1e-9
  Lambda <- diag(c(0.0, rep(lam, p_dim - 1)))

  for (iter in seq_len(60)) {
    eta <- pmin(pmax(Z %*% beta, -30.0), 30.0)
    prob <- 1.0 / (1.0 + exp(-eta))
    prob <- pmin(pmax(prob, 1e-12), 1.0 - 1e-12)
    w <- as.vector(prob * (1.0 - prob))
    W_Z <- Z * w
    Hessian <- crossprod(Z, W_Z) + Lambda
    grad <- crossprod(Z, prob - y_vec) + Lambda %*% beta

    step <- tryCatch({
      solve(Hessian, grad)
    }, error = function(e) {
      MASS::ginv(Hessian) %*% grad
    })

    beta <- beta - as.vector(step)
    if (max(abs(step)) < 1e-6) break
  }

  # 5. Covariance matrix & Standard Errors
  eta_fin <- pmin(pmax(Z %*% beta, -30.0), 30.0)
  p_fin <- pmin(pmax(1.0 / (1.0 + exp(-eta_fin)), 1e-12), 1.0 - 1e-12)
  w_fin <- as.vector(p_fin * (1.0 - p_fin))
  H_fin <- crossprod(Z, Z * w_fin) + Lambda

  cov_mat <- tryCatch({
    solve(H_fin)
  }, error = function(e) {
    MASS::ginv(H_fin)
  })

  se <- sqrt(pmax(1e-12, diag(cov_mat)))

  feat_betas <- beta[-1]
  feat_se <- se[-1]

  infer_domain <- function(fn) {
    fn_l <- tolower(fn)
    if (any(sapply(c("age", "gender", "sex", "race", "ethnicity", "male", "female"), function(k) grepl(k, fn_l, fixed = TRUE)))) {
      return("Demographics")
    } else if (any(sapply(c("dx_", "cond_", "condition_", "charlson", "elixhauser", "cci", "diabetes", "hypertension", "copd", "ckd"), function(k) grepl(k, fn_l, fixed = TRUE)))) {
      return("Comorbidities")
    } else if (any(sapply(c("rx_", "drug_", "med_", "statin", "insulin", "aspirin"), function(k) grepl(k, fn_l, fixed = TRUE)))) {
      return("Medications")
    } else if (any(sapply(c("proc_", "procedure_"), function(k) grepl(k, fn_l, fixed = TRUE)))) {
      return("Procedures")
    } else if (any(sapply(c("lab_", "measurement_", "val_", "creatinine", "glucose", "a1c", "sbp", "dbp", "bmi"), function(k) grepl(k, fn_l, fixed = TRUE)))) {
      return("Laboratory & Vitals")
    }
    "Clinical Features"
  }

  rows_list <- list()
  for (i in seq_along(active_cols)) {
    fn <- active_cols[i]
    b <- feat_betas[i]
    s_err <- feat_se[i]
    or_val <- exp(b)
    ci_l <- exp(b - 1.96 * s_err)
    ci_u <- exp(b + 1.96 * s_err)
    z_score <- if (s_err > 0) b / s_err else 0.0
    p_val <- 2.0 * (1.0 - stats::pnorm(abs(z_score)))

    dom <- if (!is.null(domain_mapping) && fn %in% names(domain_mapping)) {
      domain_mapping[[fn]]
    } else {
      infer_domain(fn)
    }

    p_str <- if (p_val < 0.001) "<0.001" else sprintf("%.3f", p_val)

    rows_list[[i]] <- data.frame(
      domain = dom,
      variable = fn,
      unit = col_units[fn],
      odds_ratio = round(or_val, 2),
      ci_95_lower = round(ci_l, 2),
      ci_95_upper = round(ci_u, 2),
      p_value = p_val,
      or_ci_formatted = sprintf("%.2f [%.2f-%.2f]", or_val, ci_l, ci_u),
      p_formatted = p_str,
      beta = b,
      se = s_err,
      z_score = z_score,
      stringsAsFactors = FALSE
    )
  }

  table_df <- do.call(rbind, rows_list)
  table_df <- table_df[order(table_df$domain, table_df$p_value), ]
  rownames(table_df) <- NULL

  res <- list(
    table = table_df,
    coefficients = stats::setNames(feat_betas, active_cols),
    standard_errors = stats::setNames(feat_se, active_cols),
    covariance_matrix = cov_mat,
    dropped_features = dropped_features,
    metadata = list(
      n_samples = n_samples,
      n_features = length(active_cols),
      alpha = alpha,
      regularize = regularize,
      intercept = beta[1]
    )
  )
  class(res) <- c("consortium_risk_factors", "list")
  res
}

#' @method as.data.frame consortium_risk_factors
#' @export
as.data.frame.consortium_risk_factors <- function(x, ...) {
  x$table
}

#' @method print consortium_risk_factors
#' @export
print.consortium_risk_factors <- function(x, ...) {
  cat(sprintf("=== Consortium Multivariable Risk Factors [Table 5: %d predictors, L2 alpha = %.2f] ===\n",
              nrow(x$table), x$metadata$alpha))
  if (length(x$dropped_features) > 0) {
    cat(sprintf("Dropped collinear features: %s\n", paste(x$dropped_features, collapse = ", ")))
  }
  print(x$table[, c("domain", "variable", "unit", "or_ci_formatted", "p_formatted")], row.names = FALSE)
  invisible(x)
}

#' @method plot consortium_risk_factors
#' @export
plot.consortium_risk_factors <- function(x, ...) {
  df <- x$table
  if (nrow(df) == 0) return(invisible(NULL))

  df <- df[rev(seq_len(nrow(df))), ]
  n_vars <- nrow(df)
  y_pos <- seq_len(n_vars)

  x_min <- min(0.1, min(df$ci_95_lower) * 0.8)
  x_max <- max(5.0, max(df$ci_95_upper) * 1.2)

  old_par <- graphics::par(mar = c(5, 12, 4, 2))
  on.exit(graphics::par(old_par))

  graphics::plot(
    df$odds_ratio, y_pos, pch = 15, col = "#08519c", cex = 1.2,
    xlim = c(x_min, x_max), ylim = c(0.5, n_vars + 0.5),
    log = "x", xlab = "Odds Ratio (log scale)", ylab = "",
    yaxt = "n", main = "Multivariable Odds Ratios (95% CI)"
  )
  graphics::abline(v = 1.0, col = "gray50", lty = 2)

  for (i in seq_len(n_vars)) {
    graphics::lines(c(df$ci_95_lower[i], df$ci_95_upper[i]), c(y_pos[i], y_pos[i]),
                    col = "#3182bd", lwd = 2)
  }

  labels <- paste0(df$variable, " (", df$unit, ")")
  graphics::axis(2, at = y_pos, labels = labels, las = 2, cex.axis = 0.8)
}


# ---------------------------------------------------------------------------
# Internal-External Cross-Validation (IECV) Matrix
# ---------------------------------------------------------------------------

#' Internal-External Cross-Validation (IECV) Matrix
#'
#' @description
#' Evaluates model transportability across multi-hospital network studies by fitting
#' models on Site i and evaluating on Site j for all (i, j) pairs in K x K consortium matrix.
#'
#' @param site_data Data frame containing predictive features, target, and `site_col`, or named list of data frames per site.
#' @param feature_cols Character vector of feature column names.
#' @param target_col Name of the binary target outcome column.
#' @param site_col Name of the site identifier column (default `"site"`).
#' @param regularize Logical; whether to apply L2 ridge regularization (default `TRUE`).
#' @param alpha Numeric; L2 penalty parameter (default `1.0`).
#'
#' @return An S3 object of class `consortium_cross_site_validation` containing `$auroc_matrix`, `$brier_matrix`, and `$summary`.
#' @export
consortium_cross_site_validation <- function(site_data,
                                             feature_cols,
                                             target_col,
                                             site_col = "site",
                                             regularize = TRUE,
                                             alpha = 1.0) {
  if (is.data.frame(site_data)) {
    if (!site_col %in% names(site_data)) {
      stop(sprintf("Site column '%s' not found in site_data.", site_col), call. = FALSE)
    }
    site_names <- sort(unique(as.character(site_data[[site_col]])))
    sites_list <- split(site_data, site_data[[site_col]])
  } else if (is.list(site_data)) {
    sites_list <- site_data
    site_names <- sort(names(sites_list))
  } else {
    stop("`site_data` must be a data frame or named list of data frames.", call. = FALSE)
  }

  k <- length(site_names)
  if (k < 2) {
    stop(sprintf("IECV requires at least 2 distinct sites, found %d.", k), call. = FALSE)
  }

  auroc_mat <- matrix(0.0, nrow = k, ncol = k, dimnames = list(site_names, site_names))
  brier_mat <- matrix(0.0, nrow = k, ncol = k, dimnames = list(site_names, site_names))

  trained_models <- list()
  for (s_train in site_names) {
    df_tr <- sites_list[[s_train]]
    cols_req <- c(feature_cols, target_col)
    df_tr <- df_tr[stats::complete.cases(df_tr[, cols_req, drop = FALSE]), , drop = FALSE]

    rf <- consortium_multivariable_risk_factors(
      X = df_tr[, feature_cols, drop = FALSE],
      y = df_tr[[target_col]],
      regularize = regularize,
      alpha = alpha,
      standardize_continuous = FALSE,
      drop_collinear = FALSE
    )
    trained_models[[s_train]] <- list(intercept = rf$metadata$intercept, betas = rf$coefficients)
  }

  calc_auc_r <- function(y_true, y_prob) {
    pos <- (y_true == 1)
    neg <- (y_true == 0)
    n_pos <- sum(pos)
    n_neg <- sum(neg)
    if (n_pos == 0 || n_neg == 0) return(0.5)
    r <- rank(y_prob)
    u <- sum(r[pos]) - (n_pos * (n_pos + 1)) / 2
    as.numeric(u / (n_pos * n_neg))
  }

  for (i in seq_along(site_names)) {
    s_tr <- site_names[i]
    m_info <- trained_models[[s_tr]]
    b_vec <- m_info$betas
    icpt <- m_info$intercept

    for (j in seq_along(site_names)) {
      s_te <- site_names[j]
      df_te <- sites_list[[s_te]]
      cols_req <- c(feature_cols, target_col)
      df_te <- df_te[stats::complete.cases(df_te[, cols_req, drop = FALSE]), , drop = FALSE]

      X_te <- as.matrix(df_te[, feature_cols, drop = FALSE])
      y_te <- as.numeric(df_te[[target_col]])

      eta <- icpt + as.vector(X_te %*% b_vec[feature_cols])
      prob <- 1.0 / (1.0 + exp(-pmin(pmax(eta, -30.0), 30.0)))

      auc_val <- calc_auc_r(y_te, prob)
      brier_val <- mean((y_te - prob)^2)

      auroc_mat[i, j] <- round(auc_val, 3)
      brier_mat[i, j] <- round(brier_val, 4)
    }
  }

  diag_vals <- diag(auroc_mat)
  off_diag_vals <- auroc_mat[row(auroc_mat) != col(auroc_mat)]
  m_internal <- mean(diag_vals)
  m_external <- mean(off_diag_vals)
  gap <- m_internal - m_external

  summary_df <- data.frame(
    n_sites = k,
    mean_internal_auroc = round(m_internal, 3),
    mean_external_auroc = round(m_external, 3),
    generalization_gap = round(gap, 3),
    mean_brier_score = round(mean(brier_mat), 4),
    transportability_status = if (gap <= 0.05) "EXCELLENT" else if (gap <= 0.10) "MODERATE" else "DOMAIN_SHIFT_DETECTED",
    stringsAsFactors = FALSE
  )

  site_summaries <- list()
  for (s in site_names) {
    int_auc <- auroc_mat[s, s]
    ext_vals <- auroc_mat[s, site_names != s]
    m_ext <- if (length(ext_vals) > 0) mean(ext_vals) else int_auc
    gap_s <- int_auc - m_ext
    site_summaries[[s]] <- data.frame(
      site = s,
      internal_auroc = round(int_auc, 3),
      mean_auroc_external = round(m_ext, 3),
      transportability_gap = round(gap_s, 3),
      stringsAsFactors = FALSE
    )
  }
  site_summary_df <- do.call(rbind, site_summaries)

  res <- list(
    auroc_matrix = as.data.frame(auroc_mat),
    brier_matrix = as.data.frame(brier_mat),
    summary = site_summary_df,
    consortium_summary = summary_df,
    sites = site_names,
    metadata = list(alpha = alpha, regularize = regularize)
  )
  class(res) <- c("consortium_cross_site_validation", "list")
  res
}

#' @method as.data.frame consortium_cross_site_validation
#' @export
as.data.frame.consortium_cross_site_validation <- function(x, ...) {
  x$auroc_matrix
}

#' @method summary consortium_cross_site_validation
#' @export
summary.consortium_cross_site_validation <- function(object, ...) {
  object$summary
}

#' @method print consortium_cross_site_validation
#' @export
print.consortium_cross_site_validation <- function(x, ...) {
  cs <- x$consortium_summary
  cat(sprintf("=== Internal-External Cross-Validation (IECV) [%d sites] ===\n", length(x$sites)))
  cat(sprintf("Mean Internal AUROC: %.3f | Mean External AUROC: %.3f | Generalization Gap: %.3f\n\n",
              cs$mean_internal_auroc, cs$mean_external_auroc, cs$generalization_gap))
  cat("K x K AUROC Matrix (Rows = Train Site, Columns = Test Site):\n")
  print(x$auroc_matrix)
  invisible(x)
}

#' @method plot consortium_cross_site_validation
#' @export
plot.consortium_cross_site_validation <- function(x, metric = c("auroc", "brier"), ...) {
  metric <- match.arg(metric)
  mat <- if (metric == "brier") as.matrix(x$brier_matrix) else as.matrix(x$auroc_matrix)
  k <- nrow(mat)
  labels <- rownames(mat)

  old_par <- graphics::par(mar = c(5, 5, 4, 2))
  on.exit(graphics::par(old_par))

  rot_mat <- t(mat[rev(seq_len(k)), , drop = FALSE])
  col_palette <- grDevices::colorRampPalette(c("#f7fbff", "#6baed6", "#08519c"))(50)

  graphics::image(
    seq_len(k), seq_len(k), rot_mat,
    col = col_palette, zlim = if (metric == "brier") c(0.0, max(0.5, max(mat))) else c(0.5, 1.0),
    xlab = "Evaluation Site (Test)", ylab = "Model Origin (Train)",
    main = if (metric == "brier") "IECV Brier Score Matrix" else "IECV AUROC Matrix",
    xaxt = "n", yaxt = "n"
  )
  graphics::axis(1, at = seq_len(k), labels = labels)
  graphics::axis(2, at = seq_len(k), labels = rev(labels), las = 2)

  for (i in seq_len(k)) {
    for (j in seq_len(k)) {
      v <- mat[i, j]
      y_coord <- k - i + 1
      font_weight <- if (i == j) 2 else 1
      txt_col <- if (v > 0.8 && metric != "brier") "white" else "black"
      graphics::text(j, y_coord, sprintf("%.3f", v), col = txt_col, font = font_weight, cex = 0.9)
    }
  }
  invisible(x)
}

