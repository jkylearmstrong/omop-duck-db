test_that("load_vocabulary loads a small vocab bundle", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  vocab_dir <- file.path(testthat::test_path(), "testdata", "tiny_vocab")

  build_schema(db_path = db_path)
  ok <- load_vocabulary(vocab_dir = vocab_dir, db_path = db_path)
  expect_true(ok)

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  concepts <- DBI::dbGetQuery(con, "SELECT concept_code FROM concept ORDER BY concept_code")
  expect_equal(concepts$concept_code, c("FAKE001", "FAKE002"))
})

test_that("load_vocabulary rolls back a table with a NOT NULL violation, leaving prior data intact", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))

  build_schema(db_path = db_path)
  load_vocabulary(vocab_dir = file.path(testthat::test_path(), "testdata", "tiny_vocab"), db_path = db_path)

  ok <- suppressWarnings(load_vocabulary(
    vocab_dir = file.path(testthat::test_path(), "testdata", "tiny_vocab_bad"),
    db_path = db_path
  ))
  expect_false(ok)

  # DuckDB's Windows file lock can briefly outlive dbDisconnect(shutdown=TRUE)
  # until the R-level connection object is garbage collected.
  gc()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  # The failed reload should have rolled back -- original 2 rows still present,
  # not the bad row and not an empty table.
  concepts <- DBI::dbGetQuery(con, "SELECT concept_code FROM concept ORDER BY concept_code")
  expect_equal(concepts$concept_code, c("FAKE001", "FAKE002"))
})
