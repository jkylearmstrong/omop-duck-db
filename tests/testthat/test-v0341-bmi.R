# 0.34.1: BMI computed from height and weight, then taken from the year before admission. Mirror of tests/test_c11_fixes_0341.py.
# Synthetic data only.

bmi_db <- function() {
  con <- DBI::dbConnect(duckdb::duckdb())
  DBI::dbExecute(con, "CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE,
                       measurement_datetime TIMESTAMP, value_as_number DOUBLE, unit_concept_id INT)")
  DBI::dbExecute(con, "INSERT INTO measurement VALUES
    (1, 1, 3036277, DATE '2020-01-11', NULL, 70, NULL), (2, 1, 3025315, DATE '2020-01-11', NULL, 200, NULL),
    (3, 2, 1, DATE '2019-06-01', NULL, 31, NULL), (4, 2, 1, DATE '2019-03-01', NULL, 25, NULL),
    (5, 3, 3036277, DATE '2019-09-01', NULL, 170, 8582), (6, 3, 3025315, DATE '2019-12-01', NULL, 70, 9529),
    (7, 4, 1, DATE '2018-11-01', NULL, 29, NULL),
    (8, 5, 1, DATE '2020-01-12', NULL, 22, NULL), (9, 5, 3036277, DATE '2020-01-12', NULL, 70, NULL),
    (10, 5, 3025315, DATE '2020-01-12', NULL, 300, NULL),
    (11, 6, 3036277, DATE '2020-01-12', NULL, 7, NULL), (12, 6, 3025315, DATE '2020-01-12', NULL, 200, NULL)")
  DBI::dbExecute(con, "CREATE TABLE cohort_t AS SELECT subject_id, DATE '2020-01-10' AS cohort_start_date, DATE '2020-01-15' AS cohort_end_date,
                       100 + subject_id AS visit_occurrence_id, 0 AS outcome_flag FROM range(1, 7) t(subject_id)")
  con
}

bmi_run <- function(con, ...) {
  suppressWarnings(extract_benchmark_features(con, "cohort_t", include_demographics = FALSE, include_elixhauser = FALSE,
                                              include_medications = FALSE, include_labs = FALSE, include_utilization = FALSE,
                                              include_disposition = FALSE, vital_concept_ids = list(bmi = 1L), ...))
}

test_that("BMI is computed, then taken from the year before admission", {
  con <- bmi_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  f <- bmi_run(con, profile = "ederri", include_protocol_history = FALSE)
  bmi <- stats::setNames(f$vital_bmi, f$person_id)
  expect_equal(unname(bmi["1"]), 28.70, tolerance = 0.01)
  expect_equal(unname(bmi["2"]), 31)
  expect_equal(unname(bmi["3"]), 24.22, tolerance = 0.01)
  expect_true(is.na(bmi["4"]) && is.na(bmi["6"]))
  expect_equal(unname(bmi["5"]), 22)
  expect_equal(attr(f, "bmi_filled"), list(derived_in_stay = 1L, recorded_prior = 1L, derived_prior = 1L))
})

test_that("bmi_fill options", {
  con <- bmi_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  f <- bmi_run(con)
  expect_equal(f$vital_bmi[f$person_id == 1], 28.70, tolerance = 0.01)
  expect_true(is.na(f$vital_bmi[f$person_id == 2]))
  expect_equal(sum(!is.na(bmi_run(con, bmi_fill = "none")$vital_bmi)), 1L)
  f <- bmi_run(con, bmi_fill = "derive_lookback", bmi_lookback_days = 800)
  expect_equal(f$vital_bmi[f$person_id == 4], 29)
  expect_error(bmi_run(con, bmi_fill = "impute"), "bmi_fill")
})
