test_that("pooled duplicate row is withheld when a pair is suppressed", {
  ids <- list(A = 1:507, B = c(501:1000, 2001:2007), C = c(2001:2493, 5001:5100))
  out <- consortium_cross_site_duplicates(ids, key = "k", min_cell_count = 11)
  expect_true(any(out$is_suppressed[out$site_1 != "ALL"]))
  total <- out[out$site_1 == "ALL", ]
  expect_true(total$is_suppressed)
  expect_equal(total$shared_patients, "withheld")
  expect_true(is.na(total$overlap_pct))
  ok <- consortium_cross_site_duplicates(list(A = 1:100, B = 51:150), key = "k")
  expect_equal(ok$shared_patients[ok$site_1 == "ALL"], "50")
  expect_equal(ok$n_site_1[ok$site_1 == "ALL"], 150)
})

test_that("pooled n_site is withheld with the pooled row (sum of sizes minus pooled would reveal the pair)", {
  ids <- list(A = 1:1000, B = 994:1993, C = 5001:6000)
  out <- consortium_cross_site_duplicates(ids, key = "k", min_cell_count = 11)
  total <- out[out$site_1 == "ALL", ]
  expect_true(total$is_suppressed)
  expect_true(is.na(total$n_site_1) && is.na(total$n_site_2))
})

test_that("remap_discharge_status dry_run changes nothing", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence AS SELECT * FROM (VALUES (1, 0, 'IP'), (2, 0, 'HOME')) t(visit_occurrence_id, discharged_to_concept_id, discharged_to_source_value)")
  expect_equal(remap_discharge_status(con, dry_run = TRUE), 1L)
  expect_equal(as.integer(DBI::dbGetQuery(con, "SELECT SUM(discharged_to_concept_id) AS s FROM visit_occurrence")$s), 0L)
})

test_that("cap_observation_period shortens and deletes", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE observation_period AS SELECT * FROM (VALUES (1, DATE '2020-01-01', DATE '2026-10-23'), (2, DATE '2026-10-23', DATE '2026-10-23'), (3, DATE '2020-01-01', DATE '2020-06-01')) t(observation_period_id, observation_period_start_date, observation_period_end_date)")
  expect_equal(cap_observation_period(con, "2026-09-30", dry_run = TRUE), list(shortened = 1L, deleted = 1L))
  expect_equal(as.integer(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM observation_period")$n), 3L)
  expect_equal(cap_observation_period(con, "2026-09-30"), list(shortened = 1L, deleted = 1L))
  r <- DBI::dbGetQuery(con, "SELECT MAX(observation_period_end_date)::VARCHAR AS mx, COUNT(*) AS n FROM observation_period")
  expect_equal(r$mx, "2026-09-30")
  expect_equal(as.integer(r$n), 2L)
})

test_that("extract_patient_features does not multiply events across tables", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT 1 AS cohort_definition_id, 1 AS subject_id, DATE '2020-03-01' AS cohort_start_date, DATE '2020-03-05' AS cohort_end_date")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS SELECT i AS condition_occurrence_id, 1 AS person_id, DATE '2020-02-01' AS condition_start_date FROM range(1, 21) t(i)")
  DBI::dbExecute(con, "CREATE TABLE drug_exposure AS SELECT i AS drug_exposure_id, 1 AS person_id, DATE '2020-02-10' AS drug_exposure_start_date FROM range(1, 8) t(i)")
  DBI::dbExecute(con, "CREATE TABLE measurement AS SELECT * FROM (VALUES (1, 1, 3001, DATE '2020-02-15', 10.0), (2, 1, 3001, DATE '2020-02-16', 20.0)) t(measurement_id, person_id, measurement_concept_id, measurement_date, value_as_number)")
  df <- extract_patient_features(con, cohort_id = 1, lookback_days = c(30, 90), include_demographics = FALSE,
                                 measurement_concept_ids = 3001)
  expect_equal(nrow(df), 1L)
  expect_equal(as.integer(df$condition_count_30d), 20L)
  expect_equal(as.integer(df$drug_count_30d), 7L)
  expect_equal(unname(unlist(df[1, names(df) == "mean_measurement_3001"])), c(15, 15))
})
