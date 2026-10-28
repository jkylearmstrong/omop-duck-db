#' Stream DuckDB Tables into an Andromeda Object
#'
#' Copies DuckDB tables into an OHDSI Andromeda object in chunks, so large covariate tables never have to fit in
#' memory twice. Tables are always written through Andromeda's own interface (`andromeda[[name]] <- data` and
#' `Andromeda::appendToTable()`), so they are visible in `names(andromeda)` whichever storage engine the installed
#' Andromeda version uses (SQLite in 0.x, DuckDB in 1.x). Earlier versions attached the Andromeda file to DuckDB as
#' SQLite; that wrote into a different file than the one Andromeda reads whenever the SQLite extension was
#' available, and returned an empty Andromeda.
#'
#' @param con Active DuckDB connection (duckdb::duckdb()).
#' @param table_names Character vector of table names in DuckDB to transfer (e.g. `c("covariates", "covariateRef", "analysisRef")`).
#'   If `NULL`, transfers the covariate tables present, or every table when none match.
#' @param andromeda Existing Andromeda object. If `NULL`, a new one is initialized.
#' @param output_file Deprecated and ignored (kept so existing calls still work).
#' @param chunk_size Rows copied per batch (default 50000).
#' @return A populated `Andromeda` object.
#' @export
duckdb_to_andromeda <- function(con,
                                table_names = NULL,
                                andromeda = NULL,
                                output_file = NULL,
                                chunk_size = 50000L) {
  if (!requireNamespace("Andromeda", quietly = TRUE)) {
    stop("Package 'Andromeda' is required for duckdb_to_andromeda().", call. = FALSE)
  }
  if (!inherits(con, "duckdb_connection")) {
    stop("`con` must be an active DuckDB connection.", call. = FALSE)
  }
  chunk_size <- as.integer(chunk_size)
  if (length(chunk_size) != 1L || is.na(chunk_size) || chunk_size < 1L) {
    stop("`chunk_size` must be a positive whole number.", call. = FALSE)
  }

  if (is.null(table_names)) {
    all_tbls <- DBI::dbListTables(con)
    candidates <- c("covariates", "covariateref", "analysisref")
    table_names <- all_tbls[tolower(all_tbls) %in% candidates]
    if (length(table_names) == 0) table_names <- all_tbls
  }

  if (is.null(andromeda)) {
    andromeda <- Andromeda::andromeda()
  }

  for (tbl in table_names) {
    qtbl <- DBI::dbQuoteIdentifier(con, tbl)
    total <- as.numeric(DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM %s;", qtbl))$n)
    if (total == 0) {
      # Keep the table's columns even when it is empty.
      andromeda[[tbl]] <- DBI::dbGetQuery(con, sprintf("SELECT * FROM %s LIMIT 0;", qtbl))
      next
    }
    offset <- 0
    while (offset < total) {
      chunk <- DBI::dbGetQuery(con, sprintf("SELECT * FROM %s LIMIT %d OFFSET %.0f;", qtbl, chunk_size, offset))
      if (nrow(chunk) == 0) break
      if (offset == 0) {
        andromeda[[tbl]] <- chunk
      } else {
        Andromeda::appendToTable(andromeda[[tbl]], chunk)
      }
      offset <- offset + nrow(chunk)
    }
    # Flush chunked appends so DuckDB is quiescent before GC finalisers run (Windows CI crash, 0.8.5 audit).
    if (requireNamespace("Andromeda", quietly = TRUE) &&
        exists("flushAndromeda", envir = asNamespace("Andromeda"), inherits = FALSE)) {
      try(Andromeda::flushAndromeda(andromeda), silent = TRUE)
    }
  }

  andromeda
}

#' Convert DuckDB Tables to an Andromeda Object
#'
#' Shorthand for [duckdb_to_andromeda()].
#'
#' @param con Active DuckDB connection.
#' @param table_names Names of tables to transfer.
#' @return An `Andromeda` object.
#' @export
as_andromeda <- function(con, table_names = NULL) {
  duckdb_to_andromeda(con = con, table_names = table_names)
}
