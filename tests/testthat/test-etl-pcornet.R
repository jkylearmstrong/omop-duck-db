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

test_that("load_care_site ingests facility.csv and/or creates root institutional site", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path))
  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  load_mapping_macros(con)

  source_dir <- tempfile()
  dir.create(source_dir)
  fac_csv <- "FACILITYID,FACILITY_TYPE,FACILITY_LOCATION\nFAC1,Hospital,Building A\n"
  writeLines(fac_csv, file.path(source_dir, "facility.csv"))

  # Case 1: facility.csv + root site
  load_care_site(con, source_dir, site_id = 1, site_anon = "Site A", site_name = "Hospital Alpha")
  rows <- DBI::dbGetQuery(con, "SELECT care_site_id, care_site_name, care_site_source_value FROM care_site ORDER BY care_site_id")
  expect_equal(nrow(rows), 2)
  root <- rows[rows$care_site_id == 1, ]
  expect_equal(root$care_site_name, "Site A")
  expect_equal(root$care_site_source_value, "Hospital Alpha")

  # Case 2: empty source_dir, root site only
  DBI::dbExecute(con, "DELETE FROM care_site")
  source_empty <- tempfile()
  dir.create(source_empty)
  load_care_site(con, source_empty, site_id = 2, site_anon = "Site B", site_name = "Hospital Beta")
  rows2 <- DBI::dbGetQuery(con, "SELECT care_site_id, care_site_name, care_site_source_value FROM care_site")
  expect_equal(nrow(rows2), 1)
  expect_equal(rows2$care_site_id, 2)
  expect_equal(rows2$care_site_name, "Site B")
  expect_equal(rows2$care_site_source_value, "Hospital Beta")
})

test_that("etl_pcornet site identification, disambiguation, and cdm_source metadata", {
  source_dir <- tempfile()
  dir.create(source_dir)

  writeLines("PATID,SEX,BIRTH_DATE,RACE,HISPANIC,PROVIDERID\n100,M,1990-01-01,05,N,PR1\n", file.path(source_dir, "demographic.csv"))
  writeLines("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,ADMIT_TIME,DISCHARGE_DATE,DISCHARGE_TIME,PROVIDERID,FACILITYID,DISCHARGE_STATUS\nE1,100,IP,2024-01-01,10:00,2024-01-02,12:00,PR1,,A\n", file.path(source_dir, "encounter.csv"))
  writeLines("DIAGNOSISID,PATID,ENCOUNTERID,ENC_TYPE,ADMIT_DATE,PROVIDERID,DX,DX_TYPE,DX_SOURCE,PDX\nD1,100,E1,IP,2024-01-01,PR1,I10,10,DI,P\n", file.path(source_dir, "diagnosis.csv"))
  writeLines("PROCEDURESID,PATID,ENCOUNTERID,PX,PX_TYPE,PX_DATE,PROVIDERID\nPX1,100,E1,99213,01,2024-01-01,PR1\n", file.path(source_dir, "procedures.csv"))
  writeLines("LAB_RESULT_CM_ID,PATID,ENCOUNTERID,SPECIMEN_DATE,SPECIMEN_TIME,RESULT_DATE,RESULT_TIME,RESULT_NUM,RESULT_UNIT,LAB_LOINC,PROVIDERID,RAW_LAB_NAME,RAW_LAB_CODE\nL1,100,E1,2024-01-01,10:00,2024-01-01,10:30,5.4,mg/dL,2345-7,PR1,Glucose,GLU\n", file.path(source_dir, "lab_result_cm.csv"))
  writeLines("PRESCRIBINGID,PATID,ENCOUNTERID,RX_PROVIDERID,RX_ORDER_DATE,RX_ORDER_TIME,RX_START_DATE,RX_END_DATE,RX_DOSE_ORDERED,RX_DOSE_ORDERED_UNIT,RX_QUANTITY,RX_REFILLS,RXNORM_CUI,RAW_RX_MED_NAME,RAW_RX_NDC\nRX1,100,E1,PR1,2024-01-01,10:00,2024-01-01,2024-01-10,10,mg,30,0,197361,Aspirin,\n", file.path(source_dir, "prescribing.csv"))
  writeLines("PATID,DEATH_DATE,DEATH_DATE_IMPUTE,DEATH_SOURCE,DEATH_MATCH_CONFIDENCE\n100,2024-02-01,N,L,H\n", file.path(source_dir, "death.csv"))

  db1_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db1_path))
  build_schema(db_path = db1_path)

  etl_pcornet(
    source_dir = source_dir,
    db_path = db1_path,
    site_id = 1,
    site_anon = "Site A",
    site_name = "Hospital Alpha",
    disambiguate_patids = TRUE
  )

  con1 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db1_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con1, shutdown = TRUE), add = TRUE)

  # Check cdm_source
  src_df <- DBI::dbGetQuery(con1, "SELECT cdm_source_name, cdm_source_abbreviation, cdm_holder FROM cdm_source")
  expect_equal(src_df$cdm_source_name, "Site A")
  expect_equal(src_df$cdm_source_abbreviation, "Site A")
  expect_equal(src_df$cdm_holder, "Hospital Alpha")

  # Check person
  p_df <- DBI::dbGetQuery(con1, "SELECT person_id, person_source_value, care_site_id FROM person")
  expect_equal(p_df$person_source_value, "100-1")
  expect_equal(p_df$care_site_id, 1)
  expected_pid <- p_df$person_id[1]

  # Check FK across tables
  for (tbl in c("visit_occurrence", "condition_occurrence", "procedure_occurrence", "measurement", "drug_exposure", "death")) {
    pid <- DBI::dbGetQuery(con1, sprintf("SELECT person_id FROM %s LIMIT 1", tbl))$person_id[1]
    expect_equal(pid, expected_pid)
  }

  vo_cs <- DBI::dbGetQuery(con1, "SELECT care_site_id FROM visit_occurrence")$care_site_id[1]
  expect_equal(vo_cs, 1)

  DBI::dbDisconnect(con1, shutdown = TRUE)

  # Run site 2 with same PATID
  db2_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db2_path), add = TRUE)
  build_schema(db_path = db2_path)

  etl_pcornet(
    source_dir = source_dir,
    db_path = db2_path,
    site_id = 2,
    site_anon = "Site B",
    site_name = "Hospital Beta",
    disambiguate_patids = TRUE
  )

  con2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db2_path, read_only = TRUE)
  on.exit(DBI::dbDisconnect(con2, shutdown = TRUE), add = TRUE)

  p_df2 <- DBI::dbGetQuery(con2, "SELECT person_id, person_source_value, care_site_id FROM person")
  expect_equal(p_df2$person_source_value, "100-2")
  expect_equal(p_df2$care_site_id, 2)
  expect_false(p_df2$person_id[1] == expected_pid)
})

