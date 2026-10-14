#' In-Engine Propensity Score and Inverse Probability of Treatment Weighting (IPTW) Module
#'
#' @description
#' Computes logistic regression propensity scores, Inverse Probability of Treatment
#' Weighting (IPTW: ATE, ATT/SMR, stabilized weights), and pre/post Standardized Mean
#' Difference (SMD) covariate balance diagnostics for comparative effectiveness studies.
#'
#' @param con Active DBI/DuckDB connection (required if `cohort_table` is a character table name).
#' @param cohort_table Character table name or `data.frame` containing cohort subjects and covariates.
#' @param target_id Integer identifier representing target (treated) cohort (`Z = 1`, default 1L).
#' @param comparator_id Integer identifier representing comparator cohort (`Z = 0`, default 2L).
#' @param covariate_cols Optional character vector of covariate column names to balance.
#'   If `NULL`, auto-detects all numeric columns excluding cohort identifiers.
#' @param treatment_col Column containing group indicators (default `"cohort_definition_id"`).
#' @param trim Optional clipping threshold for extreme propensity scores (default `0.01`).
#'
#' @return An object of class `c("omop_propensity_result", "list")` containing:
#' \describe{
#'   \item{data}{`data.frame` with added columns `propensity_score`, `iptw_ate`, `iptw_att`, and `iptw_stabilized_ate`.}
#'   \item{balance}{`data.frame` of pre- and post-weighting SMDs and means for each covariate.}
#'   \item{coefficients}{Named numeric vector of logistic regression coefficients.}
#'   \item{max_post_smd}{Numeric maximum post-weighting ATE SMD across all evaluated covariates.}
#' }
#' @export
generate_propensity_weights <- function(con = NULL,
                                        cohort_table = "cohort",
                                        target_id = 1L,
                                        comparator_id = 2L,
                                        covariate_cols = NULL,
                                        treatment_col = "cohort_definition_id",
                                        trim = 0.01) {
  if (is.character(cohort_table)) {
    if (is.null(con)) {
      stop("Must provide active DBI connection `con` when `cohort_table` is a string table name.")
    }
    sql <- sprintf(
      "SELECT * FROM %s WHERE %s IN (%d, %d)",
      cohort_table, treatment_col, as.integer(target_id), as.integer(comparator_id)
    )
    df <- DBI::dbGetQuery(con, sql)
  } else if (is.data.frame(cohort_table)) {
    df <- cohort_table[cohort_table[[treatment_col]] %in% c(target_id, comparator_id), , drop = FALSE]
  } else {
    stop("`cohort_table` must be a character table name or a data.frame.")
  }

  if (nrow(df) == 0) {
    stop(sprintf("No rows found matching %s in (%s, %s).", treatment_col, target_id, comparator_id))
  }

  df[[".treatment_z"]] <- as.integer(df[[treatment_col]] == target_id)
  z <- df[[".treatment_z"]]

  if (is.null(covariate_cols)) {
    exclude_cols <- c(
      treatment_col, ".treatment_z", "cohort_definition_id", "subject_id",
      "person_id", "cohort_start_date", "cohort_end_date", "visit_occurrence_id",
      "propensity_score", "iptw_ate", "iptw_att"
    )
    covariate_cols <- names(df)[sapply(df, is.numeric) & !(names(df) %in% exclude_cols)]
  }

  if (length(covariate_cols) == 0) {
    stop("No valid numeric covariate columns found or specified for propensity modeling.")
  }

  # Prepare covariate matrix with median imputation for missing values
  X_df <- data.frame(row.names = seq_len(nrow(df)))
  for (col in covariate_cols) {
    vals <- as.numeric(df[[col]])
    if (all(is.na(vals))) {
      med <- 0.0
    } else {
      med <- stats::median(vals, na.rm = TRUE)
    }
    vals[is.na(vals)] <- med
    X_df[[col]] <- vals
  }

  # Scale covariates for numerical stability in logistic regression
  X_scaled <- as.data.frame(scale(X_df))
  # Handle zero-variance columns in scale()
  for (col in names(X_scaled)) {
    if (any(is.nan(X_scaled[[col]]))) {
      X_scaled[[col]] <- 0.0
    }
  }

  fit_df <- cbind(z_outcome = z, X_scaled)
  fit <- stats::glm(z_outcome ~ ., data = fit_df, family = stats::binomial())

  eta <- stats::predict(fit, type = "link")
  eta <- pmin(pmax(eta, -30.0), 30.0)
  e_raw <- 1.0 / (1.0 + exp(-eta))

  if (!is.null(trim) && trim > 0) {
    e <- pmin(pmax(e_raw, trim), 1.0 - trim)
  } else {
    e <- pmin(pmax(e_raw, 1e-6), 1.0 - 1e-6)
  }

  # Compute IPTW weights
  ate <- (z / e) + ((1.0 - z) / (1.0 - e))
  att <- z + (1.0 - z) * (e / (1.0 - e))
  z_mean <- mean(z)
  stab_ate <- (z * z_mean / e) + ((1.0 - z) * (1.0 - z_mean) / (1.0 - e))

  df[["propensity_score"]] <- e
  df[["iptw_ate"]] <- ate
  df[["iptw_att"]] <- att
  df[["iptw_stabilized_ate"]] <- stab_ate

  # Calculate SMD balance table
  balance_rows <- list()
  tgt_idx <- which(z == 1)
  cmp_idx <- which(z == 0)

  calc_smd_internal <- function(x_t, x_c, w_t = NULL, w_c = NULL, base_pooled_sd = NULL) {
    if (is.null(w_t)) {
      m_t <- mean(x_t)
      v_t <- if (length(x_t) > 1) stats::var(x_t) else 0.0
    } else {
      s_wt <- sum(w_t)
      m_t <- if (s_wt > 0) sum(w_t * x_t) / s_wt else 0.0
      v_t <- 0.0
    }

    if (is.null(w_c)) {
      m_c <- mean(x_c)
      v_c <- if (length(x_c) > 1) stats::var(x_c) else 0.0
    } else {
      s_wc <- sum(w_c)
      m_c <- if (s_wc > 0) sum(w_c * x_c) / s_wc else 0.0
      v_c <- 0.0
    }

    if (is.null(base_pooled_sd)) {
      p_var <- (v_t + v_c) / 2.0
      p_sd <- if (p_var > 0) sqrt(p_var) else 0.0
    } else {
      p_sd <- base_pooled_sd
    }

    smd_val <- if (p_sd <= 1e-12) 0.0 else abs(m_t - m_c) / p_sd
    list(smd = smd_val, m_t = m_t, m_c = m_c, p_sd = p_sd)
  }

  for (col in covariate_cols) {
    vals_t <- X_df[[col]][tgt_idx]
    vals_c <- X_df[[col]][cmp_idx]

    w_t_ate <- ate[tgt_idx]
    w_c_ate <- ate[cmp_idx]
    w_t_att <- att[tgt_idx]
    w_c_att <- att[cmp_idx]

    # Pre-weighting SMD
    pre <- calc_smd_internal(vals_t, vals_c)
    # Post-weighting ATE SMD
    post_ate <- calc_smd_internal(vals_t, vals_c, w_t = w_t_ate, w_c = w_c_ate, base_pooled_sd = pre$p_sd)
    # Post-weighting ATT SMD
    post_att <- calc_smd_internal(vals_t, vals_c, w_t = w_t_att, w_c = w_c_att, base_pooled_sd = pre$p_sd)

    balance_rows[[length(balance_rows) + 1]] <- data.frame(
      covariate = col,
      pre_smd = pre$smd,
      post_smd_ate = post_ate$smd,
      post_smd_att = post_att$smd,
      mean_target_pre = pre$m_t,
      mean_comparator_pre = pre$m_c,
      mean_target_post_ate = post_ate$m_t,
      mean_comparator_post_ate = post_ate$m_c,
      stringsAsFactors = FALSE
    )
  }

  balance_df <- do.call(rbind, balance_rows)
  max_post_smd <- if (nrow(balance_df) > 0) max(balance_df$post_smd_ate, na.rm = TRUE) else 0.0

  df[[".treatment_z"]] <- NULL

  res <- list(
    data = df,
    balance = balance_df,
    coefficients = stats::coef(fit),
    max_post_smd = max_post_smd
  )
  class(res) <- c("omop_propensity_result", "list")
  res
}

