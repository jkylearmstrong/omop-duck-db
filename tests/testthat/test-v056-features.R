test_that("export_plp_study_package generates complete R study package structure", {
  tmp_dir <- tempfile("plp_test_")
  dir.create(tmp_dir, recursive = TRUE)
  on.exit(unlink(tmp_dir, recursive = TRUE), add = TRUE)

  pkg_path <- export_plp_study_package(
    output_dir = tmp_dir,
    package_name = "ReadmissionPredictor",
    target_id = 1L,
    outcome_id = 2L,
    db_path = "omop_test.duckdb",
    model_type = "lasso",
    cv_folds = 5L,
    test_fraction = 0.25
  )

  expect_true(dir.exists(pkg_path))
  expect_true(file.exists(file.path(pkg_path, "DESCRIPTION")))
  expect_true(file.exists(file.path(pkg_path, "NAMESPACE")))
  expect_true(file.exists(file.path(pkg_path, "R", "Main.R")))
  expect_true(file.exists(file.path(pkg_path, "R", "ShinyApp.R")))
  expect_true(file.exists(file.path(pkg_path, "inst", "settings", "study_specs.json")))
  expect_true(file.exists(file.path(pkg_path, "inst", "shiny", "app.R")))
  expect_true(file.exists(file.path(pkg_path, "inst", "sql", "cohorts.sql")))
  expect_true(file.exists(file.path(pkg_path, "README.md")))

  main_lines <- readLines(file.path(pkg_path, "R", "Main.R"))
  expect_true(any(grepl("PatientLevelPrediction::runPlp", main_lines)))
  expect_true(any(grepl("setLassoLogisticRegression", main_lines)))

  specs <- jsonlite::fromJSON(file.path(pkg_path, "inst", "settings", "study_specs.json"))
  expect_equal(specs$packageName, "ReadmissionPredictor")
  expect_equal(specs$targetCohortId, 1L)
  expect_equal(specs$outcomeCohortId, 2L)
  expect_equal(specs$modelType, "lasso")
})

test_that("duckdb_to_andromeda and as_andromeda stream tables to Andromeda", {
  skip_if_not_installed("Andromeda")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE covariates AS 
    SELECT 1 AS row_id, 201826001 AS covariate_id, 1.0 AS covariate_value
    UNION ALL
    SELECT 2 AS row_id, 316139001 AS covariate_id, 1.0 AS covariate_value;

    CREATE TABLE covariateref AS
    SELECT 201826001 AS covariate_id, 'Type 2 Diabetes' AS covariate_name, 102 AS analysis_id, 201826 AS concept_id;

    CREATE TABLE analysisref AS
    SELECT 102 AS analysis_id, 'ConditionOccurrence' AS analysis_name, 'Condition' AS domain_id, 'Y' AS is_binary;
  ")

  andr <- duckdb_to_andromeda(con, table_names = c("covariates", "covariateref", "analysisref"))
  # Close Andromeda's own DuckDB connection before the test's connection is shut down (runs first).
  on.exit(close_plp(andr), add = TRUE, after = FALSE)
  expect_true(inherits(andr, "Andromeda") || inherits(andr, "environment") || is.list(andr))

  # Test table existence in Andromeda
  tbls <- tolower(names(andr))
  expect_true("covariates" %in% tbls)
  expect_true("covariateref" %in% tbls)
  expect_true("analysisref" %in% tbls)
})

test_that("to_typst and to_tikz generate valid manuscript diagram formats", {
  df <- data.frame(
    step_number = c(1L, 2L, 3L),
    step_name = c("Initial Inpatient Admissions", "Adults Age >= 18", "Verified Follow-up"),
    subjects_retained = c(1000L, 850L, 720L),
    subjects_dropped = c(0L, 150L, 130L),
    percent_retained = c(100.0, 85.0, 72.0),
    exclusion_reason = c("Baseline", "Pediatric age < 18", "Lost to follow-up"),
    stringsAsFactors = FALSE
  )
  attrition <- structure(df, class = c("consort_attrition", "data.frame"))

  # 1. Typst
  typst_str <- to_typst(attrition)
  expect_true(grepl("#align\\(center\\)", typst_str))
  expect_true(grepl("#rect", typst_str))
  expect_true(grepl("Step 1:", typst_str))
  expect_true(grepl("Excluded", typst_str))

  # 2. TikZ
  tikz_str <- to_tikz(attrition)
  expect_true(grepl("\\\\begin\\{tikzpicture\\}", tikz_str))
  expect_true(grepl("\\\\node \\[box\\]", tikz_str))
  expect_true(grepl("\\\\node \\[exclude,", tikz_str))
  expect_true(grepl("\\\\end\\{tikzpicture\\}", tikz_str))
})

