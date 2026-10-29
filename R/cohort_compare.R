#' Render, translate and run one portable cohort SQL file on DuckDB
#'
#' Uses OHDSI's SqlRender (`render()` -> `translate(targetDialect = "duckdb")` -> `splitSql()`), the reference
#' implementation for the OHDSI SQL in `inst/cohorts/`.
#'
#' @param con A DuckDB DBI connection.
#' @param sql_or_path Path to a `.sql` file, or the SQL text itself.
#' @param ... Named parameters rendered into `@name` placeholders.
#' @return Invisibly, `NULL`.
#' @export
run_cohort_sql <- function(con, sql_or_path, ...) {
  if (!requireNamespace("SqlRender", quietly = TRUE)) {
    stop("run_cohort_sql() needs the SqlRender package: install.packages(\"SqlRender\")", call. = FALSE)
  }
  is_path <- length(sql_or_path) == 1L && !grepl("\n", sql_or_path, fixed = TRUE) && nchar(sql_or_path) < 400L &&
    grepl("[.]sql$", sql_or_path, ignore.case = TRUE)
  sql <- if (is_path) paste(readLines(sql_or_path, warn = FALSE, encoding = "UTF-8"), collapse = "\n") else sql_or_path
  params <- list(...)
  # SqlRender warns about parameters a file does not use; the shared parameter set is applied to every file
  params <- params[vapply(names(params), function(n) grepl(paste0("@", n), sql, fixed = TRUE), logical(1))]
  sql <- SqlRender::translate(do.call(SqlRender::render, c(list(sql), params)), targetDialect = "duckdb")
  for (stmt in SqlRender::splitSql(sql)) DBI::dbExecute(con, stmt)
  invisible(NULL)
}

#' Compare two target-cohort definitions side by side
#'
#' Runs two target-cohort SQL files on one CDM and compares them (FR-D8). Both cohorts are written to a TEMP table,
#' so a read-only CDM works. `outcome_sql` (e.g. `outcome_cohort.sql`) adds a binary `outcome`: an outcome event
#' starting in `(start, start + followup_days]`. R port of `omop_etl.cohort_sql.compare_cohort_definitions()`.
#'
#' @param con A DuckDB DBI connection to the CDM.
#' @param target_a,target_b Paths to the two target-cohort SQL files.
#' @param outcome_sql Optional path to the outcome-cohort SQL file.
#' @param params_a,params_b Named lists overriding parameters per definition, e.g.
#'   `list(followup_evidence = "observation_period_or_event")`.
#' @param common_params Named list of parameters applied to both definitions.
#' @param followup_days Follow-up window in days (default 30).
#' @param cdm_database_schema CDM schema (default `"main"`).
#' @return A list with `summary` (one row per definition: `definition`, `n`, `events`, `outcome_rate`) and
#'   `overlap` (the [cohort_overlap()] table).
#' @export
compare_cohort_definitions <- function(con, target_a, target_b, outcome_sql = NULL,
                                       params_a = list(), params_b = list(), common_params = list(),
                                       followup_days = 30L, cdm_database_schema = "main") {
  followup_days <- as.integer(followup_days)
  base <- utils::modifyList(
    list(cdm_database_schema = cdm_database_schema, target_database_schema = "temp.main",
         target_cohort_table = "_cmp_cohort", followup_days = followup_days, min_age = 18L),
    common_params)
  run <- function(path, extra, id) {
    do.call(run_cohort_sql, c(list(con, path), utils::modifyList(utils::modifyList(base, extra), list(target_cohort_id = id))))
  }
  DBI::dbExecute(con, "DROP TABLE IF EXISTS temp.main._cmp_cohort")
  DBI::dbExecute(con, paste("CREATE TEMP TABLE _cmp_cohort (cohort_definition_id INTEGER, subject_id BIGINT,",
                            "cohort_start_date DATE, cohort_end_date DATE)"))
  on.exit(try(DBI::dbExecute(con, "DROP TABLE IF EXISTS temp.main._cmp_cohort"), silent = TRUE), add = TRUE)
  run(target_a, params_a, 1L)
  run(target_b, params_b, 2L)
  has_outcome <- !is.null(outcome_sql)
  if (has_outcome) run(outcome_sql, list(), 3L)

  outcome_expr <- if (has_outcome) {
    sprintf(paste("CASE WHEN EXISTS (SELECT 1 FROM _cmp_cohort o WHERE o.cohort_definition_id = 3",
                  "AND o.subject_id = t.subject_id AND o.cohort_start_date > t.cohort_start_date",
                  "AND o.cohort_start_date <= t.cohort_start_date + INTERVAL '%d' DAY) THEN 1 ELSE 0 END"),
            followup_days)
  } else "NULL"
  frames <- lapply(1:2, function(i) {
    DBI::dbGetQuery(con, sprintf(
      "SELECT t.subject_id, t.cohort_start_date, %s AS outcome FROM _cmp_cohort t WHERE t.cohort_definition_id = %d",
      outcome_expr, i))
  })
  summary <- data.frame(
    definition = c("A", "B"),
    n = vapply(frames, nrow, integer(1)),
    events = vapply(frames, function(f) if (has_outcome) as.integer(sum(f$outcome)) else NA_integer_, integer(1)),
    outcome_rate = vapply(frames, function(f) if (has_outcome && nrow(f) > 0L) mean(f$outcome) else NA_real_, numeric(1)),
    stringsAsFactors = FALSE)
  overlap <- cohort_overlap(NULL, frames[[1]], frames[[2]], by = NULL,
                            outcome_col = if (has_outcome) "outcome" else NULL)
  list(summary = summary, overlap = overlap)
}
