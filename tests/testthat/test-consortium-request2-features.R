# Tests covering the 6 modular forward-looking features from tasks/request2.txt:
# 1. Secondary Cell Suppression (audit_cell_suppression with secondary_suppression = TRUE)
# 2. L2 Regularized Multivariable Odds Ratios & Table 5 (consortium_multivariable_risk_factors)
# 3. Pure DuckDB OHDSI Cohort Diagnostics (consortium_cohort_diagnostics)
# 4. Internal-External Cross-Validation (IECV) Matrix (consortium_cross_site_validation)
# 5. Bidirectional OHDSI Circe JSON Exporter (export_to_circe_json / cohort_flow$to_circe_json)
# 6. End-to-End Manuscript Scaffold Builder (build_manuscript_scaffold)

testthat::test_that("Feature 1: secondary cell suppression masks complementary cell to prevent algebraic disclosure", {
  df <- data.frame(
    stratum = c("Group A", "Group B", "Total"),
    n_count = c(5L, 45L, 50L),
    n_formatted = c("5 (10.0%)", "45 (90.0%)", "50 (100.0%)"),
    stringsAsFactors = FALSE
  )

  # Primary suppression only
  res_pri <- audit_cell_suppression(df, min_cell_count = 11L, secondary_suppression = FALSE, return_audit = TRUE)
  testthat::expect_s3_class(res_pri, "consortium_privacy_audit")
  testthat::expect_equal(res_pri$primary_count, 2L) # 5 and "5 (10.0%)"
  testthat::expect_equal(res_pri$secondary_count, 0L)

  # Secondary suppression active
  res_sec <- audit_cell_suppression(df, min_cell_count = 11L, secondary_suppression = TRUE, return_audit = TRUE)
  testthat::expect_s3_class(res_sec, "consortium_privacy_audit")
  testthat::expect_true(res_sec$secondary_count > 0L)

  tbl <- res_sec$tables$table
  testthat::expect_equal(tbl$n_count[1], "<11")
  testthat::expect_equal(tbl$n_count[2], "<11")
  testthat::expect_equal(as.integer(tbl$n_count[3]), 50L) # Marginal total preserved
})

testthat::test_that("Feature 2: consortium_multivariable_risk_factors fits L2 penalized logistic model with S3 methods", {
  set.seed(42)
  n <- 150
  age <- stats::rnorm(n, 65, 10)
  diabetes <- stats::rbinom(n, 1, 0.3)
  chf <- stats::rbinom(n, 1, 0.2)
  diabetes_dupe <- diabetes # Collinear column

  logits <- -1.8 + 0.04 * (age - 65) + 1.2 * diabetes + 1.0 * chf
  prob <- 1.0 / (1.0 + exp(-logits))
  y <- stats::rbinom(n, 1, prob)

  X <- data.frame(
    age = age,
    diabetes = diabetes,
    diabetes_dupe = diabetes_dupe,
    heart_failure = chf,
    stringsAsFactors = FALSE
  )

  rf <- consortium_multivariable_risk_factors(
    X = X,
    y = y,
    alpha = 1.0,
    drop_collinear = TRUE,
    standardize_continuous = TRUE
  )

  testthat::expect_s3_class(rf, "consortium_risk_factors")
  testthat::expect_true(length(rf$dropped_features) >= 1) # Collinear duplicate dropped
  testthat::expect_true(nrow(rf$table) >= 3)
  testthat::expect_true("odds_ratio" %in% names(rf$table))
  testthat::expect_true("ci_95_lower" %in% names(rf$table))
  testthat::expect_true("ci_95_upper" %in% names(rf$table))

  # Test S3 methods
  df_res <- as.data.frame(rf)
  testthat::expect_s3_class(df_res, "data.frame")

  # Test print method output
  output <- utils::capture.output(print(rf))
  testthat::expect_true(any(grepl("Multivariable Risk Factors", output)))

  # Test base graphics forest plot
  tmp_pdf <- tempfile(fileext = ".pdf")
  grDevices::pdf(tmp_pdf)
  plot(rf)
  grDevices::dev.off()
  testthat::expect_true(file.exists(tmp_pdf) && file.size(tmp_pdf) > 0)
  unlink(tmp_pdf)
})

