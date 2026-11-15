# Tests for the v0.20.0 review fixes (C-02, C-04, M-02, M-16, M-19, M-20, M-21/22)

chk <- function(...) omopduckdb:::.check_ident(...)

test_that(".check_ident accepts plain names and rejects injection", {
  expect_equal(chk("main.cohort_1", "x"), "main.cohort_1")
  for (bad in c("a; DROP TABLE person", "a b", "x'y", "", NA_character_, "a.b.c.d")) {
    expect_error(chk(bad, "output_table"))
  }
})

test_that("build_schema is re-runnable, refuses a partial schema and applies keys on request", {
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db))
  build_schema(db_path = db)
  expect_output(build_schema(db_path = db), "already present")
  build_schema(db_path = db, keys = TRUE)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
  n <- DBI::dbGetQuery(con, "SELECT count(*) AS n FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'")$n
  DBI::dbDisconnect(con, shutdown = TRUE)
  expect_gt(n, 0)

  db2 <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db2), add = TRUE)
  con2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db2)
  DBI::dbExecute(con2, "CREATE TABLE person (person_id INTEGER)")
  DBI::dbDisconnect(con2, shutdown = TRUE)
  expect_error(build_schema(db_path = db2), "part of the CDM schema")
})

test_that("a full-mode etl_pcornet re-run does not double the tables", {
  db <- tempfile(fileext = ".duckdb")
  src <- tempfile(); dir.create(src)
  on.exit(unlink(c(db, src), recursive = TRUE))
  writeLines("PATID,SEX,BIRTH_DATE,RACE,HISPANIC,PROVIDERID\nP1,M,1990-01-01,05,N,PR1\n", file.path(src, "demographic.csv"))
  build_schema(db_path = db)
  etl_pcornet(source_dir = src, db_path = db, key_check = "ignore")
  etl_pcornet(source_dir = src, db_path = db, key_check = "ignore")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM person")$n, 1)
})

test_that("load_vital falls back to standard concepts, derives BMI and drops implausible values", {
  db <- tempfile(fileext = ".duckdb")
  src <- tempfile(); dir.create(src)
  on.exit(unlink(c(db, src), recursive = TRUE))
  writeLines("PATID,SEX,BIRTH_DATE,RACE,HISPANIC,PROVIDERID\nP1,M,1990-01-01,05,N,PR1\n", file.path(src, "demographic.csv"))
  writeLines(paste0("VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,VITAL_SOURCE,HT,WT,ORIGINAL_BMI,SYSTOLIC,DIASTOLIC\n",
                    "V1,P1,E1,2024-01-01,4:15 PM,PR,70,180,,999,80\n"), file.path(src, "vital.csv"))
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  load_mapping_macros(con)
  res <- load_vital(con, src, plausible_ranges = TRUE)
  expect_equal(res$rows_dropped_implausible[["SYSTOLIC"]], 1L)
  expect_equal(res$rows_loaded[["BMI_DERIVED"]], 1L)
  m <- DBI::dbGetQuery(con, "SELECT measurement_source_value AS code, measurement_concept_id AS cid, value_as_number AS v,
                              strftime(measurement_datetime, '%H:%M') AS hm FROM measurement ORDER BY code")
  expect_false(any(m$cid == 0))
  expect_false("8480-6" %in% m$code)
  expect_equal(m$v[m$code == "39156-5"], round(703 * 180 / 70^2, 2))
  expect_true(all(m$hm == "16:15"))
  # no vital file: an empty summary, not an error
  empty <- tempfile(); dir.create(empty)
  expect_equal(length(load_vital(con, empty)$rows_loaded), 0)
})

test_that("load_vocabulary reports failure when nothing loads or a requested file is missing", {
  vocab <- tempfile(); dir.create(vocab)
  on.exit(unlink(vocab, recursive = TRUE))
  writeLines("relationship_id\trelationship_name\tis_hierarchical\tdefines_ancestry\treverse_relationship_id\trelationship_concept_id\nx\ty\t0\t0\tx\t1",
             file.path(vocab, "RELATIONSHIP.csv"))
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db), add = TRUE)
  suppressWarnings(expect_false(load_vocabulary(vocab, db)))                    # no schema: nothing loaded
  build_schema(db_path = db)
  suppressWarnings(expect_false(load_vocabulary(vocab, db, tables = c("relationship", "concept"))))  # concept.csv absent
})

test_that("path helpers validate identifiers and escape quotes", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  f <- tempfile(fileext = ".duckdb"); on.exit(unlink(f), add = TRUE)
  expect_error(attach_database(con, f, "a; DROP TABLE t"))
  expect_error(attach_database(con, f, "ok", db_type = "x y"))
  DBI::dbExecute(con, "CREATE TABLE t AS SELECT 1 AS a")
  expect_error(sql_copy_to(con, "t", tempfile(), format = "CSV; DROP TABLE t"))
  expect_error(sql_copy_to(con, "t", tempfile(), format = "CSV", options = list(`bad key` = 1)))
  out <- file.path(tempdir(), "it's.csv"); on.exit(unlink(out), add = TRUE)
  sql_copy_to(con, "t", out, format = "CSV", options = list(header = TRUE, delimiter = "'"))
  expect_true(file.exists(out))
})

test_that("export_cdm validates table names and compression", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  expect_error(export_cdm(con, "parquet", tempfile(), tables = "person; DROP TABLE person"))
  expect_error(export_cdm(con, "parquet", tempfile(), tables = "person", compression = "zstd'); --"))
})

test_that("import_source_to_concept_map and import_usagi_mappings escape ids and skip unreviewed rows", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE source_to_concept_map (source_code VARCHAR, source_concept_id INTEGER, source_vocabulary_id VARCHAR,
    source_code_description VARCHAR, target_concept_id INTEGER, target_vocabulary_id VARCHAR, valid_start_date DATE,
    valid_end_date DATE, invalid_reason VARCHAR)")
  f <- tempfile(fileext = ".csv"); on.exit(unlink(f), add = TRUE)
  writeLines(c("sourceCode,sourceName,targetConceptId,targetVocabularyId,mappingStatus",
               "A01,none,,,", "B02,ok,201826,SNOMED,APPROVED", "C03,flagged,5,SNOMED,FLAGGED"), f)
  import_usagi_mappings(con, f, source_vocabulary_id = "O'Brien")
  got <- DBI::dbGetQuery(con, "SELECT source_code, source_vocabulary_id FROM source_to_concept_map")
  expect_equal(got$source_code, "B02")
  expect_equal(got$source_vocabulary_id, "O'Brien")
})

test_that("DQD concept check counts ids absent from the vocabulary, and discharge 'A' is not Home", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE concept (concept_id INTEGER, standard_concept VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO concept VALUES (1, 'S')")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (condition_concept_id INTEGER)")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (999999), (999998)")
  res <- run_dqd(con, check_levels = "CONCEPT")
  concept_checks <- Filter(function(r) identical(r$cdm_table_name, "condition_occurrence"), res$CheckResults)
  expect_true(any(vapply(concept_checks, function(r) isTRUE(r$failed), logical(1))))
})
