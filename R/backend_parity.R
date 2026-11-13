#' BigQuery vs DuckDB, and R vs Python, parity checks
#'
#' @description
#' The BigQuery demo ([run_bigquery_demo()]) builds a cohort and features in GoogleSQL. These functions run the same
#' steps on DuckDB and compare the answers, as the Python module `omop_etl.backend_parity` does with the same rules:
#'
#' - [run_duckdb_demo()]: the cohort from the native Circe compiler and the features from
#'   `inst/sql/duckdb_demo_features.sql`, the DuckDB twin of `bigquery_demo_features.sql`.
#' - [copy_demo_sample_from_bigquery()]: copies every row of a fixed sample of persons (and the `concept_ancestor` rows
#'   their events need) from BigQuery into DuckDB, so the cohort and features can be rebuilt locally and compared with
#'   the BigQuery tables for the same persons.
#' - [compare_cohorts()], [compare_features()], [compare_column_types()], [check_id_precision()],
#'   [compare_demo_summaries()] and [compare_demo_outputs()]: the comparisons.
#'
#' Ids are compared as exact decimal text. A 64-bit id read into a double is exact only up to 2^53 and one read into an
#' R integer only up to 2^31 - 1, so ids are kept as `integer64` or text, and a double id at or above 2^53 is refused
#' rather than compared.
#' @name backend_parity
NULL

#' Feature columns written by both backends (`subject_id` is the key)
#' @format A character vector.
#' @export
DEMO_FEATURE_COLUMNS <- c("index_date", "age_at_index", "sex", "race", "prior_ip_count", "prior_ed_count", "prior_os_count",
                          "prior_ip_os_ed_count", "readmission_count_twelve_months")

# DuckDB types that hold each BigQuery type without loss; INT64 into a 32-bit type is allowed except for key ids
.BQ_TO_DUCKDB <- list(
  INT64 = c("BIGINT", "HUGEINT", "INTEGER", "SMALLINT", "TINYINT"), FLOAT64 = c("DOUBLE", "FLOAT", "DECIMAL"),
  NUMERIC = c("DECIMAL", "DOUBLE"), BIGNUMERIC = c("DECIMAL", "DOUBLE"), STRING = "VARCHAR", BOOL = "BOOLEAN",
  DATE = "DATE", DATETIME = "TIMESTAMP", TIMESTAMP = c("TIMESTAMP", "TIMESTAMP WITH TIME ZONE"), BYTES = "BLOB"
)
.MAX_EXACT_DOUBLE <- 2^53

# a column holding row ids (person_id, visit_occurrence_id, event_id, ...) that must stay 64-bit; concept ids and cohort
# definition ids are small by construction
.is_key_id <- function(x) {
  x <- tolower(x)
  grepl("_id$", x) & !grepl("concept_id$", x) & !grepl("definition_id$", x)
}

.id_text <- function(x, what = "id") {
  if (inherits(x, "integer64")) {
    out <- as.character(x)
  } else if (is.double(x)) {
    if (any(abs(x) >= .MAX_EXACT_DOUBLE, na.rm = TRUE)) {
      stop(what, " was read as a double at or above 2^53 and may be truncated; read ids as integer64 or text ",
           "(duckdb::duckdb(bigint = \"integer64\"), bigrquery bigint = \"integer64\")", call. = FALSE)
    }
    out <- sprintf("%.0f", x)
  } else if (is.factor(x)) {
    out <- trimws(as.character(x))
  } else {
    out <- trimws(as.character(x))
  }
  out[is.na(x) | out == "NA"] <- ""
  out
}

.date_text <- function(x) {
  out <- substr(as.character(x), 1L, 10L)
  out[is.na(out)] <- ""
  out
}

.duckdb_demo_sql <- function(file) {
  path <- system.file("sql", file, package = "omopduckdb")
  if (!nzchar(path)) path <- file.path(getwd(), "inst", "sql", file)
  paste(readLines(path, warn = FALSE), collapse = "\n")
}

