test_that("route_drug_domains moves non-Drug concepts and sets value_as_number NULL (0.36.0 M1/M2)", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path), add = TRUE)
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(try(DBI::dbDisconnect(con, shutdown = TRUE), silent = TRUE), add = TRUE)
  DBI::dbExecute(con, "
    INSERT INTO concept VALUES
    (1503297, 'metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (4200001, 'Insulin syringe', 'Device', 'SNOMED', 'Physical Object', 'S', 'SYR1', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (900002, 'IV infusion', 'Procedure', 'HCPCS', 'HCPCS', 'S', 'J9999', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (4300001, 'Care observation', 'Observation', 'SNOMED', 'Context-dependent', 'S', 'OBS1', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (3004501, 'Glucose test', 'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7', DATE '1970-01-01', DATE '2099-12-31', NULL)
  ")
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1970, 0, 0, 'P1')")
  DBI::dbExecute(con, "
    INSERT INTO drug_exposure (
      drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
      drug_type_concept_id, quantity, drug_source_value, drug_source_concept_id, dose_unit_source_value
    ) VALUES
    (1, 1, 1503297, DATE '2021-01-01', DATE '2021-01-01', 38000180, 500.0, '6809', 1503297, 'mg'),
    (2, 1, 4200001, DATE '2021-01-01', DATE '2021-01-01', 38000180, 1.0, 'SYR1', 4200001, 'each'),
    (3, 1, 900002, DATE '2021-01-01', DATE '2021-01-01', 38000180, 1.0, 'J9999', 900002, 'bag'),
    (4, 1, 4300001, DATE '2021-01-01', DATE '2021-01-01', 38000180, 15.0, 'OBS1', 4300001, 'mL'),
    (5, 1, 3004501, DATE '2021-01-01', DATE '2021-01-01', 38000180, 2.5, '2345-7', 3004501, 'strip')
  ")

  dry <- route_drug_domains(con, dry_run = TRUE)
  expect_equal(dry$total_routed, 4L)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM drug_exposure")$n[[1]], 5)

  res <- route_drug_domains(con)
  expect_equal(res$total_routed, 4L)
  expect_equal(DBI::dbGetQuery(con, "SELECT drug_concept_id FROM drug_exposure")$drug_concept_id, 1503297)
  expect_equal(DBI::dbGetQuery(con, "SELECT device_concept_id FROM device_exposure")$device_concept_id, 4200001)
  expect_equal(DBI::dbGetQuery(con, "SELECT procedure_concept_id FROM procedure_occurrence")$procedure_concept_id, 900002)

  obs <- DBI::dbGetQuery(con, "SELECT value_as_number, value_source_value, unit_source_value FROM observation")
  expect_true(is.na(obs$value_as_number[[1]]))
  expect_equal(obs$value_source_value[[1]], "15")
  expect_equal(obs$unit_source_value[[1]], "mL")

  meas <- DBI::dbGetQuery(con, "SELECT value_as_number, value_source_value, unit_source_value FROM measurement")
  expect_true(is.na(meas$value_as_number[[1]]))
  expect_equal(meas$value_source_value[[1]], "2.5")
  expect_equal(meas$unit_source_value[[1]], "strip")
})

test_that("backfill_all R wrapper validates db_path argument cleanly (0.36.0 S2)", {
  expect_error(backfill_all(123, tempdir()), "db_path")
})

test_that("etl_pcornet records fallback dates and dropped undated rows on attr and callback (0.36.0)", {
  src <- tempfile("pcornet_dates_")
  dir.create(src)
  on.exit(unlink(src, recursive = TRUE), add = TRUE)
  writeLines(c("PATID,BIRTH_DATE,SEX,RACE,HISPANIC", "P1,1970-01-01,F,05,N"), file.path(src, "demographic.csv"))
  writeLines(c("ENCOUNTERID,PATID,ENC_TYPE,ADMIT_DATE,DISCHARGE_DATE,DISCHARGE_STATUS",
               "E1,P1,IP,2021-07-10,2021-07-12,HO",
               "E_BAD,P1,IP,NI,,HO"), file.path(src, "encounter.csv"))
  writeLines(c("PROCEDURESID,PATID,ENCOUNTERID,PX_TYPE,PX,PX_DATE,ADMIT_DATE",
               "PX1,P1,E1,02,J9999,,2021-07-11",
               "PX2,P1,E1,02,J9999,,",
               "PX_BAD,P1,,02,J9999,NI,"), file.path(src, "procedures.csv"))

  db_path <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db_path), add = TRUE)
  build_schema(db_path)

  got_cb <- NULL
  res <- etl_pcornet(src, db_path, key_check = "ignore", date_audit_callback = function(rep) got_cb <<- rep)
  expect_equal(as.character(res), db_path)
  expect_equal(attr(res, "date_audit"), got_cb)
  by_src <- stats::setNames(got_cb, vapply(got_cb, `[[`, character(1), "source"))
  expect_equal(by_src$ENCOUNTER$dropped_undated, 1L)
  expect_equal(by_src$PROCEDURES$dated_by_fallback, 2L)
  expect_equal(by_src$PROCEDURES$fallback_by_column$ADMIT_DATE, 1L)
  expect_equal(by_src$PROCEDURES$fallback_by_column$visit_start_date, 1L)
  expect_equal(by_src$PROCEDURES$dropped_undated, 1L)
})
