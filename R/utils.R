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
con_is_read_only <- function(con) {
  res <- DBI::dbGetQuery(con, "SELECT readonly FROM duckdb_databases() WHERE database_name = current_database()")
  nrow(res) > 0 && isTRUE(as.logical(res$readonly[[1]]))
}

# DuckDB reports a missing table/macro as a "Catalog Error" and a missing column as a "Binder Error"
# (R raises a generic error class for both).
#' @keywords internal
#' @noRd
is_unresolved_name_error <- function(e) grepl("(Catalog|Binder) Error", conditionMessage(e))

# Split a macro file into statements. Sufficient for inst/sql: no string literal in those files
# contains `--` or `;` (tests/testthat/test-omop-connect.R checks the split against the files).
#' @keywords internal
#' @noRd
split_macro_statements <- function(sql) {
  stmts <- trimws(strsplit(gsub("--[^\r\n]*", "", sql), ";", fixed = TRUE)[[1]])
  stmts[nzchar(stmts)]
}

# Installed copy of a macro file, else the source-tree copy under the working directory.
#' @keywords internal
#' @noRd
macro_file_path <- function(mf) {
  macros_path <- system.file("sql", mf, package = "omopduckdb")
  if (!file.exists(macros_path)) {
    macros_path <- file.path(getwd(), "inst", "sql", mf)
  }
  macros_path
}

# Load the shared SQL macro files (inst/sql) into a connection.
#
# DuckDB validates the body of some macros when it *creates* them, so a macro such as
# map_to_standard_concept_id() cannot be created while a vocabulary table (or a column it reads) is not
# visible on the connection. Others, like descendants_of(), are created regardless and look their table
# up when they are used.
#
# temporary: TRUE creates TEMP macros (session-scoped, nothing written to the database file, works on
#   read-only databases); FALSE creates persistent macros stored in the database. NULL (default) uses
#   TEMP only when the connection's current database is read-only, so ETL runs on a writable database
#   persist macros exactly as before.
# skip_unresolved: FALSE (default) raises when a macro's table or column is missing, as always. TRUE skips
#   such macros (every other macro is still created; only DuckDB's catalog and binder errors are skipped,
#   any other error still raises) and returns their names invisibly, for the caller to report.
#' @keywords internal
#' @noRd
load_mapping_macros <- function(con, temporary = NULL, skip_unresolved = FALSE) {
  macro_files <- c("mapping_macros.sql", "cohort_readmission.sql", "table1_aggregations.sql", "cohort_mortality.sql")
  if (is.null(temporary)) temporary <- con_is_read_only(con)
  skipped <- character()
  for (mf in macro_files) {
    macros_path <- macro_file_path(mf)
    if (!file.exists(macros_path)) {
      warning("SQL macro file '", mf, "' was not found; the macros it defines are not available.", call. = FALSE)
      next
    }
    sql <- paste(readLines(macros_path), collapse = "\n")
    if (isTRUE(temporary)) {
      sql <- gsub("\\bCREATE\\s+OR\\s+REPLACE\\s+MACRO\\b", "CREATE OR REPLACE TEMP MACRO", sql, ignore.case = TRUE, perl = TRUE)
    }
    if (!isTRUE(skip_unresolved)) {
      DBI::dbExecute(con, sql)
      next
    }
    for (stmt in split_macro_statements(sql)) {
      tryCatch(
        DBI::dbExecute(con, stmt),
        error = function(e) {
          if (!is_unresolved_name_error(e)) stop(e)
          skipped <<- c(skipped, regmatches(stmt, regexec("\\bMACRO\\s+(\\w+)", stmt, perl = TRUE))[[1]][[2]])
        }
      )
    }
  }
  invisible(skipped)
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
  LAB_LOINC = c("LAB_LOINC", "LOINC"),
  FACILITYID = c("FACILITYID", "FACILITY_ID"),
  FACILITY_TYPE = c("FACILITY_TYPE", "FACILITY_LOCATION"),
  FACILITY_LOCATION = c("FACILITY_LOCATION", "FACILITY_LOCATION_ZIP"),
  ADDRESSID = c("ADDRESSID", "ADDRESS_ID"),
  ADDRESS_CITY = c("ADDRESS_CITY", "CITY"),
  ADDRESS_STATE = c("ADDRESS_STATE", "STATE"),
  ADDRESS_ZIP5 = c("ADDRESS_ZIP5", "ZIP5", "ZIP"),
  ADDRESS_ZIP9 = c("ADDRESS_ZIP9", "ZIP9"),
  ADDRESS_PREFERRED = c("ADDRESS_PREFERRED", "PREFERRED"),
  ADDRESS_USE = c("ADDRESS_USE", "USE"),
  ADDRESS_TYPE = c("ADDRESS_TYPE", "TYPE"),
  ADDRESS_PERIOD_START = c("ADDRESS_PERIOD_START", "PERIOD_START"),
  ADDRESS_PERIOD_END = c("ADDRESS_PERIOD_END", "PERIOD_END")
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
