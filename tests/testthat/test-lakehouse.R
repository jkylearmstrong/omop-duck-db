test_that("omop_connect_lakehouse registers views over mock lakehouse tables", {
  tmp_dir <- tempfile(pattern = "lake_")
  dir.create(tmp_dir)
  person_dir <- file.path(tmp_dir, "person")
  dir.create(person_dir)

  on.exit({
    unlink(tmp_dir, recursive = TRUE)
  }, add = TRUE)

  df1 <- data.frame(
    person_id = c(1L, 2L),
    gender_concept_id = c(8507L, 8532L),
    year_of_birth = c(1980L, 1990L)
  )

  init_con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  duckdb::duckdb_register(init_con, "df1", df1)
  parquet_path <- file.path(person_dir, "part-0.parquet")
  parquet_path <- gsub("\\\\", "/", parquet_path)
  DBI::dbExecute(init_con, sprintf("COPY df1 TO '%s' (FORMAT PARQUET);", parquet_path))
  DBI::dbDisconnect(init_con, shutdown = TRUE)

  con <- omop_connect_lakehouse(
    lake_uri = tmp_dir,
    format = "delta",
    tables = c("person")
  )
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  res_v <- DBI::dbGetQuery(con, "SELECT * FROM v_person ORDER BY person_id")
  expect_equal(nrow(res_v), 2L)
  expect_equal(res_v$person_id, c(1L, 2L))

  res_bare <- DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person")
  expect_equal(res_bare$n, 2L)
})
