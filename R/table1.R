#' Table 1 Descriptive Statistics Generator and Validation Harness
#'
#' Emits standardized epidemiological baseline tables with whole cohort,
#' strata-specific summaries (Mean +/- SD, Median [IQR], N (%)), Standardized
#' Mean Differences (SMD), and p-values, alongside Table 1b completeness audits.

#' Default Consolidated Acute Lab LOINC Mappings
#' @export
DEFAULT_LAB_LOINCS <- list(
  albumin = c("1751-7", "2862-1"),
  alt = c("1742-6", "1743-4", "1744-2"),
  anion_gap = c("1863-0", "48642-3"),
  ast = c("1920-8", "30239-8"),
  bicarbonate = c("1963-8", "2028-9", "2026-3"),
  bun = c("3094-0", "6299-2"),
  creatinine = c("2160-0", "38483-4"),
  egfr = c("33914-3", "48643-1", "62238-1", "88293-6", "88294-4"),
  glucose = c("2345-7", "2339-0", "41653-7"),
  hba1c = c("4548-4", "17856-6", "17855-8"),
  hematocrit = c("20570-8", "4544-3", "4545-0"),
  ldl = c("13457-7", "2089-1", "18262-6"),
  wbc = c("26464-8", "6690-2"),
  sodium = c("2951-2", "2947-0")
)

#' Default Vital Sign LOINC Mappings
#' @export
DEFAULT_VITAL_LOINCS <- list(
  bmi = "39156-5",
  systolic_bp = "8480-6",
  diastolic_bp = "8462-4"
)

#' Default Core Chronic Medication Classes (ATC / RxNorm Ancestors)
#' @export
DEFAULT_MEDICATION_CONCEPTS <- list(
  insulins = c(21600712L, 1782521L, 1502809L),
  metformin = c(1503297L, 21600744L),
  sulfonylureas = c(1502855L, 1502826L, 21600749L),
  statins = c(1539403L, 1545958L, 1551860L, 21601783L),
  antihypertensives = 21601664L,
  raas_inhibitors = 21601744L,
  beta_blockers = c(21601664L, 1314002L, 21601668L),
  systemic_corticosteroids = c(21602722L, 1506270L)
)

#' @keywords internal
#' @noRd
.calc_smd_cont <- function(s1, s0) {
  s1 <- s1[!is.na(s1)]
  s0 <- s0[!is.na(s0)]
  if (length(s1) < 2 || length(s0) < 2) return(NA_real_)
  m1 <- mean(s1)
  m0 <- mean(s0)
  v1 <- stats::var(s1)
  v0 <- stats::var(s0)
  pooled_sd <- sqrt((v1 + v0) / 2.0)
  if (is.na(pooled_sd) || pooled_sd <= 1e-9) return(0.0)
  abs(m1 - m0) / pooled_sd
}

#' @keywords internal
#' @noRd
.calc_smd_bin <- function(p1, p0) {
  denom <- sqrt((p1 * (1.0 - p1) + p0 * (1.0 - p0)) / 2.0)
  if (is.na(denom) || denom <= 1e-9) return(0.0)
  abs(p1 - p0) / denom
}

#' @keywords internal
#' @noRd
.fmt_pval <- function(p) {
  if (is.null(p) || is.na(p)) return("-")
  if (p < 0.001) "<0.001" else sprintf("%.3f", p)
}

