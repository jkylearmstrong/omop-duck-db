#' @keywords internal
#' @noRd
find_source_file <- function(source_dir, table_name) {
  matches <- list.files(source_dir, pattern = paste0("(?i)^", table_name, "\\.csv$"), full.names = TRUE)
  if (length(matches) == 0) {
    return(NULL)
  }
  matches[[1]]
}

#' @keywords internal
#' @noRd
run_insert <- function(con, label, sql) {
  cat("Loading", label, "...\n")
  n <- DBI::dbExecute(con, sql)
  cat("  ->", n, "rows inserted\n")
  n
}

#' @keywords internal
#' @noRd
read_local_config <- function(filename) {
  config_path <- file.path("config", filename)
  if (file.exists(config_path)) yaml::read_yaml(config_path) else list()
}

#' @keywords internal
#' @noRd
load_mapping_macros <- function(con) {
  macros_path <- system.file("sql", "mapping_macros.sql", package = "omopduckdb")
  if (!file.exists(macros_path)) {
    macros_path <- file.path(getwd(), "inst", "sql", "mapping_macros.sql")
  }
  DBI::dbExecute(con, paste(readLines(macros_path), collapse = "\n"))
}

#' @keywords internal
#' @noRd
`%||%` <- function(x, y) if (is.null(x)) y else x

#' @keywords internal
#' @noRd
.DEFAULT_ALIASES <- list(
  PATID = c("PATID", "SSID", "PAT_ID", "PERSON_ID"),
  PROVIDERID = c("PROVIDERID", "PROVIDER_ID"),
  ENCOUNTERID = c("ENCOUNTERID", "ENCOUNTER_ID"),
  DIAGNOSISID = c("DIAGNOSISID", "DIAGNOSIS_ID"),
  PROCEDURESID = c("PROCEDURESID", "PROCEDURES_ID", "PROCEDUREID"),
  LAB_RESULT_CM_ID = c("LAB_RESULT_CM_ID", "LAB_RESULT_ID", "MEASUREMENTID"),
  PRESCRIBINGID = c("PRESCRIBINGID", "PRESCRIBING_ID", "DRUGEXPOSUREID"),
  RAW_RX_MED_NAME = c("RAW_RX_MED_NAME", "RX_MED_NAME", "MED_NAME"),
  RAW_RX_NDC = c("RAW_RX_NDC", "RX_NDC", "NDC"),
  RXNORM_CUI = c("RXNORM_CUI", "RXNORM", "CUI"),
  RAW_LAB_NAME = c("RAW_LAB_NAME", "LAB_NAME"),
  RAW_LAB_CODE = c("RAW_LAB_CODE", "LAB_CODE"),
  LAB_LOINC = c("LAB_LOINC", "LOINC")
)

#' @keywords internal
#' @noRd
prepare_source_view <- function(con, view_name, csv_path, expected_columns, aliases = .DEFAULT_ALIASES) {
  cols_df <- DBI::dbGetQuery(con, sprintf(
    "DESCRIBE SELECT * FROM read_csv_auto('%s', union_by_name = true, all_varchar = true) LIMIT 0",
    csv_path
  ))
  actual_cols <- stats::setNames(cols_df$column_name, toupper(cols_df$column_name))

  exprs <- character(length(expected_columns))
  for (i in seq_along(expected_columns)) {
    tgt <- expected_columns[[i]]
    tgt_upper <- toupper(tgt)
    candidates <- aliases[[tgt_upper]] %||% tgt_upper
    if (!tgt_upper %in% toupper(candidates)) {
      candidates <- c(tgt_upper, candidates)
    }

    found <- NULL
    for (cand in candidates) {
      if (toupper(cand) %in% names(actual_cols)) {
        found <- actual_cols[[toupper(cand)]]
        break
      }
    }

    if (!is.null(found)) {
      exprs[[i]] <- sprintf('"%s" AS %s', found, tgt)
    } else {
      exprs[[i]] <- sprintf("NULL AS %s", tgt)
    }
  }

  sql <- sprintf(
    "CREATE OR REPLACE TEMPORARY VIEW %s AS SELECT %s FROM read_csv_auto('%s', union_by_name = true, all_varchar = true);",
    view_name, paste(exprs, collapse = ", "), csv_path
  )
  DBI::dbExecute(con, sql)
}