#' Build the BigQuery demo's cohort and features on DuckDB
#'
#' The cohort comes from [compile_circe_to_duckdb()] and the features from `inst/sql/duckdb_demo_features.sql`, which
#' computes the same columns as the GoogleSQL the demo runs on BigQuery. Both tables are written to the connection's
#' current schema. Ids are returned as text, so they are exact whatever the connection's `bigint` setting.
#'
#' @param con A DuckDB connection holding the CDM.
#' @param cdm_schema Schema holding the CDM tables.
#' @param cohort_json Circe JSON; defaults to the shipped `diabetes_phenotype.circe.json`.
#' @param cohort_id Cohort definition id.
#' @param cohort_table,features_table Tables to (re)create.
#' @return A list with `cohort` and `features` data frames.
#' @export
run_duckdb_demo <- function(con, cdm_schema = "main", cohort_json = NULL, cohort_id = 1L, cohort_table = "demo_cohort",
                            features_table = "demo_features") {
  .check_ident(cdm_schema, "cdm_schema", 1L)
  .check_ident(cohort_table, "cohort_table", 1L)
  .check_ident(features_table, "features_table", 1L)
  cohort_json <- cohort_json %||% file.path(cohort_definitions_dir(), "diabetes_phenotype.circe.json")
  cohort_id <- as.integer(cohort_id)
  sql <- compile_circe_to_duckdb(cohort_json, target_cohort_id = cohort_id, cdm_schema = cdm_schema)
  DBI::dbExecute(con, sprintf("CREATE OR REPLACE TABLE %s (cohort_definition_id BIGINT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE)", cohort_table))
  DBI::dbExecute(con, sprintf("INSERT INTO %s %s", cohort_table, sql))
  feats <- .duckdb_demo_sql("duckdb_demo_features.sql")
  vals <- list(cdm = cdm_schema, cohort = cohort_table, features_table = features_table, cohort_id = cohort_id)
  for (nm in names(vals)) feats <- gsub(paste0("{{", nm, "}}"), vals[[nm]], feats, fixed = TRUE)
  DBI::dbExecute(con, feats)
  cohort <- DBI::dbGetQuery(con, sprintf(paste(
    "SELECT cohort_definition_id, CAST(subject_id AS VARCHAR) AS subject_id, cohort_start_date, cohort_end_date",
    "FROM %s ORDER BY subject_id, cohort_start_date"), cohort_table))
  features <- DBI::dbGetQuery(con, sprintf(
    "SELECT * REPLACE (CAST(subject_id AS VARCHAR) AS subject_id) FROM %s ORDER BY subject_id", features_table))
  list(cohort = cohort, features = features)
}

#' Compare two cohort tables
#'
#' @param left,right Data frames with `subject_id`, `cohort_start_date` and `cohort_end_date`.
#' @param examples Number of example ids to list from each side's extras (keep 0 unless the data are synthetic).
#' @return A list of counts; `match` is `TRUE` when the (subject, start, end) rows are the same.
#' @export
compare_cohorts <- function(left, right, examples = 0L) {
  key <- function(d) unique(paste(.id_text(d$subject_id, "subject_id"), .date_text(d$cohort_start_date),
                                  .date_text(d$cohort_end_date), sep = "|"))
  a <- key(left); b <- key(right)
  sa <- unique(.id_text(left$subject_id)); sb <- unique(.id_text(right$subject_id))
  out <- list(n_left_rows = length(a), n_right_rows = length(b), n_left_subjects = length(sa), n_right_subjects = length(sb),
              n_subjects_both = length(intersect(sa, sb)), n_subjects_only_left = length(setdiff(sa, sb)),
              n_subjects_only_right = length(setdiff(sb, sa)), n_rows_only_left = length(setdiff(a, b)),
              n_rows_only_right = length(setdiff(b, a)), same_subjects = setequal(sa, sb), match = setequal(a, b))
  if (examples > 0L) {
    out$examples_only_left <- utils::head(sort(setdiff(sa, sb)), examples)
    out$examples_only_right <- utils::head(sort(setdiff(sb, sa)), examples)
  }
  out
}

