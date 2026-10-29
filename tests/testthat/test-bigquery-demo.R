demo_values <- list(cdm = "bigquery-public-data.cms_synthetic_patient_data_omop", scratch = "my-proj.scratch",
                    cohort_table = "demo_cohort", features_table = "demo_features", cohort_id = 1L)
# the shipped definition (inst/cohorts); tests that mock the compiler only need a file that exists
demo_repo_cohort_json <- file.path(cohort_definitions_dir(), "diabetes_phenotype.circe.json")
demo_cohort_json <- local({
  f <- tempfile(fileext = ".circe.json")
  writeLines("{}", f)
  f
})
demo_all_tables <- c("person", "visit_occurrence", "observation_period", "concept_ancestor", "condition_occurrence",
                     "drug_exposure", "measurement")

fake_runner <- function(tables = demo_all_tables, fail_availability = FALSE) {
  log <- new.env()
  log$sql <- character()
  list(
    log = log,
    execute = function(sql) log$sql <- c(log$sql, sql),
    ensure_dataset = function(dataset) log$dataset <- dataset,
    fetch = function(sql) {
      log$sql <- c(log$sql, sql)
      if (grepl("INFORMATION_SCHEMA", sql)) return(data.frame(table_name = tables))
      if (grepl("n_numeric_values", sql)) {
        if (fail_availability) stop("Not found: Table measurement")
        return(data.frame(n_measurement_rows = 9, n_numeric_values = 0, n_creatinine = 0, n_glucose = 0, n_albumin = 0,
                          n_systolic_bp = 0, n_diastolic_bp = 0))
      }
      data.frame(subject_id = 1:3, age_at_index = c(55, 61, 72), sex = c("Male", "Female", "Female"),
                 race = c("White", "Black or African American", "White"), prior_ip_os_ed_count = c(0, 2, 5),
                 readmission_count_twelve_months = c(0, 0, 1))
    }
  )
}

test_that("summary reports labs and vitals as unavailable, not as NaN, on claims data", {
  feats <- data.frame(age_at_index = c(60, 70), sex = c("Male", "Female"), race = c("White", "White"),
                      prior_ip_os_ed_count = c(3, 0), readmission_count_twelve_months = c(1, 0))
  avail <- data.frame(n_measurement_rows = 5, n_numeric_values = 0, n_creatinine = 0, n_glucose = 0, n_albumin = 0,
                      n_systolic_bp = 0, n_diastolic_bp = 0)
  s <- summarize_bigquery_demo(feats, avail)
  expect_equal(s$n_cohort, 2)
  expect_equal(s$utilization$prior_ip_os_ed_count$mean, 1.5)
  expect_true(all(vapply(s$clinical_telemetry, function(v) grepl("unavailable", v$status), logical(1))))
  expect_no_error(jsonlite::toJSON(s, auto_unbox = TRUE, null = "null"))
  empty <- summarize_bigquery_demo(feats[0, ], NULL)
  expect_equal(empty$n_cohort, 0)
  expect_null(empty$age_at_index$mean)
})

test_that("the feature and availability templates fill every placeholder", {
  for (f in c("bigquery_demo_features.sql", "bigquery_demo_availability.sql")) {
    sql <- omopduckdb:::.demo_template(f, demo_values)
    expect_false(grepl("{{", sql, fixed = TRUE))
    expect_true(grepl("`bigquery-public-data.cms_synthetic_patient_data_omop.", sql, fixed = TRUE))
    expect_false(grepl("omop`.", sql, fixed = TRUE))  # project.dataset is never backticked on its own
  }
  expect_error(omopduckdb:::.demo_template("bigquery_demo_features.sql", list(cdm = "x")), "placeholder")
})

test_that("run_bigquery_demo runs end to end against a fake runner and writes the disclaimer", {
  testthat::local_mocked_bindings(compile_cohort_bigquery = function(...) c("CREATE TABLE t", "INSERT INTO t SELECT 1"))
  for (fail in c(FALSE, TRUE)) {
    out <- withr::local_tempdir()
    r <- fake_runner(fail_availability = fail)
    s <- run_bigquery_demo("my-proj", out, cohort_json = demo_cohort_json, runner = r)
    expect_equal(s$n_cohort, 3)
    expect_equal(r$log$dataset, "my-proj.omop_duckdb_demo")
    expect_true(all(vapply(s$clinical_telemetry, function(v) grepl("unavailable", v$status), logical(1))))
    report <- paste(readLines(file.path(out, "demo_report.md")), collapse = "\n")
    expect_true(grepl("SYNTHETIC", report, fixed = TRUE))
    expect_true(file.exists(file.path(out, "demo_features.csv")))
  }
})

