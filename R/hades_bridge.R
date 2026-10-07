#' Check that a DuckDB file is ready for OHDSI HADES (PatientLevelPrediction / DeepPatientLevelPrediction)
#'
#' PatientLevelPrediction, DeepPatientLevelPrediction and FeatureExtraction read the CDM through
#' DatabaseConnector, which opens the DuckDB file on its own and **cannot `ATTACH` anything on connect**.
#' So everything HADES queries has to live inside the file. This runs the checks against a *fresh
#' connection to the file with nothing attached*, i.e. exactly what DatabaseConnector will see, which is
#' why a vocabulary that is only reachable through an attached `central_vocab` catalog is reported as a
#' failure here even though it works in your own session.
#'
#' Checks: required CDM tables exist, `person` / `observation_period` are populated, the vocabulary
#' (`concept`) is readable from inside the file, and the cohort table has the OHDSI cohort columns and
#' rows.
#'
#' DatabaseConnector opens the file **read-write**, which cannot coexist with *another process* holding it,
#' even read-only (for example a Python session that still has the database open). Connections within
#' this R session never conflict. A check therefore opens the file read-write, as DatabaseConnector does,
#' and reports who is in the way instead of letting `getPlpData()` fail later with an IO error.
#'
#' @param db_path Path to the DuckDB database file.
#' @param cdm_database_schema Schema holding the CDM tables (default `"main"`).
#' @param cohort_database_schema Schema holding the cohort table (default: the CDM schema).
#' @param cohort_table Cohort table name in OHDSI cohort format (default `"cohort"`).
#' @return A data.frame with columns `check`, `ok`, `detail` and class `hades_preflight`; the attribute
#'   `all_ok` is `TRUE` when every check passed.
#' @export
hades_preflight <- function(db_path,
                            cdm_database_schema = "main",
                            cohort_database_schema = cdm_database_schema,
                            cohort_table = "cohort") {
  if (!file.exists(db_path)) stop(sprintf("DuckDB file not found: %s", db_path), call. = FALSE)
  checks <- list()
  add <- function(check, ok, detail) {
    checks[[length(checks) + 1L]] <<- data.frame(check = check, ok = isTRUE(ok), detail = detail, stringsAsFactors = FALSE)
  }

  # How DatabaseConnector will open the file: read-write. Fails if another process holds the database.
  rw <- tryCatch(DBI::dbConnect(duckdb::duckdb(), dbdir = db_path), error = function(e) e)
  if (inherits(rw, "error")) {
    add("file can be opened read-write (as DatabaseConnector does)", FALSE,
        paste0("another process (for example a Python session with the database open, even read-only) probably holds ",
               "the file; close it before using PatientLevelPrediction. DuckDB said: ",
               substr(gsub("\\s+", " ", conditionMessage(rw)), 1, 120)))
  } else {
    DBI::dbDisconnect(rw, shutdown = TRUE)
    add("file can be opened read-write (as DatabaseConnector does)", TRUE, "no other process holds it")
  }

  # The remaining checks read through a fresh read-only connection with nothing attached.
  con <- tryCatch(
    DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE),
    error = function(e) stop(sprintf("Could not open '%s' read-only: %s", db_path, conditionMessage(e)), call. = FALSE)
  )
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  count_rows <- function(schema, table) {
    tryCatch(as.numeric(DBI::dbGetQuery(con, sprintf("SELECT COUNT(*) AS n FROM %s.%s", schema, table))$n),
             error = function(e) NA_real_)
  }

  for (tbl in c("person", "observation_period", "visit_occurrence", "condition_occurrence", "drug_exposure",
                "procedure_occurrence", "measurement")) {
    n <- count_rows(cdm_database_schema, tbl)
    add(sprintf("table %s", tbl), !is.na(n), if (is.na(n)) "missing or unreadable" else sprintf("%.0f rows", n))
  }
  for (tbl in c("person", "observation_period")) {
    n <- count_rows(cdm_database_schema, tbl)
    if (!is.na(n)) add(sprintf("%s populated", tbl), n > 0, sprintf("%.0f rows", n))
  }

  n_concept <- count_rows(cdm_database_schema, "concept")
  add("vocabulary readable inside the file", !is.na(n_concept) && n_concept > 0,
      if (is.na(n_concept)) {
        paste0("`concept` is missing or unreadable without ATTACH (a view over an attached central_vocab?). ",
               "DatabaseConnector cannot attach it; materialise the vocabulary tables the analyses need into this file.")
      } else if (n_concept == 0) {
        "`concept` is empty: covariate names will be 'Unknown concept' and ancestor-based analyses return nothing"
      } else {
        sprintf("%.0f concepts", n_concept)
      })

  cohort_cols <- tryCatch(
    DBI::dbGetQuery(con, sprintf("DESCRIBE SELECT * FROM %s.%s", cohort_database_schema, cohort_table))$column_name,
    error = function(e) NULL)
  need <- c("cohort_definition_id", "subject_id", "cohort_start_date", "cohort_end_date")
  if (is.null(cohort_cols)) {
    add(sprintf("cohort table %s", cohort_table), FALSE, "missing or unreadable")
  } else {
    missing_cols <- setdiff(need, tolower(cohort_cols))
    n <- count_rows(cohort_database_schema, cohort_table)
    add(sprintf("cohort table %s", cohort_table), length(missing_cols) == 0 && n > 0,
        if (length(missing_cols) > 0) sprintf("missing columns: %s", paste(missing_cols, collapse = ", "))
        else sprintf("%.0f rows", n))
  }

  out <- do.call(rbind, checks)
  attr(out, "all_ok") <- all(out$ok)
  class(out) <- c("hades_preflight", "data.frame")
  out
}