#' Generate Table 1 and Completeness Matrix
#'
#' Generates standard epidemiological Table 1 descriptive statistics and Table 1b completeness audit.
#'
#' @param data A `data.frame` or active DuckDB DBI connection.
#' @param strata_col Column name defining stratification (default "outcome_flag").
#' @param continuous_vars Character vector of continuous column names.
#' @param categorical_vars Character vector of categorical column names.
#' @param table_name Character string of table name if `data` is a DuckDB connection.
#' @param strata_labels Named list mapping strata values to display names.
#' @param compute_smd Logical. Compute Standardized Mean Differences between strata (default TRUE).
#' @param compute_pvalues Logical. Compute p-values between strata (default TRUE).
#' @param decimal_places Integer decimal places for numeric outputs (default 1).
#' @return A list containing:
#'   - `table1`: Formatted Table 1 descriptive statistics.
#'   - `table1b`: Table 1b completeness / missingness matrix.
#' @export
generate_table1 <- function(data,
                            strata_col = "outcome_flag",
                            continuous_vars = NULL,
                            categorical_vars = NULL,
                            table_name = NULL,
                            strata_labels = NULL,
                            compute_smd = TRUE,
                            compute_pvalues = TRUE,
                            decimal_places = 1) {
  df <- if (inherits(data, "DBIConnection")) {
    if (is.null(table_name)) stop("table_name is required when data is a DBIConnection.")
    DBI::dbGetQuery(data, sprintf("SELECT * FROM %s", table_name))
  } else if (is.data.frame(data)) {
    data
  } else {
    stop("data must be a data.frame or DBIConnection.")
  }

  has_strata <- !is.null(strata_col) && (strata_col %in% names(df))
  strata_values <- if (has_strata) sort(unique(df[[strata_col]][!is.na(df[[strata_col]])])) else character(0)
  if (length(strata_values) < 2) has_strata <- FALSE

  strata_map <- strata_labels %||% list()
  total_n <- nrow(df)
  strata_dfs <- list()
  strata_ns <- list()

  if (has_strata) {
    for (v in strata_values) {
      sdf <- df[df[[strata_col]] == v, , drop = FALSE]
      strata_dfs[[as.character(v)]] <- sdf
      strata_ns[[as.character(v)]] <- nrow(sdf)
    }
  }

  skip_cols <- c(strata_col, "subject_id", "person_id", "visit_occurrence_id", "cohort_definition_id", "cohort_start_date", "cohort_end_date")
  avail_cols <- setdiff(names(df), skip_cols)

  if (is.null(continuous_vars) && is.null(categorical_vars)) {
    cont_vars <- character(0)
    cat_vars <- character(0)
    for (col in avail_cols) {
      if (is.numeric(df[[col]])) {
        unq <- unique(df[[col]][!is.na(df[[col]])])
        if (length(unq) <= 5 && all(unq %in% c(0, 1))) {
          cat_vars <- c(cat_vars, col)
        } else if (length(unq) <= 4) {
          cat_vars <- c(cat_vars, col)
        } else {
          cont_vars <- c(cont_vars, col)
        }
      } else {
        cat_vars <- c(cat_vars, col)
      }
    }
  } else {
    cont_vars <- continuous_vars %||% character(0)
    cat_vars <- categorical_vars %||% character(0)
  }

  table1_rows <- list()
  table1b_rows <- list()

  overall_header <- sprintf("Overall (N=%s)", format(total_n, big.mark = ","))
  header_row <- list(
    Variable = "Total Patients",
    Category = "Count"
  )
  header_row[[overall_header]] <- sprintf("%s (100.0%%)", format(total_n, big.mark = ","))

  if (has_strata) {
    for (v in strata_values) {
      v_char <- as.character(v)
      lbl <- if (!is.null(strata_map[[v_char]])) strata_map[[v_char]] else sprintf("Stratum %s", v_char)
      n_v <- strata_ns[[v_char]]
      col_lbl <- sprintf("%s (N=%s)", lbl, format(n_v, big.mark = ","))
      header_row[[col_lbl]] <- sprintf("%s (%.*f%%)", format(n_v, big.mark = ","), decimal_places, n_v / total_n * 100.0)
    }
    if (compute_smd) header_row$SMD <- "-"
    if (compute_pvalues) header_row$p_value <- "-"
  }
  table1_rows[[length(table1_rows) + 1]] <- header_row

  # 1. Continuous Variables
  for (var in cont_vars) {
    if (!var %in% names(df)) next
    s_all <- as.numeric(df[[var]])
    all_clean <- s_all[!is.na(s_all)]

    mean_all <- if (length(all_clean) > 0) mean(all_clean) else NA_real_
    std_all <- if (length(all_clean) > 1) stats::sd(all_clean) else NA_real_
    med_all <- if (length(all_clean) > 0) stats::median(all_clean) else NA_real_
    q25_all <- if (length(all_clean) > 0) stats::quantile(all_clean, 0.25) else NA_real_
    q75_all <- if (length(all_clean) > 0) stats::quantile(all_clean, 0.75) else NA_real_

    miss_n_all <- sum(is.na(s_all))
    miss_pct_all <- if (total_n > 0) miss_n_all / total_n * 100.0 else 0.0

    mean_sd_row <- list(
      Variable = var,
      Category = "Mean (SD)"
    )
    mean_sd_row[[overall_header]] <- if (!is.na(mean_all)) sprintf("%.*f (%.*f)", decimal_places, mean_all, decimal_places, std_all) else "-"

    med_iqr_row <- list(
      Variable = var,
      Category = "Median [IQR]"
    )
    med_iqr_row[[overall_header]] <- if (!is.na(med_all)) sprintf("%.*f [%.*f, %.*f]", decimal_places, med_all, decimal_places, q25_all, decimal_places, q75_all) else "-"

    t1b_row <- list(
      Variable = var,
      overall_missing_n = as.integer(miss_n_all),
      overall_missing_pct = round(miss_pct_all, 1)
    )

    if (has_strata) {
      s_strata <- list()
      for (v in strata_values) {
        v_char <- as.character(v)
        s_str <- as.numeric(strata_dfs[[v_char]][[var]])
        s_clean <- s_str[!is.na(s_str)]
        s_strata[[v_char]] <- s_clean

        m_v <- if (length(s_clean) > 0) mean(s_clean) else NA_real_
        std_v <- if (length(s_clean) > 1) stats::sd(s_clean) else NA_real_
        med_v <- if (length(s_clean) > 0) stats::median(s_clean) else NA_real_
        q25_v <- if (length(s_clean) > 0) stats::quantile(s_clean, 0.25) else NA_real_
        q75_v <- if (length(s_clean) > 0) stats::quantile(s_clean, 0.75) else NA_real_

        lbl <- if (!is.null(strata_map[[v_char]])) strata_map[[v_char]] else sprintf("Stratum %s", v_char)
        col_lbl <- sprintf("%s (N=%s)", lbl, format(strata_ns[[v_char]], big.mark = ","))

        mean_sd_row[[col_lbl]] <- if (!is.na(m_v)) sprintf("%.*f (%.*f)", decimal_places, m_v, decimal_places, std_v) else "-"
        med_iqr_row[[col_lbl]] <- if (!is.na(med_v)) sprintf("%.*f [%.*f, %.*f]", decimal_places, med_v, decimal_places, q25_v, decimal_places, q75_v) else "-"

        m_miss <- sum(is.na(s_str))
        m_pct <- if (strata_ns[[v_char]] > 0) m_miss / strata_ns[[v_char]] * 100.0 else 0.0
        t1b_row[[sprintf("stratum_%s_missing_n", v_char)]] <- as.integer(m_miss)
        t1b_row[[sprintf("stratum_%s_missing_pct", v_char)]] <- round(m_pct, 1)
      }

      if (compute_smd && length(strata_values) == 2) {
        smd_val <- .calc_smd_cont(s_strata[[as.character(strata_values[2])]], s_strata[[as.character(strata_values[1])]])
        mean_sd_row$SMD <- if (!is.na(smd_val)) sprintf("%.3f", smd_val) else "-"
        med_iqr_row$SMD <- "-"
      } else if (compute_smd) {
        mean_sd_row$SMD <- "-"
        med_iqr_row$SMD <- "-"
      }

      if (compute_pvalues && length(strata_values) == 2) {
        pval <- tryCatch({
          s1 <- s_strata[[as.character(strata_values[2])]]
          s0 <- s_strata[[as.character(strata_values[1])]]
          if (length(s1) >= 2 && length(s0) >= 2) stats::t.test(s1, s0)$p.value else NA_real_
        }, error = function(e) NA_real_)
        mean_sd_row$p_value <- .fmt_pval(pval)
        med_iqr_row$p_value <- "-"
      } else if (compute_pvalues) {
        mean_sd_row$p_value <- "-"
        med_iqr_row$p_value <- "-"
      }
    }

    table1_rows[[length(table1_rows) + 1]] <- mean_sd_row
    table1_rows[[length(table1_rows) + 1]] <- med_iqr_row
    table1b_rows[[length(table1b_rows) + 1]] <- t1b_row
  }

  # 2. Categorical Variables
  for (var in cat_vars) {
    if (!var %in% names(df)) next
    raw_s <- df[[var]]
    miss_n_all <- sum(is.na(raw_s))
    miss_pct_all <- if (total_n > 0) miss_n_all / total_n * 100.0 else 0.0

    t1b_row <- list(
      Variable = var,
      overall_missing_n = as.integer(miss_n_all),
      overall_missing_pct = round(miss_pct_all, 1)
    )

    cats <- sort(unique(raw_s[!is.na(raw_s)]))
    if (length(cats) == 0) cats <- "Unknown"

    pval_cat <- if (has_strata && length(strata_values) == 2 && compute_pvalues) {
      tryCatch({
        tab <- table(df[[var]], df[[strata_col]])
        if (nrow(tab) >= 2 && ncol(tab) >= 2) suppressWarnings(stats::chisq.test(tab)$p.value) else NA_real_
      }, error = function(e) NA_real_)
    } else {
      NA_real_
    }


    first_cat <- TRUE
    for (cat in cats) {
      n_cat_all <- sum(raw_s == cat, na.rm = TRUE)
      pct_cat_all <- if (total_n > 0) n_cat_all / total_n * 100.0 else 0.0

      cat_row <- list(
        Variable = if (first_cat) var else "",
        Category = as.character(cat)
      )
      cat_row[[overall_header]] <- sprintf("%s (%.*f%%)", format(n_cat_all, big.mark = ","), decimal_places, pct_cat_all)

      if (has_strata) {
        prop_strata <- list()
        for (v in strata_values) {
          v_char <- as.character(v)
          sdf <- strata_dfs[[v_char]]
          n_v <- sum(sdf[[var]] == cat, na.rm = TRUE)
          pct_v <- if (strata_ns[[v_char]] > 0) n_v / strata_ns[[v_char]] * 100.0 else 0.0
          prop_strata[[v_char]] <- pct_v / 100.0

          lbl <- if (!is.null(strata_map[[v_char]])) strata_map[[v_char]] else sprintf("Stratum %s", v_char)
          col_lbl <- sprintf("%s (N=%s)", lbl, format(strata_ns[[v_char]], big.mark = ","))
          cat_row[[col_lbl]] <- sprintf("%s (%.*f%%)", format(n_v, big.mark = ","), decimal_places, pct_v)

          if (first_cat) {
            m_miss <- sum(is.na(sdf[[var]]))
            m_pct <- if (strata_ns[[v_char]] > 0) m_miss / strata_ns[[v_char]] * 100.0 else 0.0
            t1b_row[[sprintf("stratum_%s_missing_n", v_char)]] <- as.integer(m_miss)
            t1b_row[[sprintf("stratum_%s_missing_pct", v_char)]] <- round(m_pct, 1)
          }
        }

        if (compute_smd && length(strata_values) == 2) {
          p1 <- prop_strata[[as.character(strata_values[2])]]
          p0 <- prop_strata[[as.character(strata_values[1])]]
          smd_cat <- .calc_smd_bin(p1, p0)
          cat_row$SMD <- if (!is.na(smd_cat)) sprintf("%.3f", smd_cat) else "-"
        } else if (compute_smd) {
          cat_row$SMD <- "-"
        }

        if (compute_pvalues) {
          cat_row$p_value <- if (first_cat) .fmt_pval(pval_cat) else ""
        }
      }

      table1_rows[[length(table1_rows) + 1]] <- cat_row
      first_cat <- FALSE
    }
    table1b_rows[[length(table1b_rows) + 1]] <- t1b_row
  }

  df_t1 <- as.data.frame(do.call(rbind, lapply(table1_rows, as.data.frame, stringsAsFactors = FALSE, check.names = FALSE)))
  df_t1b <- as.data.frame(do.call(rbind, lapply(table1b_rows, as.data.frame, stringsAsFactors = FALSE, check.names = FALSE)))

  list(
    table1 = df_t1,
    table1b = df_t1b
  )
}

