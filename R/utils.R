#' @keywords internal
#' @noRd
find_source_file <- function(source_dir, table_name, source_format = NULL) {
  formats <- if (is.null(source_format)) c("csv", "parquet") else tolower(source_format)
  for (fmt in formats) {
    matches <- sort(list.files(source_dir, pattern = paste0("^", table_name, "\\.", fmt, "$"), full.names = TRUE, ignore.case = TRUE))
    if (length(matches)) return(matches[[1]])
  }
  NULL
}

#' @keywords internal
#' @noRd
.source_relation <- function(path) {
  if (grepl("\\.parquet$", path, ignore.case = TRUE)) {
    sprintf("(SELECT COLUMNS(*)::VARCHAR FROM read_parquet(%s))", sql_path_literal(path))
  } else {
    sprintf("read_csv_auto(%s, union_by_name = true, all_varchar = true)", sql_path_literal(path))
  }
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

#' Load SQL Macros into Connection
#'
#' @param con Active DuckDB connection.
#' @param temporary Logical, whether to load macros as temporary. Default matches read-only state.
#' @param skip_unresolved Logical, whether to skip macros that fail to bind unresolved dependencies.
#' @return Character vector of skipped macro names invisibly.
#' @export
load_macros <- function(con, temporary = NULL, skip_unresolved = FALSE) {
  load_mapping_macros(con, temporary = temporary, skip_unresolved = skip_unresolved)
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
  MEDADMINID = c("MEDADMINID", "MEDADMIN_ID"),
  MEDADMIN_CODE = c("MEDADMIN_CODE", "MED_CODE", "RXNORM_CUI", "RXNORM"),
  MEDADMIN_TYPE = c("MEDADMIN_TYPE", "ADMIN_TYPE", "MED_TYPE", "MEDADMIN_CODE_TYPE"),
  MEDADMIN_START_DATE = c("MEDADMIN_START_DATE", "ADMIN_DATE", "START_DATE"),
  MEDADMIN_START_TIME = c("MEDADMIN_START_TIME", "ADMIN_TIME", "START_TIME"),
  MEDADMIN_STOP_DATE = c("MEDADMIN_STOP_DATE", "STOP_DATE"),
  RAW_MEDADMIN_MED_NAME = c("RAW_MEDADMIN_MED_NAME", "MEDADMIN_MED_NAME", "RAW_RX_MED_NAME", "MED_NAME"),
  DISPENSINGID = c("DISPENSINGID", "DISPENSING_ID"),
  DISPENSING_TYPE = c("DISPENSING_TYPE", "DISP_TYPE", "DISPENSING_SRC", "NDC_TYPE", "DISP_CODE_TYPE"),
  DISPENSING_DATE = c("DISPENSING_DATE", "DISPENSE_DATE"),
  DISPENSING_SUP = c("DISPENSING_SUP", "DAYS_SUPPLY", "SUPPLY"),
  DISPENSING_AMT = c("DISPENSING_AMT", "QUANTITY", "AMOUNT"),
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

# Active only inside etl_pcornet(mode = "append"): holds `since` and `patid_suffix`.
.append_state <- new.env(parent = emptyenv())
.SINCE_COLS <- list(
  `_temp_encounter` = "ADMIT_DATE", `_temp_diagnosis` = c("DX_DATE", "ADMIT_DATE"), `_temp_procedures` = c("PX_DATE", "ADMIT_DATE"),
  `_temp_lab_result` = "RESULT_DATE", `_temp_vital` = "MEASURE_DATE", `_temp_prescribing` = c("RX_START_DATE", "RX_ORDER_DATE"),
  `_temp_med_admin` = "MEDADMIN_START_DATE", `_temp_dispensing` = c("DISPENSE_DATE", "DISPENSING_DATE"), `_temp_death` = "DEATH_DATE")
.APPEND_ANTIJOIN <- list(
  `_temp_demographic` = c("PATID", "person", "person_source_value"),
  `_temp_provider` = c("PROVIDERID", "provider", "provider_source_value"),
  `_temp_encounter` = c("ENCOUNTERID", "visit_occurrence", "visit_source_value"))

.append_filter <- function(view_name, expected_columns) {
  conds <- character()
  if (!is.null(.append_state$since) && !is.null(.SINCE_COLS[[view_name]])) {
    cols <- intersect(.SINCE_COLS[[view_name]], expected_columns)
    if (length(cols)) conds <- c(conds, sprintf("COALESCE(%s) >= DATE '%s'", paste(sprintf("parse_omop_date(%s)", cols), collapse = ", "), .append_state$since))
  }
  aj <- .APPEND_ANTIJOIN[[view_name]]
  if (!is.null(aj) && aj[[1]] %in% expected_columns) {
    key <- aj[[1]]
    if (identical(view_name, "_temp_demographic") && !is.null(.append_state$patid_suffix)) key <- sprintf("%s || '%s'", key, .append_state$patid_suffix)
    conds <- c(conds, sprintf("NOT EXISTS (SELECT 1 FROM %s t WHERE t.%s = %s)", aj[[2]], aj[[3]], key))
  }
  if (length(conds)) paste0(" WHERE ", paste(conds, collapse = " AND ")) else ""
}

#' @keywords internal
#' @noRd
prepare_source_view <- function(con, view_name, csv_path, expected_columns, aliases = .DEFAULT_ALIASES) {
  cols_df <- DBI::dbGetQuery(con, sprintf(
    "DESCRIBE SELECT * FROM %s LIMIT 0",
    .source_relation(csv_path)
  ))
  actual_cols <- stats::setNames(cols_df$column_name, toupper(cols_df$column_name))

  exprs <- character(length(expected_columns))
  aliased <- character(0)
  padded <- character(0)
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
      if (toupper(found) != tgt_upper) aliased <- c(aliased, sprintf("%s -> %s", found, tgt))
    } else {
      exprs[[i]] <- sprintf("NULL AS %s", tgt)
      padded <- c(padded, tgt)
    }
  }

  label <- basename(csv_path)
  if (length(aliased)) cat(sprintf("  [%s] aliased columns: %s
", label, paste(aliased, collapse = ", ")))
  if (length(padded)) cat(sprintf("  [%s] null-padded missing columns: %s
", label, paste(padded, collapse = ", ")))

  sql <- sprintf(
    "CREATE OR REPLACE TEMPORARY VIEW %s AS SELECT %s FROM %s;",
    view_name, paste(exprs, collapse = ", "), .source_relation(csv_path)
  )
  if (!is.null(.append_state$active)) {
    sql <- sprintf("CREATE OR REPLACE TEMPORARY VIEW %s AS SELECT * FROM (SELECT %s FROM %s) s%s;",
                   view_name, paste(exprs, collapse = ", "), .source_relation(csv_path), .append_filter(view_name, expected_columns))
  }
  DBI::dbExecute(con, sql)
}

#' Fault-tolerant view over a source CSV
#'
#' Creates the temporary view `src_<table_name>` over a source CSV, applying case-insensitive header
#' aliases (e.g. `ssid` -> `PATID`) and padding absent expected columns with `NULL`. The aliased and
#' null-padded columns are printed. Mirror of `omop_etl.build_safe_source_view` in Python.
#'
#' @param con DuckDB DBI connection.
#' @param table_name Source table name (the view is `src_<table_name>`).
#' @param csv_path Path to the CSV.
#' @param expected_columns Columns the view must expose.
#' @param aliases Named list mapping an expected column (upper case) to accepted header names.
#' @return Invisibly, the view name.
#' @export
build_safe_source_view <- function(con, table_name, csv_path, expected_columns, aliases = .DEFAULT_ALIASES) {
  view_name <- paste0("src_", table_name)
  prepare_source_view(con, view_name, csv_path, expected_columns, aliases)
  invisible(view_name)
}
