#' Pipeline Reconciliation, Cohort Overlap, and Parity Ledger
#'
#' Provides tools for comparing and validating new OMOP pipelines against legacy implementations:
#' \itemize{
#'   \item \code{cohort_from_index_stays}: Maps external/legacy index stays to OMOP person_id and visit_occurrence_id.
#'   \item \code{compare_feature_tables}: Feature-by-feature parity ledger with SMD, correlations, and scale checks.
#'   \item \code{cohort_overlap}: Reconciles two cohort definitions with Jaccard overlap and outcome rate deltas.
#'   \item \code{run_study_grid}: Sensitivity grid executing multiple study specification variants side by side.
#' }
#'
#' @name reconciliation
NULL

#' Force Study Cohort Index Stays from an External List
#'
#' Maps source patient and encounter identifiers to standard OMOP \code{person_id} and
#' \code{visit_occurrence_id}, producing a compliant OMOP cohort table while reporting
#' match/unmatched diagnostic tallies.
#'
#' @param con Active DuckDB DBI connection.
#' @param stays Input data frame, CSV/parquet path, or table name.
#' @param id_type Identifier type ('person_source_value' or 'person_id').
#' @param person_source_col Patient identifier column in input stays (default 'person_source_value').
#' @param visit_source_col Optional encounter identifier column (default 'visit_source_value').
#' @param index_date_col Index/admission date column in input stays (default 'index_date').
#' @param end_date_col Optional discharge/end date column in input stays (default 'end_date').
#' @param cohort_definition_id OMOP cohort definition identifier (default 1).
#' @param site_col Site column name if present (default 'site').
#' @param target_table Optional table name to persist the cohort in DuckDB.
#' @param group_by Optional column name (e.g. 'SITE') to stratify match diagnostics by.
#' @return A list with \code{cohort} (data frame) and \code{audit} (list of match metrics).
#' @export
cohort_from_index_stays <- function(con, stays, id_type = "person_source_value",
                                    person_source_col = "person_source_value",
                                    visit_source_col = "visit_source_value",
                                    index_date_col = "index_date",
                                    end_date_col = "end_date",
                                    cohort_definition_id = 1L,
                                    site_col = "site",
                                    target_table = NULL,
                                    group_by = NULL) {
  input_df <- if (is.data.frame(stays)) {
    stays
  } else if (is.character(stays) && (grepl("\\.(csv|tsv|parquet)$", stays, ignore.case = TRUE) || file.exists(stays))) {
    if (grepl("\\.parquet$", stays, ignore.case = TRUE)) {
      DBI::dbGetQuery(con, sprintf("SELECT * FROM read_parquet('%s')", stays))
    } else {
      DBI::dbGetQuery(con, sprintf("SELECT * FROM read_csv_auto('%s')", stays))
    }
  } else {
    DBI::dbGetQuery(con, sprintf("SELECT * FROM %s", stays))
  }

  total_stays <- nrow(input_df)
  unique_input_pts <- if (person_source_col %in% names(input_df)) length(unique(input_df[[person_source_col]])) else total_stays

  cols_lower <- as.list(stats::setNames(names(input_df), tolower(names(input_df))))
  actual_pid_col <- cols_lower[[tolower(person_source_col)]] %||% person_source_col
  actual_idx_col <- cols_lower[[tolower(index_date_col)]] %||% index_date_col
  actual_end_col <- if (!is.null(end_date_col)) cols_lower[[tolower(end_date_col)]] else NULL
  actual_vis_col <- if (!is.null(visit_source_col)) cols_lower[[tolower(visit_source_col)]] else NULL
  actual_site_col <- cols_lower[[tolower(site_col)]]

  DBI::dbWriteTable(con, "_ext_stays_df", input_df, temporary = TRUE, overwrite = TRUE)
  on.exit(try(DBI::dbExecute(con, "DROP TABLE IF EXISTS _ext_stays_df"), silent = TRUE), add = TRUE)

  # Person join
  if (id_type == "person_id" || actual_pid_col == "person_id") {
    person_join <- paste0("JOIN person p ON p.person_id = s.", actual_pid_col)
  } else {
    person_join <- sprintf("JOIN person p ON UPPER(TRIM(CAST(p.person_source_value AS VARCHAR))) = UPPER(TRIM(CAST(s.%s AS VARCHAR)))", actual_pid_col)
  }

  # Visit join
  visit_join <- ""
  visit_sel <- "NULL AS visit_occurrence_id"
  if (!is.null(actual_vis_col) && actual_vis_col %in% names(input_df)) {
    visit_join <- sprintf("LEFT JOIN visit_occurrence vo ON vo.person_id = p.person_id AND UPPER(TRIM(CAST(vo.visit_source_value AS VARCHAR))) = UPPER(TRIM(CAST(s.%s AS VARCHAR)))", actual_vis_col)
    visit_sel <- "vo.visit_occurrence_id"
  } else if (!is.null(actual_idx_col) && actual_idx_col %in% names(input_df)) {
    visit_join <- sprintf("LEFT JOIN visit_occurrence vo ON vo.person_id = p.person_id AND CAST(vo.visit_start_date AS DATE) = CAST(s.%s AS DATE)", actual_idx_col)
    visit_sel <- "vo.visit_occurrence_id"
  }

  site_sel <- if (!is.null(actual_site_col)) sprintf(", s.%s AS site", actual_site_col) else ""
  end_date_expr <- if (!is.null(actual_end_col)) sprintf("COALESCE(CAST(s.%s AS DATE), CAST(s.%s AS DATE))", actual_end_col, actual_idx_col) else sprintf("CAST(s.%s AS DATE)", actual_idx_col)

  sql <- sprintf("
    SELECT DISTINCT
      %d AS cohort_definition_id,
      p.person_id AS subject_id,
      CAST(s.%s AS DATE) AS cohort_start_date,
      %s AS cohort_end_date,
      %s
      %s
    FROM _ext_stays_df s
    %s
    %s
  ", as.integer(cohort_definition_id), actual_idx_col, end_date_expr, visit_sel, site_sel, person_join, visit_join)

  cohort_df <- DBI::dbGetQuery(con, sql)

  matched_pts <- if (nrow(cohort_df) > 0) length(unique(cohort_df$subject_id)) else 0L
  unmatched_pts <- max(0L, unique_input_pts - matched_pts)
  matched_encs <- if (nrow(cohort_df) > 0) sum(!is.na(cohort_df$visit_occurrence_id)) else 0L
  unmatched_encs <- total_stays - matched_encs
  enc_match_rate <- if (total_stays > 0) round(matched_encs / total_stays, 4) else 1.0

  audit <- list(
    total_input_stays = total_stays,
    matched_patients = matched_pts,
    unmatched_patients = unmatched_pts,
    patient_match_rate = if (unique_input_pts > 0) round(matched_pts / unique_input_pts, 4) else 1.0,
    matched_encounters = matched_encs,
    unmatched_encounters = unmatched_encs,
    encounter_match_rate = enc_match_rate
  )

  if (!is.null(group_by)) {
    cols_map <- stats::setNames(names(input_df), tolower(names(input_df)))
    grp_col <- cols_map[[tolower(group_by)]]
    if (!is.null(grp_col)) {
      by_group <- list()
      grp_vals <- unique(input_df[[grp_col]])
      for (gv in grp_vals) {
        g_stays <- input_df[input_df[[grp_col]] == gv, , drop = FALSE]
        g_pts <- if (actual_pid_col %in% names(g_stays)) length(unique(g_stays[[actual_pid_col]])) else nrow(g_stays)
        g_coh <- if (grp_col %in% names(cohort_df)) {
          cohort_df[cohort_df[[grp_col]] == gv, , drop = FALSE]
        } else if ("site" %in% names(cohort_df) && tolower(grp_col) %in% c("site", "site_id", "site_anon")) {
          cohort_df[cohort_df[["site"]] == gv, , drop = FALSE]
        } else {
          cohort_df[cohort_df$subject_id %in% g_stays[[actual_pid_col]], , drop = FALSE]
        }
        g_tot <- nrow(g_stays)
        g_m_pts <- if (nrow(g_coh) > 0) length(unique(g_coh$subject_id)) else 0L
        g_um_pts <- max(0L, g_pts - g_m_pts)
        g_m_encs <- if (nrow(g_coh) > 0) sum(!is.na(g_coh$visit_occurrence_id)) else 0L
        g_um_encs <- g_tot - g_m_encs
        by_group[[as.character(gv)]] <- list(
          total_input_stays = g_tot,
          matched_patients = g_m_pts,
          unmatched_patients = g_um_pts,
          patient_match_rate = if (g_pts > 0) round(g_m_pts / g_pts, 4) else 1.0,
          matched_encounters = g_m_encs,
          unmatched_encounters = g_um_encs,
          encounter_match_rate = if (g_tot > 0) round(g_m_encs / g_tot, 4) else 1.0
        )
      }
      audit$by_group <- by_group
      if (toupper(group_by) %in% c("SITE", "SITE_ID", "SITE_ANON")) {
        audit$by_site <- by_group
      }
    }
  }

  if (!is.null(target_table)) {
    DBI::dbWriteTable(con, target_table, cohort_df, overwrite = TRUE)
  }

  list(cohort = cohort_df, audit = audit)
}

#' Export Companion Identifier Manifest
#'
#' Outputs a 5-column manifest:
#' \code{person_id}, \code{person_source_value}, \code{visit_occurrence_id}, \code{visit_source_value}, \code{index_date}
#' with strict row-order preservation.
#'
#' @param source Input data frame, DuckDB connection, table name, or CSV/Parquet path.
#' @param output_path Optional path to write output CSV or Parquet file.
#' @param con Optional active DuckDB connection.
#' @param cohort_table Table name if source is a connection.
#' @param person_col Person ID column name in source.
#' @param visit_col Visit occurrence ID column name in source.
#' @param index_date_col Index date column name in source.
#' @param person_source_col Person source value column in source.
#' @param visit_source_col Visit source value column in source.
#' @return A data frame with the 5 standardized columns.
#' @export
export_companion_manifest <- function(source, output_path = NULL, con = NULL,
                                      cohort_table = NULL, person_col = NULL,
                                      visit_col = NULL, index_date_col = NULL,
                                      person_source_col = NULL, visit_source_col = NULL) {
  df <- if (is.data.frame(source)) {
    source
  } else if (inherits(source, "DBIConnection")) {
    con <- source
    tbl <- cohort_table %||% "cohort"
    DBI::dbGetQuery(source, paste0("SELECT * FROM ", tbl))
  } else if (is.character(source) && (grepl("\\.(csv|tsv|parquet)$", source, ignore.case = TRUE) || file.exists(source))) {
    if (grepl("\\.parquet$", source, ignore.case = TRUE)) {
      if (!is.null(con)) DBI::dbGetQuery(con, sprintf("SELECT * FROM read_parquet('%s')", source)) else stop("Parquet reading requires con connection.")
    } else {
      utils::read.csv(source, stringsAsFactors = FALSE)
    }
  } else if (is.character(source) && !is.null(con)) {
    DBI::dbGetQuery(con, paste0("SELECT * FROM ", source))
  } else {
    stop("Invalid source provided to export_companion_manifest.")
  }

  n <- nrow(df)
  cols_lower <- as.list(stats::setNames(names(df), tolower(names(df))))

  pid_c <- person_col %||% cols_lower[["person_id"]] %||% cols_lower[["subject_id"]] %||% cols_lower[["pid"]]
  pids <- if (!is.null(pid_c) && pid_c %in% names(df)) df[[pid_c]] else rep(NA_integer_, n)

  psv_c <- person_source_col %||% cols_lower[["person_source_value"]] %||% cols_lower[["mrn"]] %||% cols_lower[["pat_id"]] %||% cols_lower[["patient_id"]]
  psvs <- if (!is.null(psv_c) && psv_c %in% names(df)) df[[psv_c]] else rep(NA_character_, n)

  vid_c <- visit_col %||% cols_lower[["visit_occurrence_id"]] %||% cols_lower[["visit_id"]] %||% cols_lower[["encounter_id"]] %||% cols_lower[["hadm_id"]]
  vids <- if (!is.null(vid_c) && vid_c %in% names(df)) df[[vid_c]] else rep(NA_integer_, n)

  vsv_c <- visit_source_col %||% cols_lower[["visit_source_value"]] %||% cols_lower[["encounter_source_value"]] %||% cols_lower[["visit_source"]] %||% cols_lower[["enc_id"]]
  vsvs <- if (!is.null(vsv_c) && vsv_c %in% names(df)) df[[vsv_c]] else rep(NA_character_, n)

  idx_c <- index_date_col %||% cols_lower[["index_date"]] %||% cols_lower[["cohort_start_date"]] %||% cols_lower[["visit_start_date"]] %||% cols_lower[["admit_date"]]
  idxs <- if (!is.null(idx_c) && idx_c %in% names(df)) as.character(df[[idx_c]]) else rep(NA_character_, n)

  manifest <- data.frame(
    person_id = pids,
    person_source_value = as.character(psvs),
    visit_occurrence_id = vids,
    visit_source_value = as.character(vsvs),
    index_date = as.character(idxs),
    stringsAsFactors = FALSE
  )

  if (!is.null(con)) {
    try({
      # Fill person_source_value
      na_psv <- is.na(manifest$person_source_value) & !is.na(manifest$person_id)
      if (any(na_psv)) {
        req_pids <- unique(manifest$person_id[na_psv])
        req_pids <- req_pids[!is.na(req_pids)]
        if (length(req_pids) > 0) {
          res <- DBI::dbGetQuery(con, sprintf("SELECT person_id, person_source_value FROM person WHERE person_id IN (%s)", paste(as.integer(req_pids), collapse = ", ")))
          if (nrow(res) > 0) {
            m <- match(manifest$person_id, res$person_id)
            manifest$person_source_value[na_psv & !is.na(m)] <- res$person_source_value[m[na_psv & !is.na(m)]]
          }
        }
      }

      # Fill person_id
      na_pid <- is.na(manifest$person_id) & !is.na(manifest$person_source_value)
      if (any(na_pid)) {
        req_psvs <- unique(manifest$person_source_value[na_pid])
        req_psvs <- req_psvs[!is.na(req_psvs)]
        if (length(req_psvs) > 0) {
          q_psvs <- paste(sprintf("'%s'", req_psvs), collapse = ", ")
          res <- DBI::dbGetQuery(con, sprintf("SELECT person_source_value, person_id FROM person WHERE UPPER(TRIM(person_source_value)) IN (%s)", toupper(q_psvs)))
          if (nrow(res) > 0) {
            m <- match(toupper(trimws(manifest$person_source_value)), toupper(trimws(res$person_source_value)))
            manifest$person_id[na_pid & !is.na(m)] <- res$person_id[m[na_pid & !is.na(m)]]
          }
        }
      }

      # Fill visit_source_value
      na_vsv <- is.na(manifest$visit_source_value) & !is.na(manifest$visit_occurrence_id)
      if (any(na_vsv)) {
        req_vids <- unique(manifest$visit_occurrence_id[na_vsv])
        req_vids <- req_vids[!is.na(req_vids)]
        if (length(req_vids) > 0) {
          res <- DBI::dbGetQuery(con, sprintf("SELECT visit_occurrence_id, visit_source_value, visit_start_date FROM visit_occurrence WHERE visit_occurrence_id IN (%s)", paste(as.integer(req_vids), collapse = ", ")))
          if (nrow(res) > 0) {
            m <- match(manifest$visit_occurrence_id, res$visit_occurrence_id)
            manifest$visit_source_value[na_vsv & !is.na(m)] <- res$visit_source_value[m[na_vsv & !is.na(m)]]
            na_idx <- is.na(manifest$index_date)
            manifest$index_date[na_idx & !is.na(m)] <- as.character(res$visit_start_date[m[na_idx & !is.na(m)]])
          }
        }
      }

      # Fill visit_occurrence_id
      na_vid <- is.na(manifest$visit_occurrence_id) & !is.na(manifest$visit_source_value)
      if (any(na_vid)) {
        req_vsvs <- unique(manifest$visit_source_value[na_vid])
        req_vsvs <- req_vsvs[!is.na(req_vsvs)]
        if (length(req_vsvs) > 0) {
          q_vsvs <- paste(sprintf("'%s'", req_vsvs), collapse = ", ")
          res <- DBI::dbGetQuery(con, sprintf("SELECT visit_source_value, visit_occurrence_id, visit_start_date FROM visit_occurrence WHERE UPPER(TRIM(visit_source_value)) IN (%s)", toupper(q_vsvs)))
          if (nrow(res) > 0) {
            m <- match(toupper(trimws(manifest$visit_source_value)), toupper(trimws(res$visit_source_value)))
            manifest$visit_occurrence_id[na_vid & !is.na(m)] <- res$visit_occurrence_id[m[na_vid & !is.na(m)]]
            na_idx <- is.na(manifest$index_date)
            manifest$index_date[na_idx & !is.na(m)] <- as.character(res$visit_start_date[m[na_idx & !is.na(m)]])
          }
        }
      }
    }, silent = TRUE)
  }

  if (!is.null(output_path)) {
    dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
    utils::write.csv(manifest, output_path, row.names = FALSE)
  }

  rownames(manifest) <- NULL
  manifest
}

#' Feature Parity Ledger Comparing Two Derivations
#'
#' Quantifies standardized mean differences (SMD), correlation, missingness gaps, and variance ratios
#' to validate a new OMOP feature pipeline against an older one.
#'
#' @param left First feature matrix data frame.
#' @param right Second feature matrix data frame.
#' @param keys Key column or vector of column names to join on.
#' @param by Optional site column to stratify parity ledger by (default NULL).
#' @param tolerances Optional named list of custom per-feature SMD thresholds.
#' @param smd_threshold Standardized mean difference threshold flagging discrepancy (default 0.10).
#' @param missingness_gap_threshold Maximum allowable missingness gap (default 0.05 = 5 percent).
#' @param scale_ratio_threshold Maximum allowable ratio of standard deviations (default 5.0).
#' @param min_cell_count Cell count threshold for privacy suppression (default 11).
#' @param left_manifest Optional companion manifest for left matrix.
#' @param right_manifest Optional companion manifest for right matrix.
#' @param stream_to_parquet Optional path to stream results to Parquet file(s).
#' @param partition_by Optional column name to partition Parquet outputs by.
#' @param to_arrow_dataset Logical; if TRUE, returns an Arrow Dataset object.
#' @return A data frame with columns:
#'   \code{site}, \code{feature}, \code{matched_n}, \code{left_mean}, \code{right_mean},
#'   \code{left_sd}, \code{right_sd}, \code{smd}, \code{correlation}, \code{left_missing_rate},
#'   \code{right_missing_rate}, \code{missingness_gap}, \code{flagged}, \code{reason}.
#' @export
compare_feature_tables <- function(left, right, keys = NULL, by = NULL, tolerances = NULL,
                                   smd_threshold = 0.10, missingness_gap_threshold = 0.05,
                                   scale_ratio_threshold = 5.0, min_cell_count = 11L,
                                   left_manifest = NULL, right_manifest = NULL,
                                   stream_to_parquet = NULL, partition_by = NULL,
                                   to_arrow_dataset = FALSE) {
  left_df <- left
  if (!is.null(left_manifest)) {
    lm <- if (is.data.frame(left_manifest)) left_manifest else utils::read.csv(left_manifest, stringsAsFactors = FALSE)
    for (c in c("person_id", "visit_occurrence_id", "index_date", "person_source_value", "visit_source_value")) {
      if (c %in% names(lm) && !c %in% names(left_df) && nrow(lm) == nrow(left_df)) {
        left_df[[c]] <- lm[[c]]
      }
    }
  }

  right_df <- right
  if (!is.null(right_manifest)) {
    rm <- if (is.data.frame(right_manifest)) right_manifest else utils::read.csv(right_manifest, stringsAsFactors = FALSE)
    for (c in c("person_id", "visit_occurrence_id", "index_date", "person_source_value", "visit_source_value")) {
      if (c %in% names(rm) && !c %in% names(right_df) && nrow(rm) == nrow(right_df)) {
        right_df[[c]] <- rm[[c]]
      }
    }
  }

  if (is.null(keys)) {
    cand_keys <- list(
      c("person_id", "visit_occurrence_id"),
      c("person_id", "index_date"),
      c("person_id"),
      c("subject_id", "visit_occurrence_id"),
      c("subject_id")
    )
    found_key <- NULL
    for (cand in cand_keys) {
      if (all(cand %in% names(left_df)) && all(cand %in% names(right_df))) {
        found_key <- cand
        break
      }
    }
    key_list <- found_key %||% "person_id"
  } else {
    key_list <- as.character(keys)
  }

  common_cols <- setdiff(intersect(names(left_df), names(right_df)), key_list)
  if (!is.null(by) && by %in% common_cols) common_cols <- setdiff(common_cols, by)

  merged <- merge(left_df, right_df, by = key_list, suffixes = c("_L", "_R"))
  if (nrow(merged) == 0) {
    return(data.frame(
      site = character(0), feature = character(0), matched_n = character(0), left_mean = numeric(0),
      right_mean = numeric(0), left_sd = numeric(0), right_sd = numeric(0), smd = numeric(0),
      correlation = numeric(0), left_missing_rate = numeric(0), right_missing_rate = numeric(0),
      missingness_gap = numeric(0), flagged = logical(0), reason = character(0), stringsAsFactors = FALSE
    ))
  }

  site_col_l <- if (!is.null(by) && paste0(by, "_L") %in% names(merged)) paste0(by, "_L") else (if (!is.null(by) && by %in% names(merged)) by else NULL)
  if (is.null(site_col_l)) {
    merged[["_site"]] <- "ALL"
    site_col_l <- "_site"
  }

  records <- list()
  sites <- unique(merged[[site_col_l]])
  sites <- sites[!is.na(sites)]
  sites_to_process <- if (!"ALL" %in% sites && length(sites) > 1) c(as.character(sites), "ALL") else as.character(sites)

  for (site in sites_to_process) {
    s_df <- if (identical(site, "ALL")) merged else merged[merged[[site_col_l]] == site, , drop = FALSE]
    n_matched <- nrow(s_df)
    if (n_matched == 0) next

    for (feat in common_cols) {
      col_l <- paste0(feat, "_L")
      col_r <- paste0(feat, "_R")
      if (!col_l %in% names(s_df) || !col_r %in% names(s_df)) next

      s1 <- s_df[[col_l]]
      s2 <- s_df[[col_r]]

      m_rate_l <- mean(is.na(s1))
      m_rate_r <- mean(is.na(s2))
      miss_gap <- abs(m_rate_l - m_rate_r)

      valid <- !is.na(s1) & !is.na(s2)
      v1 <- suppressWarnings(as.numeric(s1[valid]))
      v2 <- suppressWarnings(as.numeric(s2[valid]))

      if (length(v1) == 0 || any(is.na(v1)) || any(is.na(v2))) next

      mean_l <- mean(v1)
      mean_r <- mean(v2)
      sd_l <- if (length(v1) > 1) stats::sd(v1) else 0.0
      sd_r <- if (length(v2) > 1) stats::sd(v2) else 0.0

      pooled_sd <- if (sd_l > 0 || sd_r > 0) sqrt((sd_l^2 + sd_r^2) / 2.0) else 0.0
      smd <- if (pooled_sd > 0) abs(mean_l - mean_r) / pooled_sd else 0.0

      corr <- if (sd_l > 0 && sd_r > 0) stats::cor(v1, v2) else (if (mean_l == mean_r) 1.0 else 0.0)

      scale_ratio <- if (sd_r > 0 && sd_l > 0) max(sd_l / sd_r, sd_r / sd_l) else 1.0

      reasons <- character(0)
      feat_smd_thresh <- if (!is.null(tolerances) && feat %in% names(tolerances)) tolerances[[feat]] else smd_threshold
      if (smd > feat_smd_thresh) reasons <- c(reasons, "large_smd")
      if (miss_gap > missingness_gap_threshold) reasons <- c(reasons, "missingness_gap")
      if (scale_ratio > scale_ratio_threshold) reasons <- c(reasons, "scale_mismatch")
      if (sd_l == 0.0 && sd_r == 0.0) {
        if (mean_l == 0.0 && mean_r == 0.0) reasons <- c(reasons, "all_zero") else reasons <- c(reasons, "zero_variance")
      }

      records[[length(records) + 1L]] <- data.frame(
        site = site, feature = feat,
        matched_n = if (n_matched >= min_cell_count) as.character(n_matched) else sprintf("<%d", min_cell_count),
        left_mean = round(mean_l, 4), right_mean = round(mean_r, 4),
        left_sd = round(sd_l, 4), right_sd = round(sd_r, 4),
        smd = round(smd, 4), correlation = round(corr, 4),
        left_missing_rate = round(m_rate_l, 4), right_missing_rate = round(m_rate_r, 4),
        missingness_gap = round(miss_gap, 4),
        flagged = length(reasons) > 0, reason = paste(reasons, collapse = ","),
        stringsAsFactors = FALSE
      )
    }
  }

  out <- if (length(records)) do.call(rbind, records) else data.frame()
  if (nrow(out) == 0) {
    return(data.frame(
      site = character(0), feature = character(0), matched_n = character(0), left_mean = numeric(0),
      right_mean = numeric(0), left_sd = numeric(0), right_sd = numeric(0), smd = numeric(0),
      correlation = numeric(0), left_missing_rate = numeric(0), right_missing_rate = numeric(0),
      missingness_gap = numeric(0), flagged = logical(0), reason = character(0), stringsAsFactors = FALSE
    ))
  }
  rownames(out) <- NULL

  if (!is.null(stream_to_parquet) && nzchar(stream_to_parquet)) {
    out_dir <- dirname(stream_to_parquet)
    if (!dir.exists(out_dir)) dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
    if (requireNamespace("arrow", quietly = TRUE)) {
      if (!is.null(partition_by) && length(partition_by) > 0 && all(partition_by %in% names(out))) {
        arrow::write_dataset(out, path = stream_to_parquet, partitioning = partition_by)
      } else {
        arrow::write_parquet(out, stream_to_parquet)
      }
    }
  }

  if (isTRUE(to_arrow_dataset) && requireNamespace("arrow", quietly = TRUE)) {
    return(arrow::as_arrow_table(out))
  }

  out
}

#' Cohort Reconciliation and Membership Overlap
#'
#' Compares participants included in Cohort A only, Cohort B only, or both,
#' computing Jaccard similarity indices and event rate deltas.
#'
#' @param con Optional DuckDB DBI connection (if cohort_a/b are table names).
#' @param cohort_a First cohort data frame or table name.
#' @param cohort_b Second cohort data frame or table name.
#' @param by Optional site column to stratify by (default 'site').
#' @param person_col Person identifier column name (default 'subject_id').
#' @param outcome_col Optional binary outcome column name to compare event rates.
#' @return A data frame with columns \code{site}, \code{n_cohort_a}, \code{n_cohort_b},
#'   \code{both_count}, \code{a_only_count}, \code{b_only_count}, \code{jaccard_index}, \code{concordance_rate}.
#' @export
cohort_overlap <- function(con = NULL, cohort_a, cohort_b, by = "site",
                           person_col = "subject_id", outcome_col = NULL) {
  df_a <- if (is.character(cohort_a)) DBI::dbGetQuery(con, sprintf("SELECT * FROM %s", cohort_a)) else cohort_a
  df_b <- if (is.character(cohort_b)) DBI::dbGetQuery(con, sprintf("SELECT * FROM %s", cohort_b)) else cohort_b

  pcol_a <- if (person_col %in% names(df_a)) person_col else (if ("person_id" %in% names(df_a)) "person_id" else "subject_id")
  pcol_b <- if (person_col %in% names(df_b)) person_col else (if ("person_id" %in% names(df_b)) "person_id" else "subject_id")

  site_a <- if (!is.null(by) && by %in% names(df_a)) by else NULL
  if (is.null(site_a)) {
    df_a[["_site"]] <- "ALL"
    site_a <- "_site"
  }

  site_b <- if (!is.null(by) && by %in% names(df_b)) by else NULL
  if (is.null(site_b)) {
    df_b[["_site"]] <- "ALL"
    site_b <- "_site"
  }

  sites <- unique(c(as.character(df_a[[site_a]]), as.character(df_b[[site_b]])))
  sites_to_process <- if (!"ALL" %in% sites && length(sites) > 1) c(sites, "ALL") else sites

  records <- list()
  has_outcome <- !is.null(outcome_col) && outcome_col %in% names(df_a) && outcome_col %in% names(df_b)

  for (s in sites_to_process) {
    sub_a <- if (identical(s, "ALL")) df_a else df_a[df_a[[site_a]] == s, , drop = FALSE]
    sub_b <- if (identical(s, "ALL")) df_b else df_b[df_b[[site_b]] == s, , drop = FALSE]

    pids_a <- unique(sub_a[[pcol_a]][!is.na(sub_a[[pcol_a]])])
    pids_b <- unique(sub_b[[pcol_b]][!is.na(sub_b[[pcol_b]])])

    both_pids <- intersect(pids_a, pids_b)
    a_only_pids <- setdiff(pids_a, pids_b)
    b_only_pids <- setdiff(pids_b, pids_a)
    union_pids <- union(pids_a, pids_b)

    both_cnt <- length(both_pids)
    a_only_cnt <- length(a_only_pids)
    b_only_cnt <- length(b_only_pids)
    union_cnt <- length(union_pids)

    jaccard <- if (union_cnt > 0) round(both_cnt / union_cnt, 4) else 1.0
    denom <- a_only_cnt + b_only_cnt + both_cnt
    concordance <- if (denom > 0) round(both_cnt / denom, 4) else 1.0

    rec <- data.frame(
      site = s, n_cohort_a = length(pids_a), n_cohort_b = length(pids_b),
      both_count = both_cnt, a_only_count = a_only_cnt, b_only_count = b_only_cnt,
      jaccard_index = jaccard, concordance_rate = concordance,
      stringsAsFactors = FALSE
    )

    if (has_outcome) {
      a_rate <- if (a_only_cnt > 0) mean(sub_a[[outcome_col]][sub_a[[pcol_a]] %in% a_only_pids], na.rm = TRUE) else 0.0
      b_rate <- if (b_only_cnt > 0) mean(sub_b[[outcome_col]][sub_b[[pcol_b]] %in% b_only_pids], na.rm = TRUE) else 0.0
      both_rate <- if (both_cnt > 0) mean(sub_a[[outcome_col]][sub_a[[pcol_a]] %in% both_pids], na.rm = TRUE) else 0.0

      rec$a_only_event_rate <- round(a_rate, 4)
      rec$b_only_event_rate <- round(b_rate, 4)
      rec$both_event_rate <- round(both_rate, 4)
    }

    records[[length(records) + 1L]] <- rec
  }

  out <- do.call(rbind, records)
  rownames(out) <- NULL
  out
}

#' Study Sensitivity Grid Execution
#'
#' Runs Cartesian product of study options (e.g. sampling rules, lookback windows,
#' wash-out days) and tabulates comparative N, events, and rates side by side.
#'
#' @param con Active DuckDB DBI connection.
#' @param study_fn Function returning a cohort data frame given parameter arguments.
#' @param variants Named list mapping parameter names to vectors of candidate values.
#' @param base_args Named list of fixed base arguments.
#' @param outcome_col Name of event/outcome indicator column (default 'outcome').
#' @param site_col Site column name if present (default 'site').
#' @return A data frame with variant parameters, \code{site}, \code{total_n}, \code{event_count}, \code{event_rate}.
#' @export
run_study_grid <- function(con, study_fn, variants, base_args = list(),
                           outcome_col = "outcome", site_col = "site") {
  keys <- names(variants)
  grid <- expand.grid(variants, stringsAsFactors = FALSE)

  records <- list()
  for (i in seq_len(nrow(grid))) {
    vparams <- as.list(grid[i, , drop = FALSE])
    run_kwargs <- utils::modifyList(base_args, vparams)

    cohort_df <- do.call(study_fn, run_kwargs)
    if (nrow(cohort_df) == 0) {
      rec <- cbind(as.data.frame(vparams), data.frame(site = "ALL", total_n = 0L, event_count = 0L, event_rate = 0.0, stringsAsFactors = FALSE))
      records[[length(records) + 1L]] <- rec
      next
    }

    site_actual <- if (site_col %in% names(cohort_df)) site_col else NULL
    if (is.null(site_actual)) {
      cohort_df[["_site"]] <- "ALL"
      site_actual <- "_site"
    }

    sites <- unique(cohort_df[[site_actual]])
    sites_to_process <- if (!"ALL" %in% sites && length(sites) > 1) c(as.character(sites), "ALL") else as.character(sites)

    for (s in sites_to_process) {
      sub <- if (identical(s, "ALL")) cohort_df else cohort_df[cohort_df[[site_actual]] == s, , drop = FALSE]
      n <- nrow(sub)
      if (!is.null(outcome_col) && outcome_col %in% names(sub)) {
        events <- sum(sub[[outcome_col]] == 1, na.rm = TRUE)
        rate <- if (n > 0) round(events / n, 4) else 0.0
      } else {
        events <- 0L
        rate <- 0.0
      }

      rec <- cbind(
        as.data.frame(vparams),
        data.frame(site = s, total_n = n, event_count = events, event_rate = rate, stringsAsFactors = FALSE)
      )
      records[[length(records) + 1L]] <- rec
    }
  }

  out <- do.call(rbind, records)
  rownames(out) <- NULL
  out
}
