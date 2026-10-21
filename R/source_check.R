#' Source-file contract of the PCORnet -> OMOP ETL
#'
#' Per source table: accepted file stems, required and optional columns, the OMOP table it feeds, and whether the ETL
#' cannot run without it. Files are `<table>.csv` or `<table>.parquet` (case-insensitive; CSV preferred when both
#' exist), columns are matched case-insensitively with the header aliases of the ETL (e.g. `SSID` for `PATID`), and every
#' column is read as text and cast by the ETL, so dates may be `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`. Missing optional
#' columns are null-padded. Mirror of `omop_etl.source_check.SOURCE_CONTRACT`.
#'
#' @format A named list.
#' @export
SOURCE_CONTRACT <- list(
  demographic = list(stems = "demographic", required = c("PATID", "BIRTH_DATE", "SEX"),
                     optional = c("RACE", "HISPANIC", "PROVIDERID"), feeds = "person", needed = TRUE),
  encounter = list(stems = "encounter", required = c("ENCOUNTERID", "PATID", "ENC_TYPE", "ADMIT_DATE"),
                   optional = c("DISCHARGE_DATE", "DISCHARGE_STATUS", "PROVIDERID", "FACILITYID"),
                   feeds = "visit_occurrence", needed = TRUE),
  diagnosis = list(stems = "diagnosis", required = c("PATID", "DX", "DX_TYPE"),
                   optional = c("DIAGNOSISID", "ENCOUNTERID", "ADMIT_DATE", "DX_DATE", "PDX", "PROVIDERID"),
                   feeds = "condition_occurrence", needed = FALSE),
  procedures = list(stems = "procedures", required = c("PATID", "PX", "PX_TYPE"),
                    optional = c("PROCEDURESID", "ENCOUNTERID", "PX_DATE", "PROVIDERID"),
                    feeds = "procedure_occurrence", needed = FALSE),
  lab_result_cm = list(stems = "lab_result_cm", required = c("PATID", "LAB_LOINC", "RESULT_NUM"),
                       optional = c("LAB_RESULT_CM_ID", "ENCOUNTERID", "RESULT_DATE", "RESULT_UNIT", "RAW_LAB_NAME"),
                       feeds = "measurement", needed = FALSE),
  prescribing = list(stems = "prescribing", required = "PATID",
                     optional = c("PRESCRIBINGID", "ENCOUNTERID", "RXNORM_CUI", "RX_START_DATE", "RX_END_DATE"),
                     feeds = "drug_exposure", needed = FALSE),
  death = list(stems = "death", required = "PATID", optional = "DEATH_DATE", feeds = "death", needed = FALSE),
  provider = list(stems = "provider", required = "PROVIDERID", optional = c("PROVIDER_NPI", "PROVIDER_SEX"),
                  feeds = "provider", needed = FALSE),
  vital = list(stems = "vital", required = "PATID", optional = character(), feeds = "measurement", needed = FALSE),
  med_admin = list(stems = "med_admin", required = "PATID", optional = character(), feeds = "drug_exposure", needed = FALSE),
  dispensing = list(stems = "dispensing", required = "PATID", optional = character(), feeds = "drug_exposure", needed = FALSE),
  lds_address_history = list(stems = c("lds_address_history", "address_history"), required = "PATID",
                             optional = character(), feeds = "location", needed = FALSE)
)

#' Check a source directory against the ETL file/column contract
#'
#' Reports which expected source tables and columns are present and which ETL steps will be skipped. Mirror of
#' `omop_etl.source_check.check_source` and the `omop-duckdb check-source` command.
#'
#' @param source_dir Directory of `<table>.csv` / `<table>.parquet` files.
#' @param source_format `"csv"`, `"parquet"` or `NULL` (either).
#' @param raise_on_error Stop with a `source_contract_error` naming each missing required table / column.
#' @return A list: `ok`, `tables` (per table `file`, `present`, `missing_required`, `missing_optional`, `feeds`),
#'   `skipped` (OMOP tables that will not be built) and `errors`.
#' @export
check_source <- function(source_dir, source_format = NULL, raise_on_error = FALSE) {
  fmt <- if (is.null(source_format)) NULL else tolower(source_format)
  if (!is.null(fmt) && !fmt %in% c("csv", "parquet")) stop("source_format must be 'csv', 'parquet' or NULL.", call. = FALSE)
  if (!dir.exists(source_dir)) stop("Source directory not found: ", source_dir, call. = FALSE)
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  tables <- list(); errors <- character(); skipped <- character()
  for (name in names(SOURCE_CONTRACT)) {
    spec <- SOURCE_CONTRACT[[name]]
    path <- NULL
    for (stem in spec$stems) { path <- find_source_file(source_dir, stem, fmt); if (!is.null(path)) break }
    entry <- list(file = if (is.null(path)) NULL else basename(path), present = !is.null(path), feeds = spec$feeds,
                  missing_required = character(), missing_optional = character())
    if (is.null(path)) {
      if (spec$needed) {
        errors <- c(errors, sprintf("table '%s': required file %s.%s not found in %s", name, name,
                                    if (is.null(fmt)) "csv|parquet" else fmt, source_dir))
      } else skipped <- c(skipped, spec$feeds)
    } else {
      have <- toupper(DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", .source_relation(path)))$column_name)
      present <- function(col) any(toupper(c(col, .DEFAULT_ALIASES[[toupper(col)]])) %in% have)
      entry$missing_required <- spec$required[!vapply(spec$required, present, logical(1))]
      entry$missing_optional <- spec$optional[!vapply(spec$optional, present, logical(1))]
      for (col in entry$missing_required) {
        errors <- c(errors, sprintf("table '%s' (%s): required column '%s' is missing", name, entry$file, col))
      }
    }
    tables[[name]] <- entry
  }
  fed <- unique(unlist(lapply(tables, function(e) if (e$present) e$feeds)))
  report <- list(ok = length(errors) == 0L, source_dir = source_dir, source_format = if (is.null(fmt)) "csv|parquet" else fmt,
                 tables = tables, skipped = sort(setdiff(unique(skipped), fed)), errors = errors)
  if (raise_on_error && length(errors)) {
    stop(structure(class = c("source_contract_error", "error", "condition"),
                   list(message = paste0("Source contract violations:\n  ", paste(errors, collapse = "\n  ")), call = NULL)))
  }
  report
}