.missing <- function(x) if (is.character(x)) is.na(x) | x == "" else is.na(x)

.same_values <- function(x, y, tolerance) {
  mx <- .missing(x); my <- .missing(y)
  same <- mx & my
  both <- !mx & !my
  if (inherits(x, c("Date", "POSIXt")) || inherits(y, c("Date", "POSIXt"))) {
    same[both] <- .date_text(x[both]) == .date_text(y[both])
    return(same)
  }
  nx <- suppressWarnings(as.numeric(as.character(x))); ny <- suppressWarnings(as.numeric(as.character(y)))
  numeric_pair <- both & !is.na(nx) & !is.na(ny) & !(is.character(x) & is.character(y))
  same[numeric_pair] <- abs(nx[numeric_pair] - ny[numeric_pair]) <= tolerance
  text <- both & !numeric_pair
  same[text] <- as.character(x[text]) == as.character(y[text])
  same
}

#' Compare two feature tables row by row
#'
#' Numbers agree within `tolerance`, dates on the day, text exactly; a value missing on both sides agrees.
#'
#' @param left,right Data frames of features.
#' @param key Key column (default `subject_id`).
#' @param columns Columns to compare; default [DEMO_FEATURE_COLUMNS] present on either side.
#' @param tolerance Allowed absolute difference for numbers.
#' @return A list: key counts, per-column mismatch counts and `match`.
#' @export
compare_features <- function(left, right, key = "subject_id", columns = NULL, tolerance = 0) {
  columns <- columns %||% intersect(DEMO_FEATURE_COLUMNS, union(names(left), names(right)))
  lk <- .id_text(left[[key]], key); rk <- .id_text(right[[key]], key)
  if (anyDuplicated(lk) || anyDuplicated(rk)) stop(key, " is not unique in the feature tables", call. = FALSE)
  both <- sort(intersect(lk, rk))
  li <- match(both, lk); ri <- match(both, rk)
  per <- lapply(stats::setNames(columns, columns), function(col) {
    if (!col %in% names(left) || !col %in% names(right)) {
      return(list(n_compared = 0L, n_mismatch = NA_integer_, missing_in = if (!col %in% names(left)) "left" else "right"))
    }
    list(n_compared = length(both), n_mismatch = sum(!.same_values(left[[col]][li], right[[col]][ri], tolerance)))
  })
  ok <- length(both) == length(lk) && length(both) == length(rk) &&
    all(vapply(per, function(v) isTRUE(v$n_mismatch == 0L), logical(1)))
  list(n_left = length(lk), n_right = length(rk), n_both = length(both), n_only_left = length(lk) - length(both),
       n_only_right = length(rk) - length(both), columns = per, match = ok)
}

.duck_base_type <- function(t) {
  t <- toupper(trimws(sub("\\(.*\\)$", "", t)))
  map <- c(INT = "INTEGER", INT4 = "INTEGER", INT8 = "BIGINT", LONG = "BIGINT", REAL = "FLOAT", TEXT = "VARCHAR",
           STRING = "VARCHAR", BOOL = "BOOLEAN", NUMERIC = "DECIMAL", TIMESTAMPTZ = "TIMESTAMP WITH TIME ZONE",
           TIMESTAMP_S = "TIMESTAMP", TIMESTAMP_MS = "TIMESTAMP", TIMESTAMP_NS = "TIMESTAMP")
  ifelse(t %in% names(map), map[t], t)
}