#' @export
print.hades_preflight <- function(x, ...) {
  df <- as.data.frame(unclass(x), stringsAsFactors = FALSE)
  attr(df, "all_ok") <- NULL
  cat(sprintf("HADES preflight: %s\n", if (isTRUE(attr(x, "all_ok"))) "all checks passed" else "PROBLEMS FOUND"))
  for (i in seq_len(nrow(df))) cat(sprintf("  [%s] %s: %s\n", if (df$ok[i]) "ok" else "!!", df$check[i], df$detail[i]))
  invisible(x)
}

#' PatientLevelPrediction database details for an omop-duck-db DuckDB file
#'
#' Wires DuckDB defaults into [PatientLevelPrediction::createDatabaseDetails()]: DatabaseConnector's native
#' DuckDB connection to `db_path`, the `main` schema, and the standard OHDSI `cohort` table that
#' [create_cohort()], [build_readmission_cohort()] and friends write. The result works with
#' `PatientLevelPrediction::getPlpData()` and `runPlp()`, and with DeepPatientLevelPrediction models
#' (including its temporal transformer, whose input is `FeatureExtraction::createTemporalSequenceCovariateSettings()`).
#'
#' By default the file is first checked with [hades_preflight()] and an error is raised if it is not
#' HADES-ready (most importantly: vocabulary tables must be inside the file).
#'
#' @param db_path Path to the DuckDB database file. No other process may hold it (see [hades_preflight()]).
#' @param target_id Target cohort definition id.
#' @param outcome_ids Outcome cohort definition id(s).
#' @param cdm_database_schema,cohort_database_schema,outcome_database_schema Schemas (default `"main"`).
#' @param cohort_table,outcome_table Cohort tables (default `"cohort"`).
#' @param cdm_database_id Identifier recorded with results; default is `cdm_source_abbreviation` if present,
#'   otherwise the file name.
#' @param preflight Run [hades_preflight()] first and stop on failure (default `TRUE`).
#' @return A `databaseDetails` object from PatientLevelPrediction.
#' @export
plp_database_details <- function(db_path,
                                 target_id,
                                 outcome_ids,
                                 cdm_database_schema = "main",
                                 cohort_database_schema = cdm_database_schema,
                                 cohort_table = "cohort",
                                 outcome_database_schema = cohort_database_schema,
                                 outcome_table = cohort_table,
                                 cdm_database_id = NULL,
                                 preflight = TRUE) {
  for (pkg in c("DatabaseConnector", "PatientLevelPrediction")) {
    if (!requireNamespace(pkg, quietly = TRUE)) stop(sprintf("plp_database_details() needs the '%s' package.", pkg), call. = FALSE)
  }
  if (isTRUE(preflight)) {
    pf <- hades_preflight(db_path, cdm_database_schema, cohort_database_schema, cohort_table)
    if (!isTRUE(attr(pf, "all_ok"))) {
      bad <- pf[!pf$ok, ]
      stop(sprintf("'%s' is not ready for HADES:\n%s\n(Use preflight = FALSE to skip this check.)", db_path,
                   paste(sprintf("  - %s: %s", bad$check, bad$detail), collapse = "\n")), call. = FALSE)
    }
  }
  if (is.null(cdm_database_id)) {
    cdm_database_id <- tools::file_path_sans_ext(basename(db_path))
    con <- tryCatch(DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE), error = function(e) NULL)
    if (!is.null(con)) {
      abbr <- tryCatch(DBI::dbGetQuery(con, "SELECT cdm_source_abbreviation FROM cdm_source LIMIT 1")[[1]],
                       error = function(e) NULL)
      DBI::dbDisconnect(con, shutdown = TRUE)
      if (length(abbr) == 1 && !is.na(abbr) && nzchar(abbr)) cdm_database_id <- abbr
    }
  }
  PatientLevelPrediction::createDatabaseDetails(
    connectionDetails = DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path),
    cdmDatabaseSchema = cdm_database_schema,
    cdmDatabaseName = cdm_database_id,
    cdmDatabaseId = cdm_database_id,
    cohortDatabaseSchema = cohort_database_schema,
    cohortTable = cohort_table,
    outcomeDatabaseSchema = outcome_database_schema,
    outcomeTable = outcome_table,
    targetId = target_id,
    outcomeIds = outcome_ids
  )
}