testthat::test_that("Feature 3: consortium_cohort_diagnostics produces pure DuckDB cohort characterization", {
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE))

  DBI::dbExecute(con, "
    CREATE TABLE person (
      person_id BIGINT,
      gender_concept_id BIGINT,
      year_of_birth INTEGER,
      month_of_birth INTEGER,
      day_of_birth INTEGER,
      race_concept_id BIGINT,
      ethnicity_concept_id BIGINT
    );
    INSERT INTO person VALUES
      (1, 8507, 1955, 1, 1, 8527, 38003563),
      (2, 8532, 1960, 6, 1, 8527, 38003563),
      (3, 8532, 1975, 3, 1, 8527, 38003563),
      (4, 8507, 1980, 8, 1, 8527, 38003563),
      (5, 8532, 1945, 11, 1, 8527, 38003563);

    CREATE TABLE observation_period (
      observation_period_id BIGINT,
      person_id BIGINT,
      observation_period_start_date DATE,
      observation_period_end_date DATE,
      period_type_concept_id BIGINT
    );
    INSERT INTO observation_period VALUES
      (1, 1, DATE '2018-01-01', DATE '2023-12-31', 32817),
      (2, 2, DATE '2019-01-01', DATE '2023-12-31', 32817),
      (3, 3, DATE '2018-01-01', DATE '2023-12-31', 32817),
      (4, 4, DATE '2019-01-01', DATE '2023-12-31', 32817),
      (5, 5, DATE '2018-01-01', DATE '2023-12-31', 32817);

    CREATE TABLE cohort (
      cohort_definition_id INTEGER,
      subject_id BIGINT,
      cohort_start_date DATE,
      cohort_end_date DATE
    );
    INSERT INTO cohort VALUES
      (101, 1, DATE '2020-03-15', DATE '2020-06-15'),
      (101, 2, DATE '2020-07-20', DATE '2020-10-20'),
      (101, 3, DATE '2021-01-10', DATE '2021-05-10'),
      (101, 4, DATE '2021-04-12', DATE '2021-08-12'),
      (101, 5, DATE '2022-09-01', DATE '2022-12-01');
  ")

  diag <- consortium_cohort_diagnostics(
    con,
    cohort_table = "cohort",
    cohort_id = 101L,
    secondary_suppression = TRUE
  )

  testthat::expect_s3_class(diag, "consortium_cohort_diagnostics")
  testthat::expect_true(nrow(diag$incidence) > 0)
  testthat::expect_true(nrow(diag$time_to_event) > 0)
  testthat::expect_true(nrow(diag$population_pyramid) > 0)

  # Test S3 print and plot methods
  out_p <- utils::capture.output(print(diag))
  testthat::expect_true(any(grepl("OHDSI Cohort Diagnostics", out_p)))

  tmp_pdf <- tempfile(fileext = ".pdf")
  grDevices::pdf(tmp_pdf)
  plot(diag)
  grDevices::dev.off()
  testthat::expect_true(file.exists(tmp_pdf) && file.size(tmp_pdf) > 0)
  unlink(tmp_pdf)
})

testthat::test_that("Feature 4: consortium_cross_site_validation evaluates multi-site transportability matrices", {
  set.seed(42)
  # Generate 3 site cohorts
  make_site_df <- function(site_name, n, b_age) {
    age <- stats::rnorm(n, 65, 8)
    cad <- stats::rbinom(n, 1, 0.4)
    logits <- -1.5 + b_age * (age - 65) + 1.1 * cad
    prob <- 1.0 / (1.0 + exp(-logits))
    outcome <- stats::rbinom(n, 1, prob)
    data.frame(
      site = site_name,
      age = age,
      cad = cad,
      outcome = outcome,
      stringsAsFactors = FALSE
    )
  }

  site_data <- rbind(
    make_site_df("Site_Alpha", 80, 0.03),
    make_site_df("Site_Beta", 80, 0.03),
    make_site_df("Site_Gamma", 80, 0.01)
  )

  iecv <- consortium_cross_site_validation(
    site_data = site_data,
    feature_cols = c("age", "cad"),
    target_col = "outcome",
    site_col = "site",
    regularize = TRUE,
    alpha = 1.0
  )

  testthat::expect_s3_class(iecv, "consortium_cross_site_validation")
  testthat::expect_equal(nrow(iecv$auroc_matrix), 3L)
  testthat::expect_equal(ncol(iecv$auroc_matrix), 3L)
  testthat::expect_true(nrow(iecv$summary) >= 3)
  testthat::expect_true("transportability_gap" %in% names(iecv$summary))

  # S3 print and plot methods
  out_txt <- utils::capture.output(print(iecv))
  testthat::expect_true(any(grepl("Internal-External Cross-Validation", out_txt)))

  tmp_pdf <- tempfile(fileext = ".pdf")
  grDevices::pdf(tmp_pdf)
  plot(iecv)
  grDevices::dev.off()
  testthat::expect_true(file.exists(tmp_pdf) && file.size(tmp_pdf) > 0)
  unlink(tmp_pdf)
})

