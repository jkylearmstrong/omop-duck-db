test_that("load_from_bigquery calls bq_table_download with valid arguments and keeps 64-bit ids", {
  skip_if_not_installed("bigrquery")
  seen <- NULL
  # same formals as bigrquery::bq_table_download (no `...`), so an unknown argument name is an error as in production
  fake_download <- function(x, n_max = Inf, page_size = NULL, start_index = 0L, max_connections = 6L, quiet = NA,
                            bigint = c("integer", "integer64", "numeric", "character"), api = c("json", "arrow"),
                            billing = x$project) {
    seen <<- list(bigint = match.arg(bigint), billing = billing, project = x$project, table = x$table)
    data.frame(concept_id = bit64::as.integer64(c(1, 2^40)), concept_name = c("a", "b"))
  }
  testthat::local_mocked_bindings(bq_table_download = fake_download, .package = "bigrquery")
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db), add = TRUE)
  capture.output(load_from_bigquery(billing_project = "my-proj", dataset = "bigquery-public-data.some_omop",
                                    tables = "concept", db_path = db))
  expect_equal(seen$billing, "my-proj")
  expect_equal(seen$bigint, "integer64")
  expect_equal(seen$project, "bigquery-public-data")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  expect_equal(DBI::dbGetQuery(con, "SELECT max(concept_id)::DOUBLE AS m FROM concept")$m, 2^40)
})
