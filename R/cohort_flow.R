#' `cohort_flow()` (computable phenotype + CONSORT), `generate_table2()` (completeness profiler) and
#' `as_plp_arrow()` (in-memory Arrow hand-off to PatientLevelPrediction)
#'
#' Mirrors of `omop_etl.CohortFlow`, `Table2Generator` and `as_plp_arrow` in Python.
#' @name cohort_flow_tools
#' @keywords internal
NULL

#' @keywords internal
#' @noRd
.flow_anchors <- c(admission = "visit_start_date", discharge = "visit_end_date")

#' Sequential cohort filters, one set of counts, CONSORT outputs and pre-flight checks
#'
#' The base population is `visit_occurrence` restricted to `visit_concept_ids` (default inpatient 9201 and
#' emergency-to-inpatient 262). Each step is a SQL predicate over alias `v` (a `visit_occurrence` row) and `p`
#' (`person`); steps apply cumulatively, so the attrition table, the final cohort and the diagram can never
#' disagree. The unit of analysis is the index visit (`persons` counts distinct people).
#'
#' ```
#' flow <- cohort_flow(con, anchor = "admission", follow_up_days = 30)
#' flow$add_step("Adults", "EXTRACT(year FROM v.visit_start_date) - p.year_of_birth >= 18")
#' res <- flow$run()
#' res$attrition; res$checks; flow$to_mermaid(); flow$to_graphviz()
#' ```
#'
#' @param con DuckDB DBI connection with an OMOP CDM.
#' @param visit_concept_ids Index visit concept ids.
#' @param anchor `"admission"` (`visit_start_date`) or `"discharge"` (`visit_end_date`).
#' @param follow_up_days If set, the pre-flight verifies every row can be followed that long (or died first).
#' @param one_visit_per_person Keep only each person's first index visit.
#' @return A list of functions (`add_step`, `run`, `to_markdown`, `to_mermaid`, `to_latex`, `to_graphviz`) and
#'   a `state` environment holding `cohort`, `attrition` and `checks` after `run()`.
#' @export
cohort_flow <- function(con, visit_concept_ids = c(9201L, 262L), anchor = c("admission", "discharge"),
                        follow_up_days = NULL, one_visit_per_person = FALSE) {
  anchor <- match.arg(anchor)
  st <- new.env(parent = emptyenv())
  st$steps <- list(); st$cohort <- NULL; st$attrition <- NULL; st$checks <- NULL
  self <- new.env(parent = emptyenv())
  ids <- paste(as.integer(visit_concept_ids), collapse = ", ")
  base <- sprintf("FROM visit_occurrence v JOIN person p ON p.person_id = v.person_id WHERE v.visit_concept_id IN (%s)", ids)
  anchor_col <- .flow_anchors[[anchor]]
  cnt <- function(sql) unlist(DBI::dbGetQuery(con, sql)[1, ], use.names = FALSE)

  add_step <- function(name, predicate) {
    st$steps[[length(st$steps) + 1L]] <- list(name = name, predicate = predicate)
    invisible(self)
  }
  preflight <- function() {
    c_df <- st$cohort
    out <- list()
    add <- function(check, ok, detail) {
      out[[length(out) + 1L]] <<- data.frame(check = check, status = if (ok) "PASS" else "FAIL", detail = detail,
                                             stringsAsFactors = FALSE)
    }
    bad_type <- sum(!c_df$visit_concept_id %in% visit_concept_ids)
    add(sprintf("visit_concept_id in (%s)", paste(visit_concept_ids, collapse = ", ")), bad_type == 0,
        sprintf("%d cohort rows outside the set", bad_type))
    add("visit types are inpatient (9201) or ER-to-inpatient (262)", all(visit_concept_ids %in% c(9201L, 262L)),
        sprintf("visit_concept_ids=(%s)%s", paste(visit_concept_ids, collapse = ", "),
                if (setequal(visit_concept_ids, 9201L)) " (262 transfers excluded - intentional?)" else ""))
    miss_end <- sum(is.na(c_df$cohort_end_date))
    add("visit_end_date present", miss_end == 0, sprintf("%d cohort rows lack visit_end_date", miss_end))
    neg <- sum(as.Date(c_df$visit_end_date) < as.Date(c_df$visit_start_date), na.rm = TRUE)
    add("visit_end_date >= visit_start_date", neg == 0, sprintf("%d rows end before they start", neg))
    add(sprintf("anchor = %s (%s)", anchor, anchor_col), TRUE,
        if (anchor == "admission") "readmission/follow-up windows should start at discharge"
        else "labs/meds 'at admission' must not use post-discharge data")
    if (!is.null(follow_up_days)) {
      lbl <- sprintf("%d-day follow-up verifiable", as.integer(follow_up_days))
      res <- tryCatch({
        DBI::dbWriteTable(con, "_flow_cohort", c_df, temporary = TRUE, overwrite = TRUE)
        on.exit(try(DBI::dbExecute(con, "DROP TABLE IF EXISTS _flow_cohort"), silent = TRUE), add = TRUE)
        short <- cnt(sprintf(paste0(
          "WITH obs AS (SELECT person_id, MAX(observation_period_end_date) e FROM observation_period GROUP BY 1), ",
          "d AS (SELECT person_id, MIN(death_date) dd FROM death GROUP BY 1) ",
          "SELECT COUNT(*) FROM _flow_cohort c LEFT JOIN obs ON obs.person_id = c.subject_id ",
          "LEFT JOIN d ON d.person_id = c.subject_id WHERE obs.e IS NULL OR (obs.e < CAST(c.visit_end_date AS DATE) + %d ",
          "AND (d.dd IS NULL OR d.dd > obs.e))"), as.integer(follow_up_days)))
        add(lbl, short == 0, sprintf(paste0("%d/%d rows censored by end of observation before day %d and not dead ",
                                            "(handle as censored or add an exclusion step)"),
                                     short, nrow(c_df), as.integer(follow_up_days)))
        TRUE
      }, error = function(e) {
        add(lbl, FALSE, paste("cannot verify:", conditionMessage(e)))
        FALSE
      })
    }
    dup <- sum(duplicated(c_df$subject_id))
    add("one row per person", dup == 0, sprintf("%d repeat index visits (set one_visit_per_person = TRUE to dedupe)", dup))
    do.call(rbind, out)
  }
  run <- function() {
    counts <- list(c("All visits", cnt("SELECT COUNT(*), COUNT(DISTINCT person_id) FROM visit_occurrence")))
    counts[[2]] <- c(sprintf("Index visit types (%s)", paste(visit_concept_ids, collapse = ", ")),
                     cnt(paste("SELECT COUNT(*), COUNT(DISTINCT v.person_id)", base)))
    preds <- character(0)
    for (s in st$steps) {
      preds <- c(preds, paste0("(", s$predicate, ")"))
      counts[[length(counts) + 1L]] <- c(s$name, cnt(paste("SELECT COUNT(*), COUNT(DISTINCT v.person_id)", base,
                                                           "AND", paste(preds, collapse = " AND "))))
    }
    where <- paste(c(base, preds), collapse = " AND ")
    sel <- sprintf(paste0("SELECT v.person_id AS subject_id, v.visit_occurrence_id, v.visit_concept_id, ",
                          "v.%s AS cohort_start_date, v.visit_end_date AS cohort_end_date, v.visit_start_date, ",
                          "v.visit_end_date %s"), anchor_col, where)
    if (isTRUE(one_visit_per_person)) {
      sel <- paste0("SELECT * EXCLUDE rn FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY subject_id ",
                    "ORDER BY cohort_start_date, visit_occurrence_id) rn FROM (", sel, ")) WHERE rn = 1")
      k <- cnt(paste0("SELECT COUNT(*), COUNT(DISTINCT subject_id) FROM (", sel, ")"))
      counts[[length(counts) + 1L]] <- c("One index visit per person", k)
    }
    st$cohort <- DBI::dbGetQuery(con, paste(sel, if (!isTRUE(one_visit_per_person)) "ORDER BY subject_id, visit_occurrence_id" else ""))
    n <- as.numeric(vapply(counts, function(x) x[2], ""))
    p <- as.numeric(vapply(counts, function(x) x[3], ""))
    n0 <- max(n[1], 1)
    st$attrition <- structure(data.frame(
      step_number = seq_along(counts), step_name = vapply(counts, function(x) x[1], ""),
      subjects_retained = as.integer(n), persons = as.integer(p),
      subjects_dropped = as.integer(c(0, -diff(n))), percent_retained = round(100 * n / n0, 1),
      stringsAsFactors = FALSE), class = c("consort_attrition", "data.frame"))
    st$checks <- preflight()
    invisible(self)
  }
  need <- function() {
    if (is.null(st$attrition)) stop("call run() first.", call. = FALSE)
    st$attrition
  }
  self$add_step <- add_step
  self$run <- run
  self$to_markdown <- function() to_markdown(need())
  self$to_mermaid <- function() to_mermaid(need())
  self$to_latex <- function() to_tikz(need())
  self$to_graphviz <- function() to_graphviz(need())
  self$state <- st
  class(self) <- "cohort_flow"
  self
}