#' 1:k Nearest-Neighbor Caliper Matching on Propensity Scores
#'
#' @description
#' Performs greedy nearest-neighbor matching on the logit of the propensity score
#' within a user-specified standard deviation caliper.
#'
#' @param df Data frame containing propensity scores and treatment indicators.
#' @param propensity_score_col Column name holding estimated propensity scores.
#' @param treatment_col Binary indicator column (1 = target/treated, 0 = comparator/control).
#' @param caliper Caliper distance in units of SD of logit(PS) (default 0.2).
#' @param ratio Number of comparator subjects to match per target subject (default 1L).
#'
#' @return A list with `matched_data` (subset of matched rows) and `pairs` (pairing ledger).
#' @export
match_nearest_neighbor <- function(df,
                                   propensity_score_col = "propensity_score",
                                   treatment_col = "treatment",
                                   caliper = 0.2,
                                   ratio = 1L) {
  if (!propensity_score_col %in% names(df)) {
    stop(sprintf("Column '%s' not found in df.", propensity_score_col))
  }
  if (!treatment_col %in% names(df)) {
    stop(sprintf("Column '%s' not found in df.", treatment_col))
  }

  ps <- df[[propensity_score_col]]
  ps_clipped <- pmin(pmax(ps, 1e-6), 1.0 - 1e-6)
  logit_ps <- log(ps_clipped / (1.0 - ps_clipped))

  sd_logit <- stats::sd(logit_ps)
  if (is.na(sd_logit) || sd_logit <= 1e-9) {
    max_dist <- caliper
  } else {
    max_dist <- caliper * sd_logit
  }

  t_idx <- which(df[[treatment_col]] == 1)
  c_idx <- which(df[[treatment_col]] == 0)

  used_controls <- rep(FALSE, length(c_idx))
  names(used_controls) <- as.character(c_idx)

  matched_target_idx <- integer(0)
  matched_control_idx <- integer(0)
  pair_rows <- list()

  for (t_i in t_idx) {
    t_logit <- logit_ps[t_i]
    avail_controls <- c_idx[!used_controls[as.character(c_idx)]]
    if (length(avail_controls) == 0) next

    dists <- abs(logit_ps[avail_controls] - t_logit)
    ord <- order(dists)

    matched_for_this_t <- 0L
    for (k in ord) {
      if (dists[k] <= max_dist && matched_for_this_t < ratio) {
        c_i <- avail_controls[k]
        used_controls[as.character(c_i)] <- TRUE
        matched_target_idx <- c(matched_target_idx, t_i)
        matched_control_idx <- c(matched_control_idx, c_i)
        pair_rows[[length(pair_rows) + 1]] <- data.frame(
          target_row = t_i,
          control_row = c_i,
          logit_distance = dists[k],
          stringsAsFactors = FALSE
        )
        matched_for_this_t <- matched_for_this_t + 1L
      }
    }
  }

  all_matched_idx <- unique(c(matched_target_idx, matched_control_idx))
  matched_df <- df[all_matched_idx, , drop = FALSE]
  matched_df$matched <- 1L

  pairs_df <- if (length(pair_rows) > 0) do.call(rbind, pair_rows) else data.frame()
  list(matched_data = matched_df, pairs = pairs_df)
}

