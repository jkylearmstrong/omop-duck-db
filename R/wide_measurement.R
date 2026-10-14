#' Default PCORnet VITAL concept mapping for `load_wide_measurement()`
#'
#' A named list: PCORnet VITAL column -> `list(concept_id, loinc, unit_concept_id, unit)`, using the same
#' LOINC / unit choices as the `vital.csv` ETL.
#'
#' @format A named list.
#' @export
DEFAULT_VITAL_CONCEPT_MAPPING <- list(
  HT = list(concept_id = 3036277L, loinc = "8302-2", unit_concept_id = 9326L, unit = "[in_us]"),
  WT = list(concept_id = 3025315L, loinc = "29463-7", unit_concept_id = 8739L, unit = "[lb_av]"),
  BMI = list(concept_id = 3038553L, loinc = "39156-5", unit_concept_id = 9531L, unit = "kg/m2"),
  ORIGINAL_BMI = list(concept_id = 3038553L, loinc = "39156-5", unit_concept_id = 9531L, unit = "kg/m2"),
  SYSTOLIC = list(concept_id = 3004249L, loinc = "8480-6", unit_concept_id = 8876L, unit = "mm[Hg]"),
  DIASTOLIC = list(concept_id = 3012888L, loinc = "8462-4", unit_concept_id = 8876L, unit = "mm[Hg]")
)

#' Default clinical plausibility bounds for PCORnet vitals
#'
#' A named list: column -> `c(min, max)` (inclusive). SBP 30-300, DBP 10-200, BMI 5-120.
#'
#' @format A named list.
#' @export
DEFAULT_VITAL_PLAUSIBILITY_FILTERS <- list(
  SYSTOLIC = c(30, 300),
  DIASTOLIC = c(10, 200),
  BMI = c(5, 120),
  ORIGINAL_BMI = c(5, 120)
)

#' @keywords internal
#' @noRd
.wm_table_ident <- function(name) {
  if (!all(grepl("^[A-Za-z_][A-Za-z0-9_]*$", strsplit(name, ".", fixed = TRUE)[[1]]))) {
    stop("Invalid table name '", name, "'.")
  }
  name
}

#' @keywords internal
#' @noRd
.wm_ident <- function(name) paste0('"', gsub('"', '""', name, fixed = TRUE), '"')

#' @keywords internal
#' @noRd
.wm_source_relation <- function(source) {
  s <- as.character(source)
  if (startsWith(trimws(s), "(")) return(s)
  if (file.exists(s) || grepl("\\.(csv|tsv|txt|parquet)(\\.gz)?$", s, ignore.case = TRUE)) {
    if (grepl("\\.parquet$", s, ignore.case = TRUE)) {
      return(paste0("read_parquet(", sql_path_literal(s), ")"))
    }
    return(paste0("read_csv_auto(", sql_path_literal(s), ", all_varchar = true, union_by_name = true)"))
  }
  .wm_table_ident(s)
}

#' @keywords internal
#' @noRd
.wm_resolve_col <- function(columns, wanted, role) {
  hit <- columns[[tolower(wanted)]]
  if (is.null(hit)) {
    stop(sprintf("%s column '%s' not found in source (columns: %s).", role, wanted, paste(columns, collapse = ", ")))
  }
  hit
}

#' @keywords internal
#' @noRd
.wm_normalize_mapping <- function(con, concept_mapping) {
  out <- list()
  for (col in names(concept_mapping)) {
    spec <- concept_mapping[[col]]
    entry <- if (is.numeric(spec) && length(spec) == 1) list(concept_id = spec) else as.list(spec)
    if (is.null(entry$concept_id)) {
      found <- NA
      if (!is.null(entry$loinc)) {
        found <- tryCatch(
          DBI::dbGetQuery(con, paste0(
            "SELECT COALESCE(MIN(cr.concept_id_2), MIN(CASE WHEN c.standard_concept = 'S' THEN c.concept_id END)) AS id ",
            "FROM concept c LEFT JOIN concept_relationship cr ",
            "ON cr.concept_id_1 = c.concept_id AND cr.relationship_id = 'Maps to' ",
            "WHERE c.vocabulary_id = 'LOINC' AND c.concept_code = ", DBI::dbQuoteString(con, entry$loinc)
          ))$id[[1]],
          error = function(e) NA
        )
      }
      if (is.na(found)) {
        stop(sprintf("concept_mapping[['%s']] has no concept_id and the LOINC code '%s' could not be resolved through the concept table.",
                     col, if (is.null(entry$loinc)) "" else entry$loinc))
      }
      entry$concept_id <- found
    }
    entry$concept_id <- as.integer(entry$concept_id)
    out[[col]] <- entry
  }
  out
}

