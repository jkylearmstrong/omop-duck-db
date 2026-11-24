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