#' Consortium Causal Inference Propensity Engine
#'
#' @description
#' Supports 1:k nearest-neighbor caliper matching and IPTW weighting for observational cohorts.
#'
#' @param con Active DBI connection (required if `cohort_table` is a string table name).
#' @param cohort_table Character table name or `data.frame`.
#' @param exposure_col Column indicating treatment (1 = target, 0 = comparator).
#' @param covariate_cols Optional character vector of covariate column names to balance.
#' @param method Adjustment method: 'matching' (1:k caliper matching) or 'iptw' (IPTW weighting).
#' @param caliper Caliper distance in units of SD of logit(PS) (default 0.2).
#' @param ratio Ratio for 1:k matching (default 1L).
#' @param trim Trimming threshold for extreme propensity scores (default 0.01).
#'
#' @return An object of class `c("omop_propensity_result", "list")`.
#' @export
consortium_propensity_score <- function(con = NULL,
                                        cohort_table = "cohort",
                                        exposure_col = "treatment",
                                        covariate_cols = NULL,
                                        method = "matching",
                                        caliper = 0.2,
                                        ratio = 1L,
                                        trim = 0.01) {
  if (is.character(cohort_table)) {
    if (is.null(con)) {
      stop("Must provide active DBI connection `con` when `cohort_table` is a character table name.")
    }
    df <- DBI::dbGetQuery(con, sprintf("SELECT * FROM %s", cohort_table))
  } else if (is.data.frame(cohort_table)) {
    df <- as.data.frame(cohort_table)
  } else {
    stop("`cohort_table` must be a character table name or data.frame.")
  }

  if (!exposure_col %in% names(df)) {
    stop(sprintf("Exposure column '%s' not found in cohort.", exposure_col))
  }

  iptw_res <- generate_propensity_weights(
    con = NULL,
    cohort_table = df,
    target_id = 1L,
    comparator_id = 0L,
    covariate_cols = covariate_cols,
    treatment_col = exposure_col,
    trim = trim
  )

  if (tolower(method) == "iptw") {
    return(iptw_res)
  }

  # Matching method
  scored_df <- iptw_res$data
  covs <- if (!is.null(covariate_cols)) covariate_cols else iptw_res$balance$covariate

  match_res <- match_nearest_neighbor(
    scored_df,
    propensity_score_col = "propensity_score",
    treatment_col = exposure_col,
    caliper = caliper,
    ratio = ratio
  )

  matched_df <- match_res$matched_data
  if (nrow(matched_df) == 0) {
    res <- list(
      data = matched_df,
      balance = iptw_res$balance,
      coefficients = iptw_res$coefficients,
      max_post_smd = 1.0,
      converged = FALSE
    )
    class(res) <- c("omop_propensity_result", "list")
    return(res)
  }

  tgt_m <- matched_df[matched_df[[exposure_col]] == 1, , drop = FALSE]
  cmp_m <- matched_df[matched_df[[exposure_col]] == 0, , drop = FALSE]

  bal_rows <- list()
  for (col in covs) {
    x_t <- as.numeric(tgt_m[[col]])
    x_c <- as.numeric(cmp_m[[col]])

    m_t <- mean(x_t, na.rm = TRUE)
    m_c <- mean(x_c, na.rm = TRUE)
    v_t <- if (length(x_t) > 1) stats::var(x_t, na.rm = TRUE) else 0.0
    v_c <- if (length(x_c) > 1) stats::var(x_c, na.rm = TRUE) else 0.0

    p_sd <- sqrt((v_t + v_c) / 2.0)
    post_smd <- if (is.na(p_sd) || p_sd <= 1e-12) 0.0 else abs(m_t - m_c) / p_sd

    pre_row <- iptw_res$balance[iptw_res$balance$covariate == col, ]
    pre_smd <- if (nrow(pre_row) > 0) pre_row$pre_smd[1] else 0.0

    bal_rows[[length(bal_rows) + 1]] <- data.frame(
      covariate = col,
      pre_smd = round(pre_smd, 4),
      post_smd = round(post_smd, 4),
      balanced = as.logical(post_smd < 0.10),
      mean_target_matched = round(m_t, 4),
      mean_comparator_matched = round(m_c, 4),
      stringsAsFactors = FALSE
    )
  }

  bal_df <- do.call(rbind, bal_rows)
  max_post_smd <- if (nrow(bal_df) > 0) max(bal_df$post_smd, na.rm = TRUE) else 0.0

  res <- list(
    data = matched_df,
    balance = bal_df,
    coefficients = iptw_res$coefficients,
    max_post_smd = max_post_smd,
    converged = TRUE
  )
  class(res) <- c("omop_propensity_result", "list")
  res
}