test_that("missing CDM tables and a placeholder billing project fail with clear messages", {
  testthat::local_mocked_bindings(compile_cohort_bigquery = function(...) "CREATE TABLE t")
  expect_error(run_bigquery_demo("my-proj", withr::local_tempdir(), cohort_json = demo_cohort_json, runner = fake_runner(setdiff(demo_all_tables, "concept_ancestor"))),
               "missing concept_ancestor")
  expect_error(run_bigquery_demo("REPLACE_WITH_YOUR_GCP_PROJECT_ID", withr::local_tempdir(), cohort_json = demo_cohort_json, dry_run = TRUE), "billing_project")
})

test_that("the diabetes definition compiles to GoogleSQL through CirceR + SqlRender", {
  skip_if_not_installed("CirceR")
  skip_if_not_installed("SqlRender")
  skip_if_not(file.exists(demo_repo_cohort_json))
  stmts <- compile_cohort_bigquery(demo_repo_cohort_json,
                                   "bigquery-public-data.cms_synthetic_patient_data_omop", "my-proj.scratch")
  sql <- paste(stmts, collapse = "\n")
  expect_true(grepl("`bigquery-public-data.cms_synthetic_patient_data_omop.person`", sql, fixed = TRUE))
  # untranslated placeholders and temp-table markers; CirceR's own SQL comments may contain a "#" ("POWER(2,# of inclusion rules)")
  expect_false(grepl("cdm_ph|vocab_ph|tgt_ph|#", gsub("--[^\n]*", "", sql)))
})

test_that("live BigQuery smoke test (opt in with OMOP_BQ_BILLING_PROJECT; bills that project)", {
  skip_if(!nzchar(Sys.getenv("OMOP_BQ_BILLING_PROJECT")), "set OMOP_BQ_BILLING_PROJECT to run against BigQuery")
  skip_if_not_installed("CirceR")
  skip_if_not_installed("bigrquery")
  s <- run_bigquery_demo(Sys.getenv("OMOP_BQ_BILLING_PROJECT"), withr::local_tempdir(), cohort_json = demo_repo_cohort_json)
  expect_gt(s$n_cohort, 0)
})

test_that("a dry run needs neither CirceR nor credentials", {
  testthat::local_mocked_bindings(requireNamespace = function(package, ...) FALSE, .package = "base")
  out <- withr::local_tempdir()
  expect_warning(run_bigquery_demo("my-proj", out, cohort_json = demo_cohort_json, dry_run = TRUE), "feature SQL only")
  expect_true(file.exists(file.path(out, "features.googlesql.sql")))
  expect_false(file.exists(file.path(out, "cohort.googlesql.sql")))
  expect_true(grepl("`my-proj.omop_duckdb_demo.demo_features`", paste(readLines(file.path(out, "features.googlesql.sql")), collapse = "
"), fixed = TRUE))
})

test_that("the default runner sends queries through bigrquery with the billing project", {
  skip_if_not_installed("bigrquery")
  calls <- list()
  testthat::local_mocked_bindings(
    bq_perform_query = function(query, billing, ...) { calls$perform <<- list(query = query, billing = billing); "job" },
    bq_job_wait = function(x, ...) { calls$waited <<- x; x },
    bq_project_query = function(x, query, ...) { calls$project <<- list(x = x, query = query); "tbl" },
    bq_table_download = function(x, ...) { force(x); tibble::tibble(a = 1L) },
    bq_dataset = function(project, dataset) list(project = project, dataset = dataset),
    bq_dataset_exists = function(x) FALSE,
    bq_dataset_create = function(x, ...) { calls$created <<- x; x },
    .package = "bigrquery"
  )
  r <- omopduckdb:::.bq_runner("my-proj")
  r$execute("SELECT 1")
  expect_equal(calls$perform$billing, "my-proj")
  expect_equal(calls$waited, "job")
  expect_equal(r$fetch("SELECT 2"), data.frame(a = 1L))
  expect_equal(calls$project$x, "my-proj")
  r$ensure_dataset("my-proj.scratch")
  expect_equal(calls$created, list(project = "my-proj", dataset = "scratch"))
})
