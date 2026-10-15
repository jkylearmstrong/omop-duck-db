#' Data Quality, Plausibility, and Feature Integrity Audits
#'
#' Provides rigorous, publication-grade data quality assertions:
#' \itemize{
#'   \item \code{audit_code_list_coverage}: Audits whether supplied clinical code lists omit high-frequency concepts.
#'   \item \code{panel_missingness_audit}: Detects discordant missingness within multi-analyte lab panels (e.g. BMP/CBC).
#'   \item \code{plausibility_report}: Summary table and feature-matrix physiological plausibility gates.
#'   \item \code{detect_redundant_features}: Detects deterministic linear conversions, ratios, and multicollinear features.
#'   \item \code{dqd_summary}: Verifies DQD pass-rate mathematical consistency and reproducibility across sites.
#' }
#'
#' @name quality
NULL

#' @keywords internal
#' @noRd
.default_physiologic_bounds <- list(
  sodium = c(100, 180),
  potassium = c(1.5, 9.0),
  chloride = c(60, 140),
  bicarbonate = c(5, 55),
  bun = c(1, 200),
  creatinine = c(0.1, 30.0),
  glucose = c(20, 1500),
  wbc = c(0.1, 150.0),
  rbc = c(1.0, 10.0),
  hemoglobin = c(2.0, 25.0),
  hematocrit = c(10.0, 75.0),
  platelets = c(5, 2000),
  sbp = c(40, 300),
  dbp = c(20, 200),
  heart_rate = c(20, 250),
  respiratory_rate = c(4, 70),
  temperature = c(25.0, 45.0),
  spo2 = c(40, 100),
  bmi = c(10.0, 100.0),
  weight = c(1.0, 500.0),
  height = c(30.0, 250.0)
)

#' @keywords internal
#' @noRd
.default_panels <- list(
  BMP = c("sodium", "potassium", "chloride", "bicarbonate", "bun", "creatinine", "glucose"),
  CBC = c("wbc", "rbc", "hemoglobin", "hematocrit", "platelets")
)

#' @keywords internal
#' @noRd
.domain_table_map <- list(
  condition = c("condition_occurrence", "condition_concept_id", "condition_source_value", "condition_source_concept_id"),
  conditions = c("condition_occurrence", "condition_concept_id", "condition_source_value", "condition_source_concept_id"),
  drug = c("drug_exposure", "drug_concept_id", "drug_source_value", "drug_source_concept_id"),
  drugs = c("drug_exposure", "drug_concept_id", "drug_source_value", "drug_source_concept_id"),
  medication = c("drug_exposure", "drug_concept_id", "drug_source_value", "drug_source_concept_id"),
  measurement = c("measurement", "measurement_concept_id", "measurement_source_value", "measurement_source_concept_id"),
  measurements = c("measurement", "measurement_concept_id", "measurement_source_value", "measurement_source_concept_id"),
  procedure = c("procedure_occurrence", "procedure_concept_id", "procedure_source_value", "procedure_source_concept_id"),
  procedures = c("procedure_occurrence", "procedure_concept_id", "procedure_source_value", "procedure_source_concept_id")
)