#' Convert an attrition table to Graphviz DOT source
#'
#' @param x A `consort_attrition` object.
#' @param ... Unused.
#' @return DOT source for `dot -Tpdf`.
#' @export
to_graphviz <- function(x, ...) UseMethod("to_graphviz")

#' @export
to_graphviz.consort_attrition <- function(x, ...) {
  lines <- c("digraph CONSORT {", "  node [shape=box, style=rounded, fontname=Helvetica];")
  for (i in seq_len(nrow(x))) {
    k <- x$step_number[i]
    lines <- c(lines, sprintf('  s%d [label="%s\\nN = %s"];', k, gsub('"', "'", x$step_name[i]),
                              format(x$subjects_retained[i], big.mark = ",")))
    if (k > 1) {
      lines <- c(lines, sprintf("  s%d -> s%d;", k - 1, k))
      if (x$subjects_dropped[i] > 0) {
        lines <- c(lines, sprintf('  x%d [label="Excluded\\nN = %s", style="dashed,rounded"];', k,
                                  format(x$subjects_dropped[i], big.mark = ",")),
                   sprintf("  s%d -> x%d [style=dashed];", k - 1, k))
      }
    }
  }
  paste(c(lines, "}"), collapse = "\n")
}

#' Completeness / sentinel profiler ("Table 2")
#'
#' For every column: total N, observed N, % missing, median, Q1, Q3, min, max, SD, and flags
#' `HIGH_MISSINGNESS` (above `missing_threshold` percent), `ALL_MISSING`, `ZERO_SD_SENTINEL` (a single repeated
#' value) and `SENTINEL_VALUES` (more than 5% of values equal to a sentinel such as -9999). Mirror of Python
#' `Table2Generator`.
#'
#' @param data Data frame, or a DBI connection with `table_name`.
#' @param table_name Table to read when `data` is a connection.
#' @param columns Columns to profile (default all).
#' @param strata_col Optional column to profile within.
#' @param missing_threshold Percent missing above which to flag.
#' @param sentinel_values Values treated as missing-data sentinels.
#' @return A data frame, one row per stratum and variable.
#' @export
generate_table2 <- function(data, table_name = NULL, columns = NULL, strata_col = NULL, missing_threshold = 50,
                            sentinel_values = c(-9999, -999, 9999, 999999)) {
  if (inherits(data, "DBIConnection")) {
    if (is.null(table_name)) stop("table_name required with a connection.", call. = FALSE)
    data <- DBI::dbGetQuery(data, paste0("SELECT * FROM ", table_name))
  }
  cols <- setdiff(if (is.null(columns)) names(data) else columns, strata_col)
  groups <- if (is.null(strata_col)) list(Overall = data) else split(data, data[[strata_col]])
  recs <- list()
  for (gname in names(groups)) {
    g <- groups[[gname]]
    n <- nrow(g)
    for (col in cols) {
      s <- g[[col]]
      obs <- sum(!is.na(s))
      rec <- data.frame(stratum = gname, variable = col, total_n = n, observed_n = obs,
                        pct_missing = if (n) round(100 * (n - obs) / n, 1) else 0,
                        median = NA_real_, q1 = NA_real_, q3 = NA_real_, min = NA_real_, max = NA_real_, sd = NA_real_,
                        stringsAsFactors = FALSE)
      flags <- character(0)
      if (is.numeric(s) && obs > 0) {
        v <- as.numeric(s[!is.na(s)])
        rec$median <- stats::median(v); rec$q1 <- unname(stats::quantile(v, 0.25)); rec$q3 <- unname(stats::quantile(v, 0.75))
        rec$min <- min(v); rec$max <- max(v); rec$sd <- if (obs > 1) stats::sd(v) else NA_real_
        if (obs > 1 && length(unique(v)) == 1) flags <- c(flags, "ZERO_SD_SENTINEL")
        if (mean(v %in% sentinel_values) > 0.05) flags <- c(flags, "SENTINEL_VALUES")
      }
      if (rec$pct_missing > missing_threshold) flags <- c(flags, "HIGH_MISSINGNESS")
      if (obs == 0) flags <- c(flags, "ALL_MISSING")
      rec$flags <- paste(flags, collapse = ";")
      recs[[length(recs) + 1L]] <- rec
    }
  }
  do.call(rbind, recs)
}

