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
    st$cohort_sql <- sel
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
  stream_to_parquet <- function(output_path, partition_by = NULL) {
    if (is.null(st$cohort)) run()
    out_dir <- dirname(output_path)
    if (!dir.exists(out_dir)) dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
    part_clause <- ""
    if (!is.null(partition_by) && length(partition_by) > 0) {
      part_clause <- sprintf(", PARTITION_BY (%s)", paste(partition_by, collapse = ", "))
    }
    sql_path <- gsub("\\\\", "/", output_path)
    if (!is.null(st$cohort_sql) && nzchar(st$cohort_sql)) {
      clean_sql <- trimws(st$cohort_sql)
      clean_sql <- sub(";*$", "", clean_sql)
      DBI::dbExecute(con, sprintf("COPY (%s) TO '%s' (FORMAT PARQUET%s)", clean_sql, sql_path, part_clause))
    } else {
      duckdb::duckdb_register(con, "_flow_stream_tmp", st$cohort)
      DBI::dbExecute(con, sprintf("COPY _flow_stream_tmp TO '%s' (FORMAT PARQUET%s)", sql_path, part_clause))
      duckdb::duckdb_unregister(con, "_flow_stream_tmp")
    }
    output_path
  }
  to_arrow_dataset <- function() {
    if (is.null(st$cohort)) run()
    if (requireNamespace("arrow", quietly = TRUE)) {
      return(arrow::as_arrow_table(st$cohort))
    }
    st$cohort
  }
  self$add_step <- add_step
  self$run <- run
  self$stream_to_parquet <- stream_to_parquet
  self$to_arrow_dataset <- to_arrow_dataset
  self$to_markdown <- function() to_markdown(need())
  self$to_mermaid <- function() to_mermaid(need())
  self$to_latex <- function() to_tikz(need())
  self$to_graphviz <- function() to_graphviz(need())
  self$visit_concept_ids <- visit_concept_ids
  self$anchor <- anchor
  self$follow_up_days <- follow_up_days
  self$one_visit_per_person <- one_visit_per_person
  self$to_circe_json <- function(cohort_name = "Consortium Cohort", cohort_description = NULL, output_path = NULL) {
    flow_obj <- list(
      visit_concept_ids = visit_concept_ids,
      anchor = anchor,
      follow_up_days = follow_up_days,
      one_visit_per_person = one_visit_per_person,
      steps = st$steps
    )
    export_to_circe_json(flow_obj, cohort_name = cohort_name, cohort_description = cohort_description, output_path = output_path)
  }
  self$state <- st
  class(self) <- "cohort_flow"
  self
}

#' @export
stream_to_parquet <- function(x, output_path, partition_by = NULL, ...) UseMethod("stream_to_parquet")

#' @export
stream_to_parquet.cohort_flow <- function(x, output_path, partition_by = NULL, ...) {
  x$stream_to_parquet(output_path, partition_by = partition_by)
}

#' @export
to_arrow_dataset <- function(x, ...) UseMethod("to_arrow_dataset")

