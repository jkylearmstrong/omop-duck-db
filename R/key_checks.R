#' Build-time surrogate-key collision check
#'
#' `pcornet_id()` maps a source id to a 63-bit key. A collision silently merges two source rows, so the number of
#' distinct source ids must equal the number of distinct keys. `check_key_collisions()` verifies that for one source
#' column; `check_etl_keys()` runs it on the ids the PCORnet ETL turns into surrogate keys. R port of
#' `omop_etl.key_checks`.
#'
#' @param con A DuckDB DBI connection with the mapping macros loaded (see `load_mapping_macros()`).
#' @param source A table, view or `read_csv_auto(...)`/`read_parquet(...)` relation expression.
#' @param id_column Column holding the source id.
#' @param key_expr Key expression with a `{col}` placeholder (default `"pcornet_id({col})"`).
#' @param on_collision `"warn"` (default), `"raise"` or `"ignore"`.
#' @return `check_key_collisions()`: a list with `source`, `id_column`, `distinct_ids`, `distinct_keys`, `lost`, `ok`.
#'   `check_etl_keys()`: a list of such lists, each also holding `table`, `cdm_table` and `cdm_rows`.
#' @export
check_key_collisions <- function(con, source, id_column, key_expr = "pcornet_id({col})",
                                 on_collision = c("warn", "raise", "ignore")) {
  on_collision <- match.arg(on_collision)
  if (!grepl('^([A-Za-z_][A-Za-z0-9_]*|"[^"]+")$', id_column)) stop("Invalid id_column: ", id_column)
  if (!(grepl("^[A-Za-z_][A-Za-z0-9_]*([.][A-Za-z_][A-Za-z0-9_]*)?$", source) || grepl("(", source, fixed = TRUE))) {
    stop("Invalid source: ", source)
  }
  col <- if (grepl("{col}", key_expr, fixed = TRUE)) sprintf("CAST(%s AS VARCHAR)", id_column) else id_column
  key <- gsub("{col}", col, key_expr, fixed = TRUE)
  r <- DBI::dbGetQuery(con, sprintf(
    "SELECT COUNT(DISTINCT %s) AS ids, COUNT(DISTINCT %s) AS keys FROM %s WHERE %s IS NOT NULL",
    id_column, key, source, id_column))
  ids <- as.numeric(r$ids)
  keys <- as.numeric(r$keys)
  res <- list(source = source, id_column = id_column, distinct_ids = ids, distinct_keys = keys,
              lost = ids - keys, ok = ids == keys)
  if (!res$ok) {
    msg <- sprintf(paste0("Surrogate-key collision: %.0f distinct %s.%s values map to only %.0f keys ",
                          "(%.0f would be merged into other rows)."), ids, source, id_column, keys, ids - keys)
    if (on_collision == "raise") stop(structure(class = c("key_collision_error", "error", "condition"),
                                                list(message = msg, call = sys.call(-1))))
    if (on_collision == "warn") warning(msg, call. = FALSE)
  }
  res
}

#' @rdname check_key_collisions
#' @param source_dir Directory of PCORnet source files.
#' @param site_id,disambiguate_patids As in [etl_pcornet()]; `PATID` is checked only when both are set.
#' @export
check_etl_keys <- function(con, source_dir, site_id = NULL, disambiguate_patids = FALSE,
                           on_collision = c("warn", "raise", "ignore")) {
  on_collision <- match.arg(on_collision)
  jobs <- list(list("encounter", "ENCOUNTERID", "pcornet_id({col})", "visit_occurrence"))
  if (isTRUE(disambiguate_patids) && !is.null(site_id)) {
    jobs[[2]] <- list("demographic", "PATID", sprintf("pcornet_id({col} || '-%d')", as.integer(site_id)), "person")
  }
  out <- list()
  for (j in jobs) {
    path <- find_source_file(source_dir, j[[1]])
    if (is.null(path)) next
    relation <- .source_relation(path)
    have <- DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s LIMIT 0", relation))$column_name
    candidates <- toupper(c(j[[2]], .DEFAULT_ALIASES[[j[[2]]]]))
    found <- have[match(TRUE, toupper(have) %in% candidates)]
    if (is.na(found)) next
    res <- check_key_collisions(con, relation, if (grepl("^[A-Za-z_][A-Za-z0-9_]*$", found)) found else sprintf('"%s"', found),
                                key_expr = j[[3]], on_collision = on_collision)
    res$table <- j[[1]]
    res$cdm_table <- j[[4]]
    res$cdm_rows <- as.numeric(DBI::dbGetQuery(con, sprintf("SELECT count(*) AS n FROM %s", j[[4]]))$n)
    out[[length(out) + 1L]] <- res
  }
  out
}
