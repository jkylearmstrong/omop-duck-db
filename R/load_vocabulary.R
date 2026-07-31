#' Load or refresh the OHDSI Athena vocabulary into the OMOP CDM database
#'
#' Each table is truncated and reloaded inside its own transaction, with a
#' row-count sanity check before commit -- so a bad/partial download can only
#' fail that one table (rolled back, left untouched), not half-clobber the
#' rest of the vocabulary.
#'
#' @param vocab_dir Path to an extracted Athena vocabulary download (a
#'   directory of tab-delimited `*.csv` files).
#' @param db_path Path to the DuckDB database file (schema must already exist,
#'   see [build_schema()]).
#' @return `TRUE` if every matched table loaded successfully, `FALSE` if any
#'   table failed and was rolled back.
#' @export
load_vocabulary <- function(vocab_dir, db_path = "omop_cdm.duckdb") {
  if (!dir.exists(vocab_dir)) {
    stop("Vocabulary directory not found: ", vocab_dir)
  }

  vocab_files <- list.files(path = vocab_dir, full.names = TRUE, recursive = TRUE, pattern = "\\.csv$")
  if (length(vocab_files) == 0) {
    stop("No CSV files found under: ", vocab_dir)
  }
  names(vocab_files) <- stringr::str_remove_all(basename(vocab_files), "(?i)\\.csv$")

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  existing_tables <- DBI::dbListTables(con)

  all_ok <- TRUE
  for (table_name in names(vocab_files)) {
    if (!(tolower(table_name) %in% tolower(existing_tables))) {
      cat("Skipping", table_name, "- no matching table in schema (run build_schema() first)\n")
      next
    }
    file_path <- vocab_files[[table_name]]
    cat("Reloading", table_name, "from", basename(file_path), "...\n")

    DBI::dbExecute(con, "BEGIN TRANSACTION")
    ok <- tryCatch(
      {
        DBI::dbExecute(con, paste0("DELETE FROM ", table_name))
        DBI::dbExecute(con, paste0(
          "COPY ", table_name, " FROM '", file_path, "' (DELIMITER '\t', HEADER, DATEFORMAT '%Y%m%d');"
        ))
        row_count <- DBI::dbGetQuery(con, paste0("SELECT COUNT(*) AS n FROM ", table_name))$n
        if (row_count == 0) stop("table loaded with zero rows")
        DBI::dbExecute(con, "COMMIT")
        cat("  ->", row_count, "rows (committed)\n")
        TRUE
      },
      error = function(e) {
        DBI::dbExecute(con, "ROLLBACK")
        cat("  Error loading", table_name, ":", conditionMessage(e), "- rolled back, table unchanged\n")
        FALSE
      }
    )
    all_ok <- all_ok && ok
  }

  invisible(all_ok)
}