testthat::test_that("Feature 5: export_to_circe_json exports CohortFlow into standard OHDSI Circe JSON", {
  flow_obj <- structure(
    list(
      visit_concept_ids = c(9201L, 262L),
      follow_up_days = 30L,
      one_visit_per_person = TRUE,
      steps = list(
        list(name = "Adults", predicate = "age >= 18"),
        list(name = "Diabetes", predicate = "condition_concept_id IN (201826)")
      )
    ),
    class = "omop_cohort_flow"
  )

  circe <- export_to_circe_json(
    cohort_flow = flow_obj,
    cohort_name = "Adult Inpatient Diabetes Cohort"
  )

  testthat::expect_type(circe, "list")
  testthat::expect_equal(circe$Title, "Adult Inpatient Diabetes Cohort")
  testthat::expect_true("PrimaryCriteria" %in% names(circe))
  testthat::expect_equal(length(circe$InclusionRules), 2L)
  testthat::expect_equal(circe$InclusionRules[[1]]$name, "Adults")
  testthat::expect_equal(circe$InclusionRules[[2]]$name, "Diabetes")

  # Verify to_json output
  tmp_json <- tempfile(fileext = ".json")
  export_to_circe_json(flow_obj, cohort_name = "Saved Cohort", output_path = tmp_json)
  testthat::expect_true(file.exists(tmp_json))
  parsed <- jsonlite::fromJSON(tmp_json)
  testthat::expect_equal(parsed$Title, "Saved Cohort")
  unlink(tmp_json)
})

testthat::test_that("Feature 6: build_manuscript_scaffold compiles Quarto and Typst documents with TRIPOD appendix", {
  tables <- list(
    table1 = data.frame(Characteristic = c("Age", "Male"), Site1 = c("65 (10)", "40 (40%)")),
    table2 = data.frame(Step = c("Initial", "Final"), Count = c(500L, 250L)),
    table5 = data.frame(variable = c("Age", "Hypertension"), odds_ratio = c(1.04, 1.75))
  )

  # 1. Quarto JAMA Scaffold
  qmd <- build_manuscript_scaffold(
    study_title = "Multicenter Cardiovascular Transportability",
    target_journal = "JAMA",
    format = "quarto",
    tables = tables
  )

  testthat::expect_s3_class(qmd, "consortium_manuscript")
  qmd_str <- as.character(qmd)
  testthat::expect_true(grepl("Multicenter Cardiovascular Transportability", qmd_str, fixed = TRUE))
  testthat::expect_true(grepl("## Abstract", qmd_str, fixed = TRUE))
  testthat::expect_true(grepl("**Importance**:", qmd_str, fixed = TRUE))
  testthat::expect_true(grepl("## Methods", qmd_str, fixed = TRUE))
  testthat::expect_true(grepl("## Results", qmd_str, fixed = TRUE))
  testthat::expect_true(grepl("TRIPOD Statement Checklist", qmd_str, fixed = TRUE))

  # 2. Typst Lancet Scaffold
  typ <- build_manuscript_scaffold(
    study_title = "Predictive Transportability across UK Hospitals",
    target_journal = "Lancet",
    format = "typst",
    tables = tables
  )

  testthat::expect_s3_class(typ, "consortium_manuscript")
  typ_str <- as.character(typ)
  testthat::expect_true(grepl("#set document(", typ_str, fixed = TRUE))
  testthat::expect_true(grepl("Interpretation", typ_str, fixed = TRUE))
  testthat::expect_true(grepl("TRIPOD", typ_str, fixed = TRUE))

  # Test file saving
  tmp_file <- tempfile(fileext = ".qmd")
  saved_path <- qmd$save(tmp_file)
  testthat::expect_true(file.exists(tmp_file))
  testthat::expect_true(grepl("Multicenter Cardiovascular", readLines(tmp_file, n = 5)[2], fixed = TRUE))
  unlink(tmp_file)
})