#' Compare column types between BigQuery and DuckDB
#'
#' @param bigquery Rows of BigQuery `INFORMATION_SCHEMA.COLUMNS` (`table_name`, `column_name`, `data_type`).
#' @param duckdb Rows of DuckDB `information_schema.columns` with the same three columns.
#' @return A data frame, one row per (table, column), with `status`: `ok`; `id_narrowed` (a key id that is `INT64` in
#'   BigQuery but not `BIGINT` in DuckDB, so ids above 2^31 - 1 or 2^53 break); `type_mismatch`; or `only_bigquery` /
#'   `only_duckdb` (the CDM versions differ; informational). Only `id_narrowed` and `type_mismatch` are failures.
#' @export
compare_column_types <- function(bigquery, duckdb) {
  norm <- function(d, side) {
    out <- data.frame(table_name = tolower(d$table_name), column_name = tolower(d$column_name),
                      type = as.character(d$data_type), stringsAsFactors = FALSE)
    names(out)[3] <- paste0(side, "_type")
    out
  }
  m <- merge(norm(bigquery, "bigquery"), norm(duckdb, "duckdb"), by = c("table_name", "column_name"), all = TRUE)
  bq <- toupper(trimws(sub("\\(.*\\)$", "", m$bigquery_type)))
  dk <- .duck_base_type(m$duckdb_type)
  m$status <- vapply(seq_len(nrow(m)), function(i) {
    if (is.na(m$duckdb_type[i])) return("only_bigquery")
    if (is.na(m$bigquery_type[i])) return("only_duckdb")
    if (bq[i] == "INT64" && .is_key_id(m$column_name[i]) && !dk[i] %in% c("BIGINT", "HUGEINT")) return("id_narrowed")
    allowed <- .BQ_TO_DUCKDB[[bq[i]]]
    if (!is.null(allowed) && dk[i] %in% allowed) "ok" else "type_mismatch"
  }, character(1))
  m <- m[order(m$table_name, m$column_name), , drop = FALSE]
  rownames(m) <- NULL
  m
}

#' Id columns that cannot hold every 64-bit id exactly
#'
#' @param df A data frame downloaded from BigQuery or read from DuckDB.
#' @param columns Columns to check; default every key id (a name ending in `_id`, except concept and cohort definition
#'   ids).
#' @return A data frame of problems (`column`, `type`, `problem`); zero rows when all is well. `integer64` and text are
#'   fine; a double is exact only up to 2^53 and an R integer only up to 2^31 - 1.
#' @export
check_id_precision <- function(df, columns = NULL) {
  columns <- columns %||% names(df)[.is_key_id(names(df))]
  rows <- lapply(columns, function(col) {
    x <- df[[col]]
    if (inherits(x, "integer64") || is.character(x)) return(NULL)
    type <- class(x)[[1]]
    problem <- if (is.integer(x)) "integer: holds ids only up to 2^31 - 1" else if (is.double(x)) "double: exact only up to 2^53" else paste0(type, ": not an integer type")
    data.frame(column = col, type = type, problem = problem, stringsAsFactors = FALSE)
  })
  rows <- Filter(Negate(is.null), rows)
  if (!length(rows)) return(data.frame(column = character(), type = character(), problem = character()))
  do.call(rbind, rows)
}

.SUMMARY_KEYS <- c("n_cohort", "age_at_index", "sex", "race", "utilization", "clinical_telemetry",
                   "numeric_measurement_values_in_cohort")

.summary_diff <- function(a, b, path, tol) {
  if (is.list(a) && is.list(b) && (length(names(a)) || length(names(b)) || (!length(a) && !length(b)))) {
    keys <- sort(union(names(a), names(b)))
    return(unlist(lapply(keys, function(k) {
      if (!k %in% names(a) || !k %in% names(b)) return(sprintf("%s.%s: only in %s", path, k, if (k %in% names(a)) "left" else "right"))
      .summary_diff(a[[k]], b[[k]], paste0(path, ".", k), tol)
    })))
  }
  show <- function(x) if (is.null(x)) "NULL" else if (is.character(x)) sprintf("'%s'", x) else format(x)
  if (is.null(a) || is.null(b)) return(if (is.null(a) && is.null(b)) NULL else sprintf("%s: %s vs %s", path, show(a), show(b)))
  if (is.character(a) || is.character(b) || is.logical(a) || is.logical(b)) {
    return(if (identical(a, b)) NULL else sprintf("%s: %s vs %s", path, show(a), show(b)))
  }
  if (is.numeric(a) && is.numeric(b)) {
    exact <- grepl("(\\.n|n_cohort|_in_cohort)$", path) || grepl("n_numeric", path)
    bad <- if (exact) as.numeric(a) != as.numeric(b) else abs(as.numeric(a) - as.numeric(b)) > tol
    return(if (isTRUE(bad)) sprintf("%s: %s vs %s", path, show(a), show(b)) else NULL)
  }
  if (identical(a, b)) NULL else sprintf("%s: %s vs %s", path, show(a), show(b))
}

