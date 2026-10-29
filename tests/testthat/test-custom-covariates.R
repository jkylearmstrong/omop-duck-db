cc_cdm <- function() {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  list(con = con, db_path = db_path)
}
cc_concepts <- list(frailty = c(2000200001, "Frailty score"), frailty_alt = c(2000200002, "Frailty score, alternative definition"))
cc_values <- function() data.frame(person_id = c(1, 1, 2, 3), date = as.Date("2020-01-01"),
                                   feature = c("frailty", "frailty_alt", "frailty", "frailty"), value = c(2, 3, NA, 5))

test_that("custom covariates are written as measurement rows, replaced on re-run, and validated", {
  fx <- cc_cdm()
  on.exit({ DBI::dbDisconnect(fx$con, shutdown = TRUE); unlink(fx$db_path) }, add = TRUE)
  DBI::dbExecute(fx$con, "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id) VALUES (1, 1, 3004410, DATE '2020-01-01', 0)")
  expect_equal(write_custom_covariates(fx$con, cc_values(), cc_concepts), 3)
  expect_equal(write_custom_covariates(fx$con, cc_values(), cc_concepts), 3)
  m <- DBI::dbGetQuery(fx$con, "SELECT person_id, measurement_concept_id, value_as_number, unit_concept_id FROM measurement WHERE measurement_concept_id > 2000000000 ORDER BY 1, 2")
  expect_equal(m$value_as_number, c(2, 3, 5))
  expect_true(all(m$unit_concept_id == 0))
  expect_equal(DBI::dbGetQuery(fx$con, "SELECT count(*) AS n FROM measurement")$n, 4)
  expect_equal(DBI::dbGetQuery(fx$con, "SELECT count(*) AS n FROM concept WHERE vocabulary_id = 'omop-duck-db'")$n, 2)
  expect_error(write_custom_covariates(fx$con, cc_values(), list(frailty = c(3004410, "x"), frailty_alt = c(2000200002, "y"))), "2,000,000,000")
  v <- cc_values(); v$feature <- "other"
  expect_error(write_custom_covariates(fx$con, v, cc_concepts), "no entry")
  expect_error(write_custom_covariates(fx$con, cc_values()[, -2], cc_concepts), "missing column")
})

test_that("custom_covariate_settings asks FeatureExtraction for the custom concepts", {
  skip_if_not_installed("FeatureExtraction")
  cs <- custom_covariate_settings(c(2000200001, 2000200002), long_term_start_days = -180)
  expect_true(isTRUE(cs$MeasurementValueLongTerm))
  expect_equal(cs$longTermStartDays, -180)
  expect_equal(cs$includedCovariateConceptIds, c(2000200001, 2000200002))
})

test_that("wide features get stable named concepts beside the standard one", {
  fx <- cc_cdm()
  on.exit({ DBI::dbDisconnect(fx$con, shutdown = TRUE); unlink(fx$db_path) }, add = TRUE)
  wide <- data.frame(person_id = c(1, 2), date = as.Date("2020-01-01"), charlson_site = c(3, 1), elix_site = c(4, NA))
  ids <- write_custom_features(fx$con, wide, labels = c(charlson_site = "Charlson index, site definition"))
  expect_equal(as.numeric(ids$charlson_site[[1]]), 2001000000)
  expect_equal(as.numeric(ids$elix_site[[1]]), 2001000001)
  again <- write_custom_features(fx$con, wide)
  expect_equal(as.numeric(again$charlson_site[[1]]), 2001000000)
  expect_equal(DBI::dbGetQuery(fx$con, "SELECT count(*) AS n FROM measurement")$n, 3)
  expect_equal(DBI::dbGetQuery(fx$con, "SELECT concept_name AS n FROM concept WHERE concept_id = 2001000000")$n, "Charlson index, site definition")
})
