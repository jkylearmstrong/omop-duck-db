# Tests covering the 5 feature requests from tasks/requests1.txt:
# 1. Consortium-Level Scoring Wrapper on federated_consortium (consortium_calculate_scores)
# 2. Cross-Site Model Calibration & Decile Drift (consortium_calibration)
# 3. Clinical Decision Curve Analysis (decision_curve_analysis)
# 4. Automated European & IFCC Metric Unit Scaling (Hemoglobin mmol/L, HbA1c IFCC mmol/mol, Creatinine umol/L)
# 5. Competing Risks Cumulative Incidence Function (estimate_cif, plot_cif, fine_gray_estimator)

r5_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  con
}

test_that("Consortium scoring wrapper evaluates risk scores across federated sites", {
  db_a <- tempfile(fileext = ".duckdb")
  db_b <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db_a)
  build_schema(db_path = db_b)

  for (pair in list(list(db_a, c(1L, 2L)), list(db_b, c(3L, 4L)))) {
    con_site <- DBI::dbConnect(duckdb::duckdb(), dbdir = pair[[1]])
    pids <- pair[[2]]
    for (pid in pids) {
      DBI::dbExecute(con_site, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1960, 8527, 38003563)", pid))
      DBI::dbExecute(con_site, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, %d, 9201, DATE '2023-01-01', DATE '2023-01-05', 32817)", 100L + pid, pid))
      DBI::dbExecute(con_site, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number) VALUES (%d, %d, 3000963, DATE '2023-01-04', 32817, 11.0)", 200L + pid, pid))
    }
    DBI::dbExecute(con_site, "INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date) SELECT 1, person_id, visit_start_date, visit_end_date FROM visit_occurrence")
    DBI::dbDisconnect(con_site, shutdown = TRUE)
  }

  fc <- federated_consortium(sites = c("SiteA" = db_a, "SiteB" = db_b))
  on.exit(consortium_close(fc), add = TRUE)

  res <- consortium_calculate_scores(fc, cohort_table = "cohort")
  expect_s3_class(res, "consortium_scores")
  expect_true(!is.null(res$scores))
  expect_true(!is.null(res$site_summary))
  expect_true(!is.null(res$consortium_summary))

  df <- as.data.frame(res)
  expect_true(nrow(df) >= 4)
  expect_true("site" %in% names(df))
  expect_true("lace_score" %in% names(df))
  expect_true("hospital_score" %in% names(df))

  summ <- summary(res)
  expect_true("mean_lace_score" %in% names(summ))
  expect_true("mean_hospital_score" %in% names(summ))
  expect_true("SiteA" %in% summ$site)
  expect_true("SiteB" %in% summ$site)

  expect_output(print(res), "Consortium Bedside Scoring")
})

test_that("consortium_calibration computes cross-site Brier score, ECE, GND test, and decile drift", {
  set.seed(42)
  n <- 300
  y_true <- rbinom(n, 1, 0.25)
  y_prob <- pmin(pmax(rbeta(n, 2, 5), 0.01), 0.99)
  site_labels <- sample(c("SiteA", "SiteB", "SiteC"), size = n, replace = TRUE)

  cal <- consortium_calibration(
    y_true = y_true,
    y_prob = y_prob,
    site_labels = site_labels,
    n_bins = 5
  )

  expect_s3_class(cal, "consortium_calibration")
  expect_true(is.data.frame(cal$site_metrics))
  expect_true(is.data.frame(cal$calibration_curves))
  expect_true(is.data.frame(cal$decile_drift))

  expect_true("brier_score" %in% names(cal$site_metrics))
  expect_true("ece" %in% names(cal$site_metrics))
  expect_true("gnd_statistic" %in% names(cal$site_metrics))
  expect_true("gnd_p_value" %in% names(cal$site_metrics))

  df <- as.data.frame(cal)
  expect_equal(df, cal$site_metrics)

  expect_output(print(cal), "Cross-Site Model Calibration")
  expect_silent(plot(cal))
})

test_that("decision_curve_analysis computes net benefit and standardized net benefit across thresholds", {
  set.seed(42)
  n <- 400
  y_true <- rbinom(n, 1, 0.20)
  y_prob <- pmin(pmax(rbeta(n, 2, 6), 0.01), 0.99)

  dca <- decision_curve_analysis(
    y_true = y_true,
    y_prob = y_prob,
    threshold_range = c(0.05, 0.35),
    step = 0.05
  )

  expect_s3_class(dca, "decision_curve_analysis")
  df <- dca$net_benefit
  expect_true(is.data.frame(df))
  expect_true(all(c("threshold", "model", "all", "none", "standardized_net_benefit", "net_reduction_interventions") %in% names(df)))
  expect_true(all(df$none == 0.0))

  summ <- summary(dca)
  expect_true(is.list(summ))
  expect_true(!is.null(summ$prevalence))
  expect_true(!is.null(summ$max_net_benefit))

  expect_output(print(dca), "Clinical Decision Curve Analysis")
  expect_silent(plot(dca))
})

