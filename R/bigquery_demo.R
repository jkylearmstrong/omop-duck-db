#' Disclaimer printed on every BigQuery demonstration report
#' @format A character string.
#' @export
BIGQUERY_DEMO_DISCLAIMER <- paste(
  "These results come from CMS DE-SynPUF, a SYNTHETIC claims dataset. They verify that the software runs (cohort SQL,",
  "dialect translation, feature extraction). They say nothing about real-world clinical performance or human",
  "epidemiology, and SynPUF has no laboratory values or vital signs, so only the administrative utilization",
  "sub-model can be exercised."
)

#' Note printed beside the utilization features in the BigQuery demonstration report
#' @format A character string.
#' @export
BIGQUERY_DEMO_UTILIZATION_NOTE <- paste(
  "Utilization is counted before the cohort entry date, which is each patient's FIRST qualifying diabetes event (not a",
  "sampled hospitalization). In claims data most patients have few or no earlier inpatient stays, so",
  "readmission_count_twelve_months is expected to be zero for most of the cohort."
)

.DEMO_PUBLIC_DATASET <- "bigquery-public-data.cms_synthetic_patient_data_omop"
.DEMO_UTILIZATION <- c("prior_ip_os_ed_count", "readmission_count_twelve_months")
.DEMO_TELEMETRY <- c(serum_creatinine = "n_creatinine", serum_glucose = "n_glucose", serum_albumin = "n_albumin",
                     systolic_bp = "n_systolic_bp", diastolic_bp = "n_diastolic_bp")

.demo_dataset <- function(x, what) {
  bits <- strsplit(x, ".", fixed = TRUE)[[1]]
  if (length(bits) != 2L || !grepl("^[A-Za-z0-9][A-Za-z0-9._:-]*$", bits[[1]]) || !grepl("^[A-Za-z_][A-Za-z0-9_]*$", bits[[2]])) {
    stop(what, " must be 'project.dataset', got '", x, "'", call. = FALSE)
  }
  x  # templates add the table and ONE pair of backticks around the whole project.dataset.table
}

.demo_template <- function(file, values) {
  path <- system.file("sql", file, package = "omopduckdb")
  if (!nzchar(path)) path <- file.path(getwd(), "inst", "sql", file)
  sql <- paste(readLines(path, warn = FALSE), collapse = "\n")
  for (nm in names(values)) sql <- gsub(paste0("{{", nm, "}}"), as.character(values[[nm]]), sql, fixed = TRUE)
  left <- regmatches(sql, regexpr("\\{\\{\\w+\\}\\}", sql))
  if (length(left)) stop("no value for SQL placeholder ", left, call. = FALSE)
  sql
}

#' Compile a Circe cohort definition to BigQuery (GoogleSQL) statements
#'
#' Uses CirceR to build the cohort query and SqlRender to translate it to the `bigquery` dialect, exactly as
#' CohortGenerator would, but returns the statements instead of running them. The target cohort table is created
#' first because Circe only inserts into it.
#'
#' @param circe_json Circe JSON string or file path.
#' @param cdm,scratch,vocabulary `"project.dataset"` strings: the OMOP CDM, a dataset you may write to, and where
#'   `concept_ancestor` lives (default `cdm`).
#' @param cohort_table,cohort_id Target cohort table (in `scratch`) and cohort definition id.
#' @return A character vector of GoogleSQL statements, to run in order.
#' @export
compile_cohort_bigquery <- function(circe_json, cdm, scratch, cohort_table = "demo_cohort", cohort_id = 1L,
                                    vocabulary = NULL) {
  for (pkg in c("CirceR", "SqlRender")) {
    if (!requireNamespace(pkg, quietly = TRUE)) stop("compile_cohort_bigquery() needs the ", pkg, " package.", call. = FALSE)
  }
  if (!grepl("^[A-Za-z_][A-Za-z0-9_]*$", cohort_table)) stop("cohort_table must be a plain identifier.", call. = FALSE)
  q <- c(cdm_ph = .demo_dataset(cdm, "cdm"), vocab_ph = .demo_dataset(vocabulary %||% cdm, "vocabulary"),
         tgt_ph = .demo_dataset(scratch, "scratch"))
  json <- if (file.exists(circe_json)) paste(readLines(circe_json, warn = FALSE), collapse = "\n") else circe_json
  sql <- CirceR::buildCohortQuery(CirceR::cohortExpressionFromJson(json),
                                  CirceR::createGenerateOptions(generateStats = FALSE))
  sql <- SqlRender::render(sql, cdm_database_schema = "cdm_ph", vocabulary_database_schema = "vocab_ph",
                           target_database_schema = "tgt_ph", results_database_schema = "tgt_ph",
                           target_cohort_table = cohort_table, target_cohort_id = as.integer(cohort_id))
  sql <- SqlRender::translate(sql, targetDialect = "bigquery", tempEmulationSchema = "tgt_ph")
  stmts <- SqlRender::splitSql(sql)
  stmts <- vapply(stmts, function(s) {
    for (ph in names(q)) s <- gsub(paste0("\\b", ph, "\\.(\\w+)"), paste0("`", q[[ph]], ".\\1`"), s)
    s
  }, character(1), USE.NAMES = FALSE)
  create <- sprintf("CREATE OR REPLACE TABLE `%s.%s` (cohort_definition_id INT64, subject_id INT64, cohort_start_date DATE, cohort_end_date DATE)",
                    q[["tgt_ph"]], cohort_table)
  c(create, stmts)
}

