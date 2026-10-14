#' Automated Negative Control & Empirical Calibration Module
#'
#' @description
#' Traverses the OMOP CDM concept hierarchy to identify candidate negative control
#' outcomes with zero plausible mechanistic links to index exposures/conditions,
#' and fits empirical null distributions to calibrate study p-values and confidence intervals.
#'
#' @name negative_controls
NULL

#' Generate Candidate Negative Control Concepts
#'
#' @description
#' Identifies standard concepts in `target_domain` that have no ancestor, descendant,
#' or direct relational connections to `exposure_concept_id`.
#'
#' @param con Active DBI/DuckDB connection.
#' @param exposure_concept_id Integer concept ID for primary exposure/treatment.
#' @param target_domain Character domain ID (default "Condition").
#' @param candidate_limit Maximum number of candidates to return (default 50L).
#' @param min_patient_count Minimum observed patient count in the domain occurrence table (default 1L).
#'
#' @return A data.frame of candidate negative control concepts.
#' @export
generate_negative_controls <- function(con,
                                       exposure_concept_id,
                                       target_domain = "Condition",
                                       candidate_limit = 50L,
                                       min_patient_count = 1L) {
  domain <- if (tolower(target_domain) == "condition") "Condition" else target_domain

  tables <- tolower(DBI::dbListTables(con))
  has_ancestor <- "concept_ancestor" %in% tables
  has_rel <- "concept_relationship" %in% tables
  has_concept <- "concept" %in% tables

  domain_table_map <- list(
    "Condition" = "condition_occurrence",
    "Drug" = "drug_exposure",
    "Measurement" = "measurement",
    "Procedure" = "procedure_occurrence"
  )
  occ_table <- if (domain %in% names(domain_table_map)) domain_table_map[[domain]] else "condition_occurrence"
  has_occurrence <- occ_table %in% tables

  col_map <- list(
    "condition_occurrence" = "condition_concept_id",
    "drug_exposure" = "drug_concept_id",
    "measurement" = "measurement_concept_id",
    "procedure_occurrence" = "procedure_concept_id"
  )
  concept_id_col <- if (occ_table %in% names(col_map)) col_map[[occ_table]] else "condition_concept_id"

  if (!has_concept) {
    return(data.frame(
      concept_id = integer(0),
      concept_name = character(0),
      domain_id = character(0),
      vocabulary_id = character(0),
      patient_count = integer(0),
      rationale = character(0),
      stringsAsFactors = FALSE
    ))
  }

  excluded_ids <- c(as.integer(exposure_concept_id))

  if (has_ancestor) {
    anc_sql <- sprintf(
      "SELECT ancestor_concept_id, descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id = %d OR descendant_concept_id = %d",
      as.integer(exposure_concept_id), as.integer(exposure_concept_id)
    )
    anc_df <- DBI::dbGetQuery(con, anc_sql)
    if (nrow(anc_df) > 0) {
      excluded_ids <- unique(c(excluded_ids, anc_df$ancestor_concept_id, anc_df$descendant_concept_id))
    }
  }

  if (has_rel) {
    rel_sql <- sprintf(
      "SELECT concept_id_1, concept_id_2 FROM concept_relationship WHERE concept_id_1 = %d OR concept_id_2 = %d",
      as.integer(exposure_concept_id), as.integer(exposure_concept_id)
    )
    rel_df <- DBI::dbGetQuery(con, rel_sql)
    if (nrow(rel_df) > 0) {
      excluded_ids <- unique(c(excluded_ids, rel_df$concept_id_1, rel_df$concept_id_2))
    }
  }

  excluded_ids <- excluded_ids[!is.na(excluded_ids)]
  excl_str <- if (length(excluded_ids) > 0) paste(excluded_ids, collapse = ", ") else "0"

  if (has_occurrence) {
    sql <- sprintf("
      SELECT 
        c.concept_id,
        c.concept_name,
        c.domain_id,
        c.vocabulary_id,
        COUNT(DISTINCT o.person_id) AS patient_count,
        'No plausible ancestral or relational link to index exposure' AS rationale
      FROM concept c
      JOIN %s o ON c.concept_id = o.%s
      WHERE LOWER(c.domain_id) = LOWER('%s')
        AND c.standard_concept = 'S'
        AND c.concept_id NOT IN (%s)
      GROUP BY c.concept_id, c.concept_name, c.domain_id, c.vocabulary_id
      HAVING COUNT(DISTINCT o.person_id) >= %d
      ORDER BY patient_count DESC, c.concept_id
      LIMIT %d
    ", occ_table, concept_id_col, domain, excl_str, as.integer(min_patient_count), as.integer(candidate_limit))
  } else {
    sql <- sprintf("
      SELECT 
        c.concept_id,
        c.concept_name,
        c.domain_id,
        c.vocabulary_id,
        0 AS patient_count,
        'No plausible ancestral or relational link to index exposure' AS rationale
      FROM concept c
      WHERE LOWER(c.domain_id) = LOWER('%s')
        AND c.standard_concept = 'S'
        AND c.concept_id NOT IN (%s)
      ORDER BY c.concept_id
      LIMIT %d
    ", domain, excl_str, as.integer(candidate_limit))
  }

  DBI::dbGetQuery(con, sql)
}

#' Calibrate Nominal Study Estimates Using Empirical Null Distribution
#'
#' @description
#' Implements OHDSI HADES empirical calibration, estimating null distribution mean and
#' variance from negative control estimates and calibrating p-values and confidence intervals.
#'
#' @param null_estimates Data frame with columns `log_rr` (or `effect`) and `se_log_rr` (or `se`),
#'   or a numeric vector / matrix of negative control estimates.
#' @param observed_effect Numeric point estimate on the log scale (e.g. log RR or log HR).
#' @param observed_se Numeric standard error of the observed point estimate (default 0.10).
#'
#' @return A list of class `c("omop_empirical_calibration", "list")` with null parameters and calibrated statistics.
#' @export
calibrate_estimates <- function(null_estimates, observed_effect, observed_se = 0.10) {
  if (is.data.frame(null_estimates)) {
    cols <- names(null_estimates)
    eff_col <- cols[tolower(cols) %in% c("log_rr", "effect", "estimate", "log_hr", "beta", "x")][1]
    if (is.na(eff_col)) eff_col <- cols[1]

    se_col <- cols[tolower(cols) %in% c("se_log_rr", "se", "std_err", "stderr", "se_beta", "y")][1]

    effects <- as.numeric(null_estimates[[eff_col]])
    effects <- effects[!is.na(effects)]
    ses <- if (!is.na(se_col)) as.numeric(null_estimates[[se_col]])[!is.na(effects)] else rep(0.10, length(effects))
  } else if (is.matrix(null_estimates) && ncol(null_estimates) >= 2) {
    effects <- as.numeric(null_estimates[, 1])
    ses <- as.numeric(null_estimates[, 2])
  } else {
    effects <- as.numeric(null_estimates)
    ses <- rep(0.10, length(effects))
  }

  n_ctrl <- length(effects)
  if (n_ctrl < 1) {
    stop("At least one negative control estimate is required for calibration.")
  }

  # Weighted mean
  weights <- 1.0 / pmax(ses^2, 1e-6)
  mu_null <- sum(weights * effects) / sum(weights)

  sample_var <- if (n_ctrl > 1) stats::var(effects) else 0.0
  mean_se_sq <- mean(ses^2)
  sigma_sq <- max(0.0, sample_var - mean_se_sq)
  sigma_null <- if (sigma_sq > 0) sqrt(sigma_sq) else 0.0

  se_obs <- if (!is.null(observed_se) && observed_se > 0) observed_se else 0.10

  # Nominal
  z_nom <- observed_effect / se_obs
  p_nom <- 2.0 * (1.0 - stats::pnorm(abs(z_nom)))
  ci_nom <- c(observed_effect - 1.96 * se_obs, observed_effect + 1.96 * se_obs)

  # Calibrated
  cal_eff <- observed_effect - mu_null
  cal_se <- sqrt(se_obs^2 + sigma_null^2)
  z_cal <- cal_eff / cal_se
  p_cal <- 2.0 * (1.0 - stats::pnorm(abs(z_cal)))
  ci_cal <- c(cal_eff - 1.96 * cal_se, cal_eff + 1.96 * cal_se)

  res <- list(
    null_mean = round(mu_null, 4),
    null_sd = round(sigma_null, 4),
    nominal_effect = round(observed_effect, 4),
    nominal_se = round(se_obs, 4),
    nominal_p = round(p_nom, 6),
    nominal_ci = round(ci_nom, 4),
    calibrated_effect = round(cal_eff, 4),
    calibrated_se = round(cal_se, 4),
    calibrated_p = round(p_cal, 6),
    calibrated_ci = round(ci_cal, 4),
    n_negative_controls = n_ctrl
  )
  class(res) <- c("omop_empirical_calibration", "list")
  res
}
