# 0.34.2 (Kyle, after the C15 close-out): n_secondary_dx counts diagnoses on the index visit only; a diagnosis with no visit id
# counts by its date. Mirrors tests/test_drug_nonstandard_0342.py. Synthetic data only.

test_that("n_secondary_dx counts the index visit only when the cohort has visit_occurrence_id", {
  con <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT,
                       visit_start_date DATE, visit_end_date DATE)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence VALUES (100, 1, 9201, DATE '2020-01-10', DATE '2020-01-15'),
                       (101, 1, 9202, DATE '2020-01-12', DATE '2020-01-12')")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,
                       condition_source_value VARCHAR, visit_occurrence_id BIGINT, condition_status_source_value VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (1, 1, DATE '2020-01-10', 'I50.9', 100, 'P'),
                       (1, 2, DATE '2020-01-10', 'E11.9', 100, NULL), (1, 3, DATE '2020-01-12', 'M54.5', 101, NULL),
                       (1, 4, DATE '2020-01-13', 'N18.3', NULL, NULL)")
  DBI::dbExecute(con, "CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE)")
  cohort <- data.frame(row_id = 0L, subject_id = 1L, cohort_start_date = as.Date("2020-01-10"), cohort_end_date = as.Date("2020-01-15"))
  expect_equal(extract_protocol_history_features(con, cohort)$n_secondary_dx, 3)
  cohort$visit_occurrence_id <- 100L
  expect_equal(extract_protocol_history_features(con, cohort)$n_secondary_dx, 2)
})

test_that("tobacco is the level of the latest record up to discharge (0.34.2)", {
  con <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT,
                       visit_start_date DATE, visit_end_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,
                       condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (1, 1, DATE '2015-01-01', 'F17.210', NULL),
                       (1, 2, DATE '2019-06-01', 'Z87.891', NULL), (2, 1, DATE '2015-01-01', 'Z87.891', NULL),
                       (2, 2, DATE '2019-06-01', 'F17.210', NULL), (3, 1, DATE '2019-06-01', 'Z87.891', NULL),
                       (3, 2, DATE '2019-06-01', 'F17.210', NULL), (5, 1, DATE '2020-02-01', 'F17.210', NULL)")
  DBI::dbExecute(con, "CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE,
                       value_as_concept_id INT, value_source_value VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO observation VALUES (4, 43054909, DATE '2016-01-01', 45881517, '01'),
                       (4, 43054909, DATE '2019-12-01', 45879404, '04')")
  cohort <- data.frame(row_id = 0:4, subject_id = 1:5, cohort_start_date = as.Date(rep("2020-01-10", 5)),
                       cohort_end_date = as.Date(rep("2020-01-15", 5)))
  expect_equal(extract_protocol_history_features(con, cohort)$tobacco,
               c("Former user", "Current user", "Current user", "Never user", "Missing / Unknown"))
})

test_that("stays still in hospital (PCORnet SH) are excluded when asked, and by the diabetes cohort (0.34.2)", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, add = TRUE)
  src <- c("'HO'", "' sh '", "NULL")
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1960, 0, 0)", pid))
    DBI::dbExecute(con, sprintf("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) VALUES (%d, %d, DATE '2015-01-01', DATE '2022-01-01', 0)", pid, pid))
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, discharged_to_concept_id, discharged_to_source_value) VALUES (%d, %d, 9201, DATE '2020-03-01', DATE '2020-03-05', 0, 0, %s)", pid, pid, src[pid]))
  }
  run <- function(...) define_study_cohort(con, visit_type = "inpatient", target_outcome = "none", washin_days = 0,
                                           require_verified_followup = FALSE, materialise = FALSE, ...)
  expect_equal(sort(run()$subject_id), 1:3)
  expect_equal(sort(run(still_in_hospital_status = "SH")$subject_id), c(1L, 3L))
  expect_equal(formals(build_ederri_diabetes_cohort)$still_in_hospital_status, "SH")
})
