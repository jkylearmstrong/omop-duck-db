test_that("etl_pcornet loads the fixture with expected row counts", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  fixture_dir <- normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))

  build_schema(db_path = db_path)
  etl_pcornet(source_dir = fixture_dir, db_path = db_path)

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  count_of <- function(tbl) DBI::dbGetQuery(con, paste0("SELECT COUNT(*) AS n FROM ", tbl))$n

  expect_equal(count_of("person"), 3)
  expect_equal(count_of("provider"), 2)
  expect_equal(count_of("visit_occurrence"), 3)
  expect_equal(count_of("condition_occurrence"), 4)
  expect_equal(count_of("procedure_occurrence"), 3)
  expect_equal(count_of("measurement"), 3)
  expect_equal(count_of("drug_exposure"), 3)
})

test_that("etl_pcornet skips tables whose source file is missing", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  source_dir <- tempfile()
  dir.create(source_dir)
  # Only demographic.csv present -- everything else should be skipped, not error.
  file.copy(
    file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample", "demographic.csv"),
    file.path(source_dir, "demographic.csv")
  )

  build_schema(db_path = db_path)
  expect_no_error(etl_pcornet(source_dir = source_dir, db_path = db_path))

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM person")$n, 3)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM visit_occurrence")$n, 0)
})