#' CohortMethod database connection details for an omop-duck-db DuckDB file
#'
#' Provides native DuckDB DatabaseConnector details and schemas for OHDSI CohortMethod:
#' `DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path)`.
#'
#' @param db_path Path to the DuckDB database file.
#' @param cdm_database_schema Schema holding the CDM tables (default `"main"`).
#' @param cohort_database_schema Schema holding the cohort table (default: the CDM schema).
#' @param cohort_table Cohort table name in OHDSI format (default `"cohort"`).
#' @param preflight Run [hades_preflight()] first and stop on failure (default `TRUE`).
#' @return A list with `connectionDetails`, `cdmDatabaseSchema`, `cohortDatabaseSchema`, `cohortTable`.
#' @export
cm_database_details <- function(db_path,
                                cdm_database_schema = "main",
                                cohort_database_schema = cdm_database_schema,
                                cohort_table = "cohort",
                                preflight = TRUE) {
  if (!requireNamespace("DatabaseConnector", quietly = TRUE)) {
    stop("cm_database_details() needs the 'DatabaseConnector' package.", call. = FALSE)
  }
  if (isTRUE(preflight)) {
    pf <- hades_preflight(db_path, cdm_database_schema, cohort_database_schema, cohort_table)
    if (!isTRUE(attr(pf, "all_ok"))) {
      bad <- pf[!pf$ok, ]
      stop(sprintf("'%s' is not ready for HADES:\n%s\n(Use preflight = FALSE to skip this check.)", db_path,
                   paste(sprintf("  - %s: %s", bad$check, bad$detail), collapse = "\n")), call. = FALSE)
    }
  }
  list(
    connectionDetails = DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path),
    cdmDatabaseSchema = cdm_database_schema,
    cohortDatabaseSchema = cohort_database_schema,
    cohortTable = cohort_table
  )
}