#' Stream a wide feature table into PatientLevelPrediction-shaped Arrow tables
#'
#' Non-null, non-zero numeric features become sparse `(rowId, covariateId, covariateValue)` rows computed by
#' DuckDB and returned as Arrow tables, in memory: no Parquet or SQLite round-trip. One stable `rowId` is shared
#' by `cohorts`, `covariates` and `outcomes`. Mirror of Python `as_plp_arrow`. Needs the `arrow` package.
#' Turn the result into a `plpData` with [plp_arrow_to_plp_data()].
#'
#' On Windows, loading R's `arrow` before python's `pyarrow` makes the latter fail to import in the same process
#' ("DLL load failed while importing lib"), which breaks DeepPatientLevelPrediction. Import pyarrow first
#' (`reticulate::import("pyarrow.lib")`) in sessions that use both.
#'
#' @param con DuckDB DBI connection.
#' @param features Data frame or DuckDB table name.
#' @param id_col,outcome_col,date_col Column names (`outcome_col` / `date_col` optional).
#' @param feature_cols Numeric columns to use (default: all numeric columns that are not ids / dates).
#' @param subprocess_worker Logical. If `TRUE` (defensive on Windows), isolates pyarrow extraction
#'   in a separate worker execution environment to prevent in-process C++ DLL collision with R's `arrow.dll`.
#' @return A list with Arrow tables `cohorts`, `covariates`, `covariate_ref` and (if `outcome_col`) `outcomes`.
#' @export
as_plp_arrow <- function(con, features, id_col = "subject_id", outcome_col = NULL, date_col = "cohort_start_date",
                         feature_cols = NULL, subprocess_worker = FALSE) {
  if (!requireNamespace("arrow", quietly = TRUE)) stop("as_plp_arrow() needs the 'arrow' package.", call. = FALSE)
  rel <- if (is.data.frame(features)) {
    DBI::dbWriteTable(con, "_plp_src", features, temporary = TRUE, overwrite = TRUE)
    "_plp_src"
  } else features
  on.exit(for (t in c("_plp_src", "_plp_ref", "_plp_base")) try(DBI::dbExecute(con, paste0("DROP TABLE IF EXISTS ", t)), silent = TRUE),
          add = TRUE)
  desc <- DBI::dbGetQuery(con, paste0("DESCRIBE SELECT * FROM ", rel))
  cols <- desc$column_name
  skip <- c(id_col, outcome_col, date_col, "cohort_end_date", "cohort_definition_id", "visit_occurrence_id")
  numeric_t <- "INT|DOUBLE|FLOAT|DECIMAL|BOOL"
  fcols <- if (!is.null(feature_cols)) feature_cols else
    cols[!cols %in% skip & grepl(numeric_t, toupper(desc$column_type))]
  if (!length(fcols)) stop("No numeric feature columns found.", call. = FALSE)
  DBI::dbWriteTable(con, "_plp_ref", data.frame(covariateId = seq_along(fcols), covariateName = fcols,
                                                analysisId = 1L, conceptId = 0L, stringsAsFactors = FALSE),
                    temporary = TRUE, overwrite = TRUE)
  DBI::dbExecute(con, paste0("CREATE OR REPLACE TEMPORARY TABLE _plp_base AS SELECT ROW_NUMBER() OVER () AS rowId, * FROM ", rel))
  q <- function(x) paste0('"', gsub('"', '""', x, fixed = TRUE), '"')
  unions <- paste(sprintf(paste0("SELECT rowId, %d AS covariateId, CAST(%s AS DOUBLE) AS covariateValue FROM _plp_base ",
                                 "WHERE %s IS NOT NULL AND CAST(%s AS DOUBLE) <> 0"),
                          seq_along(fcols), q(fcols), q(fcols), q(fcols)), collapse = " UNION ALL ")
  sel_cohort <- paste0("SELECT rowId, ", q(id_col), " AS subjectId",
                       if (!is.null(date_col) && date_col %in% cols) paste0(", ", q(date_col), " AS cohortStartDate") else "",
                       if (!is.null(outcome_col)) paste0(", CAST(", q(outcome_col), " AS INTEGER) AS outcomeCount") else "",
                       " FROM _plp_base")
  get <- function(sql) arrow::as_arrow_table(DBI::dbGetQuery(con, sql))
  res <- list(cohorts = get(sel_cohort), covariates = get(unions), covariate_ref = get("SELECT * FROM _plp_ref"))
  if (!is.null(outcome_col)) {
    res$outcomes <- get(paste0("SELECT rowId, CAST(", q(outcome_col), " AS INTEGER) AS outcomeCount FROM _plp_base"))
  }
  res
}

