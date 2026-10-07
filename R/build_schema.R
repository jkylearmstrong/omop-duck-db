#' Build the OMOP CDM v5.4 schema into a DuckDB database file
#'
#' `inst/extdata/5.4/duckdb/*.sql` is the schema source of truth, shared with
#' the Python ETL (`python/omop_etl/build_omop_cdm.py`) -- only pass
#' `regenerate_ddl = TRUE` when bumping CDM versions during package
#' development, not on every run (and note it writes back into the package's
#' `inst/` source tree, so it only works when developing the package itself,
#' e.g. via `devtools::load_all()`, not against an installed copy).
#'
#' @param db_path Path to the DuckDB database file to create/open (used if `con` is NULL).
#' @param regenerate_ddl Regenerate the DDL via `CommonDataModel::buildRelease()`
#'   before building the schema. Requires the `CommonDataModel` package.
#' @param con Active DuckDB connection (DBI::dbConnect). If provided, schema is built on this connection.
#' @return Invisibly, `db_path` or `con`.
#' @export
build_schema <- function(db_path = "omop_cdm.duckdb", regenerate_ddl = FALSE, con = NULL) {
  extdata_dir <- system.file("extdata", package = "omopduckdb")
  ddl_path <- file.path(extdata_dir, "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql")

  if (regenerate_ddl || !file.exists(ddl_path)) {
    if (!requireNamespace("CommonDataModel", quietly = TRUE)) {
      stop("Regenerating the DDL requires the 'CommonDataModel' package (not installed).")
    }
    cat("Generating CDM v5.4 DDL via CommonDataModel::buildRelease()...\n")
    CommonDataModel::buildRelease(
      cdmVersions = "5.4",
      targetDialects = "duckdb",
      outputfolder = extdata_dir
    )
  }

  ddl_sql <- readLines(ddl_path)
  ddl_string <- paste(ddl_sql, collapse = "\n")
  ddl_string <- gsub("@cdmDatabaseSchema.", "", ddl_string, fixed = TRUE)
  ddl_string <- gsub(" NUMERIC ", " DOUBLE ", ddl_string, fixed = TRUE)

  if (!is.null(con)) {
    DBI::dbExecute(con, ddl_string)
    return(invisible(con))
  }

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, ddl_string)

  cat("Schema built at", db_path, "\n")
  invisible(db_path)
}
