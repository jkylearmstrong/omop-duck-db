#' Build the OMOP CDM v5.4 schema into a DuckDB database file
#'
#' `inst/extdata/5.4/duckdb/*.sql` is the schema source of truth, shared with
#' the Python ETL (`python/omop_etl/build_omop_cdm.py`) -- only pass
#' `regenerate_ddl = TRUE` when bumping CDM versions during package
#' development, not on every run (and note it writes back into the package's
#' `inst/` source tree, so it only works when developing the package itself,
#' e.g. via `devtools::load_all()`, not against an installed copy).
#'
#' @param db_path Path to the DuckDB database file to create/open.
#' @param regenerate_ddl Regenerate the DDL via `CommonDataModel::buildRelease()`
#'   before building the schema. Requires the `CommonDataModel` package.
#' @param keys Also apply the primary keys and indices (default `FALSE`: a primary key makes the bulk loads reject
#'   duplicate ids, and DuckDB cannot apply the CDM foreign-key constraints). Re-running on an existing schema is a no-op.
#' @return Invisibly, `db_path`.
#' @export
build_schema <- function(db_path = "omop_cdm.duckdb", regenerate_ddl = FALSE, keys = FALSE) {
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

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  wanted <- regmatches(ddl_string, gregexpr("(?i)CREATE\\s+TABLE\\s+(?:IF\\s+NOT\\s+EXISTS\\s+)?\\w+", ddl_string, perl = TRUE))[[1]]
  wanted <- unique(tolower(sub("^.*\\s", "", wanted)))
  have <- tolower(DBI::dbListTables(con))
  present <- intersect(wanted, have)
  if (length(present) && length(present) == length(wanted)) {
    cat("Schema already present at", db_path, "- nothing to build\n")
  } else if (length(present)) {
    stop("Database already has part of the CDM schema (", paste(present, collapse = ", "), "); refusing to build over it.", call. = FALSE)
  } else {
    DBI::dbWithTransaction(con, DBI::dbExecute(con, ddl_string))
    cat("Schema built at", db_path, "\n")
  }
  if (isTRUE(keys)) {
    read_sql <- function(name) {
      p <- file.path(extdata_dir, "5.4", "duckdb", paste0("OMOPCDM_duckdb_5.4_", name, ".sql"))
      gsub(" NUMERIC ", " DOUBLE ", gsub("@cdmDatabaseSchema.", "", paste(readLines(p), collapse = "\n"), fixed = TRUE), fixed = TRUE)
    }
    if (nrow(DBI::dbGetQuery(con, "SELECT 1 FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY' LIMIT 1")) == 0) {
      DBI::dbWithTransaction(con, DBI::dbExecute(con, read_sql("primary_keys")))
    }
    if (nrow(DBI::dbGetQuery(con, "SELECT 1 FROM duckdb_indexes() LIMIT 1")) == 0) {
      DBI::dbWithTransaction(con, DBI::dbExecute(con, read_sql("indices")))
    }
  }
  invisible(db_path)
}