#' Summarise the BigQuery demonstration features
#'
#' Cohort size, demographics, prior-year utilization and lab / vital-sign availability. Safe on an empty cohort and on
#' data without labs: unavailable telemetry is reported as 100% missing, never imputed.
#'
#' @param features Data frame from the features query (`age_at_index`, `sex`, `race`, utilization columns).
#' @param availability One-row data frame from the availability query, or `NULL` (no measurement table).
#' @return A list.
#' @export
summarize_bigquery_demo <- function(features, availability = NULL) {
  n <- nrow(features)
  tab <- function(col) {
    if (!n || !col %in% names(features)) return(list())
    t <- sort(table(features[[col]]), decreasing = TRUE)
    lapply(stats::setNames(as.list(as.integer(t)), names(t)), function(k) list(n = k, pct = round(100 * k / n, 1)))
  }
  num <- function(col) {
    x <- if (col %in% names(features)) suppressWarnings(as.numeric(features[[col]])) else numeric()
    x <- x[!is.na(x)]
    if (!length(x)) return(list(mean = NULL, sd = NULL, median = NULL))
    list(mean = round(mean(x), 2), sd = if (length(x) > 1) round(stats::sd(x), 2) else NULL, median = stats::median(x))
  }
  have <- function(col) {
    v <- if (!is.null(availability) && nrow(availability) && col %in% names(availability)) availability[[col]][[1]] else 0
    if (is.na(v)) 0 else as.numeric(v)
  }
  list(
    n_cohort = n, age_at_index = num("age_at_index"), sex = tab("sex"), race = tab("race"),
    utilization = stats::setNames(lapply(.DEMO_UTILIZATION, num), .DEMO_UTILIZATION),
    clinical_telemetry = lapply(.DEMO_TELEMETRY, function(col) {
      h <- have(col)
      list(n_numeric_values = h, status = if (h > 0) "available" else "unavailable in this data (100% missing, not imputed)")
    }),
    numeric_measurement_values_in_cohort = have("n_numeric_values"),
    model_scope = paste("administrative utilization sub-model:", paste(.DEMO_UTILIZATION, collapse = ", "))
  )
}

.demo_report <- function(summary, run) {
  f <- function(x) if (is.null(x)) "NA" else format(x)
  out <- c("# BigQuery SynPUF demonstration report", "", paste0("> **", BIGQUERY_DEMO_DISCLAIMER, "**"), "",
           "## Run", "", paste0("- Billing project: `", run$billing_project, "`"), paste0("- Source: `", run$dataset, "`"),
           paste0("- Cohort: `", run$cohort_json, "` (cohort id ", run$cohort_id, ")"), "")
  if (isTRUE(run$dry_run)) return(paste(c(out, "Dry run: SQL was compiled and written; nothing was executed on BigQuery.", ""), collapse = "\n"))
  rows <- function(d) vapply(names(d), function(k) sprintf("| %s | %d (%s%%) |", k, d[[k]]$n, d[[k]]$pct), "")
  age <- summary$age_at_index
  out <- c(out, "| Step | Seconds |", "|---|---|", sprintf("| %s | %s |", names(run$timings_seconds), unlist(run$timings_seconds)), "",
           "## Cohort", "", paste0("- Patients: **", summary$n_cohort, "**"),
           paste0("- Age at index: mean ", f(age$mean), ", SD ", f(age$sd), ", median ", f(age$median)), "",
           "| Sex | n (%) |", "|---|---|", rows(summary$sex), "", "| Race | n (%) |", "|---|---|", rows(summary$race), "",
           "## Prior-year utilization (administrative sub-model)", "", paste0("_", BIGQUERY_DEMO_UTILIZATION_NOTE, "_"), "", "| Feature | Mean | SD | Median |", "|---|---|---|---|",
           sprintf("| %s | %s | %s | %s |", names(summary$utilization),
                   vapply(summary$utilization, function(v) f(v$mean), ""), vapply(summary$utilization, function(v) f(v$sd), ""),
                   vapply(summary$utilization, function(v) f(v$median), "")), "",
           "## Clinical telemetry", "", "| Feature | Numeric values in cohort | Status |", "|---|---|---|",
           sprintf("| %s | %s | %s |", names(summary$clinical_telemetry),
                   vapply(summary$clinical_telemetry, function(v) f(v$n_numeric_values), ""),
                   vapply(summary$clinical_telemetry, function(v) v$status, "")), "",
           paste0("Scope: ", summary$model_scope, "."))
  paste(c(out, ""), collapse = "\n")
}