test_that("create_federated_consortium connects multiple sites with zero-copy v_* views", {
  db1_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db1_path))
  build_schema(db_path = db1_path)
  con1 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db1_path)
  load_mapping_macros(con1)
  load_care_site(con1, tempdir(), site_id = 1, site_anon = "Site A", site_name = "Hospital Alpha")
  DBI::dbExecute(con1, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value, care_site_id) VALUES (101, 8507, 1990, 8527, 38003564, 'P1-1', 1);")
  DBI::dbExecute(con1, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, care_site_id) VALUES (201, 101, 9201, '2024-01-01', '2024-01-01', 32827, 1);")
  DBI::dbDisconnect(con1, shutdown = TRUE)

  db2_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db2_path), add = TRUE)
  build_schema(db_path = db2_path)
  con2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db2_path)
  load_mapping_macros(con2)
  load_care_site(con2, tempdir(), site_id = 2, site_anon = "Site B", site_name = "Hospital Beta")
  DBI::dbExecute(con2, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value, care_site_id) VALUES (102, 8532, 1985, 8527, 38003564, 'P2-2', 2);")
  DBI::dbExecute(con2, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, care_site_id) VALUES (202, 102, 9202, '2024-02-01', '2024-02-01', 32827, 2);")
  DBI::dbDisconnect(con2, shutdown = TRUE)

  fed_con <- create_federated_consortium(c("Site A" = db1_path, "Site B" = db2_path))
  on.exit(DBI::dbDisconnect(fed_con, shutdown = TRUE), add = TRUE)

  persons <- DBI::dbGetQuery(fed_con, "SELECT site_id, site_anon, person_id, person_source_value FROM v_person ORDER BY site_id")
  expect_equal(nrow(persons), 2)
  expect_equal(persons$site_id, c(1, 2))
  expect_equal(persons$site_anon, c("Site A", "Site B"))
  expect_equal(persons$person_id, c(101, 102))

  visits <- DBI::dbGetQuery(fed_con, "SELECT site_id, site_anon, visit_occurrence_id, person_id FROM v_visit_occurrence ORDER BY site_id")
  expect_equal(nrow(visits), 2)
  expect_equal(visits$site_id, c(1, 2))
  expect_equal(visits$site_anon, c("Site A", "Site B"))
  expect_equal(visits$visit_occurrence_id, c(201, 202))
})

