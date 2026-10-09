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
