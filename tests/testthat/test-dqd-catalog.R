# The R DQD catalog gives the same results as omop_etl.dqd_catalog (tests/test_dqd_catalog.py) on the same planted data.
dqd_today <- as.Date("2024-01-01")

dqd_cdm <- function(env = parent.frame()) {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  withr::defer({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db) }, envir = env)
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1980, 8527, 38003563), (2, 8507, 1980, 8527, 38003563)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (1, 1, 9201, DATE '2020-01-05', DATE '2020-01-09', 0)")
  con
}

dqd_by <- function(res, name, tbl, fld = NULL) {
  hits <- Filter(function(r) r$check_name == name && r$cdm_table_name == tbl && (is.null(fld) || identical(r$cdm_field_name, fld)), res$CheckResults)
  testthat::expect_true(length(hits) > 0, info = paste(name, tbl, fld))
  hits[[1]]
}

dqd_concept <- function(con, id, name, domain, vocab, cls, std, code, to = "2099-12-31", why = "NULL") {
  DBI::dbExecute(con, sprintf("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason) VALUES (%d, '%s', '%s', '%s', '%s', %s, '%s', DATE '1970-01-01', DATE '%s', %s)",
                              id, name, domain, vocab, cls, if (is.na(std)) "NULL" else sprintf("'%s'", std), code, to, why))
}

test_that("a clean CDM passes the structural checks", {
  res <- run_dqd_catalog(dqd_cdm(), today = dqd_today)
  expect_gt(length(res$CheckResults), 500)
  for (nm in c("isPrimaryKey", "isForeignKey", "plausibleStartBeforeEnd", "plausibleAfterBirth", "plausibleFutureDate")) {
    rows <- Filter(function(r) r$check_name == nm, res$CheckResults)
    expect_true(length(rows) > 0 && !any(vapply(rows, function(r) isTRUE(r$failed), logical(1))), info = nm)
  }
  expect_equal(res$Overview$count_total, length(res$CheckResults))
  expect_equal(dqd_by(res, "cdmTable", "visit_occurrence")$execution_status, "PASSED")
  expect_equal(dqd_by(res, "isRequired", "visit_occurrence", "person_id")$execution_status, "PASSED")
  expect_equal(dqd_by(res, "measureValueCompleteness", "death", "death_date")$execution_status, "NOT_APPLICABLE")
})

test_that("planted defects are flagged, as the official DQD counts them", {
  con <- dqd_cdm()
  DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES
    (1, 99, 9201, DATE '2020-03-05', DATE '2020-03-01', 0), (2, 1, 0, DATE '1970-01-01', DATE '1970-01-02', 0), (3, 1, 9201, DATE '2030-01-01', DATE '2030-01-02', 0)")
  DBI::dbExecute(con, "INSERT INTO death (person_id, death_date, death_type_concept_id) VALUES (1, DATE '2020-02-01', 0)")
  res <- run_dqd_catalog(con, today = dqd_today)
  expect_equal(dqd_by(res, "isPrimaryKey", "visit_occurrence", "visit_occurrence_id")$num_violated_rows, 2)
  expect_true(dqd_by(res, "isForeignKey", "visit_occurrence", "person_id")$failed)
  expect_equal(dqd_by(res, "plausibleStartBeforeEnd", "visit_occurrence", "visit_start_date")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "plausibleAfterBirth", "visit_occurrence", "visit_start_date")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "plausibleFutureDate", "visit_occurrence", "visit_start_date")$num_violated_rows, 1)
  expect_true(dqd_by(res, "plausibleBeforeDeath", "visit_occurrence", "visit_start_date")$failed)
  expect_true(dqd_by(res, "standardConceptRecordCompleteness", "visit_occurrence", "visit_concept_id")$failed)
  expect_gte(res$Overview$count_failed, 6)
  expect_equal(dqd_by(res, "isPrimaryKey", "person", "person_id")$check_origin, "DQD")
  expect_equal(dqd_by(res, "plausibleFutureDate", "visit_occurrence", "visit_start_date")$check_origin, "omop-duck-db")
})

test_that("a missing table is reported, tables restricts the checks, and run_dqd_full writes JSON", {
  con <- dqd_cdm()
  DBI::dbExecute(con, "DROP TABLE care_site")
  res <- run_dqd_catalog(con, tables = c("care_site", "person"), today = dqd_today)
  expect_true(dqd_by(res, "cdmTable", "care_site")$failed)
  expect_setequal(unique(vapply(res$CheckResults, function(r) r$cdm_table_name, "")), c("care_site", "person"))
  out <- tempfile(fileext = ".json")
  full <- run_dqd_full(dqd_cdm(), out, today = dqd_today)
  expect_true(file.exists(out))
  expect_gt(full$Overview$count_total, 500)
  expect_true(any(vapply(full$CheckResults, function(r) startsWith(r$check_id, "CAT_"), logical(1))))
})