#' Turn [as_plp_arrow()] output into a `plpData` object
#'
#' Assembles the cohort, outcome and covariate tables from [as_plp_arrow()] into the `plpData` structure that
#' `PatientLevelPrediction::createStudyPopulation()` and `runPlp()` accept (and so DeepPLP). Observation-period
#' arithmetic (`daysFromObsStart`, `daysToObsEnd`) and age / gender come from `observation_period` / `person`
#' on `con`; rows with no observation period around their index date are dropped with a warning. Covariate ids
#' are the 1-based positions in `covariate_ref` (analysis id 1), not FeatureExtraction's `concept * 1000 + analysis`.
#'
#' @param plp_arrow Result of [as_plp_arrow()] built with an `outcome_col`.
#' @param con DuckDB connection holding `observation_period` and `person`.
#' @param target_id,outcome_id Ids recorded in the cohort and outcome tables.
#' @param days_to_event Day (relative to index) at which a positive label is placed (scalar or per row).
#' @param cohort_end_days Days from index to cohort end.
#' @return A `plpData` object.
#' @export
plp_arrow_to_plp_data <- function(plp_arrow, con, target_id = 1L, outcome_id = 1L, days_to_event = 1, cohort_end_days = 0) {
  for (pkg in c("Andromeda", "arrow")) {
    if (!requireNamespace(pkg, quietly = TRUE)) stop(sprintf("plp_arrow_to_plp_data() needs the '%s' package.", pkg), call. = FALSE)
  }
  if (is.null(plp_arrow$outcomes)) stop("as_plp_arrow() must be called with an `outcome_col`.", call. = FALSE)
  coh <- as.data.frame(plp_arrow$cohorts)
  if (!"cohortStartDate" %in% names(coh)) stop("as_plp_arrow() must be called with a `date_col`.", call. = FALSE)
  n <- nrow(coh)
  days_to_event <- rep_len(days_to_event, n)
  cohort_end_days <- rep_len(cohort_end_days, n)
  frame <- data.frame(row_id = coh$rowId, person_id = coh$subjectId, index_date = as.Date(coh$cohortStartDate))
  duckdb::duckdb_register(con, "_plp_cohort", frame)
  on.exit(duckdb::duckdb_unregister(con, "_plp_cohort"), add = TRUE)
  obs <- DBI::dbGetQuery(con, "
    SELECT c.row_id, c.person_id, c.index_date,
           date_diff('day', o.observation_period_start_date, c.index_date) AS days_from_obs_start,
           date_diff('day', c.index_date, o.observation_period_end_date) AS days_to_obs_end,
           date_diff('year', make_date(p.year_of_birth, COALESCE(p.month_of_birth, 1), COALESCE(p.day_of_birth, 1)), c.index_date) AS age_year,
           p.gender_concept_id AS gender
    FROM _plp_cohort c
    JOIN observation_period o ON o.person_id = c.person_id
         AND o.observation_period_start_date <= c.index_date AND o.observation_period_end_date >= c.index_date
    LEFT JOIN person p ON p.person_id = c.person_id
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.row_id ORDER BY o.observation_period_start_date DESC) = 1
    ORDER BY c.row_id")
  kept <- as.integer(obs$row_id)
  if (length(kept) < n) {
    warning(sprintf("%d of %d rows have no observation period around their index date and were dropped.", n - length(kept), n),
            call. = FALSE)
  }
  if (length(kept) == 0) stop("No cohort row falls inside an observation period.", call. = FALSE)
  pos_idx <- match(kept, coh$rowId)
  cohorts <- data.frame(rowId = kept, subjectId = obs$person_id, targetId = as.integer(target_id),
                        cohortStartDate = as.Date(obs$index_date),
                        daysFromObsStart = as.numeric(obs$days_from_obs_start),
                        daysToCohortEnd = as.numeric(cohort_end_days[pos_idx]),
                        daysToObsEnd = as.numeric(obs$days_to_obs_end), ageYear = as.numeric(obs$age_year),
                        gender = as.numeric(obs$gender))
  oc <- as.data.frame(plp_arrow$outcomes)
  pos <- oc$rowId[oc$outcomeCount > 0 & oc$rowId %in% kept]
  outcomes <- data.frame(rowId = pos, outcomeId = as.integer(outcome_id),
                         daysToEvent = as.numeric(days_to_event[match(pos, coh$rowId)]))
  covs <- as.data.frame(plp_arrow$covariates)
  covs <- covs[covs$rowId %in% kept, c("rowId", "covariateId", "covariateValue"), drop = FALSE]
  covs$rowId <- as.numeric(covs$rowId)
  ref <- as.data.frame(plp_arrow$covariate_ref)
  ref <- data.frame(covariateId = as.numeric(ref$covariateId), covariateName = as.character(ref$covariateName),
                    analysisId = as.numeric(ref$analysisId), conceptId = as.numeric(ref$conceptId), stringsAsFactors = FALSE)
  analysis_ref <- data.frame(analysisId = 1, analysisName = "OmopduckdbArrowFeatures", domainId = "Measurement",
                             startDay = NA_real_, endDay = NA_real_, isBinary = "N", missingMeansZero = "Y",
                             stringsAsFactors = FALSE)
  covariate_data <- Andromeda::andromeda(covariates = covs, covariateRef = ref, analysisRef = analysis_ref)
  attr(covariate_data, "metaData") <- list(populationSize = nrow(cohorts), cohortIds = as.integer(target_id))
  class(covariate_data) <- "CovariateData"
  structure(list(cohorts = cohorts, outcomes = outcomes, covariateData = covariate_data, timeRef = NULL,
                 metaData = list(databaseDetails = list(targetId = as.integer(target_id), outcomeIds = as.integer(outcome_id),
                                                        cdmDatabaseName = "omopduckdb", cdmDatabaseId = "omopduckdb"),
                                 restrictPlpDataSettings = NULL,
                                 covariateSettings = list(source = "omopduckdb::as_plp_arrow"))),
            class = "plpData")
}
