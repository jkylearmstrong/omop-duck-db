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

test_that("etl_pcornet handles extended ENC_TYPE and SEX mappings", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  source_dir <- tempfile()
  dir.create(source_dir)

  demo_csv <- "PATID,SEX,BIRTH_DATE,RACE,HISPANIC,PROVIDERID\nP1,OT,1990-01-01,05,N,PR1\nP2,UN,1985-05-05,05,N,PR1\nP3,NI,1980-10-10,05,N,PR1\n"
  writeLines(demo_csv, file.path(source_dir, "demographic.csv"))

  enc_csv <- "ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,PROVIDERID,FACILITYID,DISCHARGE_STATUS\nE1,P1,TH,2024-01-01,10:00,2024-01-01,10:30,PR1,F1,A\nE2,P2,OS,2024-01-02,11:00,2024-01-02,15:00,PR1,F1,A\nE3,P3,OT,2024-01-03,12:00,2024-01-03,12:45,PR1,F1,A\n"
  writeLines(enc_csv, file.path(source_dir, "encounter.csv"))

  build_schema(db_path = db_path)
  etl_pcornet(source_dir = source_dir, db_path = db_path)

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  genders <- DBI::dbGetQuery(con, "SELECT gender_source_value, gender_concept_id FROM person ORDER BY person_source_value")
  expect_equal(genders$gender_concept_id, c(8521, 8551, 8551)) # OT -> 8521, UN -> 8551, NI -> 8551

  visits <- DBI::dbGetQuery(con, "SELECT visit_source_value, visit_concept_id FROM visit_occurrence ORDER BY visit_source_value")
  expect_equal(visits$visit_concept_id, c(5083, 9201, 9202)) # TH -> 5083, OS -> 9201, OT -> 9202
})

test_that("etl_pcornet unpivots vital.csv into measurement with LOINC codes", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  source_dir <- tempfile()
  dir.create(source_dir)

  demo_csv <- "PATID,SEX,BIRTH_DATE,RACE,HISPANIC,PROVIDERID\nP1,M,1990-01-01,05,N,PR1\n"
  writeLines(demo_csv, file.path(source_dir, "demographic.csv"))

  vital_csv <- "VITALID,PATID,ENCOUNTERID,MEASURE_DATE,MEASURE_TIME,VITAL_SOURCE,HT,WT,ORIGINAL_BMI,SYSTOLIC,DIASTOLIC\nV1,P1,E1,2024-01-01,10:00,PR,70,180,25.8,120,80\n"
  writeLines(vital_csv, file.path(source_dir, "vital.csv"))

  build_schema(db_path = db_path)
  etl_pcornet(source_dir = source_dir, db_path = db_path)

  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  meas <- DBI::dbGetQuery(con, "SELECT measurement_source_value, value_as_number, unit_source_value FROM measurement ORDER BY measurement_source_value")
  expect_equal(nrow(meas), 5)
  expect_equal(meas$measurement_source_value, c("29463-7", "39156-5", "8302-2", "8462-4", "8480-6"))
  expect_equal(meas$value_as_number, c(180, 25.8, 70, 80, 120))
})

test_that("attach_central_vocabulary attaches zero-copy views", {
  vocab_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(vocab_path))
  cdm_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(cdm_path), add = TRUE)

  # Create central vocab with a mock concept
  c_vocab <- DBI::dbConnect(duckdb::duckdb(), dbdir = vocab_path)
  DBI::dbExecute(c_vocab, "CREATE TABLE concept (concept_id INT, concept_name VARCHAR, vocabulary_id VARCHAR, concept_code VARCHAR, standard_concept VARCHAR);")
  DBI::dbExecute(c_vocab, "INSERT INTO concept VALUES (3036277, 'Body height', 'LOINC', '8302-2', 'S');")
  DBI::dbDisconnect(c_vocab, shutdown = TRUE)

  # Build CDM schema and attach central vocab
  build_schema(db_path = cdm_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = cdm_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  attach_central_vocabulary(con, vocab_path, temporary = TRUE)
  res <- DBI::dbGetQuery(con, "SELECT concept_id, concept_name FROM concept WHERE concept_code = '8302-2'")
  expect_equal(nrow(res), 1)
  expect_equal(res$concept_id, 3036277)
})