test_that("concept domain and standard-validity checks run only when the concept table is populated", {
  con <- dqd_cdm()
  expect_false(any(vapply(run_dqd_catalog(con, today = dqd_today)$CheckResults, function(r) r$check_name %in% c("fkDomain", "isStandardValidConcept"), logical(1))))
  dqd_concept(con, 9201, "Inpatient", "Visit", "Visit", "Visit", "S", "IP")
  dqd_concept(con, 201820, "Diabetes", "Condition", "SNOMED", "Clinical Finding", "S", "x")
  dqd_concept(con, 1503297, "metformin", "Drug", "RxNorm", "Ingredient", "S", "y")
  dqd_concept(con, 4216643, "old death", "Condition", "SNOMED", "Clinical Finding", "S", "z", to = "2000-01-01", why = "'D'")
  dqd_concept(con, 8536, "src visit", "Visit", "Visit", "Visit", NA, "w")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES
    (1, 1, 201820, DATE '2020-01-01', 0), (2, 1, 1503297, DATE '2020-01-01', 0), (3, 1, 4216643, DATE '2020-01-01', 0), (4, 1, 0, DATE '2020-01-01', 0)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (5, 1, 8536, DATE '2020-01-05', DATE '2020-01-09', 0)")
  res <- run_dqd_catalog(con, today = dqd_today)
  expect_equal(dqd_by(res, "fkDomain", "condition_occurrence", "condition_concept_id")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "isStandardValidConcept", "condition_occurrence", "condition_concept_id")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "isStandardValidConcept", "visit_occurrence", "visit_concept_id")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "fkDomain", "condition_occurrence", "condition_concept_id")$check_origin, "DQD")
})

test_that("class, value, person-completeness, era and overlap checks find planted gaps", {
  con <- dqd_cdm()
  dqd_concept(con, 1503297, "metformin", "Drug", "RxNorm", "Ingredient", "S", "a")
  dqd_concept(con, 40000001, "metformin 500 mg", "Drug", "RxNorm", "Clinical Drug", "S", "b")
  DBI::dbExecute(con, "INSERT INTO drug_era (drug_era_id, person_id, drug_concept_id, drug_era_start_date, drug_era_end_date) VALUES
    (1, 1, 1503297, DATE '2020-01-01', DATE '2020-02-01'), (2, 1, 40000001, DATE '2020-01-01', DATE '2020-02-01')")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES
    (1, 1, 201820, DATE '2020-01-01', 0), (2, 2, 201820, DATE '2020-01-01', 0)")
  DBI::dbExecute(con, "INSERT INTO condition_era (condition_era_id, person_id, condition_concept_id, condition_era_start_date, condition_era_end_date) VALUES (1, 1, 201820, DATE '2020-01-01', DATE '2020-01-01')")
  DBI::dbExecute(con, "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) VALUES
    (1, 1, DATE '2019-01-01', DATE '2020-06-30', 0), (2, 1, DATE '2020-06-01', DATE '2021-01-01', 0), (3, 2, DATE '2019-01-01', DATE '2021-01-01', 0)")
  DBI::dbExecute(con, "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, days_supply, quantity, drug_source_concept_id) VALUES
    (1, 1, 1503297, DATE '2020-01-01', DATE '2020-02-01', 0, 30, 60, 0), (2, 1, 1503297, DATE '2020-01-01', DATE '2020-02-01', 0, 900, -1, 0)")
  DBI::dbExecute(con, "UPDATE person SET year_of_birth = 1700 WHERE person_id = 2")
  DBI::dbExecute(con, "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_concept_id) VALUES
    (1, 1, 3004249, DATE '2020-01-01', 0, 120, 8876), (2, 1, 3004249, DATE '2020-01-02', 0, 900, 8876), (3, 1, 3004249, DATE '2020-01-03', 0, 900, 0)")
  res <- run_dqd_catalog(con, today = dqd_today)
  expect_equal(dqd_by(res, "fkClass", "drug_era", "drug_concept_id")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "measureConditionEraCompleteness", "condition_era")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "measureObservationPeriodOverlap", "observation_period")$num_violated_rows, 2)
  expect_equal(dqd_by(res, "plausibleValueLow", "person", "year_of_birth")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "plausibleValueHigh", "drug_exposure", "days_supply")$num_violated_rows, 1)
  expect_equal(dqd_by(res, "plausibleValueLow", "drug_exposure", "quantity")$num_violated_rows, 1)
  hi <- dqd_by(res, "plausibleValueHigh", "measurement", "value_as_number")
  expect_equal(c(hi$num_violated_rows, hi$num_denominator_rows), c(1, 2))            # the unit-0 row is outside the limit table
  expect_true(dqd_by(res, "sourceConceptRecordCompleteness", "drug_exposure", "drug_source_concept_id")$failed)
  expect_equal(dqd_by(res, "measurePersonCompleteness", "condition_occurrence")$num_violated_rows, 0)
  expect_equal(dqd_by(res, "measurePersonCompleteness", "drug_exposure")$num_violated_rows, 1)   # person 2 has no drug
})