#' @export
to_arrow_dataset.cohort_flow <- function(x, ...) {
  x$to_arrow_dataset()
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

#' Plot Visual Cohort Attrition Flow (CONSORT / Sankey)
#'
#' @description
#' Renders cohort attrition flow diagrams in standalone SVG or interactive Plotly Sankey structure.
#'
#' @param attrition A data.frame, list, or attrition summary object.
#' @param format Output format: 'svg' (vector diagram) or 'plotly' (interactive list).
#' @param title Diagram title.
#' @param output_path Optional file path to write SVG output.
#'
#' @return SVG string (for 'svg') or list structure (for 'plotly').
#' @export
plot_consort_flow <- function(attrition,
                              format = c("svg", "plotly"),
                              title = "Cohort Attrition Flow (CONSORT)",
                              output_path = NULL) {
  format <- match.arg(format)

  if (is.data.frame(attrition)) {
    df <- as.data.frame(attrition)
  } else if (is.list(attrition) && "summary_table" %in% names(attrition)) {
    df <- as.data.frame(attrition$summary_table)
  } else if (is.list(attrition)) {
    df <- as.data.frame(attrition)
  } else {
    stop("`attrition` must be a data.frame or attrition list.")
  }

  col_map <- c(
    "step" = "step_number",
    "step_num" = "step_number",
    "name" = "step_name",
    "criteria" = "step_name",
    "n_subjects" = "subjects_retained",
    "retained" = "subjects_retained",
    "count" = "subjects_retained",
    "n_dropped" = "subjects_dropped",
    "dropped" = "subjects_dropped",
    "excluded" = "subjects_dropped",
    "pct_retained" = "percent_retained",
    "reason" = "exclusion_reason"
  )
  for (orig in names(df)) {
    low <- tolower(orig)
    if (low %in% names(col_map) && !col_map[[low]] %in% names(df)) {
      df[[col_map[[low]]]] <- df[[orig]]
    }
  }

  if (!"step_number" %in% names(df)) {
    df$step_number <- seq_len(nrow(df))
  }
  if (!"step_name" %in% names(df)) {
    df$step_name <- paste("Step", df$step_number)
  }
  if (!"subjects_retained" %in% names(df)) {
    stop("Attrition data must have a retained subject count column.")
  }

  df$subjects_retained <- as.integer(df$subjects_retained)

  if (!"subjects_dropped" %in% names(df)) {
    ret <- df$subjects_retained
    drop_vec <- c(0L)
    if (length(ret) > 1) {
      for (i in 2:length(ret)) {
        drop_vec <- c(drop_vec, max(0L, ret[i - 1] - ret[i]))
      }
    }
    df$subjects_dropped <- drop_vec
  } else {
    df$subjects_dropped <- as.integer(ifelse(is.na(df$subjects_dropped), 0L, df$subjects_dropped))
  }

  if (!"percent_retained" %in% names(df)) {
    first_n <- if (nrow(df) > 0) max(1, df$subjects_retained[1]) else 1
    df$percent_retained <- round((df$subjects_retained / first_n) * 100.0, 1)
  }

  if (!"exclusion_reason" %in% names(df)) {
    df$exclusion_reason <- "Criteria not met"
  }

  if (format == "plotly") {
    node_labels <- character(0)
    node_colors <- character(0)
    sources <- integer(0)
    targets <- integer(0)
    values <- numeric(0)
    link_colors <- character(0)

    step_node_indices <- integer(nrow(df))
    for (i in seq_len(nrow(df))) {
      s_num <- df$step_number[i]
      s_name <- df$step_name[i]
      ret <- df$subjects_retained[i]
      pct <- df$percent_retained[i]
      lbl <- sprintf("Step %d: %s<br>N = %s (%.1f%%)", s_num, s_name, format(ret, big.mark = ","), pct)
      node_idx <- length(node_labels)
      step_node_indices[i] <- node_idx
      node_labels <- c(node_labels, lbl)
      node_colors <- c(node_colors, "#3182ce")
    }

    if (nrow(df) > 1) {
      for (i in 2:nrow(df)) {
        prev_node <- step_node_indices[i - 1]
        curr_node <- step_node_indices[i]
        curr_ret <- df$subjects_retained[i]
        dropped <- df$subjects_dropped[i]
        reason <- df$exclusion_reason[i]

        if (curr_ret > 0) {
          sources <- c(sources, prev_node)
          targets <- c(targets, curr_node)
          values <- c(values, curr_ret)
          link_colors <- c(link_colors, "rgba(49, 130, 206, 0.4)")
        }

        if (dropped > 0) {
          excl_node <- length(node_labels)
          node_labels <- c(node_labels, sprintf("Excluded: %s<br>N = %s", reason, format(dropped, big.mark = ",")))
          node_colors <- c(node_colors, "#e53e3e")

          sources <- c(sources, prev_node)
          targets <- c(targets, excl_node)
          values <- c(values, dropped)
          link_colors <- c(link_colors, "rgba(229, 62, 62, 0.4)")
        }
      }
    }

    res <- list(
      data = list(list(
        type = "sankey",
        orientation = "h",
        node = list(
          pad = 15,
          thickness = 20,
          line = list(color = "#2d3748", width = 0.5),
          label = node_labels,
          color = node_colors
        ),
        link = list(
          source = sources,
          target = targets,
          value = values,
          color = link_colors
        )
      )),
      layout = list(
        title = list(text = title, font = list(family = "sans-serif", size = 16)),
        font = list(family = "sans-serif", size = 12),
        margin = list(l = 20, r = 20, t = 50, b = 20)
      )
    )
    return(res)
  }

  # SVG format
  n_steps <- nrow(df)
  width <- 750
  height <- max(400, n_steps * 130 + 80)
  box_w <- 340
  box_h <- 60
  margin_top <- 70
  center_x <- 220
  excl_x <- 520
  excl_w <- 200

  svg_elements <- c(
    sprintf('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" style="background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">', width, height, width, height),
    sprintf('<text x="%f" y="38" text-anchor="middle" font-size="17" font-weight="600" fill="#1a202c">%s</text>', width / 2, title)
  )

  for (i in seq_len(nrow(df))) {
    y <- margin_top + (i - 1) * 130
    idx <- df$step_number[i]
    name <- df$step_name[i]
    ret <- df$subjects_retained[i]
    pct <- df$percent_retained[i]
    dropped <- df$subjects_dropped[i]
    reason <- df$exclusion_reason[i]

    svg_elements <- c(
      svg_elements,
      sprintf('<rect x="%f" y="%d" width="%d" height="%d" rx="6" fill="#ebf8ff" stroke="#3182ce" stroke-width="1.5" />', center_x - box_w / 2, y, box_w, box_h),
      sprintf('<text x="%d" y="%d" text-anchor="middle" font-size="13" font-weight="600" fill="#2b6cb0">%d. %s</text>', center_x, y + 24, idx, name),
      sprintf('<text x="%d" y="%d" text-anchor="middle" font-size="12" fill="#4a5568">N = %s (%.1f%% retained)</text>', center_x, y + 45, format(ret, big.mark = ","), pct)
    )

    if (i > 1) {
      prev_y <- margin_top + (i - 2) * 130 + box_h
      svg_elements <- c(
        svg_elements,
        sprintf('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#4a5568" stroke-width="2" />', center_x, prev_y, center_x, y),
        sprintf('<polygon points="%d,%d %d,%d %d,%d" fill="#4a5568" />', center_x - 4, y - 6, center_x + 4, y - 6, center_x, y)
      )

      if (dropped > 0) {
        mid_y <- (prev_y + y) / 2
        excl_box_h <- 44
        svg_elements <- c(
          svg_elements,
          sprintf('<line x1="%d" y1="%f" x2="%d" y2="%f" stroke="#e53e3e" stroke-width="1.5" stroke-dasharray="3,3" />', center_x, mid_y, excl_x, mid_y),
          sprintf('<polygon points="%d,%f %d,%f %d,%f" fill="#e53e3e" />', excl_x - 4, mid_y - 3, excl_x - 4, mid_y + 3, excl_x + 2, mid_y),
          sprintf('<rect x="%d" y="%f" width="%d" height="%d" rx="4" fill="#fff5f5" stroke="#feb2b2" stroke-width="1" stroke-dasharray="3,3" />', excl_x, mid_y - excl_box_h / 2, excl_w, excl_box_h),
          sprintf('<text x="%d" y="%f" font-size="11" font-weight="600" fill="#c53030">Excluded (N = %s)</text>', excl_x + 10, mid_y - 4, format(dropped, big.mark = ",")),
          sprintf('<text x="%d" y="%f" font-size="10" fill="#742a2a">%s</text>', excl_x + 10, mid_y + 12, substr(reason, 1, 30))
        )
      }
    }
  }

  svg_elements <- c(svg_elements, "</svg>")
  svg_content <- paste(svg_elements, collapse = "\n")

  if (!is.null(output_path)) {
    writeLines(svg_content, output_path)
  }
  svg_content
}

#' Generate Multi-Site Attrition Heatmap
#'
#' @description
#' Generates an SVG matrix heatmap comparing attrition retention rates across consortium sites.
#'
#' @param attrition_df Data frame containing site, step, and retention percent columns.
#' @param site_col Name of column identifying site (default "site").
#' @param step_col Name of column identifying step name (default "step_name").
#' @param retention_col Name of column identifying retention percent (default "percent_retained").
#' @param title Chart title.
#' @param output_path Optional file path to write SVG output.
#'
#' @return Standalone SVG heatmap string.
#' @export
plot_multisite_attrition_heatmap <- function(attrition_df,
                                            site_col = "site",
                                            step_col = "step_name",
                                            retention_col = "percent_retained",
                                            title = "Multi-Site Attrition Profile (% Retained)",
                                            output_path = NULL) {
  df <- as.data.frame(attrition_df)
  for (c_name in c(site_col, step_col, retention_col)) {
    if (!c_name %in% names(df)) {
      match_col <- names(df)[tolower(names(df)) == tolower(c_name)]
      if (length(match_col) > 0) {
        df[[c_name]] <- df[[match_col[1]]]
      } else {
        stop(sprintf("Column '%s' not found in attrition_df.", c_name))
      }
    }
  }

  sites <- sort(unique(as.character(df[[site_col]])))
  steps <- unique(as.character(df[[step_col]]))

  n_sites <- length(sites)
  n_steps <- length(steps)

  cell_w <- 120
  cell_h <- 36
  margin_l <- 150
  margin_t <- 80
  margin_b <- 60
  margin_r = 50

  width <- margin_l + n_steps * cell_w + margin_r
  height <- margin_t + n_sites * cell_h + margin_b

  get_color <- function(val) {
    if (val >= 90.0) {
      list(bg = "#dcfce7", fg = "#166534")
    } else if (val >= 75.0) {
      list(bg = "#fef9c3", fg = "#854d0e")
    } else if (val >= 50.0) {
      list(bg = "#fed7aa", fg = "#9a3412")
    } else {
      list(bg = "#fee2e2", fg = "#991b1b")
    }
  }

  svg_elements <- c(
    sprintf('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" style="background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">', width, height, width, height),
    sprintf('<text x="%f" y="35" text-anchor="middle" font-size="16" font-weight="600" fill="#1e293b">%s</text>', width / 2, title)
  )

  for (j in seq_along(steps)) {
    x <- margin_l + (j - 1) * cell_w + cell_w / 2
    step_label <- substr(steps[j], 1, 16)
    svg_elements <- c(
      svg_elements,
      sprintf('<text x="%f" y="%d" text-anchor="middle" font-size="11" font-weight="600" fill="#475569">%s</text>', x, margin_t - 15, step_label)
    )
  }

  for (i in seq_along(sites)) {
    y <- margin_t + (i - 1) * cell_h
    site_name <- sites[i]
    svg_elements <- c(
      svg_elements,
      sprintf('<text x="%d" y="%f" text-anchor="end" font-size="12" font-weight="500" fill="#334155">%s</text>', margin_l - 15, y + cell_h / 2 + 4, site_name)
    )

    for (j in seq_along(steps)) {
      x <- margin_l + (j - 1) * cell_w
      step_name <- steps[j]
      sub_row <- df[df[[site_col]] == site_name & df[[step_col]] == step_name, , drop = FALSE]
      if (nrow(sub_row) > 0) {
        val <- as.numeric(sub_row[[retention_col]][1])
        txt <- sprintf("%.1f%%", val)
      } else {
        val <- 0.0
        txt <- "N/A"
      }

      clr <- get_color(val)
      svg_elements <- c(
        svg_elements,
        sprintf('<rect x="%d" y="%d" width="%d" height="%d" rx="4" fill="%s" stroke="#e2e8f0" stroke-width="1" />', x + 2, y + 2, cell_w - 4, cell_h - 4, clr$bg),
        sprintf('<text x="%f" y="%f" text-anchor="middle" font-size="12" font-weight="600" fill="%s">%s</text>', x + cell_w / 2, y + cell_h / 2 + 4, clr$fg, txt)
      )
    }
  }

  leg_y <- height - 25
  legend_items <- list(
    list(lbl = ">= 90% (High Retention)", bg = "#dcfce7", fg = "#166534"),
    list(lbl = "75-89%", bg = "#fef9c3", fg = "#854d0e"),
    list(lbl = "50-74%", bg = "#fed7aa", fg = "#9a3412"),
    list(lbl = "< 50% (Disproportionate Drop)", bg = "#fee2e2", fg = "#991b1b")
  )
  cur_x <- margin_l
  for (item in legend_items) {
    svg_elements <- c(
      svg_elements,
      sprintf('<rect x="%d" y="%d" width="16" height="16" rx="3" fill="%s" stroke="#cbd5e1" />', cur_x, leg_y - 12, item$bg),
      sprintf('<text x="%d" y="%d" font-size="11" fill="#475569">%s</text>', cur_x + 22, leg_y, item$lbl)
    )
    cur_x <- cur_x + 160
  }

  svg_elements <- c(svg_elements, "</svg>")
  svg_content <- paste(svg_elements, collapse = "\n")

  if (!is.null(output_path)) {
    writeLines(svg_content, output_path)
  }
  svg_content
}