test_that("extract_clinical_sequence_tokens exports HuggingFace-compatible clinical tokens", {
  db_path <- tempfile(fileext = ".duckdb")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  build_schema(db_path = db_path)
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES (1, 8507, 1960, 8527, 38003564), (2, 8532, 1955, 8527, 38003564);

    INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
    VALUES (1, 1, DATE '2019-01-01', DATE '2022-12-31', 32817), (2, 2, DATE '2019-01-01', DATE '2022-12-31', 32817);

    INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
    VALUES (201826, 'Type 2 diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', DATE '1970-01-01', DATE '2099-12-31');

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
    VALUES (101, 1, 9201, DATE '2020-05-01', DATE '2020-05-05', 32817), (102, 2, 9201, DATE '2020-06-01', DATE '2020-06-10', 32817);

    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, visit_occurrence_id)
    VALUES (1, 1, 201826, DATE '2020-01-10', 32817, 101);
  ")

  pq_path <- tempfile(fileext = ".parquet")
  on.exit(unlink(pq_path), add = TRUE)
  cohort_df <- data.frame(
    person_id = c(1, 2),
    index_date = as.Date(c("2020-05-01", "2020-06-01")),
    y = c(1, 0)
  )
  norm_pq <- gsub("\\\\", "/", pq_path)
  duckdb::duckdb_register(con, "_test_cohort", cohort_df)
  DBI::dbExecute(con, sprintf("COPY _test_cohort TO '%s' (FORMAT PARQUET);", norm_pq))
  duckdb::duckdb_unregister(con, "_test_cohort")

  tokens <- extract_clinical_sequence_tokens(con, pq_path, max_seq_len = 16)
  expect_equal(dim(tokens$input_ids), c(2, 16))
  expect_equal(dim(tokens$attention_mask), c(2, 16))
  expect_equal(dim(tokens$token_type_ids), c(2, 16))
  expect_equal(tokens$input_ids[, 1], c(2L, 2L)) # CLS token
  expect_equal(tokens$attention_mask[, 1], c(1L, 1L))
})

test_that("federated_meta_analysis calculates DerSimonian-Laird pooled estimates and heterogeneity", {
  estimates <- c(0.45, 0.38, 0.52, 0.40, 0.49)
  std_errors <- c(0.08, 0.10, 0.09, 0.07, 0.11)
  sites <- c("Temple", "PSU", "UPMC", "Hopkins", "Geisinger")

  res <- federated_meta_analysis(
    estimates = estimates,
    std_errors = std_errors,
    sites = sites,
    effect_type = "log_or",
    model = "random"
  )

  expect_s3_class(res, "federated_meta_analysis")
  expect_equal(res$k_studies, 5L)
  expect_true(res$pooled_estimate > 0.38 && res$pooled_estimate < 0.52)
  expect_true(res$se > 0)
  expect_true(res$ci_lower < res$pooled_estimate && res$ci_upper > res$pooled_estimate)
  expect_true(res$q_statistic >= 0)
  expect_true(res$i_squared >= 0)
  expect_true(res$tau_squared >= 0)

  df <- as.data.frame(res)
  expect_equal(nrow(df), 6L) # 5 sites + 1 pooled
  expect_true("Pooled (Random)" %in% df$site)

  mermaid_txt <- to_mermaid(res)
  expect_true(grepl("flowchart TD", mermaid_txt))
  expect_true(grepl("POOLED", mermaid_txt))
})

test_that("duckdb_to_andromeda copies every row across chunks and keeps empty tables (SQLite extension independent)", {
  skip_if_not_installed("Andromeda")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  DBI::dbExecute(con, "CREATE TABLE covariates AS SELECT i AS row_id, 1 AS covariate_id FROM range(25) t(i)")
  DBI::dbExecute(con, "CREATE TABLE analysisref AS SELECT 1 AS analysis_id WHERE false")

  andr <- duckdb_to_andromeda(con, table_names = c("covariates", "analysisref"), chunk_size = 10L)
  on.exit(close_plp(andr), add = TRUE, after = FALSE)
  expect_setequal(names(andr), c("covariates", "analysisref"))
  expect_equal(nrow(dplyr::collect(andr$covariates)), 25L)
  expect_equal(names(dplyr::collect(andr$analysisref)), "analysis_id")
  expect_error(duckdb_to_andromeda(con, chunk_size = 0), "chunk_size")
})

test_that("duckdb_to_andromeda survives a full copy, teardown and exit in its own R process", {
  skip_if_not_installed("Andromeda")
  skip_if_not_installed("callr")
  # Run in a child process: on the GitHub Windows runner the R process hosting the whole suite exited without a message
  # once the in-process Andromeda tests wrote tables (intermittent, cause unknown). A child that dies fails this test
  # with a message instead of ending the suite, and shows whether the adapter itself is at fault.
  out <- tryCatch(
    callr::r(function() {
      library(omopduckdb)
      con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
      DBI::dbExecute(con, "CREATE TABLE covariates AS SELECT i AS row_id, 1 AS covariate_id FROM range(25) t(i)")
      DBI::dbExecute(con, "CREATE TABLE analysisref AS SELECT 1 AS analysis_id WHERE false")
      andr <- duckdb_to_andromeda(con, table_names = c("covariates", "analysisref"), chunk_size = 10L)
      res <- list(tables = sort(names(andr)), rows = nrow(dplyr::collect(andr$covariates)),
                  cols = names(dplyr::collect(andr$analysisref)))
      Andromeda::close(andr)
      DBI::dbDisconnect(con, shutdown = TRUE)
      res
    }, show = FALSE),
    error = function(e) e)
  expect_false(inherits(out, "error"), info = if (inherits(out, "error")) conditionMessage(out) else "")
  if (!inherits(out, "error")) {
    expect_equal(out$tables, c("analysisref", "covariates"))
    expect_equal(out$rows, 25L)
    expect_equal(out$cols, "analysis_id")
  }
})
