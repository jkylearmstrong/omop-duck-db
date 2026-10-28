fixture_dir <- function() normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))

test_that("check_key_collisions reports a collision and honours on_collision", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE t AS SELECT * FROM (VALUES ('a'), ('b'), ('c')) v(id)")
  DBI::dbExecute(con, "CREATE MACRO pcornet_id(x) AS length(x)")  # 'a','b','c' all collide
  expect_warning(r <- check_key_collisions(con, "t", "id"), "collision")
  expect_equal(r$lost, 2)
  expect_false(r$ok)
  expect_error(check_key_collisions(con, "t", "id", on_collision = "raise"), class = "key_collision_error")
  expect_silent(check_key_collisions(con, "t", "id", on_collision = "ignore"))
  expect_error(check_key_collisions(con, "t", "id; DROP TABLE t"), "Invalid id_column")
})

test_that("etl_pcornet runs the key check and passes the report to the callback", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  build_schema(db_path = db_path)
  got <- NULL
  out <- capture.output(etl_pcornet(fixture_dir(), db_path, key_check = "raise",
                                    key_check_callback = function(r) got <<- r))
  expect_true(any(grepl("Key check encounter.ENCOUNTERID", out)))
  expect_length(got, 1)
  expect_true(got[[1]]$ok)
  expect_equal(got[[1]]$cdm_rows, got[[1]]$distinct_keys)
  expect_equal(got[[1]]$cdm_table, "visit_occurrence")
})

test_that("etl_pcornet(key_check = 'ignore') skips the check", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  build_schema(db_path = db_path)
  out <- capture.output(etl_pcornet(fixture_dir(), db_path, key_check = "ignore"))
  expect_false(any(grepl("Key check", out)))
})

test_that("check_etl_keys also checks PATID when ids are disambiguated by site", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  build_schema(db_path = db_path)
  capture.output(etl_pcornet(fixture_dir(), db_path, site_id = 1, disambiguate_patids = TRUE, key_check = "ignore"))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  load_mapping_macros(con)
  rep <- check_etl_keys(con, fixture_dir(), site_id = 1, disambiguate_patids = TRUE)
  expect_equal(vapply(rep, `[[`, "", "table"), c("encounter", "demographic"))
  expect_true(all(vapply(rep, `[[`, NA, "ok")))
  expect_equal(rep[[2]]$cdm_table, "person")
  # a source directory without encounter/demographic files yields an empty report
  expect_length(check_etl_keys(con, tempfile()), 0)
})

test_that("check_key_collisions rejects bad sources and accepts quoted columns", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE t AS SELECT 'a' AS \"My Id\"")
  DBI::dbExecute(con, "CREATE MACRO pcornet_id(x) AS hash(x)")
  expect_error(check_key_collisions(con, "t; DROP TABLE t", "id"), "Invalid source")
  expect_true(check_key_collisions(con, "t", "\"My Id\"")$ok)
})
