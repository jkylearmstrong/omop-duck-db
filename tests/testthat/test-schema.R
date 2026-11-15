test_that("build_schema creates the expected core CDM tables", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))

  build_schema(db_path = db_path)

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  tables <- tolower(DBI::dbListTables(con))
  expect_true(all(c(
    "person", "visit_occurrence", "condition_occurrence",
    "procedure_occurrence", "measurement", "drug_exposure",
    "provider", "concept", "concept_relationship"
  ) %in% tables))
})
