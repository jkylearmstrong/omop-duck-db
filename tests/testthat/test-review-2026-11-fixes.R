# Regression tests for the 2026-11-12 external review (v0.20.0). Each test names its review id.

fixture_pcornet <- function() {
  normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))
}

new_cdm <- function(env = parent.frame()) {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  withr::defer(unlink(db), envir = env)
  db
}

macro_con <- function(env = parent.frame()) {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE), envir = env)
  build_schema_in <- function(con) {
    ddl <- readLines(system.file("extdata", "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql", package = "omopduckdb"))
    DBI::dbExecute(con, gsub(" NUMERIC ", " DOUBLE ", gsub("@cdmDatabaseSchema.", "", paste(ddl, collapse = "\n"), fixed = TRUE)))
  }
  build_schema_in(con)
  load_mapping_macros(con)
  con
}

test_that("C-01: parse_omop_datetime resolves AM/PM and compact times", {
  con <- macro_con()
  q <- function(d, t) {
    as.character(DBI::dbGetQuery(con, sprintf("SELECT parse_omop_datetime(%s, %s) AS x", d, t))$x)
  }
  expect_equal(q("'2020-01-01'", "'4:15 PM'"), "2020-01-01 16:15:00")
  expect_equal(q("'2020-01-01'", "'11:30 PM'"), "2020-01-01 23:30:00")
  expect_equal(q("'2020-01-01'", "'12:05 AM'"), "2020-01-01 00:05:00")
  expect_equal(q("'01JAN2020'", "'4:15 PM'"), "2020-01-01 16:15:00")
  expect_equal(q("'2020-01-01'", "'415'"), "2020-01-01 04:15:00")
  expect_equal(q("'20200105'", "'1530'"), "2020-01-05 15:30:00")
  expect_equal(as.character(DBI::dbGetQuery(con, "SELECT CAST(parse_omop_date('20200105') AS DATE) AS d")$d), "2020-01-05")
})

test_that("C-02: a repeated full ETL run refuses, and overwrite = TRUE is idempotent", {
  db <- new_cdm()
  capture.output(etl_pcornet(fixture_pcornet(), db, key_check = "ignore"))
  counts <- function() {
    con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
    on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
    c(person = DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM person")$n,
      condition = DBI::dbGetQuery(con, "SELECT COUNT(*) n FROM condition_occurrence")$n)
  }
  first <- counts()
  expect_error(capture.output(etl_pcornet(fixture_pcornet(), db, key_check = "ignore")), "already holds ETL output")
  expect_equal(counts(), first)
  capture.output(etl_pcornet(fixture_pcornet(), db, key_check = "ignore", overwrite = TRUE))
  expect_equal(counts(), first)
})

test_that("C-03: consortium_patient_overlap has no self-pairs", {
  df <- consortium_patient_overlap(list(A = paste0("t", 1:5), B = paste0("u", 1:100)))
  expect_true(all(df$site_1 != df$site_2))
  expect_equal(nrow(df), 1L)
})

test_that("C-04: load_vital falls back to standard concepts, derives BMI and filters implausible values", {
  db <- new_cdm()
  src <- tempfile("vital_"); dir.create(src); on.exit(unlink(src, recursive = TRUE), add = TRUE)
  write.csv(data.frame(
    VITALID = c("V1", "V2"), PATID = "P1", ENCOUNTERID = "", MEASURE_DATE = c("2021-01-01", "2021-02-01"),
    MEASURE_TIME = "08:00", VITAL_SOURCE = "HC", HT = c("70", ""), WT = c("180", ""),
    ORIGINAL_BMI = c("", "25.8"), BMI = "", SYSTOLIC = c("120", "999"), DIASTOLIC = c("80", ""), SMOKING = ""
  ), file.path(src, "vital.csv"), row.names = FALSE, na = "")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  load_mapping_macros(con)
  out <- capture.output(res <- load_vital(con, src, plausible_ranges = TRUE))
  expect_equal(res$rows_loaded$BMI_DERIVED, 1L)
  expect_equal(res$rows_dropped_implausible$SYSTOLIC, 1L)
  cids <- DBI::dbGetQuery(con, "SELECT DISTINCT measurement_concept_id AS c FROM measurement")$c
  expect_false(0 %in% cids)
  expect_true(3004249 %in% cids)
})

