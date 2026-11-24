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
