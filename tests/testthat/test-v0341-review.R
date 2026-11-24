# Kyle's review of PR 104 (0.34.1): tobacco from PCORnet VITAL with a "Missing / Unknown" level, undotted tobacco codes, and
# observation stays in days_since_prior_ip_ed. Mirrors tests/test_pr104_review.py. Synthetic data only.

test_that("tobacco reads VITAL smoking and undotted codes, and keeps unknown; observation stays count as prior encounters", {
  con <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT,
                       visit_start_date DATE, visit_end_date DATE)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence VALUES (1, 1, 9201, DATE '2019-06-01', DATE '2019-06-03'),
                       (2, 1, 581385, DATE '2019-12-20', DATE '2019-12-21')")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,
                       condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (5, 1, DATE '2019-01-01', 'f17210', NULL),
                       (6, 1, DATE '2019-01-01', 'Z87891', NULL), (7, 1, DATE '2019-01-01', 'V1582', NULL)")
  DBI::dbExecute(con, "CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE,
                       value_as_concept_id INT, value_source_value VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO observation VALUES (1, 43054909, DATE '2019-01-01', 45881517, '01'),
                       (2, 43054909, DATE '2019-01-01', 45883458, '03'), (3, 43054909, DATE '2019-01-01', 45879404, '04'),
                       (4, 43054909, DATE '2019-01-01', 45885135, '06'), (8, 43054909, DATE '2019-01-01', 0, '04')")
  cohort <- data.frame(row_id = 0:8, subject_id = 1:9, cohort_start_date = as.Date(rep("2020-01-10", 9)),
                       cohort_end_date = as.Date(rep("2020-01-15", 9)))
  h <- extract_protocol_history_features(con, cohort)
  expect_equal(h$tobacco, c("Current user", "Former user", "Never user", "Missing / Unknown", "Current user", "Former user",
                            "Former user", "Never user", "Missing / Unknown"))
  expect_equal(c(h$days_since_prior_ip_ed[[1]], h$prior_ip_os_ed_count[[1]]), c(21, 2))
})