#' Audit Clinical Code List Coverage
#'
#' Quantifies the share of records captured by the supplied code list, and identifies
#' high-frequency omitted concepts (accounting for at least \code{min_share} of domain rows)
#' that may cause silent under-coverage across hospital sites.
#'
#' @param con Active DuckDB DBI connection.
#' @param domain Clinical domain ('condition', 'drug', 'measurement', 'procedure').
#' @param supplied_codes Character or integer vector of concept codes (ICD/RxNorm/LOINC strings) or concept IDs.
#' @param concept_set_or_ancestors Optional integer vector of ancestor concept IDs defining the broad domain scope.
#' @param table_name Optional custom table or view name override.
#' @param by_site Whether to stratify results by hospital site (default TRUE).
#' @param site_col Column name representing site (default 'site').
#' @param min_share Minimum proportion of uncaptured rows (default 0.01 = 1 percent) to flag as an omission.
#' @return A data frame with columns:
#'   \code{site}, \code{total_rows}, \code{covered_rows}, \code{coverage_share},
#'   \code{omitted_concept_id}, \code{omitted_code}, \code{omitted_concept_name},
#'   \code{omitted_rows}, \code{omitted_share}, \code{flagged}, \code{reason}.
#' @export
audit_code_list_coverage <- function(con, domain, supplied_codes, concept_set_or_ancestors = NULL,
                                     table_name = NULL, by_site = TRUE, site_col = "site",
                                     min_share = 0.01) {
  domain_clean <- tolower(trimws(as.character(domain)))
  if (!domain_clean %in% names(.domain_table_map)) {
    stop(sprintf("Unknown domain '%s'; expected one of %s.", domain, paste(names(.domain_table_map), collapse = ", ")))
  }

  mapping <- .domain_table_map[[domain_clean]]
  default_table <- mapping[1]
  concept_id_col <- mapping[2]
  source_val_col <- mapping[3]
  actual_table <- if (!is.null(table_name)) table_name else default_table

  has_table <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM information_schema.tables WHERE lower(table_name) = '%s';", tolower(actual_table)))$n > 0
  if (!has_table) {
    has_view <- DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM information_schema.views WHERE lower(table_name) = '%s';", tolower(actual_table)))$n > 0
    if (!has_view) {
      stop(sprintf("Target OMOP table or view '%s' does not exist on this connection.", actual_table))
    }
  }

  cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0;", actual_table))$column_name)
  actual_site_expr <- NULL
  person_join <- ""
  if (isTRUE(by_site)) {
    for (c in c(site_col, "site_anon", "site_id", "site")) {
      if (tolower(c) %in% cols) {
        actual_site_expr <- sprintf('t."%s"', c)
        break
      }
    }
    if (is.null(actual_site_expr) && "person_id" %in% cols) {
      has_person <- DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM information_schema.tables WHERE lower(table_name) = 'person';")$n > 0
      if (has_person) {
        p_cols <- tolower(DBI::dbGetQuery(con, "DESCRIBE SELECT * FROM person LIMIT 0;")$column_name)
        for (c in c(site_col, "site_anon", "site_id", "site")) {
          if (tolower(c) %in% p_cols) {
            actual_site_expr <- sprintf('p."%s"', c)
            person_join <- "LEFT JOIN person p ON p.person_id = t.person_id"
            break
          }
        }
      }
    }
  }

  site_expr <- if (!is.null(actual_site_expr)) actual_site_expr else "'ALL'"

  # Supplied codes
  supplied_ints <- suppressWarnings(as.integer(as.character(supplied_codes)))
  supplied_ints <- supplied_ints[!is.na(supplied_ints)]
  int_filter <- if (length(supplied_ints)) sprintf("t.%s IN (%s)", concept_id_col, paste(supplied_ints, collapse = ", ")) else "FALSE"

  supplied_strs <- toupper(trimws(as.character(supplied_codes)))
  str_list <- paste0("'", gsub("'", "''", supplied_strs), "'", collapse = ", ")
  str_filter <- if (length(supplied_strs)) sprintf("UPPER(TRIM(t.%s)) IN (%s)", source_val_col, str_list) else "FALSE"

  scope_filter <- "TRUE"
  if (!is.null(concept_set_or_ancestors) && length(concept_set_or_ancestors) > 0) {
    anc_ids <- paste(as.integer(concept_set_or_ancestors), collapse = ", ")
    scope_filter <- sprintf("(t.%s IN (%s) OR t.%s IN (SELECT descendant_concept_id FROM concept_ancestor WHERE ancestor_concept_id IN (%s)))",
                            concept_id_col, anc_ids, concept_id_col, anc_ids)
  }

  concept_ref <- tryCatch(.med_resolve_table(con, "concept"), error = function(e) "concept")

  sql <- sprintf("
    WITH scoped AS (
      SELECT 
        %s AS site,
        t.%s AS concept_id,
        t.%s AS source_value,
        (%s OR %s) AS is_covered
      FROM %s t
      %s
      WHERE %s
    ),
    site_totals AS (
      SELECT 
        site,
        COUNT(*) AS total_rows,
        COUNT(*) FILTER (WHERE is_covered) AS covered_rows
      FROM scoped
      GROUP BY site
    ),
    omissions AS (
      SELECT 
        s.site,
        s.concept_id,
        COALESCE(c.concept_code, s.source_value) AS code,
        COALESCE(c.concept_name, 'Unknown Concept') AS concept_name,
        COUNT(*) AS omitted_rows
      FROM scoped s
      LEFT JOIN %s c ON c.concept_id = s.concept_id
      WHERE NOT s.is_covered AND s.concept_id != 0
      GROUP BY s.site, s.concept_id, c.concept_code, s.source_value, c.concept_name
    )
    SELECT 
      st.site,
      st.total_rows,
      st.covered_rows,
      ROUND(CAST(st.covered_rows AS DOUBLE) / NULLIF(st.total_rows, 0), 4) AS coverage_share,
      o.concept_id AS omitted_concept_id,
      o.code AS omitted_code,
      o.concept_name AS omitted_concept_name,
      COALESCE(o.omitted_rows, 0) AS omitted_rows,
      ROUND(CAST(COALESCE(o.omitted_rows, 0) AS DOUBLE) / NULLIF(st.total_rows, 0), 4) AS omitted_share
    FROM site_totals st
    LEFT JOIN omissions o ON o.site = st.site
    ORDER BY st.site, omitted_share DESC;
  ", site_expr, concept_id_col, source_val_col, int_filter, str_filter, actual_table, person_join, scope_filter, concept_ref)

  res <- DBI::dbGetQuery(con, sql)
  if (nrow(res) == 0) {
    return(data.frame(
      site = character(0), total_rows = integer(0), covered_rows = integer(0), coverage_share = numeric(0),
      omitted_concept_id = integer(0), omitted_code = character(0), omitted_concept_name = character(0),
      omitted_rows = integer(0), omitted_share = numeric(0), flagged = logical(0), reason = character(0),
      stringsAsFactors = FALSE
    ))
  }

  res$flagged <- !is.na(res$omitted_share) & res$omitted_share >= min_share
  res$reason <- ifelse(res$flagged, "omitted_high_frequency_code", "")
  res
}

#' Audit Laboratory Panel Completeness and Discordant Missingness
#'
#' Analytes ordered together (e.g. BMP or CBC) should exhibit consistent missingness rates.
#' When a component is missing significantly more often than its panel median, or deviates
#' strongly from peer hospital sites, it flags a likely unmapped analyte.
#'
#' @param features Data frame containing patient features with analyte columns.
#' @param panels Named list of character vectors mapping panel names to analyte column names (default BMP and CBC).
#' @param by Column name to stratify by (default 'site'); if NULL or absent, audited as 'ALL'.
#' @param threshold Maximum allowable missingness gap above panel median (default 0.15 = 15 percent).
#' @param peer_gap Cross-site discrepancy gap flagging peer outliers across hospitals (default 0.20 = 20 percent).
#' @return A data frame with columns:
#'   \code{site}, \code{panel}, \code{analyte}, \code{missing_count}, \code{total_count},
#'   \code{missing_rate}, \code{panel_median_missing}, \code{missingness_gap}, \code{flagged}, \code{reason}.
#' @export
panel_missingness_audit <- function(features, panels = NULL, by = "site", threshold = 0.15, peer_gap = 0.20) {
  df <- features
  site_col <- if (!is.null(by) && by %in% names(df)) by else NULL
  if (is.null(site_col)) {
    df[["_site"]] <- "ALL"
    site_col <- "_site"
  }

  panel_defs <- if (!is.null(panels)) panels else .default_panels
  cols_lower <- stats::setNames(names(df), tolower(names(df)))

  records <- list()
  sites <- unique(df[[site_col]])
  sites <- sites[!is.na(sites)]
  sites_to_process <- if (!"ALL" %in% sites && length(sites) > 1) c(as.character(sites), "ALL") else as.character(sites)

  for (site in sites_to_process) {
    site_df <- if (identical(site, "ALL")) df else df[df[[site_col]] == site, , drop = FALSE]
    n_site <- nrow(site_df)
    if (n_site == 0) next

    for (pname in names(panel_defs)) {
      analytes <- panel_defs[[pname]]
      found_analytes <- unname(cols_lower[tolower(analytes)[tolower(analytes) %in% names(cols_lower)]])
      if (length(found_analytes) == 0) next

      m_rates <- vapply(found_analytes, function(a) mean(is.na(site_df[[a]])), numeric(1))
      med_missing <- stats::median(m_rates)

      for (a in found_analytes) {
        m_rate <- m_rates[[a]]
        gap <- m_rate - med_missing
        m_cnt <- sum(is.na(site_df[[a]]))
        records[[length(records) + 1L]] <- data.frame(
          site = site, panel = pname, analyte = a, missing_count = m_cnt, total_count = n_site,
          missing_rate = round(m_rate, 4), panel_median_missing = round(med_missing, 4),
          missingness_gap = round(gap, 4), stringsAsFactors = FALSE
        )
      }
    }
  }

  out <- if (length(records)) do.call(rbind, records) else data.frame()
  if (nrow(out) == 0) {
    return(data.frame(
      site = character(0), panel = character(0), analyte = character(0),
      missing_count = integer(0), total_count = integer(0), missing_rate = numeric(0),
      panel_median_missing = numeric(0), missingness_gap = numeric(0),
      flagged = logical(0), reason = character(0), stringsAsFactors = FALSE
    ))
  }

  reasons <- vector("list", nrow(out))
  for (i in seq_len(nrow(out))) {
    r_list <- character(0)
    if (out$missingness_gap[i] > threshold) {
      r_list <- c(r_list, "panel_missingness_gap")
    }
    reasons[[i]] <- r_list
  }

  # Multi-site peer outlier check
  per_site <- out$site != "ALL"
  if (any(per_site)) {
    keys <- unique(out[per_site, c("panel", "analyte")])
    for (k in seq_len(nrow(keys))) {
      p <- keys$panel[k]
      a <- keys$analyte[k]
      mask <- per_site & out$panel == p & out$analyte == a
      if (sum(mask) >= 3) {
        sub_indices <- which(mask)
        rates <- out$missing_rate[sub_indices]
        for (j in seq_along(sub_indices)) {
          idx <- sub_indices[j]
          peer_med <- stats::median(rates[-j])
          if (rates[j] > peer_med + peer_gap) {
            reasons[[idx]] <- c(reasons[[idx]], "peer_missingness_outlier")
          }
        }
      }
    }
  }

  out$reason <- vapply(reasons, function(r) paste(unique(r), collapse = ","), character(1))
  out$flagged <- nzchar(out$reason)
  rownames(out) <- NULL
  out
}

#' Table and Feature-Level Physiological Plausibility Report and CI Gate
#'
#' Verifies summary tables and raw feature matrices against clinical boundary ranges,
#' detecting unit mismatches (e.g. 10x decimal errors), extreme variance/SD ratios,
#' impossible percentages, and negative physiological values.
#'
#' @param table1_or_features Raw feature data frame or Table 1 summary data frame.
#' @param ranges Named list of analyte limits \code{c(lower, upper)} (defaults to physiologic bounds).
#' @param sd_to_mean_max Maximum plausible SD-to-mean ratio for strictly positive variables (default 3.0).
#' @param min_pct Minimum valid percentage (default 0.0).
#' @param max_pct Maximum valid percentage (default 100.0).
#' @param ci_gate If TRUE, throws an error if any plausibility violation is detected.
#' @return A data frame with columns:
#'   \code{feature}, \code{metric}, \code{observed_value}, \code{expected_range}, \code{flagged}, \code{reason}.
#' @export
plausibility_report <- function(table1_or_features, ranges = NULL, sd_to_mean_max = 3.0,
                                min_pct = 0.0, max_pct = 100.0, ci_gate = FALSE,
                                audit_unit_discordance = FALSE) {
  df <- table1_or_features
  bounds <- if (!is.null(ranges)) ranges else .default_physiologic_bounds
  cols_lower <- as.list(stats::setNames(names(df), tolower(names(df))))
  records <- list()

  var_col <- cols_lower[["variable"]] %||% cols_lower[["feature"]] %||% cols_lower[["covariate"]]

  if (!is.null(var_col) && any(c("mean", "median") %in% names(cols_lower))) {
    mean_col <- cols_lower[["mean"]]
    sd_col <- cols_lower[["sd"]]
    pct_col <- cols_lower[["pct"]] %||% cols_lower[["percent"]]

    for (i in seq_len(nrow(df))) {
      var_name <- trimws(as.character(df[[var_col]][i]))
      v_lower <- tolower(var_name)

      if (!is.null(pct_col) && !is.na(df[[pct_col]][i])) {
        pct_val <- suppressWarnings(as.numeric(df[[pct_col]][i]))
        if (!is.na(pct_val) && (pct_val < min_pct || pct_val > max_pct)) {
          records[[length(records) + 1L]] <- data.frame(
            feature = var_name, metric = "percent", observed_value = pct_val,
            expected_range = sprintf("[%s, %s]", min_pct, max_pct),
            flagged = TRUE, reason = "impossible_percentage", stringsAsFactors = FALSE
          )
        }
      }

      if (v_lower %in% names(bounds) && !is.null(mean_col) && !is.na(df[[mean_col]][i])) {
        m_val <- suppressWarnings(as.numeric(df[[mean_col]][i]))
        if (!is.na(m_val)) {
          b <- bounds[[v_lower]]
          lo <- b[1]
          hi <- b[2]
          if (m_val < lo || m_val > hi) {
            records[[length(records) + 1L]] <- data.frame(
              feature = var_name, metric = "mean", observed_value = m_val,
              expected_range = sprintf("[%s, %s]", lo, hi),
              flagged = TRUE, reason = "mean_outside_range", stringsAsFactors = FALSE
            )
          }

          if (!is.null(sd_col) && !is.na(df[[sd_col]][i])) {
            sd_val <- suppressWarnings(as.numeric(df[[sd_col]][i]))
            if (!is.na(sd_val) && m_val > 0 && (sd_val / m_val) > sd_to_mean_max) {
              records[[length(records) + 1L]] <- data.frame(
                feature = var_name, metric = "sd_to_mean_ratio", observed_value = round(sd_val / m_val, 2),
                expected_range = sprintf("<= %s", sd_to_mean_max),
                flagged = TRUE, reason = "sd_to_mean_ratio_high", stringsAsFactors = FALSE
              )
            }
          }
        }
      }
    }
  } else {
    num_cols <- names(df)[vapply(df, is.numeric, logical(1))]
    for (c in num_cols) {
      c_clean <- tolower(trimws(c))
      vals <- df[[c]][!is.na(df[[c]])]
      if (length(vals) == 0) next

      m_val <- mean(vals)
      sd_val <- if (length(vals) > 1) stats::sd(vals) else 0.0
      min_val <- min(vals)

      if (c_clean %in% names(bounds)) {
        b <- bounds[[c_clean]]
        lo <- b[1]
        hi <- b[2]
        if (m_val < lo || m_val > hi) {
          records[[length(records) + 1L]] <- data.frame(
            feature = c, metric = "mean", observed_value = round(m_val, 2),
            expected_range = sprintf("[%s, %s]", lo, hi),
            flagged = TRUE, reason = "mean_outside_range", stringsAsFactors = FALSE
          )
        }

        if (m_val > 0 && sd_val > 0 && (sd_val / m_val) > sd_to_mean_max) {
          records[[length(records) + 1L]] <- data.frame(
            feature = c, metric = "sd_to_mean_ratio", observed_value = round(sd_val / m_val, 2),
            expected_range = sprintf("<= %s", sd_to_mean_max),
            flagged = TRUE, reason = "sd_to_mean_ratio_high", stringsAsFactors = FALSE
          )
        }

        if (lo >= 0 && min_val < 0) {
          records[[length(records) + 1L]] <- data.frame(
            feature = c, metric = "min", observed_value = round(min_val, 2),
            expected_range = sprintf(">= %s", lo),
            flagged = TRUE, reason = "negative_clinical_value", stringsAsFactors = FALSE
          )
        }
      }

      u_vals <- unique(vals)
      if (all(u_vals %in% c(0, 1)) && length(u_vals) <= 2) {
        pct_val <- m_val * 100.0
        if (pct_val < min_pct || pct_val > max_pct) {
          records[[length(records) + 1L]] <- data.frame(
            feature = c, metric = "percentage", observed_value = round(pct_val, 2),
            expected_range = sprintf("[%s, %s]", min_pct, max_pct),
            flagged = TRUE, reason = "impossible_percentage", stringsAsFactors = FALSE
          )
        }
      }
    }
  }

  if (isTRUE(audit_unit_discordance)) {
    disc_df <- detect_unit_discordance(df)
    if (nrow(disc_df) > 0) {
      for (r in seq_len(nrow(disc_df))) {
        records[[length(records) + 1L]] <- data.frame(
          feature = as.character(disc_df$feature[r]),
          metric = "unit_discordance",
          observed_value = as.numeric(disc_df$detected_ratio[r]),
          expected_range = sprintf("ratio ~ %s", disc_df$expected_conversion[r]),
          flagged = TRUE,
          reason = sprintf("suspected_%s_vs_%s", disc_df$suspected_unit[r], disc_df$target_unit[r]),
          stringsAsFactors = FALSE
        )
      }
    }
  }

  out <- if (length(records)) do.call(rbind, records) else data.frame()
  if (nrow(out) == 0) {
    out <- data.frame(
      feature = character(0), metric = character(0), observed_value = numeric(0),
      expected_range = character(0), flagged = logical(0), reason = character(0),
      stringsAsFactors = FALSE
    )
  }

  rownames(out) <- NULL
  if (isTRUE(ci_gate) && nrow(out) > 0 && any(out$flagged)) {
    stop(sprintf("Plausibility CI gate failed with %d violations.", sum(out$flagged)))
  }
  out
}

#' Detect Redundant and Derived Features (Constant Ratios, Differences, Collinearities)
#'
#' Identifies deterministic rule-of-three conversions (e.g. Hematocrit = 3 * Hemoglobin),
#' constant offsets, and collinear features to eliminate redundant covariates before modeling.
#'
#' @param df Input feature matrix data frame.
#' @param cols Optional vector of column names to check (defaults to numeric non-ID columns).
#' @param tol Relative tolerance for checking constant ratio or difference (default 1e-3).
#' @param corr_threshold Pearson correlation threshold flagging near-perfect collinearity (default 0.999).
#' @return A data frame with columns:
#'   \code{feature_1}, \code{feature_2}, \code{redundancy_type}, \code{metric_value},
#'   \code{identical_missingness}, \code{recommendation}.
#' @export
detect_redundant_features <- function(df, cols = NULL, tol = 1e-3, corr_threshold = 0.999) {
  id_pats <- c("id", "_id", "person", "subject", "visit", "index", "_row")
  if (!is.null(cols)) {
    cand_cols <- intersect(cols, names(df))
  } else {
    num_cols <- names(df)[vapply(df, is.numeric, logical(1))]
    cand_cols <- num_cols[!vapply(num_cols, function(c) any(vapply(id_pats, function(p) grepl(p, tolower(c)), logical(1))), logical(1))]
  }

  n_cols <- length(cand_cols)
  records <- list()
  if (n_cols < 2) {
    return(data.frame(
      feature_1 = character(0), feature_2 = character(0), redundancy_type = character(0),
      metric_value = numeric(0), identical_missingness = logical(0), recommendation = character(0),
      stringsAsFactors = FALSE
    ))
  }

  for (i in seq_len(n_cols - 1L)) {
    c1 <- cand_cols[i]
    s1 <- df[[c1]]
    for (j in seq(i + 1L, n_cols)) {
      c2 <- cand_cols[j]
      s2 <- df[[c2]]

      identical_missing <- all(is.na(s1) == is.na(s2))
      valid <- !is.na(s1) & !is.na(s2)
      v1 <- as.numeric(s1[valid])
      v2 <- as.numeric(s2[valid])

      if (length(v1) < 10) next

      # 1. Constant ratio: v1 = k * v2
      nz <- v1 != 0 & v2 != 0
      if (sum(nz) >= 10) {
        ratios <- v1[nz] / v2[nz]
        ratio_sd <- stats::sd(ratios)
        ratio_mean <- mean(ratios)
        if (!is.na(ratio_sd) && ratio_sd < tol && abs(ratio_mean) > 1e-6) {
          records[[length(records) + 1L]] <- data.frame(
            feature_1 = c1, feature_2 = c2, redundancy_type = "constant_ratio",
            metric_value = round(ratio_mean, 4), identical_missingness = identical_missing,
            recommendation = sprintf("Drop one; %s is a constant %.3fx multiple of %s.", c1, ratio_mean, c2),
            stringsAsFactors = FALSE
          )
          next
        }
      }

      # 2. Constant difference: v1 = v2 + c
      diffs <- v1 - v2
      diff_sd <- stats::sd(diffs)
      diff_mean <- mean(diffs)
      if (!is.na(diff_sd) && diff_sd < tol) {
        records[[length(records) + 1L]] <- data.frame(
          feature_1 = c1, feature_2 = c2, redundancy_type = "constant_difference",
          metric_value = round(diff_mean, 4), identical_missingness = identical_missing,
          recommendation = sprintf("Drop one; %s is offset from %s by constant %.3f.", c1, c2, diff_mean),
          stringsAsFactors = FALSE
        )
        next
      }

      # 3. Near-perfect correlation
      if (stats::sd(v1) > 0 && stats::sd(v2) > 0) {
        corr <- stats::cor(v1, v2)
        if (!is.na(corr) && abs(corr) >= corr_threshold) {
          records[[length(records) + 1L]] <- data.frame(
            feature_1 = c1, feature_2 = c2, redundancy_type = "perfect_correlation",
            metric_value = round(corr, 4), identical_missingness = identical_missing,
            recommendation = sprintf("Drop one; collinearity |r| = %.4f >= %s.", corr, corr_threshold),
            stringsAsFactors = FALSE
          )
        }
      }
    }
  }

  out <- if (length(records)) do.call(rbind, records) else data.frame()
  if (nrow(out) == 0) {
    return(data.frame(
      feature_1 = character(0), feature_2 = character(0), redundancy_type = character(0),
      metric_value = numeric(0), identical_missingness = logical(0), recommendation = character(0),
      stringsAsFactors = FALSE
    ))
  }
  rownames(out) <- NULL
  out
}

#' Verify DQD Pass-Rate Mathematical Consistency and Reproducibility
#'
#' Recomputes total checks, passed checks, failed checks, and exact pass rates,
#' validating that reported pass rates step in 1/N increments and match actual counts.
#'
#' @param results DQD result list, data frame, or check results table.
#' @param by_site Whether to group results by hospital site (default TRUE).
#' @param site_col Column representing site identifier (default 'site').
#' @return A data frame with columns:
#'   \code{site}, \code{total_checks}, \code{passed}, \code{failed}, \code{not_applicable},
#'   \code{pass_rate}, \code{flagged}, \code{reason}.
#' @export
dqd_summary <- function(results, by_site = TRUE, site_col = "site") {
  df <- if (is.data.frame(results)) {
    results
  } else if (is.list(results) && ("CheckResults" %in% names(results) || "check_results" %in% names(results))) {
    as.data.frame(results$CheckResults %||% results$check_results)
  } else {
    as.data.frame(results)
  }

  if (nrow(df) == 0) {
    return(data.frame(
      site = character(0), total_checks = integer(0), passed = integer(0),
      failed = integer(0), not_applicable = integer(0), pass_rate = numeric(0),
      flagged = logical(0), reason = character(0), stringsAsFactors = FALSE
    ))
  }

  cols_lower <- as.list(stats::setNames(names(df), tolower(names(df))))
  status_col <- cols_lower[["execution_status"]] %||% cols_lower[["status"]] %||% cols_lower[["check_status"]] %||% cols_lower[["checkstatus"]]
  actual_site <- if (isTRUE(by_site)) cols_lower[[tolower(site_col)]] else NULL

  if (is.null(actual_site)) {
    df[["_site"]] <- "ALL"
    actual_site <- "_site"
  }

  summary_rows <- list()
  sites <- unique(df[[actual_site]])
  for (site in sites) {
    grp <- df[df[[actual_site]] == site, , drop = FALSE]
    total <- nrow(grp)

    if (!is.null(status_col)) {
      statuses <- toupper(as.character(grp[[status_col]]))
      passed <- sum(statuses == "PASSED")
      failed <- sum(statuses == "FAILED")
      na_cnt <- sum(statuses %in% c("NOT APPLICABLE", "NOT_APPLICABLE", "SKIPPED", "NA"))
    } else {
      viol_col <- cols_lower[["num_violated_rows"]]
      if (!is.null(viol_col)) {
        viols <- as.numeric(grp[[viol_col]])
        failed <- sum(viols > 0, na.rm = TRUE)
        passed <- sum(viols == 0, na.rm = TRUE)
        na_cnt <- total - (passed + failed)
      } else {
        passed <- total
        failed <- 0
        na_cnt <- 0
      }
    }

    eval_total <- passed + failed
    calc_rate <- if (eval_total > 0) round(passed / eval_total, 4) else 1.0

    why <- character(0)
    if (eval_total > 0) {
      k <- calc_rate * eval_total
      if (abs(k - round(k)) > 1e-3) {
        why <- c(why, "rate_not_achievable")
      }
    }

    rep_rate_col <- cols_lower[["pass_rate"]] %||% cols_lower[["pct_passed"]]
    if (!is.null(rep_rate_col) && !is.na(grp[[rep_rate_col]][1])) {
      rep_val <- as.numeric(grp[[rep_rate_col]][1])
      if (!is.na(rep_val)) {
        if (rep_val > 1.0) rep_val <- rep_val / 100.0
        if (abs(rep_val - calc_rate) > 1e-3) {
          why <- c(why, "reported_rate_inconsistent")
        }
      }
    }

    summary_rows[[length(summary_rows) + 1L]] <- data.frame(
      site = site, total_checks = total, passed = passed, failed = failed,
      not_applicable = na_cnt, pass_rate = calc_rate,
      flagged = length(why) > 0, reason = paste(why, collapse = ","),
      stringsAsFactors = FALSE
    )
  }

  out <- do.call(rbind, summary_rows)
  rownames(out) <- NULL
  out
}

#' Known laboratory unit conversions and standard ratios
#' @export
KNOWN_UNIT_CONVERSIONS <- list(
  creatinine = list(ratio = 88.42, target = "mg/dL", alt = "umol/L", concept_ids = c(3016723L, 3020564L), thresh = 15.0),
  glucose = list(ratio = 18.016, target = "mg/dL", alt = "mmol/L", concept_ids = c(3004501L, 3000483L), thresh = 25.0),
  bun = list(ratio = 2.801, target = "mg/dL", alt = "mmol/L", concept_ids = c(3013682L, 3004295L), thresh = 20.0),
  calcium = list(ratio = 4.008, target = "mg/dL", alt = "mmol/L", concept_ids = c(3015232L, 3025175L), thresh = 10.0),
  total_bilirubin = list(ratio = 17.1, target = "mg/dL", alt = "umol/L", concept_ids = c(3024128L, 3027597L), thresh = 10.0),
  albumin = list(ratio = 10.0, target = "g/dL", alt = "g/L", concept_ids = c(3024561L, 3013721L), thresh = 12.0),
  wbc = list(ratio = 1000.0, target = "10*3/uL", alt = "cells/uL", concept_ids = c(3000905L, 3010813L), thresh = 300.0),
  platelets = list(ratio = 1000.0, target = "10*3/uL", alt = "cells/uL", concept_ids = c(3024929L, 3007461L), thresh = 1500.0)
)

#' Detect Unit Discordance & Bimodal Normalization
#'
#' Audits distributions per measurement/analyte across sites or within a dataset to detect unit discordance
#' (e.g. Creatinine mg/dL vs umol/L x88.42, Glucose mg/dL vs mmol/L x18.016).
#'
#' @param con_or_df Active DuckDB connection or data frame.
#' @param measurement_concept_id Optional concept ID filter.
#' @param concept_id Alias for measurement_concept_id.
#' @param target_unit Target canonical unit (e.g. 'mg/dL').
#' @param site_col Hospital site column name (default 'site').
#' @param measurement_table Measurement table name (default 'measurement').
#' @param value_col Value column name (default 'value_as_number').
#' @param concept_col Concept column name (default 'measurement_concept_id').
#' @param measurement_concept_id Single measurement concept ID.
#' @param measurement_concept_ids Vector of measurement concept IDs.
#' @param concept_id Alias for measurement_concept_id.
#' @param target_unit Canonical unit to compare against.
#' @param site_col Hospital site column name (default 'site').
#' @param measurement_table Measurement table name (default 'measurement').
#' @param value_col Value column name (default 'value_as_number').
#' @param concept_col Measurement concept column name (default 'measurement_concept_id').
#' @param tolerance Ratio deviation tolerance (default 0.35).
#' @return A data frame of detected discordances.
#' @export
detect_unit_discordance <- function(con_or_df, measurement_concept_id = NULL, measurement_concept_ids = NULL,
                                    concept_id = NULL, target_unit = NULL, site_col = "site",
                                    measurement_table = "measurement", value_col = "value_as_number",
                                    concept_col = "measurement_concept_id", tolerance = 0.35) {
  target_cids <- unique(as.integer(c(concept_id, measurement_concept_id, measurement_concept_ids)))
  target_cids <- target_cids[!is.na(target_cids)]

  df <- if (inherits(con_or_df, "DBIConnection") || inherits(con_or_df, "duckdb_connection")) {
    DBI::dbGetQuery(con_or_df, sprintf("SELECT * FROM %s", measurement_table))
  } else {
    as.data.frame(con_or_df)
  }

  records <- list()
  cols_lower <- as.list(stats::setNames(names(df), tolower(names(df))))
  actual_site <- if (!is.null(site_col)) cols_lower[[tolower(site_col)]] else NULL
  actual_val <- cols_lower[[tolower(value_col)]] %||% value_col
  actual_concept <- cols_lower[[tolower(concept_col)]] %||% concept_col

  for (analyte in names(KNOWN_UNIT_CONVERSIONS)) {
    info <- KNOWN_UNIT_CONVERSIONS[[analyte]]
    if (length(target_cids) > 0 && !any(info$concept_ids %in% target_cids)) next
    vals_by_site <- list()
    target_series <- NULL
    cid_found <- NA_integer_

    if (actual_concept %in% names(df) && actual_val %in% names(df)) {
      for (cid in info$concept_ids) {
        sub <- df[df[[actual_concept]] == cid, , drop = FALSE]
        if (nrow(sub) > 0) {
          cid_found <- as.integer(cid)
          if (!is.null(actual_site) && actual_site %in% names(sub) && length(unique(sub[[actual_site]])) > 1) {
            for (s in unique(sub[[actual_site]])) {
              s_vals <- suppressWarnings(as.numeric(sub[[actual_val]][sub[[actual_site]] == s]))
              s_vals <- s_vals[!is.na(s_vals)]
              if (length(s_vals) > 0) vals_by_site[[as.character(s)]] <- s_vals
            }
          }
          t_vals <- suppressWarnings(as.numeric(sub[[actual_val]]))
          target_series <- t_vals[!is.na(t_vals)]
          break
        }
      }
    }

    if (is.null(target_series) || length(target_series) == 0) {
      for (c_name in names(cols_lower)) {
        if (c_name == analyte || grepl(paste0("^", analyte, "_"), c_name) || grepl(paste0("_", analyte, "$"), c_name)) {
          orig_c <- cols_lower[[c_name]]
          s_vals <- suppressWarnings(as.numeric(df[[orig_c]]))
          s_vals <- s_vals[!is.na(s_vals)]
          if (length(s_vals) > 0) {
            target_series <- s_vals
            if (!is.null(actual_site) && actual_site %in% names(df) && length(unique(df[[actual_site]])) > 1) {
              for (s in unique(df[[actual_site]])) {
                site_s <- suppressWarnings(as.numeric(df[[orig_c]][df[[actual_site]] == s]))
                site_s <- site_s[!is.na(site_s)]
                if (length(site_s) > 0) vals_by_site[[as.character(s)]] <- site_s
              }
            }
            break
          }
        }
      }
    }

    if (is.null(target_series) || length(target_series) < 2) next

    exp_ratio <- as.numeric(info$ratio)
    thresh <- as.numeric(info$thresh)

    # 1. Multi-site median audit
    if (length(vals_by_site) >= 2) {
      site_medians <- vapply(vals_by_site, stats::median, numeric(1))
      site_names <- names(site_medians)
      if (length(site_names) >= 2) {
        for (i in seq_len(length(site_names) - 1)) {
          for (j in (i + 1):length(site_names)) {
            s1 <- site_names[i]
            s2 <- site_names[j]
            m1 <- site_medians[s1]
            m2 <- site_medians[s2]
            if (m1 <= 0 || m2 <= 0) next
            obs_ratio <- max(m1, m2) / max(min(m1, m2), 1e-6)
            if (abs(obs_ratio - exp_ratio) / exp_ratio <= tolerance) {
              high_site <- if (m1 > m2) s1 else s2
              low_site <- if (m1 > m2) s2 else s1
              records[[length(records) + 1L]] <- data.frame(
                feature = analyte,
                measurement_concept_id = cid_found,
                site = sprintf("%s_vs_%s", high_site, low_site),
                median_val = round(max(m1, m2), 2),
                detected_ratio = round(obs_ratio, 2),
                expected_conversion = exp_ratio,
                suspected_unit = info$alt,
                target_unit = info$target,
                flagged = TRUE,
                is_discordant = TRUE,
                discordance_type = "cross_site_ratio",
                recommendation = sprintf("Convert site %s from %s to %s (divide by %s)", high_site, info$alt, info$target, exp_ratio),
                stringsAsFactors = FALSE
              )
            }
          }
        }
      }
    }

    # 2. Within-dataset bimodal distribution audit
    sub_low <- target_series[target_series <= thresh]
    sub_high <- target_series[target_series > thresh]
    if (length(sub_low) >= 2 && length(sub_high) >= 2) {
      m_low <- stats::median(sub_low)
      m_high <- stats::median(sub_high)
      if (m_low > 0) {
        obs_ratio <- m_high / m_low
        if (abs(obs_ratio - exp_ratio) / exp_ratio <= tolerance) {
          records[[length(records) + 1L]] <- data.frame(
            feature = analyte,
            measurement_concept_id = cid_found,
            site = "internal_bimodal",
            median_val = round(m_high, 2),
            detected_ratio = round(obs_ratio, 2),
            expected_conversion = exp_ratio,
            suspected_unit = info$alt,
            target_unit = info$target,
            flagged = TRUE,
            is_discordant = TRUE,
            discordance_type = "bimodal_distribution",
            recommendation = sprintf("Normalize values > %s from %s to %s (divide by %s)", thresh, info$alt, info$target, exp_ratio),
            stringsAsFactors = FALSE
          )
        }
      }
    }
  }

  if (length(records) == 0) {
    return(data.frame(
      feature = character(0), measurement_concept_id = integer(0), site = character(0),
      median_val = numeric(0), detected_ratio = numeric(0), expected_conversion = numeric(0),
      suspected_unit = character(0), target_unit = character(0), flagged = logical(0),
      is_discordant = logical(0), discordance_type = character(0), recommendation = character(0),
      stringsAsFactors = FALSE
    ))
  }

  out <- do.call(rbind, records)
  rownames(out) <- NULL
  out$discordance_detected <- out$is_discordant
  out$detected_conversion_ratio <- out$detected_ratio
  out
}

#' Normalize Units
#'
#' Normalizes bimodal or discordant measurement values to a canonical target unit.
#'
#' @param con_or_df Active DuckDB connection or data frame.
#' @param concept_id Optional concept ID.
#' @param feature Optional feature column name.
#' @param target_unit Target unit (default 'mg/dL').
#' @param auto_convert_ratio Numeric ratio (default 88.42).
#' @param threshold Optional threshold above which values are converted.
#' @param bimodal_threshold Alias for threshold.
#' @param value_col Value column name (default 'value_as_number').
#' @param concept_col Concept column name (default 'measurement_concept_id').
#' @param measurement_concept_id Alias for concept_id.
#' @param measurement_table Measurement table name (default 'measurement').
#' @param output_table Optional output table name to materialize into DuckDB.
#' @return Normalized data frame.
#' @export
normalize_units <- function(con_or_df, concept_id = NULL, feature = NULL,
                            target_unit = "mg/dL", auto_convert_ratio = 88.42,
                            threshold = NULL, bimodal_threshold = NULL,
                            value_col = "value_as_number",
                            concept_col = "measurement_concept_id",
                            measurement_concept_id = NULL,
                            measurement_table = "measurement",
                            output_table = NULL) {
  cid <- if (is.null(concept_id)) measurement_concept_id else concept_id
  is_con <- inherits(con_or_df, "DBIConnection") || inherits(con_or_df, "duckdb_connection")
  df <- if (is_con) {
    DBI::dbGetQuery(con_or_df, sprintf("SELECT * FROM %s", measurement_table))
  } else {
    as.data.frame(con_or_df)
  }

  thresh <- if (!is.null(bimodal_threshold)) bimodal_threshold else threshold
  if (is.null(thresh)) {
    if (auto_convert_ratio > 50) thresh <- 15.0
    else if (auto_convert_ratio > 10) thresh <- 25.0
    else thresh <- 10.0
  }

  cols_lower <- as.list(stats::setNames(names(df), tolower(names(df))))
  actual_val <- cols_lower[[tolower(value_col)]] %||% value_col
  actual_concept <- cols_lower[[tolower(concept_col)]] %||% concept_col

  if (!is.null(feature)) {
    f_col <- cols_lower[[tolower(feature)]] %||% feature
    if (f_col %in% names(df)) {
      s <- suppressWarnings(as.numeric(df[[f_col]]))
      mask <- !is.na(s) & s > thresh
      df[[f_col]][mask] <- s[mask] / auto_convert_ratio
      return(df)
    }
  }

  if (actual_concept %in% names(df) && actual_val %in% names(df)) {
    s <- suppressWarnings(as.numeric(df[[actual_val]]))
    mask <- if (!is.null(cid)) {
      !is.na(s) & (df[[actual_concept]] == as.integer(cid)) & (s > thresh)
    } else {
      !is.na(s) & (s > thresh)
    }
    df[[actual_val]][mask] <- s[mask] / auto_convert_ratio
  }

  if (!is.null(output_table) && nzchar(output_table) && is_con) {
    duckdb::duckdb_register(con_or_df, "_temp_norm_df", df)
    DBI::dbExecute(con_or_df, sprintf("CREATE OR REPLACE TABLE %s AS SELECT * FROM _temp_norm_df", output_table))
    duckdb::duckdb_unregister(con_or_df, "_temp_norm_df")
  }

  df
}

#' In-Database Fuzzy and Partial Matching for Unmapped Codes
#'
#' Uses DuckDB's native string similarity functions (jaro_winkler_similarity) and substring/token
#' matching to suggest candidate standard OMOP concepts for unmapped source values (concept_id = 0).
#'
#' @param con Active DuckDB DBI connection.
#' @param unmapped_table Table name or data.frame with unmapped entries.
#' @param domain Domain ID ('Drug', 'Condition', 'Measurement', 'Procedure').
#' @param min_similarity Minimum similarity threshold (default 0.85).
#' @param vocab_db Optional attached vocabulary schema/database name.
#' @param limit_per_code Maximum number of suggestions per source code (default 3).
#' @param source_column Optional explicit column name for source value.
#' @param output_table Optional table name to persist results.
#' @return A data.frame of ranked mapping suggestions.
#' @export
suggest_source_mappings <- function(con,
                                    unmapped_table = NULL,
                                    domain = "Drug",
                                    min_similarity = 0.85,
                                    vocab_db = NULL,
                                    limit_per_code = 3,
                                    source_column = NULL,
                                    output_table = NULL) {
  domain_key <- tolower(trimws(domain))
  domain_title <- switch(domain_key,
    condition = , conditions = "Condition",
    drug = , drugs = , medication = , medications = "Drug",
    measurement = , measurements = , lab = , labs = "Measurement",
    procedure = , procedures = "Procedure",
    tools::toTitleCase(domain_key)
  )

  # 1. Resolve source strings
  unmapped_sql <- if (is.data.frame(unmapped_table)) {
    duckdb::duckdb_register(con, "_tmp_unmapped_in_r", unmapped_table)
    on.exit(tryCatch(duckdb::duckdb_unregister(con, "_tmp_unmapped_in_r"), error = function(e) NULL), add = TRUE)
    cols_lower <- as.list(stats::setNames(names(unmapped_table), tolower(names(unmapped_table))))
    src_col <- source_column %||% cols_lower$source_value %||% cols_lower$condition_source_value %||%
      cols_lower$drug_source_value %||% cols_lower$measurement_source_value %||% names(unmapped_table)[1]
    sprintf("
      SELECT 
        CAST(%s AS VARCHAR) AS source_value,
        COUNT(*) AS unmapped_count
      FROM _tmp_unmapped_in_r
      WHERE %s IS NOT NULL AND TRIM(CAST(%s AS VARCHAR)) != ''
      GROUP BY 1
    ", src_col, src_col, src_col)
  } else if (is.character(unmapped_table) && nzchar(unmapped_table)) {
    cols <- tolower(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", unmapped_table))$column_name)
    cols_lower <- as.list(stats::setNames(cols, cols))
    src_col <- source_column %||% cols_lower$source_value %||% cols_lower$condition_source_value %||%
      cols_lower$drug_source_value %||% cols_lower$measurement_source_value %||% cols[1]
    id_col <- cols_lower$concept_id %||% cols_lower$condition_concept_id %||% cols_lower$drug_concept_id %||%
      cols_lower$measurement_concept_id %||% cols_lower$procedure_concept_id
    filter_clause <- if (!is.null(id_col)) sprintf("(%s = 0 OR %s IS NULL) AND ", id_col, id_col) else ""
    sprintf("
      SELECT 
        CAST(%s AS VARCHAR) AS source_value,
        COUNT(*) AS unmapped_count
      FROM %s
      WHERE %s%s IS NOT NULL AND TRIM(CAST(%s AS VARCHAR)) != ''
      GROUP BY 1
    ", src_col, unmapped_table, filter_clause, src_col, src_col)
  } else {
    table_map <- list(
      condition = c("condition_occurrence", "condition_concept_id", "condition_source_value"),
      conditions = c("condition_occurrence", "condition_concept_id", "condition_source_value"),
      drug = c("drug_exposure", "drug_concept_id", "drug_source_value"),
      drugs = c("drug_exposure", "drug_concept_id", "drug_source_value"),
      measurement = c("measurement", "measurement_concept_id", "measurement_source_value"),
      measurements = c("measurement", "measurement_concept_id", "measurement_source_value"),
      procedure = c("procedure_occurrence", "procedure_concept_id", "procedure_source_value"),
      procedures = c("procedure_occurrence", "procedure_concept_id", "procedure_source_value")
    )
    mapping <- table_map[[domain_key]]
    if (is.null(mapping)) stop("Unknown domain: ", domain, call. = FALSE)
    sprintf("
      SELECT 
        CAST(%s AS VARCHAR) AS source_value,
        COUNT(*) AS unmapped_count
      FROM %s
      WHERE (%s = 0 OR %s IS NULL) AND %s IS NOT NULL AND TRIM(CAST(%s AS VARCHAR)) != ''
      GROUP BY 1
    ", mapping[3], mapping[1], mapping[2], mapping[2], mapping[3], mapping[3])
  }

  vocab_ref <- if (!is.null(vocab_db) && nzchar(vocab_db)) sprintf("%s.concept", vocab_db) else "concept"
  tables <- tolower(DBI::dbGetQuery(con, "SHOW TABLES")$name)
  if (!("concept" %in% tables) && is.null(vocab_db)) {
    tryCatch({
      if (!is.null(DBI::dbGetQuery(con, "SELECT 1 FROM central_vocab.main.concept LIMIT 1"))) {
        vocab_ref <- "central_vocab.main.concept"
      }
    }, error = function(e) NULL)
  }

  match_sql <- sprintf("
    WITH unmapped_src AS (
      %s
    ),
    target_concepts AS (
      SELECT 
        concept_id,
        concept_name,
        domain_id,
        vocabulary_id,
        lower(concept_name) AS norm_name
      FROM %s
      WHERE domain_id = '%s'
        AND standard_concept = 'S'
    ),
    scored AS (
      SELECT 
        u.source_value,
        u.unmapped_count,
        c.concept_id AS suggested_concept_id,
        c.concept_name AS suggested_concept_name,
        c.domain_id,
        c.vocabulary_id,
        ROUND(GREATEST(
          jaro_winkler_similarity(lower(u.source_value), c.norm_name),
          CASE 
            WHEN lower(u.source_value) LIKE '%%' || c.norm_name || '%%' 
              OR c.norm_name LIKE '%%' || lower(u.source_value) || '%%' 
            THEN 0.90 
            ELSE 0.0 
          END
        ), 4) AS similarity_score
      FROM unmapped_src u
      CROSS JOIN target_concepts c
    ),
    filtered AS (
      SELECT 
        source_value,
        unmapped_count,
        suggested_concept_id,
        suggested_concept_name,
        domain_id,
        vocabulary_id,
        similarity_score,
        ROW_NUMBER() OVER (
          PARTITION BY source_value 
          ORDER BY similarity_score DESC, unmapped_count DESC, suggested_concept_id ASC
        ) AS rank
      FROM scored
      WHERE similarity_score >= %f
    )
    SELECT 
      source_value,
      unmapped_count,
      suggested_concept_id,
      suggested_concept_name,
      domain_id,
      vocabulary_id,
      similarity_score,
      rank
    FROM filtered
    WHERE rank <= %d
    ORDER BY unmapped_count DESC, source_value, rank;
  ", unmapped_sql, vocab_ref, domain_title, as.numeric(min_similarity), as.integer(limit_per_code))

  res_df <- DBI::dbGetQuery(con, match_sql)

  if (!is.null(output_table) && nzchar(output_table)) {
    DBI::dbExecute(con, sprintf("CREATE OR REPLACE TABLE %s AS %s", output_table, match_sql))
  }

  res_df
}


