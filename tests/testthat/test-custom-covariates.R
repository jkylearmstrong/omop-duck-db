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

test_that("a custom id that another vocabulary uses is refused before anything is written", {
  fx <- cc_cdm()
  on.exit({ DBI::dbDisconnect(fx$con, shutdown = TRUE); unlink(fx$db_path) }, add = TRUE)
  DBI::dbExecute(fx$con, "INSERT INTO concept VALUES (2000200001, 'x', 'Measurement', 'SNOMED', 'Clinical Finding', 'S', 'other', DATE '1970-01-01', DATE '2099-12-31', NULL)")
  expect_error(write_custom_covariates(fx$con, cc_values(), cc_concepts), "already used by other")
  expect_equal(DBI::dbGetQuery(fx$con, "SELECT count(*) AS n FROM measurement")$n, 0)
  expect_error(write_custom_covariates(cc_cdm()$con, cc_values(), list(frailty = c(2000200001, "a"), frailty_alt = c(2000200001, "b"))), "share one concept id")
})

test_that("a shared registry pins ids across sites and conflicts are found", {
  skip_if_not_installed("jsonlite")
  a <- cc_cdm(); b <- cc_cdm(); d <- cc_cdm()
  on.exit(for (f in list(a, b, d)) { DBI::dbDisconnect(f$con, shutdown = TRUE); unlink(f$db_path) }, add = TRUE)
  wide <- data.frame(person_id = 1, date = as.Date("2020-01-01"), charlson_site = 3)
  write_custom_features(a$con, wide)
  path <- tempfile(fileext = ".json")
  reg <- export_custom_concept_registry(a$con, path)
  expect_equal(as.numeric(reg$charlson_site$concept_id), 2001000000)
  write_custom_features(b$con, data.frame(person_id = 1, date = as.Date("2020-01-01"), elixhauser = 2))
  write_custom_features(b$con, wide)
  kinds <- check_custom_concepts(b$con, path)$kind
  expect_setequal(kinds, c("name_different_id", "id_different_name"))
  expect_error(check_custom_concepts(b$con, path, error = TRUE), "custom concept conflicts")
  got <- register_custom_concepts(d$con, c("frailty", "charlson_site"), registry = path)
  expect_equal(as.numeric(got$charlson_site[[1]]), 2001000000)
  expect_gt(as.numeric(got$frailty[[1]]), 2001000000)
  expect_equal(nrow(check_custom_concepts(d$con, path)), 0)
})

test_that("a registry id that this database uses for something else is an error", {
  fx <- cc_cdm()
  on.exit({ DBI::dbDisconnect(fx$con, shutdown = TRUE); unlink(fx$db_path) }, add = TRUE)
  DBI::dbExecute(fx$con, "INSERT INTO concept VALUES (2001000005, 'frailty', 'Measurement', 'omop-duck-db', 'Custom covariate', 'S', 'frailty', DATE '1970-01-01', DATE '2099-12-31', NULL)")
  DBI::dbExecute(fx$con, "INSERT INTO concept VALUES (2001000009, 'o', 'Measurement', 'omop-duck-db', 'Custom covariate', 'S', 'other', DATE '1970-01-01', DATE '2099-12-31', NULL)")
  expect_error(register_custom_concepts(fx$con, "frailty", registry = c(frailty = 2001000007)), "in this database but")
  expect_error(register_custom_concepts(fx$con, "charlson", registry = c(charlson = 2001000009)), "already used here by other")
  expect_error(register_custom_concepts(fx$con, "charlson", registry = c(charlson = 1503297)), "under 2000000000")
})
