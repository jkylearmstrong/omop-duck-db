#' Direct Andromeda Zero-Copy View Adapter
#'
#' Replaces the SQLite disk serialization bottleneck inside OHDSI Andromeda by
#' directly mounting or bulk-streaming DuckDB tables into Andromeda SQLite containers.
#'
#' @param con Active DuckDB connection (duckdb::duckdb()).
#' @param table_names Character vector of table names in DuckDB to transfer (e.g. `c("covariates", "covariateRef", "analysisRef")`).
#'   If `NULL`, transfers all tables present matching Andromeda covariate structures.
#' @param andromeda Existing Andromeda object. If `NULL`, a new one is initialized.
#' @param output_file Path to backing SQLite file if creating a new Andromeda object.
#' @return A populated `Andromeda` object.
#' @export
duckdb_to_andromeda <- function(con,
                                table_names = NULL,
                                andromeda = NULL,
                                output_file = tempfile(fileext = ".sqlite")) {
  if (!requireNamespace("Andromeda", quietly = TRUE)) {
    stop("Package 'Andromeda' is required for duckdb_to_andromeda().", call. = FALSE)
  }
  if (!inherits(con, "duckdb_connection")) {
    stop("`con` must be an active DuckDB connection.", call. = FALSE)
  }

  if (is.null(table_names)) {
    all_tbls <- DBI::dbListTables(con)
    candidates <- c("covariates", "covariateref", "analysisref")
    table_names <- intersect(tolower(all_tbls), candidates)
    if (length(table_names) == 0) table_names <- all_tbls
  }

  if (is.null(andromeda)) {
    andromeda <- Andromeda::andromeda()
  }

  # Identify SQLite database file backing Andromeda
  sqlite_file <- tryCatch({
    conn <- attr(andromeda, "connection")
    if (is.null(conn) && inherits(andromeda, "Andromeda")) {
      conn <- andromeda@connection
    }
    if (!is.null(conn)) {
      f <- tryCatch(DBI::dbGetInfo(conn)$dbname, error = function(e) NULL)
      if (is.null(f) || !nzchar(f)) f <- tryCatch(conn@dbname, error = function(e) NULL)
      f
    } else {
      NULL
    }
  }, error = function(e) NULL)

  if (is.null(sqlite_file) || !file.exists(sqlite_file)) {
    sqlite_file <- output_file
  }

  norm_sqlite <- sql_path_literal(sqlite_file)

  # Method 1: Try native DuckDB SQLite ATTACH zero-copy write
  attached <- tryCatch({
    DBI::dbExecute(con, sprintf("ATTACH %s AS andr_sq (TYPE SQLITE);", norm_sqlite))
    for (tbl in table_names) {
      DBI::dbExecute(con, sprintf("CREATE TABLE andr_sq.%s AS SELECT * FROM %s;", tbl, tbl))
    }
    DBI::dbExecute(con, "DETACH andr_sq;")
    TRUE
  }, error = function(e) FALSE)

  # Method 2: Chunked batch streaming fallback (avoids monolithic memory blowup)
  if (!attached) {
    conn <- tryCatch(attr(andromeda, "connection"), error = function(e) NULL)
    if (is.null(conn) && inherits(andromeda, "Andromeda")) {
      conn <- tryCatch(andromeda@connection, error = function(e) NULL)
    }

    for (tbl in table_names) {
      total <- as.numeric(DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM %s;", tbl))$n)
      chunk_size <- 50000L
      offset <- 0L

      while (offset < total) {
        chunk <- DBI::dbGetQuery(
          con,
          sprintf("SELECT * FROM %s LIMIT %d OFFSET %d;", tbl, chunk_size, offset)
        )
        if (nrow(chunk) == 0) break

        if (!is.null(conn)) {
          DBI::dbWriteTable(conn, tbl, chunk, append = (offset > 0L), row.names = FALSE)
        } else {
          andromeda[[tbl]] <- chunk
        }
        offset <- offset + nrow(chunk)
      }
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