#' Compare two `demo_summary.json` results
#'
#' For an R run against a Python run (`python -m omop_etl.demo_bigquery`), or two runs of either. Counts must be equal;
#' means, SDs, medians and percentages may differ by `tolerance` (each language rounds to one or two decimals). The run
#' block (billing project, timings) and the disclaimer are not compared.
#'
#' @param left,right A summary list or the path of a `demo_summary.json`.
#' @param tolerance Allowed absolute difference for non-count numbers.
#' @return A character vector of differences; empty when they agree.
#' @export
compare_demo_summaries <- function(left, right, tolerance = 0.051) {
  load <- function(x) if (is.character(x)) jsonlite::fromJSON(x, simplifyVector = FALSE) else x
  a <- load(left); b <- load(right)
  out <- unlist(lapply(.SUMMARY_KEYS, function(k) {
    if (!k %in% names(a) || !k %in% names(b)) {
      return(if (k %in% names(a) || k %in% names(b)) sprintf("%s: only in %s", k, if (k %in% names(a)) "left" else "right") else sprintf("%s: in neither", k))
    }
    .summary_diff(a[[k]], b[[k]], k, tolerance)
  }))
  as.character(out)
}

#' Compare two demo output folders
#'
#' Compares `demo_summary.json` and, when both folders have it, `demo_features.csv` (ids read as text).
#'
#' @param left_dir,right_dir Output folders of [run_bigquery_demo()] or `python -m omop_etl.demo_bigquery`.
#' @param tolerance Passed to [compare_demo_summaries()].
#' @return A list with `summary_differences`, `features` (from [compare_features()]) and `match`.
#' @export
compare_demo_outputs <- function(left_dir, right_dir, tolerance = 0.051) {
  out <- list(summary_differences = compare_demo_summaries(file.path(left_dir, "demo_summary.json"),
                                                           file.path(right_dir, "demo_summary.json"), tolerance))
  lf <- file.path(left_dir, "demo_features.csv"); rf <- file.path(right_dir, "demo_features.csv")
  if (file.exists(lf) && file.exists(rf)) {
    read <- function(p) utils::read.csv(p, colClasses = c(subject_id = "character"), stringsAsFactors = FALSE)
    out$features <- compare_features(read(lf), read(rf))
  }
  out$match <- !length(out$summary_differences) && (is.null(out$features) || isTRUE(out$features$match))
  out
}

# BigQuery INT64 values come back as bit64::integer64; key ids stay integer64 (exact), other integers (counts, ages,
# concept ids) become R integers, which turns an out-of-range value into NA with a warning instead of a wrong number
.bq_int64_columns <- function(df) {
  for (nm in names(df)) {
    if (inherits(df[[nm]], "integer64") && !.is_key_id(nm)) df[[nm]] <- as.integer(df[[nm]])
  }
  df
}