#' @keywords internal
#' @noRd
.wm_resolve_coalesce_groups <- function(mapping, coalesce_columns) {
  if (is.null(coalesce_columns)) return(list())
  if (is.character(coalesce_columns) && length(coalesce_columns) == 1 && identical(coalesce_columns, "auto")) {
    ids <- vapply(mapping, function(e) e$concept_id, integer(1))
    groups <- lapply(unique(ids), function(i) names(mapping)[ids == i])
    return(Filter(function(g) length(g) > 1, groups))
  }
  if (is.character(coalesce_columns) && !is.list(coalesce_columns)) {
    if (length(coalesce_columns) == 1) {
      stop("coalesce_columns must be \"auto\", NULL, or a list of column-name groups.")
    }
    coalesce_columns <- list(coalesce_columns)
  }
  groups <- list()
  seen <- character()
  for (g in coalesce_columns) {
    g <- as.character(g)
    if (length(g) < 2) stop("coalesce group (", paste(g, collapse = ", "), ") needs at least two columns.")
    for (col in g) {
      if (is.null(mapping[[col]])) stop("coalesce column '", col, "' is not in concept_mapping.")
      if (col %in% seen) stop("coalesce column '", col, "' appears in more than one group.")
      seen <- c(seen, col)
    }
    if (length(unique(vapply(g, function(col) mapping[[col]]$concept_id, integer(1)))) != 1) {
      stop("coalesce group (", paste(g, collapse = ", "), ") maps to more than one concept_id.")
    }
    groups[[length(groups) + 1L]] <- g
  }
  groups
}

