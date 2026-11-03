test_that("suppressed overlap hides the percentage", {
  tokens <- list(A = paste0("t", 1:1000), B = paste0("t", 994:1993))
  df <- consortium_patient_overlap(tokens)
  row <- df[df$site_1 != df$site_2, ][1, ]
  expect_equal(row$shared_patients, "<11")
  expect_true(is.na(row$overlap_pct))
})

test_that("remap_discharge_status is case-insensitive and idempotent", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 0, 'ip'), (2, 0, ' IP '), (3, 5, 'IP'), (4, 0, 'HOME')) t(visit_occurrence_id, discharged_to_concept_id, discharged_to_source_value)")
  expect_equal(remap_discharge_status(con), 2L)
  expect_equal(remap_discharge_status(con), 0L)
  res <- DBI::dbGetQuery(con, "SELECT discharged_to_concept_id AS d FROM visit_occurrence ORDER BY 1")$d
  expect_equal(as.integer(res), c(0L, 5L, 8717L, 8717L))
})

test_that("extract_patient_features accepts TIMESTAMP cohort dates", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE person AS SELECT 1 AS person_id, 1970 AS year_of_birth, 1 AS month_of_birth, 1 AS day_of_birth, 0 AS gender_concept_id, 0 AS race_concept_id, 0 AS ethnicity_concept_id")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS SELECT 1 AS condition_occurrence_id, 1 AS person_id, DATE '2020-01-05' AS condition_start_date")
  DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT 1 AS subject_id, TIMESTAMP '2020-01-10 08:00:00' AS cohort_start_date, TIMESTAMP '2020-01-12 08:00:00' AS cohort_end_date")
  df <- extract_patient_features(con, cohort_table = "cohort", lookback_days = 30)
  expect_equal(as.integer(df$condition_count_30d[1]), 1L)
})
