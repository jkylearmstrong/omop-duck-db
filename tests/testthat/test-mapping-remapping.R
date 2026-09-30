test_that("import_source_to_concept_map and import_usagi_mappings populate STCM", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  })

  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  # 1. Custom STCM CSV
  stcm_csv <- tempfile(fileext = ".csv")
  write.csv(data.frame(
    source_code = c("LOCAL-LAB-01", "LOCAL-LAB-02"),
    target_concept_id = c(3004410, 3004411),
    source_vocabulary_id = c("CustomLab", "CustomLab"),
    source_code_description = c("A1c local", "Glucose local")
  ), stcm_csv, row.names = FALSE)

  n <- import_source_to_concept_map(con, stcm_csv)
  expect_equal(n, 2)

  # 2. Usagi CSV
  usagi_csv <- tempfile(fileext = ".csv")
  write.csv(data.frame(
    sourceCode = c("LOCAL-DRUG-01", "LOCAL-DRUG-99"),
    sourceName = c("Amox local", "Unchecked med"),
    sourceFrequency = c(50, 10),
    mappingStatus = c("APPROVED", "UNCHECKED"),
    targetConceptId = c(1713332, 0),
    targetVocabularyId = c("RxNorm", "")
  ), usagi_csv, row.names = FALSE)

  n_usagi <- import_usagi_mappings(con, usagi_csv, source_vocabulary_id = "LocalDrug", approved_only = TRUE)
  expect_equal(n_usagi, 1)

  stcm_rows <- DBI::dbGetQuery(con, "SELECT source_code, target_concept_id FROM source_to_concept_map ORDER BY source_code")
  expect_true("LOCAL-DRUG-01" %in% stcm_rows$source_code)
  expect_true("LOCAL-LAB-01" %in% stcm_rows$source_code)
})

test_that("export_unmapped_codes exports frequency counts ready for Usagi", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  })

  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO drug_exposure (
        drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
        drug_type_concept_id, drug_source_value
    ) VALUES
    (1, 1, 0, DATE '2024-01-01', DATE '2024-01-01', 32838, 'CUSTOM-MED-A'),
    (2, 1, 0, DATE '2024-01-02', DATE '2024-01-02', 32838, 'CUSTOM-MED-A'),
    (3, 2, 0, DATE '2024-01-03', DATE '2024-01-03', 32838, 'CUSTOM-MED-B');
  ")

  out_csv <- tempfile(fileext = ".csv")
  count <- export_unmapped_codes(con, "drug_exposure", out_csv, min_frequency = 1)
  expect_equal(count, 2)

  df <- read.csv(out_csv)
  expect_equal(nrow(df), 2)
  expect_equal(df$sourceCode[[1]], "CUSTOM-MED-A")
  expect_equal(df$frequency[[1]], 2)
})

test_that("remap_cdm_table and remap_all update concept IDs and rebuild eras", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  })

  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO drug_exposure (
        drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date,
        drug_type_concept_id, drug_source_value, drug_source_concept_id
    ) VALUES (1, 101, 0, DATE '2024-01-01', DATE '2024-01-01', 32838, 'LOCAL-ASPIRIN', 0);
  ")

  DBI::dbExecute(con, "
    INSERT INTO source_to_concept_map (
        source_code, source_concept_id, source_vocabulary_id, source_code_description,
        target_concept_id, target_vocabulary_id, valid_start_date, valid_end_date
    ) VALUES ('LOCAL-ASPIRIN', 0, 'RxNorm', 'Local Aspirin 81mg', 1112807, 'RxNorm', DATE '2020-01-01', DATE '2099-12-31');
  ")

  # Dry run
  dry_res <- remap_cdm_table(con, "drug_exposure", dry_run = TRUE)
  expect_equal(dry_res$eligible_for_remapping, 1)
  expect_equal(dry_res$remappable_rows, 1)
  expect_equal(dry_res$remapped, 0)

  # Live remap
  live_res <- remap_all(con, dry_run = FALSE, rebuild_eras = TRUE)
  expect_equal(live_res$drug_exposure$remapped, 1)

  val <- DBI::dbGetQuery(con, "SELECT drug_concept_id FROM drug_exposure WHERE drug_exposure_id = 1")$drug_concept_id[[1]]
  expect_equal(val, 1112807)

  eras <- DBI::dbGetQuery(con, "SELECT person_id, drug_concept_id, drug_exposure_count FROM drug_era")
  expect_equal(nrow(eras), 1)
  expect_equal(eras$drug_concept_id[[1]], 1112807)
})

test_that("load_death handles missing and present death.csv modularly", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  })

  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  load_mapping_macros(con)

  # 1. Missing death.csv
  empty_dir <- tempfile()
  dir.create(empty_dir)
  expect_no_error(omopduckdb:::.load_death(con, empty_dir))
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM death")$n, 0)

  # 2. Present death.csv
  death_dir <- tempfile()
  dir.create(death_dir)
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value)
    VALUES (1, 8507, 1960, 8527, 38003564, 'P001');
  ")

  write.csv(data.frame(
    PATID = c("P001", "P001"),
    DEATH_DATE = c("2024-05-10", "2024-05-09"),
    DEATH_SOURCE = c("EHR", "STATE")
  ), file.path(death_dir, "death.csv"), row.names = FALSE)

  omopduckdb:::.load_death(con, death_dir)
  death_rows <- DBI::dbGetQuery(con, "SELECT person_id, death_date, cause_source_value FROM death")
  expect_equal(nrow(death_rows), 1)
  expect_equal(death_rows$person_id[[1]], 1)
  expect_equal(as.character(death_rows$death_date[[1]]), "2024-05-10")
})