#' Staged, sequential ingestion of wide measurement tables
#'
#' Unpivoting a wide table (PCORnet `vital`, flowsheets, device streams) with one giant uncommitted
#' `UNION ALL` materializes the whole result in DuckDB's append buffer and can exhaust RAM. This function
#' (a) stages the source once into a keyed intermediate table (integer primary key, values already cast to
#' `DOUBLE`, dates already normalized with [vectorized_strptime()]) and (b) inserts into `measurement` one
#' concept at a time, each pass in its own committed transaction, applying per-measurement plausibility
#' bounds. Peak memory is one column's worth of rows and a failed pass loses only that pass.
#' Mirror of `omop_etl.wide_measurement.load_wide_measurement` in Python.
#'
#' @param con DuckDB DBI connection holding the OMOP CDM (`person`, `visit_occurrence`, `measurement`).
#' @param source Table / view name, CSV / TSV / Parquet path, or a parenthesized query.
#' @param concept_mapping Named list `source_column -> list(concept_id, loinc, unit_concept_id, unit)`. A bare
#'   number is shorthand for `list(concept_id = n)`. When `concept_id` is absent the `loinc` code is resolved
#'   through `concept` / `concept_relationship`. See [DEFAULT_VITAL_CONCEPT_MAPPING].
#' @param person_col,date_col,time_col,visit_col Source column names (case-insensitive); `time_col` and
#'   `visit_col` may be `NULL`.
#' @param staged_table_name Name of the intermediate table (integer primary key `stage_id`).
#' @param batch_execution_mode `"sequential"` (default; one committed single-concept insert per pass) or
#'   `"single"` (all passes in one transaction, committed at the end; only for small inputs).
#' @param value_plausibility_filters Named list `source_column -> c(min, max)` of inclusive clinical bounds;
#'   either bound may be `NA`. Out-of-range values are dropped and counted. A `min` / `max` inside a mapping
#'   entry is used when the column has no filter here. See [DEFAULT_VITAL_PLAUSIBILITY_FILTERS].
#' @param person_id_is_cdm_id Treat `person_col` as `person.person_id` instead of the source patient id
#'   (matched to `person.person_source_value`).
#' @param measurement_type_concept_id Type concept stamped on every row.
#' @param target_table Destination (a `measurement`-shaped table).
#' @param datetime_formats,datetime_on_error Passed to [vectorized_strptime()].
#' @param keep_staged Keep the staging table after loading (default: drop it).
#' @param missing_columns `"raise"` (default) if a mapped column is absent from the source, or `"skip"` to
#'   ignore it (useful with [DEFAULT_VITAL_CONCEPT_MAPPING] on partial extracts).
#' @param coalesce_columns Source columns that carry the same measure (e.g. PCORnet `BMI` and `ORIGINAL_BMI`)
#'   and must yield ONE measurement row per source row instead of one each. `list(c("BMI", "ORIGINAL_BMI"))`
#'   lists a precedence group: the first column with a value inside its plausibility bounds wins, later
#'   columns only fill gaps. `"auto"` (default) builds a group, in `concept_mapping` order, from every set of
#'   columns sharing a `concept_id`; `NULL` disables coalescing (one row per column, as in 0.5.7). Every
#'   column of a group must map to the same `concept_id`.
#' @return A data frame with one row per source column: `source_column`, `concept_id`, `rows_non_null`,
#'   `rows_out_of_range`, `rows_unmatched_person`, `rows_inserted`, `coalesced_into`. A column folded into an
#'   earlier one reports `rows_inserted = 0` and the primary column's name in `coalesced_into`; the primary's
#'   counts cover the whole group.
#' @export
load_wide_measurement <- function(
    con, source, concept_mapping,
    person_col = "PATID", date_col = "MEASURE_DATE", time_col = "MEASURE_TIME", visit_col = "ENCOUNTERID",
    staged_table_name = "_staged_wide_measurement",
    batch_execution_mode = c("sequential", "single"),
    value_plausibility_filters = NULL,
    person_id_is_cdm_id = FALSE,
    measurement_type_concept_id = 32817L,
    target_table = "measurement",
    datetime_formats = NULL,
    datetime_on_error = "null",
    keep_staged = FALSE,
    missing_columns = c("raise", "skip"),
    coalesce_columns = "auto") {
  batch_execution_mode <- match.arg(batch_execution_mode)
  missing_columns <- match.arg(missing_columns)
  if (length(concept_mapping) == 0) stop("concept_mapping must contain at least one source column.")
  staged <- .wm_table_ident(staged_table_name)
  target <- .wm_table_ident(target_table)
  filters <- value_plausibility_filters %||% list()
  relation <- .wm_source_relation(source)

  described <- DBI::dbGetQuery(con, paste0("DESCRIBE SELECT * FROM ", relation, " LIMIT 0"))
  columns <- stats::setNames(as.list(described$column_name), tolower(described$column_name))
  person_src <- .wm_resolve_col(columns, person_col, "person")
  date_src <- .wm_resolve_col(columns, date_col, "date")
  time_src <- if (!is.null(time_col) && !is.null(columns[[tolower(time_col)]])) columns[[tolower(time_col)]] else NULL
  visit_src <- if (!is.null(visit_col) && !is.null(columns[[tolower(visit_col)]])) columns[[tolower(visit_col)]] else NULL

  if (missing_columns == "skip") {
    concept_mapping <- concept_mapping[tolower(names(concept_mapping)) %in% names(columns)]
    if (length(concept_mapping) == 0) stop("None of the mapped columns exist in the source.")
  }
  mapping <- .wm_normalize_mapping(con, concept_mapping)
  value_src <- vapply(names(mapping), function(col) .wm_resolve_col(columns, col, "measure"), character(1))
  value_cols <- stats::setNames(paste0("v", seq_along(mapping) - 1L), names(mapping))
  groups <- .wm_resolve_coalesce_groups(mapping, coalesce_columns)
  primary_of <- list()
  group_of <- list()
  for (g in groups) {
    group_of[[g[[1]]]] <- g
    for (col in g[-1]) primary_of[[col]] <- g[[1]]
  }

  # ---- 1. stage -------------------------------------------------------------------------------------
  DBI::dbExecute(con, paste0("DROP TABLE IF EXISTS ", staged))
  DBI::dbExecute(con, paste0(
    "CREATE TABLE ", staged, " (stage_id BIGINT PRIMARY KEY, person_key VARCHAR, visit_key VARCHAR, ",
    "measure_datetime TIMESTAMP, measure_time VARCHAR, ", paste0(value_cols, " DOUBLE", collapse = ", "), ")"
  ))
  dt_expr <- vectorized_strptime(.wm_ident(date_src), if (is.null(time_src)) NULL else .wm_ident(time_src),
                                 datetime_formats, datetime_on_error)
  value_select <- paste0("TRY_CAST(", .wm_ident(value_src), " AS DOUBLE)", collapse = ", ")
  visit_select <- if (is.null(visit_src)) "NULL" else paste0("CAST(", .wm_ident(visit_src), " AS VARCHAR)")
  time_select <- if (is.null(time_src)) "NULL" else paste0("CAST(", .wm_ident(time_src), " AS VARCHAR)")
  DBI::dbExecute(con, paste0(
    "INSERT INTO ", staged, " SELECT ROW_NUMBER() OVER ()::BIGINT, CAST(", .wm_ident(person_src), " AS VARCHAR), ",
    visit_select, ", ", dt_expr, ", ", time_select, ", ", value_select, " FROM ", relation
  ))

  # ---- 2. insert, one concept per committed pass ------------------------------------------------------
  person_join <- if (isTRUE(person_id_is_cdm_id)) "p.person_id = TRY_CAST(s.person_key AS BIGINT)" else "p.person_source_value = s.person_key"
  measurement_columns <- paste0(
    "measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, ",
    "measurement_time, measurement_type_concept_id, operator_concept_id, value_as_number, ",
    "value_as_concept_id, unit_concept_id, range_low, range_high, provider_id, visit_occurrence_id, ",
    "visit_detail_id, measurement_source_value, measurement_source_concept_id, unit_source_value, ",
    "unit_source_concept_id, value_source_value, measurement_event_id, meas_event_field_concept_id"
  )

  bounds <- function(col) {
    f <- filters[[col]]
    lo <- if (!is.null(f)) f[[1]] else mapping[[col]]$min
    hi <- if (!is.null(f) && length(f) > 1) f[[2]] else if (!is.null(f)) NA else mapping[[col]]$max
    list(lo = if (is.null(lo) || is.na(lo)) NULL else lo, hi = if (is.null(hi) || is.na(hi)) NULL else hi)
  }
  fmt_num <- function(x) format(as.numeric(x), scientific = FALSE, digits = 15)
  guarded <- function(col, alias = "s") {
    v <- paste0(alias, ".", value_cols[[col]])
    b <- bounds(col)
    conds <- c(if (!is.null(b$lo)) paste0(v, " >= ", fmt_num(b$lo)),
               if (!is.null(b$hi)) paste0(v, " <= ", fmt_num(b$hi)))
    if (length(conds)) paste0("CASE WHEN ", paste(conds, collapse = " AND "), " THEN ", v, " END") else v
  }
  value_expr <- function(col, alias = "s") {
    members <- if (!is.null(group_of[[col]])) group_of[[col]] else col
    if (length(members) == 1) guarded(col, alias)
    else paste0("COALESCE(", paste(vapply(members, guarded, character(1), alias = alias), collapse = ", "), ")")
  }
  raw_non_null <- function(col) {
    members <- if (!is.null(group_of[[col]])) group_of[[col]] else col
    paste(paste0(value_cols[members], " IS NOT NULL"), collapse = " OR ")
  }
  pass_sql <- function(col, offset) {
    entry <- mapping[[col]]
    expr <- value_expr(col)
    unit_sql <- if (!is.null(entry$unit)) DBI::dbQuoteString(con, entry$unit) else "NULL"
    src_val <- DBI::dbQuoteString(con, if (!is.null(entry$loinc)) entry$loinc else col)
    unit_cid <- if (!is.null(entry$unit_concept_id)) as.integer(entry$unit_concept_id) else 0L
    paste0(
      "INSERT INTO ", target, " (", measurement_columns, ") ",
      "SELECT ", format(offset, scientific = FALSE), " + s.stage_id, p.person_id, ", entry$concept_id, ", ",
      "CAST(s.measure_datetime AS DATE), s.measure_datetime, s.measure_time, ",
      as.integer(measurement_type_concept_id), ", 0, ", expr, ", 0, ", unit_cid, ", ",
      "NULL, NULL, NULL, vo.visit_occurrence_id, NULL, ", src_val, ", 0, ", unit_sql, ", ",
      unit_cid, ", CAST(", expr, " AS VARCHAR), NULL, 0 ",
      "FROM ", staged, " s JOIN person p ON ", person_join, " ",
      "LEFT JOIN visit_occurrence vo ON vo.visit_source_value = s.visit_key ",
      "WHERE (", expr, ") IS NOT NULL"
    )
  }
  count_rows <- function(col) {
    expr <- value_expr(col)
    cnt <- DBI::dbGetQuery(con, paste0(
      "SELECT COUNT(*) FILTER (WHERE ", raw_non_null(col), ") AS non_null, ",
      "COUNT(*) FILTER (WHERE (", expr, ") IS NOT NULL) AS kept FROM ", staged, " s"))
    matched <- DBI::dbGetQuery(con, paste0(
      "SELECT COUNT(*) AS n FROM ", staged, " s JOIN person p ON ", person_join,
      " WHERE (", expr, ") IS NOT NULL"))$n[[1]]
    c(non_null = cnt$non_null[[1]], kept = cnt$kept[[1]], matched = matched)
  }
  n_rows <- function() DBI::dbGetQuery(con, paste0("SELECT COUNT(*) AS n FROM ", target))$n[[1]]

  single <- batch_execution_mode == "single"
  report <- vector("list", length(mapping))
  names(report) <- names(mapping)
  in_txn <- FALSE
  on.exit({
    if (in_txn) try(DBI::dbExecute(con, "ROLLBACK"), silent = TRUE)
    if (!isTRUE(keep_staged)) try(DBI::dbExecute(con, paste0("DROP TABLE IF EXISTS ", staged)), silent = TRUE)
  }, add = TRUE)

  if (single) {
    DBI::dbExecute(con, "BEGIN TRANSACTION")
    in_txn <- TRUE
  }
  for (col in names(mapping)) {
    if (!is.null(primary_of[[col]])) {
      nn <- DBI::dbGetQuery(con, paste0("SELECT COUNT(", value_cols[[col]], ") AS n FROM ", staged))$n[[1]]
      report[[col]] <- data.frame(
        source_column = col, concept_id = mapping[[col]]$concept_id, rows_non_null = as.numeric(nn),
        rows_out_of_range = 0, rows_unmatched_person = 0, rows_inserted = 0,
        coalesced_into = primary_of[[col]], stringsAsFactors = FALSE)
      next
    }
    cnt <- count_rows(col)
    before <- n_rows()
    max_id <- DBI::dbGetQuery(con, paste0("SELECT COALESCE(MAX(measurement_id), 0) AS m FROM ", target))$m[[1]]
    if (!single) {
      DBI::dbExecute(con, "BEGIN TRANSACTION")
      in_txn <- TRUE
    }
    DBI::dbExecute(con, pass_sql(col, max_id))
    if (!single) {
      DBI::dbExecute(con, "COMMIT")
      in_txn <- FALSE
    }
    report[[col]] <- data.frame(
      source_column = col, concept_id = mapping[[col]]$concept_id,
      rows_non_null = as.numeric(cnt[["non_null"]]),
      rows_out_of_range = as.numeric(cnt[["non_null"]] - cnt[["kept"]]),
      rows_unmatched_person = as.numeric(cnt[["kept"]] - cnt[["matched"]]),
      rows_inserted = as.numeric(n_rows() - before),
      coalesced_into = NA_character_,
      stringsAsFactors = FALSE
    )
  }
  if (single) {
    DBI::dbExecute(con, "COMMIT")
    in_txn <- FALSE
  }
  out <- do.call(rbind, unname(report))
  rownames(out) <- NULL
  out
}