#' Generate SVG Love Plot for Covariate Balance
#'
#' @description
#' Generates an SVG Love Plot visualizing pre- and post-adjustment Standardized Mean Differences (SMDs)
#' against the clinical 0.10 balance threshold.
#'
#' @param balance_df Covariate balance data frame from `consortium_propensity_score` or `generate_propensity_weights`.
#' @param threshold Numerical threshold for acceptable balance (default 0.10).
#' @param title Chart title.
#' @param output_path Optional path to write the SVG file.
#'
#' @return Standalone SVG string.
#' @export
plot_love_plot <- function(balance_df,
                           threshold = 0.10,
                           title = "Covariate Balance (Love Plot)",
                           output_path = NULL) {
  df <- as.data.frame(balance_df)
  if (!"post_smd" %in% names(df) && "post_smd_ate" %in% names(df)) {
    df$post_smd <- df$post_smd_ate
  }

  covariates <- as.character(df$covariate)
  n <- length(covariates)
  height <- max(300, 40 * n + 100)
  width <- 650

  max_smd <- max(0.40, max(df$pre_smd, na.rm = TRUE) * 1.1)
  margin_left <- 180
  margin_right <- 50
  margin_top <- 60
  margin_bottom <- 50
  plot_width <- width - margin_left - margin_right
  plot_height <- height - margin_top - margin_bottom

  x_scale <- function(val) {
    margin_left + (min(val, max_smd) / max_smd) * plot_width
  }
  y_scale <- function(idx) {
    margin_top + (idx - 0.5) * (plot_height / max(1, n))
  }

  thresh_x <- x_scale(threshold)

  svg_elements <- c(
    sprintf('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" style="background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">', width, height, width, height),
    sprintf('<text x="%f" y="35" text-anchor="middle" font-size="16" font-weight="600" fill="#2d3748">%s</text>', width / 2, title),
    sprintf('<rect x="%d" y="%d" width="%d" height="%d" fill="#f8fafc" stroke="#e2e8f0" />', margin_left, margin_top, plot_width, plot_height),
    sprintf('<line x1="%f" y1="%d" x2="%f" y2="%d" stroke="#e53e3e" stroke-width="1.5" stroke-dasharray="4,4" />', thresh_x, margin_top, thresh_x, margin_top + plot_height),
    sprintf('<text x="%f" y="%d" text-anchor="middle" font-size="11" fill="#e53e3e" font-weight="600">Threshold (%s)</text>', thresh_x, margin_top - 8, threshold),
    sprintf('<circle cx="%d" cy="%d" r="5" fill="#a0aec0" />', margin_left + 20, margin_top - 20),
    sprintf('<text x="%d" y="%d" font-size="11" fill="#4a5568">Unadjusted (Pre-Match)</text>', margin_left + 30, margin_top - 16),
    sprintf('<circle cx="%d" cy="%d" r="5" fill="#3182ce" />', margin_left + 180, margin_top - 20),
    sprintf('<text x="%d" y="%d" font-size="11" fill="#4a5568">Adjusted (Post-Match)</text>', margin_left + 190, margin_top - 16)
  )

  for (i in seq_along(covariates)) {
    y <- y_scale(i)
    cov_name <- covariates[i]
    pre_val <- as.numeric(df$pre_smd[i])
    post_val <- as.numeric(df$post_smd[i])

    x_pre <- x_scale(pre_val)
    x_post <- x_scale(post_val)

    svg_elements <- c(
      svg_elements,
      sprintf('<text x="%d" y="%f" text-anchor="end" font-size="12" fill="#2d3748">%s</text>', margin_left - 10, y + 4, cov_name),
      sprintf('<line x1="%f" y1="%f" x2="%f" y2="%f" stroke="#cbd5e0" stroke-width="1.5" />', x_pre, y, x_post, y),
      sprintf('<circle cx="%f" cy="%f" r="5" fill="#a0aec0" stroke="#718096" stroke-width="1" />', x_pre, y),
      sprintf('<circle cx="%f" cy="%f" r="5.5" fill="#3182ce" stroke="#2b6cb0" stroke-width="1" />', x_post, y)
    )
  }

  svg_elements <- c(
    svg_elements,
    sprintf('<text x="%f" y="%d" text-anchor="middle" font-size="12" fill="#4a5568">Absolute Standardized Mean Difference (SMD)</text>', margin_left + plot_width / 2, height - 15),
    '</svg>'
  )

  svg_content <- paste(svg_elements, collapse = "\n")
  if (!is.null(output_path)) {
    writeLines(svg_content, output_path)
  }
  svg_content
}
