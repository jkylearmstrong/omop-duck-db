#' Standard CDM tables copied by [import_omop()]
#' @format A character vector.
#' @export
CDM_STUDY_TABLES <- c("person", "observation_period", "visit_occurrence", "visit_detail", "condition_occurrence",
                      "procedure_occurrence", "drug_exposure", "measurement", "observation", "device_exposure",
                      "death", "condition_era", "drug_era", "provider", "care_site", "location", "cdm_source")

.read_table_map <- function(table_map) {
  if (is.null(table_map)) return(list())
  if (is.character(table_map) && length(table_map) == 1L && is.null(names(table_map))) {
    if (grepl("\\.json$", table_map, ignore.case = TRUE)) {
      table_map <- jsonlite::fromJSON(table_map, simplifyVector = FALSE)
    } else {
      lines <- trimws(readLines(table_map, warn = FALSE))
      lines <- lines[nzchar(lines) & !startsWith(lines, "#") & !grepl("^expected", lines, ignore.case = TRUE)]
      parts <- strsplit(lines, ",", fixed = TRUE)
      table_map <- stats::setNames(lapply(parts, function(p) trimws(p[2])), vapply(parts, function(p) trimws(p[1]), character(1)))
    }
  }
  stats::setNames(as.list(table_map), tolower(names(table_map)))
}

.resolve_table <- function(con, requested, schema) {
  names_ <- if (is.null(schema)) DBI::dbListTables(con) else DBI::dbGetQuery(
    con, "SELECT table_name FROM information_schema.tables WHERE table_schema = ?", params = list(schema))[[1]]
  if (requested %in% names_) return(requested)
  m <- names_[tolower(names_) == tolower(requested)]
  if (length(m) == 1L) return(m)
  if (!length(m)) stop("Table '", requested, "' not found", if (!is.null(schema)) paste0(" in schema '", schema, "'"), ".", call. = FALSE)
  stop("Table name '", requested, "' is ambiguous (", paste(m, collapse = ", "), "); use the exact name via a mapping.", call. = FALSE)
}

#' Stream source tables from any DBI database to Parquet
#'
#' R counterpart of `omop_etl.extract.from_sql`. Instead of a SQLAlchemy URL it takes an open DBI connection (use the
#' site's own driver: odbc, RPostgres, ROracle, RSQLite, ...; keep credentials in environment variables, never in
#' code). Rows are fetched in chunks, columns are lower-cased (Oracle / Snowflake upper-case identifiers), table names
#' are matched case-insensitively and the output is one `<table>.parquet` per table, written through DuckDB so no
#' `arrow` installation is needed.
#'
#' @param src An open DBI connection to the source database.
#' @param tables Expected table names; an entry `"name=SOURCE_TABLE"` maps explicitly.
#' @param out_dir Output directory (created).
#' @param chunksize Rows per fetch.
#' @param schema Source schema.
#' @param table_map Named list / character vector `expected -> actual`, or the path to a JSON object or
#'   `expected,source` CSV, for sites whose tables are not named like PCORnet.
#' @return A data.frame with one row per table: `table`, `source_table`, `rows`, `columns`, `file`.
#' @export
from_sql <- function(src, tables, out_dir, chunksize = 500000L, schema = NULL, table_map = NULL) {
  chunksize <- as.integer(chunksize)
  if (chunksize < 1L) stop("chunksize must be >= 1.", call. = FALSE)
  map <- .read_table_map(table_map)
  if (!length(tables)) stop("No tables requested.", call. = FALSE)
  dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)
  ddb <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(ddb, shutdown = TRUE), add = TRUE)
  rows <- list()
  for (req in tables) {
    parts <- strsplit(req, "=", fixed = TRUE)[[1]]
    expected <- trimws(parts[1])
    wanted <- if (length(parts) > 1L) trimws(parts[2]) else if (!is.null(map[[tolower(expected)]])) map[[tolower(expected)]] else expected
    actual <- .resolve_table(src, wanted, schema)
    id <- if (is.null(schema)) DBI::dbQuoteIdentifier(src, actual) else DBI::dbQuoteIdentifier(src, DBI::Id(schema = schema, table = actual))
    res <- DBI::dbSendQuery(src, paste("SELECT * FROM", DBI::dbQuoteIdentifier(src, id)))
    chunk_dir <- tempfile("chunks_"); dir.create(chunk_dir)
    n <- 0L; i <- 0L; cols <- NULL
    repeat {
      df <- DBI::dbFetch(res, n = chunksize)
      if (is.null(cols)) cols <- tolower(names(df))
      if (nrow(df) == 0L && i > 0L) break
      names(df) <- tolower(names(df))
      i <- i + 1L
      DBI::dbWriteTable(ddb, "chunk", df, overwrite = TRUE)
      DBI::dbExecute(ddb, sprintf("COPY chunk TO '%s' (FORMAT PARQUET)", file.path(chunk_dir, sprintf("c%06d.parquet", i))))
      n <- n + nrow(df)
      if (DBI::dbHasCompleted(res)) break
    }
    DBI::dbClearResult(res)
    target <- file.path(out_dir, paste0(tolower(expected), ".parquet"))
    DBI::dbExecute(ddb, sprintf("COPY (SELECT * FROM read_parquet('%s/*.parquet')) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)",
                                sub("\\\\", "/", chunk_dir), sub("\\\\", "/", target)))
    unlink(chunk_dir, recursive = TRUE)
    rows[[length(rows) + 1L]] <- data.frame(table = tolower(expected), source_table = actual, rows = n,
                                            columns = paste(cols, collapse = ","), file = target, stringsAsFactors = FALSE)
  }
  do.call(rbind, rows)
}