# write a frame from BigQuery into DuckDB; integer64 goes through text and CAST because duckdb's R client writes a bit64
# vector as a DOUBLE reinterpretation of the bits (see load_from_bigquery())
.write_bq_frame <- function(con, table, df) {
  .check_ident(table, "table", 1L)
  is64 <- vapply(df, function(col) inherits(col, "integer64"), logical(1))
  for (nm in names(df)[is64]) df[[nm]] <- as.character(df[[nm]])
  quoted <- DBI::dbQuoteIdentifier(con, names(df))
  select_list <- ifelse(is64, sprintf("CAST(%s AS BIGINT) AS %s", quoted, quoted), quoted)
  stage <- "_bq_parity_stage"
  duckdb::duckdb_register(con, stage, df)
  on.exit(duckdb::duckdb_unregister(con, stage), add = TRUE)
  DBI::dbExecute(con, sprintf("CREATE OR REPLACE TABLE %s AS SELECT %s FROM %s", table, paste(select_list, collapse = ", "), stage))
  invisible(nrow(df))
}

.demo_sample_sql <- function(dataset, sample_modulus, sample_remainder) {
  .demo_dataset(dataset, "dataset")
  m <- as.integer(sample_modulus); r <- as.integer(sample_remainder)
  if (is.na(m) || m < 1L || is.na(r) || r < 0L || r >= m) stop("need sample_modulus >= 1 and 0 <= sample_remainder < sample_modulus", call. = FALSE)
  where <- sprintf("MOD(person_id, %d) = %d", m, r)
  tables <- c("person", "observation_period", "visit_occurrence", "condition_occurrence", "drug_exposure", "measurement")
  sql <- stats::setNames(sprintf("SELECT * FROM `%s.%s` WHERE %s", dataset, tables, where), tables)
  # the concept_ancestor rows whose descendant appears in the sample's events: enough for every concept-set lookup
  sql[["concept_ancestor"]] <- sprintf(paste(
    "SELECT ca.* FROM `%1$s.concept_ancestor` AS ca WHERE ca.descendant_concept_id IN (",
    "SELECT condition_concept_id FROM `%1$s.condition_occurrence` WHERE %2$s UNION DISTINCT",
    "SELECT drug_concept_id FROM `%1$s.drug_exposure` WHERE %2$s UNION DISTINCT",
    "SELECT measurement_concept_id FROM `%1$s.measurement` WHERE %2$s)"), dataset, where)
  sql
}

#' Copy a person sample of the BigQuery CDM into DuckDB
#'
#' Copies every row of the persons with `MOD(person_id, sample_modulus) = sample_remainder` from `person`,
#' `observation_period`, `visit_occurrence`, `condition_occurrence`, `drug_exposure` and `measurement`, plus the
#' `concept_ancestor` rows their events need. The cohort and features are per person, so [run_duckdb_demo()] on the copy
#' must give exactly the BigQuery `demo_cohort` / `demo_features` rows for the same persons. Each query scans its source
#' table once (BigQuery bills the columns read, whatever the sample size); the copy itself is small. Ids are kept
#' exact (`integer64` on download, `BIGINT` in DuckDB).
#'
#' @param billing_project Your GCP project id.
#' @param dataset Source CDM, `"project.dataset"`.
#' @param db_path DuckDB file to write (tables are replaced).
#' @param sample_modulus,sample_remainder Which persons to copy.
#' @param runner For tests: a list with `fetch(sql)` returning a data frame.
#' @return Invisibly, a data frame of table names and copied row counts.
#' @export
copy_demo_sample_from_bigquery <- function(billing_project, dataset = .DEMO_PUBLIC_DATASET, db_path,
                                           sample_modulus = 1000L, sample_remainder = 0L, runner = NULL) {
  sql <- .demo_sample_sql(dataset, sample_modulus, sample_remainder)
  if (is.null(runner)) {
    if (!requireNamespace("bigrquery", quietly = TRUE)) stop("copy_demo_sample_from_bigquery() needs bigrquery.", call. = FALSE)
    runner <- list(fetch = function(q) as.data.frame(bigrquery::bq_table_download(
      bigrquery::bq_project_query(billing_project, q, quiet = TRUE), bigint = "integer64", quiet = TRUE)))
  }
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  n <- vapply(names(sql), function(t) as.numeric(.write_bq_frame(con, t, runner$fetch(sql[[t]]))), numeric(1))
  invisible(data.frame(table = names(sql), rows = n, row.names = NULL))
}