#' @keywords internal
#' @noRd
.norm_vname <- function(x) {
  tolower(gsub("[^a-zA-Z0-9]", "", as.character(x)))
}

#' @keywords internal
#' @noRd
.extract_num <- function(x) {
  if (is.null(x) || is.na(x)) return(NA_real_)
  if (is.numeric(x)) return(as.numeric(x))
  m <- regexpr("[-+]?[0-9]*\\.?[0-9]+", as.character(x))
  if (m > 0) {
    as.numeric(regmatches(as.character(x), m))
  } else {
    NA_real_
  }
}

#' Validate Table 1 Reconciliation against Published Baseline
#'
#' Reconciles prospective OMOP-derived Table 1 distributions and counts against an existing source Table 1.
#'
#' @param omop_table1 Prospective Table 1 (data.frame, list from `generate_table1`, or CSV path).
#' @param source_table1 Baseline Table 1 (data.frame, list, or CSV path).
#' @param tolerance Relative drift threshold for feature distributions (default 0.05 = 5%).
#' @param count_tolerance Relative discrepancy threshold for patient counts (default 0.01 = 1%).
#' @return A list with reconciliation status, concordance summary, feature comparison data.frame, and missing features.
#' @export
validate_table1_reconciliation <- function(omop_table1,
                                           source_table1,
                                           tolerance = 0.05,
                                           count_tolerance = 0.01) {
  df_omop <- if (is.character(omop_table1) && file.exists(omop_table1)) {
    utils::read.csv(omop_table1, stringsAsFactors = FALSE, check.names = FALSE)
  } else if (is.list(omop_table1) && !is.data.frame(omop_table1) && !is.null(omop_table1$table1)) {
    omop_table1$table1
  } else if (is.data.frame(omop_table1)) {
    omop_table1
  } else {
    stop("omop_table1 must be a data.frame, list, or file path.")
  }

  df_source <- if (is.character(source_table1) && file.exists(source_table1)) {
    utils::read.csv(source_table1, stringsAsFactors = FALSE, check.names = FALSE)
  } else if (is.list(source_table1) && !is.data.frame(source_table1) && !is.null(source_table1$table1)) {
    source_table1$table1
  } else if (is.data.frame(source_table1)) {
    source_table1
  } else {
    stop("source_table1 must be a data.frame, list, or file path.")
  }

  omop_var_col <- names(df_omop)[grep("(?i)variable|characteristic|feature|name", names(df_omop))[1] %||% 1]
  source_var_col <- names(df_source)[grep("(?i)variable|characteristic|feature|name", names(df_source))[1] %||% 1]

  omop_val_cols <- setdiff(names(df_omop), c(omop_var_col, "Category", "SMD", "p_value"))
  source_val_cols <- setdiff(names(df_source), c(source_var_col, "Category", "SMD", "p_value"))

  omop_map <- list()
  for (i in seq_len(nrow(df_omop))) {
    vname <- trimws(as.character(df_omop[i, omop_var_col]))
    cat <- if ("Category" %in% names(df_omop)) trimws(as.character(df_omop[i, "Category"])) else ""
    key <- paste0(.norm_vname(vname), if (nzchar(cat) && cat != "Count") paste0("_", .norm_vname(cat)) else "")
    if (nzchar(key)) omop_map[[key]] <- df_omop[i, , drop = FALSE]
  }

  source_map <- list()
  for (i in seq_len(nrow(df_source))) {
    vname <- trimws(as.character(df_source[i, source_var_col]))
    cat <- if ("Category" %in% names(df_source)) trimws(as.character(df_source[i, "Category"])) else ""
    key <- paste0(.norm_vname(vname), if (nzchar(cat) && cat != "Count") paste0("_", .norm_vname(cat)) else "")
    if (nzchar(key)) source_map[[key]] <- df_source[i, , drop = FALSE]
  }

  comparisons <- list()
  drift_count <- 0
  concordant_count <- 0
  missing_features <- character(0)

  for (src_key in names(source_map)) {
    src_row <- source_map[[src_key]]
    omop_row <- omop_map[[src_key]]
    if (is.null(omop_row)) {
      # Try prefix match
      m_key <- NULL
      for (ok in names(omop_map)) {
        if (startsWith(src_key, ok) || startsWith(ok, src_key)) {
          m_key <- ok
          break
        }
      }
      if (!is.null(m_key)) {
        omop_row <- omop_map[[m_key]]
      } else {
        missing_features <- c(missing_features, as.character(src_row[[source_var_col]]))
        next
      }
    }

    src_val_raw <- if (length(source_val_cols) > 0) src_row[[source_val_cols[1]]] else NULL
    omop_val_raw <- if (length(omop_val_cols) > 0) omop_row[[omop_val_cols[1]]] else NULL

    num_src <- .extract_num(src_val_raw)
    num_omop <- .extract_num(omop_val_raw)

    if (!is.na(num_src) && !is.na(num_omop)) {
      diff <- abs(num_omop - num_src)
      rel_diff <- diff / max(abs(num_src), 1e-9)
      is_drift <- rel_diff > tolerance
      if (is_drift) {
        drift_count <- drift_count + 1
        status <- "DRIFT"
      } else {
        concordant_count <- concordant_count + 1
        status <- "CONCORDANT"
      }
      comparisons[[length(comparisons) + 1]] <- list(
        feature = as.character(src_row[[source_var_col]]),
        omop_value = num_omop,
        source_value = num_src,
        abs_diff = round(diff, 4),
        rel_diff = round(rel_diff, 4),
        status = status
      )
    }
  }

  df_comp <- if (length(comparisons) > 0) {
    as.data.frame(do.call(rbind, lapply(comparisons, as.data.frame, stringsAsFactors = FALSE)))
  } else {
    data.frame(feature = character(0), omop_value = numeric(0), source_value = numeric(0), abs_diff = numeric(0), rel_diff = numeric(0), status = character(0))
  }

  is_concordant <- (drift_count == 0) && (length(missing_features) == 0)
  overall_status <- if (is_concordant) "PASS" else if (drift_count > 0) "DRIFT_DETECTED" else "MISSING_FEATURES"

  list(
    is_concordant = is_concordant,
    status = overall_status,
    overall_summary = list(
      total_features_evaluated = length(comparisons),
      concordant_features = concordant_count,
      drift_features = drift_count,
      missing_features_count = length(missing_features),
      tolerance = tolerance
    ),
    feature_comparisons = df_comp,
    missing_features = missing_features
  )
}