test_that("M-03/M-04: race and ethnicity codes map without erasing unknowns", {
  db <- new_cdm()
  src <- tempfile("demo_"); dir.create(src); on.exit(unlink(src, recursive = TRUE), add = TRUE)
  file.copy(list.files(fixture_pcornet(), full.names = TRUE), src)
  write.csv(data.frame(PATID = c("P1", "P2", "P3"), SEX = "F", BIRTH_DATE = "1980-01-01",
                       RACE = c("01", "04", "06"), HISPANIC = c("Y", "N", "NI"), PROVIDERID = ""),
            file.path(src, "demographic.csv"), row.names = FALSE)
  capture.output(etl_pcornet(src, db, key_check = "ignore"))
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  p <- DBI::dbGetQuery(con, "SELECT person_source_value s, race_concept_id r, ethnicity_concept_id e FROM person ORDER BY 1")
  expect_equal(p$r, c(8657, 8557, 8522))
  expect_equal(p$e, c(38003563, 38003564, 0))
})

test_that("M-18: SMD of perfectly separated constants is Inf, of equal constants 0", {
  expect_equal(.calc_smd_cont(rep(5, 10), rep(7, 10)), Inf)
  expect_equal(.calc_smd_cont(rep(5, 10), rep(5, 10)), 0)
  expect_equal(.calc_smd_bin(0, 1), Inf)
  expect_equal(.calc_smd_bin(0.5, 0.5), 0)
})

test_that("M-27: Usagi import needs explicit approval and a non-zero target", {
  db <- new_cdm()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  f <- tempfile(fileext = ".csv"); on.exit(unlink(f), add = TRUE)
  write.csv(data.frame(sourceCode = c("A01", "B02", "B02", "C03"), sourceName = "n",
                       targetConceptId = c(NA, 201826, 999999, 555),
                       targetVocabularyId = "SNOMED", mappingStatus = c("", "APPROVED", "APPROVED", "FLAGGED")),
            f, row.names = FALSE, na = "")
  expect_equal(import_usagi_mappings(con, f), 1L)
  got <- DBI::dbGetQuery(con, "SELECT source_code, target_concept_id FROM source_to_concept_map")
  expect_equal(got$source_code, "B02")
  expect_equal(got$target_concept_id, 201826)
})

test_that("M-20: a quote in default_vocabulary_id cannot break out; missing target column errors", {
  db <- new_cdm()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  f <- tempfile(fileext = ".csv"); on.exit(unlink(f), add = TRUE)
  write.csv(data.frame(source_code = "E11", target_concept_id = 201826), f, row.names = FALSE)
  evil <- "x'); DROP TABLE person; --"
  expect_equal(import_source_to_concept_map(con, f, default_vocabulary_id = evil), 1L)
  expect_true("person" %in% DBI::dbListTables(con))
  write.csv(data.frame(source_code = "E11"), f, row.names = FALSE)
  expect_error(import_source_to_concept_map(con, f), "target_concept_id")
})

test_that("M-11: remap prefers the custom map over the larger 'Maps to' concept id", {
  db <- new_cdm()
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  DBI::dbExecute(con, "INSERT INTO concept VALUES (1, 'icd', 'Condition', 'ICD10CM', 'x', NULL, 'E11.9', DATE '2000-01-01', DATE '2099-12-31', NULL)")
  DBI::dbExecute(con, "INSERT INTO concept VALUES (999999, 'std', 'Condition', 'SNOMED', 'x', 'S', 'X', DATE '2000-01-01', DATE '2099-12-31', NULL)")
  DBI::dbExecute(con, "INSERT INTO concept_relationship VALUES (1, 999999, 'Maps to', DATE '2000-01-01', DATE '2099-12-31', NULL)")
  DBI::dbExecute(con, "INSERT INTO source_to_concept_map (source_code, source_concept_id, source_vocabulary_id, target_concept_id, target_vocabulary_id, valid_start_date, valid_end_date) VALUES ('E11.9', 0, 'ICD10CM', 201826, 'SNOMED', DATE '2020-01-01', DATE '2099-12-31')")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value) VALUES (1, 1, 0, DATE '2021-01-01', 0, 'E11.9')")
  capture.output(remap_cdm_table(con, "condition_occurrence"))
  expect_equal(DBI::dbGetQuery(con, "SELECT condition_concept_id c FROM condition_occurrence")$c, 201826)
})