#' Copy an OMOP CDM from a server database into a local DuckDB file
#'
#' R counterpart of `omop_etl.extract.import_omop`: builds the CDM v5.4 schema in `out` and copies the requested tables
#' from the source CDM (matching columns by name, case-insensitively; unknown server columns are dropped). Tables
#' missing on the server are skipped; `person` is required.
#'
#' @param src An open DBI connection to the server holding the CDM.
#' @param out DuckDB file to create or extend.
#' @param schema Server schema with the CDM tables.
#' @param tables Subset of CDM tables (default [CDM_STUDY_TABLES]).
#' @param chunksize Rows per fetch.
#' @param table_map Overrides for non-standard server table names (see [from_sql()]).
#' @return A data.frame: `table`, `rows` (`NA` when skipped), `note`.
#' @export
import_omop <- function(src, out, schema = NULL, tables = NULL, chunksize = 500000L, table_map = NULL) {
  wanted <- tolower(if (is.null(tables)) CDM_STUDY_TABLES else tables)
  if (!"person" %in% wanted) stop("'person' must be among the imported tables.", call. = FALSE)
  map <- .read_table_map(table_map)
  have <- tolower(if (is.null(schema)) DBI::dbListTables(src) else DBI::dbGetQuery(
    src, "SELECT table_name FROM information_schema.tables WHERE table_schema = ?", params = list(schema))[[1]])
  present <- Filter(function(t) tolower(if (is.null(map[[t]])) t else map[[t]]) %in% have, wanted)
  if (!"person" %in% present) stop("No 'person' table found", if (!is.null(schema)) paste0(" in schema '", schema, "'"), "; is this an OMOP CDM?", call. = FALSE)
  tmp <- tempfile("omop_import_"); on.exit(unlink(tmp, recursive = TRUE), add = TRUE)
  ext <- from_sql(src, present, tmp, chunksize = chunksize, schema = schema, table_map = table_map)
  build_schema(out)
  con <- DBI::dbConnect(duckdb::duckdb(), out)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  report <- data.frame(table = setdiff(wanted, present), rows = NA_integer_, note = "not found on server", stringsAsFactors = FALSE)
  for (i in seq_len(nrow(ext))) {
    t <- ext$table[i]
    target_cols <- DBI::dbGetQuery(con, sprintf("SELECT column_name FROM information_schema.columns WHERE lower(table_name) = '%s'", t))[[1]]
    if (!length(target_cols)) { report <- rbind(report, data.frame(table = t, rows = NA_integer_, note = "no such table in CDM v5.4 DDL")); next }
    src_cols <- strsplit(ext$columns[i], ",", fixed = TRUE)[[1]]
    use <- target_cols[tolower(target_cols) %in% src_cols]
    q <- paste(sprintf('"%s"', tolower(use)), collapse = ", ")
    DBI::dbExecute(con, paste0("DELETE FROM ", t))
    DBI::dbExecute(con, sprintf("INSERT INTO %s (%s) SELECT %s FROM read_parquet('%s')", t,
                                paste(sprintf('"%s"', use), collapse = ", "), q, sub("\\\\", "/", ext$file[i])))
    report <- rbind(report, data.frame(table = t, rows = DBI::dbGetQuery(con, paste0("SELECT count(*) FROM ", t))[[1]],
                                       note = "", stringsAsFactors = FALSE))
  }
  rownames(report) <- NULL
  report
}