test_that("harmonize_measurement_units scales European Hemoglobin, IFCC HbA1c, and umol/L Creatinine", {
  con <- r5_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1970, 8527, 38003563)")
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES (3000963, 'Hemoglobin', 'Measurement', 'LOINC', 'Lab Test', '59260-0', DATE '1970-01-01', DATE '2099-12-31'), (3004410, 'HbA1c IFCC', 'Measurement', 'LOINC', 'Lab Test', '59261-8', DATE '1970-01-01', DATE '2099-12-31'), (3016723, 'Creatinine', 'Measurement', 'LOINC', 'Lab Test', '2160-0', DATE '1970-01-01', DATE '2099-12-31')")

  DBI::dbExecute(con, "
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_source_value)
    VALUES
    (1, 1, 3000963, DATE '2023-01-01', 32817, 10.0, 'mmol/L'),
    (2, 1, 3004410, DATE '2023-01-01', 32817, 50.0, 'mmol/mol'),
    (3, 1, 3016723, DATE '2023-01-01', 32817, 176.8, '\u00b5mol/l'),
    (4, 1, 3000963, DATE '2023-02-01', 32817, 9.0, NULL),
    (5, 1, 3004410, DATE '2023-02-01', 32817, 60.0, NULL)
  ")

  report <- harmonize_measurement_units(con, create_view = TRUE)
  res <- DBI::dbGetQuery(con, "SELECT measurement_id, value_as_number FROM measurement_harmonized ORDER BY measurement_id")

  # Hemoglobin 10.0 * 1.611 = 16.11
  expect_equal(res$value_as_number[res$measurement_id == 1], 16.11, tolerance = 0.01)
  # HbA1c 50.0 -> (0.09148 * 50) + 2.152 = 6.726
  expect_equal(res$value_as_number[res$measurement_id == 2], 6.726, tolerance = 0.01)
  # Creatinine 176.8 / 88.4 = 2.0
  expect_equal(res$value_as_number[res$measurement_id == 3], 2.0, tolerance = 0.01)
  # NULL unit Hemoglobin 9.0 * 1.611 = 14.499
  expect_equal(res$value_as_number[res$measurement_id == 4], 14.499, tolerance = 0.01)
  # NULL unit HbA1c 60.0 -> 7.6408
  expect_equal(res$value_as_number[res$measurement_id == 5], 7.6408, tolerance = 0.01)
})

test_that("estimate_cif, plot_cif, and fine_gray_estimator handle competing risks", {
  set.seed(42)
  n <- 200
  times <- rexp(n, rate = 1 / 30) + 1
  statuses <- sample(c(0L, 1L, 2L), size = n, replace = TRUE, prob = c(0.4, 0.4, 0.2))
  treatment <- sample(c(0L, 1L), size = n, replace = TRUE)

  df <- data.frame(
    time_days = times,
    status = statuses,
    treatment = treatment
  )

  cif <- estimate_cif(df, time_col = "time_days", status_col = "status")
  expect_s3_class(cif, "cif_result")
  expect_true(is.data.frame(cif$cif_table))
  expect_true(all(c("time", "cif_primary", "cif_competing") %in% names(cif$cif_table)))
  expect_true(all(cif$cif_table$cif_primary >= 0 & cif$cif_table$cif_primary <= 1))
  expect_true(all(cif$cif_table$cif_competing >= 0 & cif$cif_table$cif_competing <= 1))

  expect_output(print(cif), "Competing Risks Cumulative Incidence")
  expect_silent(plot(cif))
  expect_silent(plot_cif(cif))

  df_cif <- as.data.frame(cif)
  expect_equal(df_cif, cif$cif_table)

  # Fine-Gray
  fg <- fine_gray_estimator(
    df,
    time_col = "time_days",
    status_col = "status",
    group_col = "treatment",
    event_code = 1L
  )
  expect_s3_class(fg, "fine_gray_result")
  expect_true(fg$shr > 0)
  expect_true(fg$p_value >= 0 && fg$p_value <= 1)
  expect_true(fg$ci_lower <= fg$shr && fg$shr <= fg$ci_upper)

  expect_output(print(fg), "Fine-Gray Subdistribution Hazard")
  fg_df <- as.data.frame(fg)
  expect_true("sHR" %in% names(fg_df))
})
