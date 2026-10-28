fixture_dir <- function() normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))

test_that("observation_period from ENROLLMENT merges spans and falls back to events", {
  src <- tempfile(); dir.create(src)
  file.copy(list.files(fixture_dir(), full.names = TRUE), src)
  writeLines(c(
    "PATID,ENR_START_DATE,ENR_END_DATE,CHART,ENR_BASIS",
    "FAKE-P001,2023-01-01,2023-06-30,Y,I",
    "FAKE-P001,2023-07-01,2024-12-31,Y,I",   # touches the first span: merged
    "FAKE-P001,2025-02-01,2025-06-30,Y,I",   # after a gap: second period
    "FAKE-P001,1850-01-01,1850-02-01,Y,I"),  # before min_date: dropped
    file.path(src, "enrollment.csv"))
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db))
  build_schema(db_path = db)
  capture.output(etl_pcornet(src, db, key_check = "ignore", enrollment_type_concept_id = 32880L))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  rows <- DBI::dbGetQuery(con, "SELECT p.person_source_value AS psv, o.observation_period_start_date AS s,
    o.observation_period_end_date AS e, o.period_type_concept_id AS t
    FROM observation_period o JOIN person p USING (person_id) ORDER BY 1, 2")
  p1 <- rows[rows$psv == "FAKE-P001", ]
  expect_equal(as.character(p1$s), c("2023-01-01", "2025-02-01"))
  expect_equal(as.character(p1$e), c("2024-12-31", "2025-06-30"))
  expect_equal(p1$t, c(32880, 32880))
  others <- rows[rows$psv != "FAKE-P001", ]
  expect_gt(nrow(others), 0)
  expect_true(all(others$t == 32827))
  expect_true(DBI::dbGetQuery(con, "SELECT COUNT(DISTINCT observation_period_id) = COUNT(*) AS ok FROM observation_period")$ok)
})

test_that("use_enrollment = TRUE without an ENROLLMENT file fails; bad values are rejected", {
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db))
  build_schema(db_path = db)
  expect_error(capture.output(etl_pcornet(fixture_dir(), db, key_check = "ignore", use_enrollment = TRUE)),
               "no ENROLLMENT file")
  expect_error(etl_pcornet(fixture_dir(), db, use_enrollment = "maybe"), "use_enrollment must be")
})

test_that("data_cutoff = 'auto' caps observation_period ends at the latest event before today (N2)", {
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db))
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id,
    ethnicity_concept_id) VALUES (1, 8507, 1980, 8527, 38003563)")
  for (d in c("2020-01-05", "2023-04-01", format(Sys.Date()))) {
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id,
      visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, 1, 9201, DATE '%s', DATE '%s', 0)",
      match(d, c("2020-01-05", "2023-04-01", format(Sys.Date()))), d, d))
  }
  expect_null(omopduckdb:::.resolve_max_date(con, NULL))
  expect_equal(omopduckdb:::.resolve_max_date(con, "auto"), "2023-04-01")
  capture.output(build_observation_period(con))
  expect_equal(as.character(DBI::dbGetQuery(con, "SELECT max(observation_period_end_date) AS e FROM observation_period")$e),
               format(Sys.Date()))
  capture.output(build_observation_period(con, max_date = "auto"))
  expect_equal(as.character(DBI::dbGetQuery(con, "SELECT max(observation_period_end_date) AS e FROM observation_period")$e),
               "2023-04-01")
})