# FeatureExtraction analysis ids for the long-term occurrence analyses; reusing them makes covariate ids
# (concept_id * 1000 + analysis_id) identical to natively extracted ones when the window matches.
.plp_analysis <- list(
  condition = list(id = 102, name = "ConditionOccurrence", domain = "Condition", table = "condition_occurrence"),
  drug = list(id = 302, name = "DrugExposure", domain = "Drug", table = "drug_exposure"),
  procedure = list(id = 502, name = "ProcedureOccurrence", domain = "Procedure", table = "procedure_occurrence")
)

#' Turn a sparse concept matrix into a PatientLevelPrediction `plpData`
#'
#' Bridges [extract_sparse_concept_matrix()] to PatientLevelPrediction and DeepPatientLevelPrediction: the
#' same matrix you would feed to glmnet or scikit-learn (and, through Python, omop-learn) becomes a
#' `plpData` object that `createStudyPopulation()`, `runPlp()` and the DeepPLP models accept. Rows keep the
#' parquet's order (`rowId` is the 1-based row position).
#'
#' Covariate ids follow the OHDSI convention `concept_id * 1000 + analysis_id`, using FeatureExtraction's
#' long-term analysis ids (condition 102, drug 302, procedure 502), so they coincide with natively extracted
#' covariates when the window is 365 days. Rows with no observation period around their index date are
#' dropped (PatientLevelPrediction requires one), with a warning.
#'
#' This produces the *non-temporal* covariate layout (patient x covariate). Temporal sequence models
#' (DeepPLP's temporal transformer) take `FeatureExtraction::createTemporalSequenceCovariateSettings()`
#' through [plp_database_details()] instead.
#'
#' @param sparse_result A result of [extract_sparse_concept_matrix()] that includes an outcome (`y`).
#' @param con DuckDB connection holding `observation_period` and `person` (used for `daysFromObsStart`,
#'   `daysToObsEnd`, age and gender).
#' @param target_id Cohort definition id recorded as `targetId`.
#' @param outcome_id Outcome id recorded in `outcomes`.
#' @param days_to_event Day (relative to index) at which a positive label is placed: a scalar, or one value
#'   per row. Binary labels carry no timing, so the default `1` puts events inside PLP's default risk window
#'   (day 1 to 365); pass real times if you have them.
#' @param cohort_end_days Days from index to cohort end (scalar or per row; default 0).
#' @return A `plpData` object (`cohorts`, `outcomes`, `covariateData`, `timeRef`, `metaData`).
#' @export
as_plp_data <- function(sparse_result, con, target_id = 1L, outcome_id = 1L, days_to_event = 1, cohort_end_days = 0) {
  for (pkg in c("Andromeda", "Matrix")) {
    if (!requireNamespace(pkg, quietly = TRUE)) stop(sprintf("as_plp_data() needs the '%s' package.", pkg), call. = FALSE)
  }
  if (!inherits(con, "duckdb_connection")) stop("`con` must be a DuckDB connection (duckdb::duckdb()).", call. = FALSE)
  y <- sparse_result$y
  if (is.null(y)) stop("sparse_result has no outcome (`y`); extract it from a cohort parquet with an outcome column.", call. = FALSE)
  X <- sparse_result$X
  cohort <- sparse_result$cohort
  n <- nrow(X)
  params <- sparse_result$params
  days_to_event <- rep_len(days_to_event, n)
  cohort_end_days <- rep_len(cohort_end_days, n)

  # observation period around each index date (+ age / gender), via a registered copy of the cohort
  frame <- data.frame(row_id = seq_len(n), person_id = cohort$person_id, index_date = cohort$index_date)
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

  cohorts <- data.frame(
    rowId = kept,
    subjectId = obs$person_id,
    targetId = as.integer(target_id),
    cohortStartDate = as.Date(obs$index_date),
    daysFromObsStart = as.numeric(obs$days_from_obs_start),
    daysToCohortEnd = as.numeric(cohort_end_days[kept]),
    daysToObsEnd = as.numeric(obs$days_to_obs_end),
    ageYear = as.numeric(obs$age_year),
    gender = as.numeric(obs$gender)
  )

  pos <- kept[as.numeric(y[kept]) == 1]
  outcomes <- data.frame(rowId = pos, outcomeId = as.integer(outcome_id), daysToEvent = as.numeric(days_to_event[pos]))

  # covariates: one record per nonzero cell, id = concept_id * 1000 + analysis_id(domain)
  concepts <- sparse_result$concepts
  analysis_id <- vapply(concepts$domain, function(d) .plp_analysis[[d]]$id, numeric(1))
  covariate_id <- concepts$concept_id * 1000 + analysis_id
  trip <- Matrix::summary(X)
  keep_cell <- trip$i %in% kept
  covariates <- data.frame(rowId = as.numeric(trip$i[keep_cell]),
                           covariateId = covariate_id[trip$j[keep_cell]],
                           covariateValue = as.numeric(trip$x[keep_cell]))

  lookback <- params$lookback_days
  window <- if (is.null(lookback)) "all prior days" else sprintf("day -%d through %d days relative to index", lookback,
                                                                  if (isTRUE(params$include_index_date)) 0L else -1L)
  used <- unique(concepts$domain)
  has_name <- !is.na(concepts$concept_name) & nzchar(concepts$concept_name)
  covariate_ref <- data.frame(
    covariateId = covariate_id,
    covariateName = sprintf("%s during %s: %s", vapply(concepts$domain, function(d) .plp_analysis[[d]]$table, character(1)),
                            window, ifelse(has_name, concepts$concept_name, "Unknown concept")),
    analysisId = analysis_id,
    conceptId = concepts$concept_id,
    stringsAsFactors = FALSE
  )
  # FeatureExtraction's own names when the window is its 365-day, index-exclusive long-term one
  standard <- identical(lookback, 365) && !isTRUE(params$include_index_date) && identical(params$value, "binary")
  analysis_ref <- data.frame(
    analysisId = vapply(used, function(d) .plp_analysis[[d]]$id, numeric(1)),
    analysisName = vapply(used, function(d) paste0(.plp_analysis[[d]]$name, if (standard) "LongTerm" else "Window"), character(1)),
    domainId = vapply(used, function(d) .plp_analysis[[d]]$domain, character(1)),
    startDay = if (is.null(lookback)) NA_real_ else -as.numeric(lookback),
    endDay = if (isTRUE(params$include_index_date)) 0 else -1,
    isBinary = if (identical(params$value, "binary")) "Y" else "N",
    missingMeansZero = NA_character_,
    stringsAsFactors = FALSE
  )

  # Same construction FeatureExtraction uses for its own CovariateData objects.
  covariate_data <- Andromeda::andromeda(covariates = covariates, covariateRef = covariate_ref, analysisRef = analysis_ref)
  attr(covariate_data, "metaData") <- list(populationSize = nrow(cohorts), cohortIds = as.integer(target_id))
  class(covariate_data) <- "CovariateData"

  structure(
    list(
      cohorts = cohorts,
      outcomes = outcomes,
      covariateData = covariate_data,
      timeRef = NULL,
      metaData = list(
        databaseDetails = list(targetId = as.integer(target_id), outcomeIds = as.integer(outcome_id),
                               cdmDatabaseName = "omopduckdb", cdmDatabaseId = "omopduckdb"),
        restrictPlpDataSettings = NULL,
        covariateSettings = list(source = "omopduckdb::extract_sparse_concept_matrix", params = params)
      )
    ),
    class = "plpData"
  )
}
