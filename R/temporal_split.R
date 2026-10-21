#' Date columns that decide which side of a cutoff a CDM row falls on
#' @format A named character vector `table -> column`.
#' @export
DEFAULT_SPLIT_DATE_COLUMNS <- c(
  visit_occurrence = "visit_start_date", visit_detail = "visit_detail_start_date",
  condition_occurrence = "condition_start_date", procedure_occurrence = "procedure_date",
  drug_exposure = "drug_exposure_start_date", measurement = "measurement_date", observation = "observation_date",
  device_exposure = "device_exposure_start_date", death = "death_date", condition_era = "condition_era_start_date",
  drug_era = "drug_era_start_date", observation_period = "observation_period_start_date"
)

.split_ident <- function(x) {
  if (!grepl("^[A-Za-z0-9_]+$", x)) stop("Invalid identifier '", x, "'.", call. = FALSE)
  sprintf('"%s"', x)
}

.split_checksum <- function(con, qualified) {
  cols <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s", qualified))$column_name
  h <- paste0("hash(", paste(sprintf('"%s"', cols), collapse = ", "), ")")
  r <- DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n, CAST(COALESCE(sum(CAST(%s AS HUGEINT)), 0) AS VARCHAR) AS ck FROM %s", h, qualified))
  list(n = as.numeric(r$n), ck = r$ck)
}

# exact sum of two non-negative decimal strings (checksums exceed double precision)
.add_big <- function(a, b) {
  x <- rev(as.integer(strsplit(a, "")[[1]])); y <- rev(as.integer(strsplit(b, "")[[1]]))
  n <- max(length(x), length(y)); length(x) <- n; length(y) <- n
  x[is.na(x)] <- 0L; y[is.na(y)] <- 0L
  carry <- 0L; out <- integer(n)
  for (i in seq_len(n)) { s <- x[i] + y[i] + carry; out[i] <- s %% 10L; carry <- s %/% 10L }
  if (carry > 0L) out <- c(out, carry)
  paste(rev(out), collapse = "")
}

#' Held-out temporal split
#'
#' Writes every row dated on/after `cutoff` to a new DuckDB file (same CDM tables), optionally deleting them from the
#' source, and verifies that kept + moved rows and checksums reconstruct the source. Mirror of
#' `omop_etl.temporal_split.split_by_date`.
#'
#' @param con Writable DuckDB DBI connection to the CDM (read-only is fine unless `remove_from_source`).
#' @param cutoff Split date (inclusive start of the held-out period); a `Date` or `"YYYY-MM-DD"`.
#' @param out_db DuckDB file to create; refuses to overwrite. Also receives full copies of the undated tables `person`,
#'   `provider`, `care_site`, `location`, `cdm_source` so it is self-contained.
#' @param tables Dated tables to split (character vector, or a named character vector `table -> date column`).
#'   Default: the CDM clinical tables present, from [DEFAULT_SPLIT_DATE_COLUMNS].
#' @param remove_from_source Delete the moved rows from the source afterwards.
#' @param manifest Optional [run_manifest()]; per-table counts are added as steps.
#' @return A list with `cutoff` and `tables` (per table `source_before`, `kept`, `moved`, checksums). A split that does
#'   not reconstruct the source raises an error.
#' @export
split_by_date <- function(con, cutoff, out_db, tables = NULL, remove_from_source = FALSE, manifest = NULL) {
  cut <- format(as.Date(cutoff))
  if (is.na(cut)) stop("cutoff must be a date.", call. = FALSE)
  have <- tolower(DBI::dbGetQuery(con, "SELECT table_name FROM information_schema.tables")$table_name)
  spec <- if (is.null(tables)) DEFAULT_SPLIT_DATE_COLUMNS[names(DEFAULT_SPLIT_DATE_COLUMNS) %in% have] else if (!is.null(names(tables))) tables else
    DEFAULT_SPLIT_DATE_COLUMNS[tables]
  missing <- setdiff(names(spec), have)
  if (length(missing)) stop("Tables not found: ", paste(missing, collapse = ", "), call. = FALSE)
  if (file.exists(out_db)) stop(out_db, " already exists; refusing to overwrite a held-out database.", call. = FALSE)
  dir.create(dirname(out_db), showWarnings = FALSE, recursive = TRUE)
  alias <- "_heldout_split"
  DBI::dbExecute(con, sprintf("ATTACH %s AS %s", sql_path_literal(out_db), alias))
  on.exit(try(DBI::dbExecute(con, sprintf("DETACH %s", alias)), silent = TRUE), add = TRUE)
  report <- list(cutoff = cut, tables = list())
  for (t in intersect(c("person", "provider", "care_site", "location", "cdm_source"), setdiff(have, names(spec)))) {
    DBI::dbExecute(con, sprintf("CREATE TABLE %s.main.%s AS SELECT * FROM %s", alias, .split_ident(t), .split_ident(t)))
  }
  for (t in names(spec)) {
    q <- .split_ident(t); c_ <- .split_ident(spec[[t]])
    before <- .split_checksum(con, q)
    DBI::dbExecute(con, sprintf("CREATE TABLE %s.main.%s AS SELECT * FROM %s WHERE %s >= DATE '%s'", alias, .split_ident(t), q, c_, cut))
    moved <- .split_checksum(con, sprintf("%s.main.%s", alias, .split_ident(t)))
    kept <- .split_checksum(con, sprintf("(SELECT * FROM %s WHERE %s < DATE '%s' OR %s IS NULL)", q, c_, cut, c_))
    if (kept$n + moved$n != before$n || !identical(.add_big(kept$ck, moved$ck), before$ck)) {
      stop("Split of ", t, " does not reconstruct the source (counts/checksums differ).", call. = FALSE)
    }
    report$tables[[t]] <- list(source_before = before$n, kept = kept$n, moved = moved$n, source_checksum = before$ck,
                               kept_checksum = kept$ck, moved_checksum = moved$ck)
    if (!is.null(manifest)) run_manifest_add_step(manifest, paste0("split:", t), moved$n, kept = kept$n, source = before$n)
  }
  if (isTRUE(remove_from_source)) {
    for (t in names(spec)) DBI::dbExecute(con, sprintf("DELETE FROM %s WHERE %s >= DATE '%s'", .split_ident(t), .split_ident(spec[[t]]), cut))
  }
  report
}