.bq_runner <- function(billing_project) {
  if (!requireNamespace("bigrquery", quietly = TRUE)) stop("run_bigquery_demo() needs the bigrquery package.", call. = FALSE)
  list(
    execute = function(sql) bigrquery::bq_job_wait(bigrquery::bq_perform_query(sql, billing = billing_project, quiet = TRUE), quiet = TRUE),
    fetch = function(sql) as.data.frame(bigrquery::bq_table_download(
      bigrquery::bq_project_query(billing_project, sql, quiet = TRUE), bigint = "integer", quiet = TRUE)),
    ensure_dataset = function(dataset) {
      ds <- bigrquery::bq_dataset(strsplit(dataset, ".", fixed = TRUE)[[1]][[1]], strsplit(dataset, ".", fixed = TRUE)[[1]][[2]])
      if (!bigrquery::bq_dataset_exists(ds)) bigrquery::bq_dataset_create(ds)
    }
  )
}

#' Build the diabetes cohort on the public CMS SynPUF OMOP dataset in BigQuery
#'
#' A software demonstration and smoke test: compiles `cohorts/diabetes_phenotype.circe.json` to GoogleSQL
#' (CirceR + SqlRender), builds the cohort and baseline features (age, sex, race, prior-year utilization) in a scratch
#' dataset of **your** project, and writes `demo_report.md`, `demo_summary.json`, `demo_features.csv` and the SQL to
#' `output_dir`. The data are synthetic claims with no labs or vital signs; those features are reported as 100%
#' missing and never imputed. Results say nothing about clinical performance (see [BIGQUERY_DEMO_DISCLAIMER]).
#'
#' Reading the public dataset needs only a project to bill; the run also needs permission to run BigQuery jobs and
#' create a dataset in `billing_project` (or a `scratch_dataset` you already own). Authenticate once with
#' `bigrquery::bq_auth()`.
#'
#' @param billing_project Your GCP project id. Defaults to `billing_project` in `config/bigquery.yml`.
#' @param output_dir Where the report and SQL are written.
#' @param dataset Source CDM, `"project.dataset"`.
#' @param scratch_dataset A writable `"project.dataset"` (default `<billing_project>.omop_duckdb_demo`).
#' @param vocabulary_dataset Where `concept_ancestor` lives if not in `dataset`.
#' @param cohort_json Circe JSON; defaults to `cohorts/diabetes_phenotype.circe.json` relative to the working directory (run from the repository root).
#' @param cohort_id Cohort definition id.
#' @param dry_run `TRUE` compiles and writes the SQL only (no BigQuery call, no credentials).
#' @param runner For tests: a list with `execute(sql)`, `fetch(sql)` and `ensure_dataset(dataset)`.
#' @return Invisibly, the summary list.
#' @export
run_bigquery_demo <- function(billing_project = NULL, output_dir = "bigquery_demo_out", dataset = NULL,
                              scratch_dataset = NULL, vocabulary_dataset = NULL, cohort_json = NULL,
                              cohort_id = 1L, dry_run = FALSE, runner = NULL) {
  cfg <- read_local_config("bigquery.yml")
  billing_project <- billing_project %||% cfg$billing_project
  dataset <- dataset %||% cfg$dataset %||% .DEMO_PUBLIC_DATASET
  if (is.null(billing_project) || !grepl("^[A-Za-z0-9][A-Za-z0-9._:-]*$", billing_project) ||
      billing_project == "REPLACE_WITH_YOUR_GCP_PROJECT_ID") {
    stop("Provide billing_project (your GCP project id) or set it in config/bigquery.yml", call. = FALSE)
  }
  cohort_json <- cohort_json %||% file.path("cohorts", "diabetes_phenotype.circe.json")
  if (!file.exists(cohort_json)) stop("cohort definition not found: ", cohort_json, call. = FALSE)
  scratch <- scratch_dataset %||% paste0(billing_project, ".omop_duckdb_demo")
  dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)
  cohort_table <- "demo_cohort"
  timings <- list()
  timed <- function(name, expr) {
    t0 <- proc.time()[["elapsed"]]
    on.exit(timings[[name]] <<- round(proc.time()[["elapsed"]] - t0, 2))
    force(expr)
  }

  # a dry run must not need CirceR/Java/credentials: without CirceR it still writes the feature SQL and says so
  can_compile <- requireNamespace("CirceR", quietly = TRUE) && requireNamespace("SqlRender", quietly = TRUE)
  if (dry_run && !can_compile) {
    warning("CirceR and/or SqlRender are not installed: the dry run writes the feature SQL only, not the cohort SQL.",
            call. = FALSE)
    cohort_sql <- NULL
  } else {
    cohort_sql <- timed("compile_cohort_to_googlesql",
                        compile_cohort_bigquery(cohort_json, dataset, scratch, cohort_table, cohort_id, vocabulary_dataset))
  }
  values <- list(cdm = .demo_dataset(dataset, "dataset"), scratch = .demo_dataset(scratch, "scratch"),
                 cohort_table = cohort_table, features_table = "demo_features", cohort_id = as.integer(cohort_id))
  features_sql <- .demo_template("bigquery_demo_features.sql", values)
  avail_sql <- .demo_template("bigquery_demo_availability.sql", values)
  if (!is.null(cohort_sql)) writeLines(paste0(cohort_sql, ";"), file.path(output_dir, "cohort.googlesql.sql"))
  writeLines(paste0(features_sql, ";"), file.path(output_dir, "features.googlesql.sql"))
  writeLines(paste0(avail_sql, ";"), file.path(output_dir, "availability.googlesql.sql"))

  run <- list(billing_project = billing_project, dataset = dataset, scratch_dataset = scratch,
              cohort_json = basename(cohort_json), cohort_id = cohort_id, dry_run = dry_run)
  if (dry_run) {
    writeLines(.demo_report(list(), run), file.path(output_dir, "demo_report.md"))
    return(invisible(list(disclaimer = BIGQUERY_DEMO_DISCLAIMER, run = run)))
  }

  runner <- runner %||% .bq_runner(billing_project)
  parts <- strsplit(dataset, ".", fixed = TRUE)[[1]]
  present <- timed("preflight", runner$fetch(sprintf("SELECT table_name FROM `%s.%s.INFORMATION_SCHEMA.TABLES`", parts[[1]], parts[[2]])))$table_name
  needed <- c("person", "visit_occurrence", "observation_period", "condition_occurrence", "drug_exposure", "measurement",
              if (is.null(vocabulary_dataset)) "concept_ancestor")
  missing <- setdiff(needed, present)
  if (length(missing)) {
    stop(dataset, " is missing ", paste(missing, collapse = ", "), " (found: ", paste(sort(present), collapse = ", "),
         "); pass vocabulary_dataset= if concept_ancestor lives elsewhere", call. = FALSE)
  }
  timed("create_scratch_dataset", runner$ensure_dataset(scratch))
  timed("build_cohort", for (stmt in cohort_sql) runner$execute(stmt))
  timed("extract_features", runner$execute(features_sql))
  features <- timed("download_features", runner$fetch(sprintf("SELECT * FROM `%s.demo_features`", .demo_dataset(scratch, "scratch"))))
  availability <- tryCatch(timed("lab_availability", runner$fetch(avail_sql)), error = function(e) {
    run$availability_note <<- paste0("availability query failed (", class(e)[[1]], "); treated as no numeric values")
    NULL
  })
  run$timings_seconds <- timings
  summary <- summarize_bigquery_demo(features, availability)
  summary$disclaimer <- BIGQUERY_DEMO_DISCLAIMER
  summary$run <- run
  jsonlite::write_json(summary, file.path(output_dir, "demo_summary.json"), auto_unbox = TRUE, pretty = TRUE, null = "null")
  utils::write.csv(features, file.path(output_dir, "demo_features.csv"), row.names = FALSE)
  writeLines(.demo_report(summary, run), file.path(output_dir, "demo_report.md"))
  invisible(summary)
}
