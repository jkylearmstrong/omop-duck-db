test_that("Version is 0.6.5 or later and commit detection works in R", {
  expect_true(utils::packageVersion("omopduckdb") >= "0.6.5")
  prov <- provenance()
  expect_true(nchar(prov$git_commit) >= 7)
  expect_true(prov$package_version >= "0.6.5")
})

test_that("Vocabulary preflight and provenance support vocab_schema and drug types", {
  vocab_file <- tempfile(fileext = ".duckdb")
  on.exit(unlink(vocab_file), add = TRUE)

  v_con <- DBI::dbConnect(duckdb::duckdb(), dbdir = vocab_file)
  DBI::dbExecute(v_con, "
    CREATE TABLE concept (
      concept_id BIGINT, concept_name VARCHAR, domain_id VARCHAR,
      vocabulary_id VARCHAR, concept_class_id VARCHAR, standard_concept VARCHAR,
      concept_code VARCHAR, valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR
    );
    INSERT INTO concept VALUES
      (111, 'Test Drug', 'Drug', 'RxNorm', 'Ingredient', 'S', '111', '1970-01-01', '2099-12-31', NULL),
      (222, 'Test Drug 2', 'Drug', 'RxNorm', 'Ingredient', 'S', '222', '1970-01-01', '2099-12-31', NULL);
    CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT, min_levels_of_separation INT, max_levels_of_separation INT);
    INSERT INTO concept_ancestor VALUES (111, 111, 0, 0), (222, 222, 0, 0);
    CREATE TABLE vocabulary (vocabulary_id VARCHAR, vocabulary_name VARCHAR, vocabulary_version VARCHAR);
    INSERT INTO vocabulary VALUES ('RxNorm', 'RxNorm', 'RxNorm 2026-01-01');
  ")
  DBI::dbDisconnect(v_con, shutdown = TRUE)

  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  DBI::dbExecute(con, sprintf("ATTACH '%s' AS central_vocab", vocab_file))

  DBI::dbExecute(con, "
    CREATE TABLE person (person_id BIGINT, year_of_birth INT, gender_concept_id INT, race_concept_id INT, ethnicity_concept_id INT);
    INSERT INTO person VALUES (1, 1980, 8507, 0, 0);
    CREATE TABLE drug_exposure (drug_exposure_id BIGINT, person_id BIGINT, drug_concept_id BIGINT, drug_type_concept_id BIGINT, drug_exposure_start_date DATE);
    INSERT INTO drug_exposure VALUES
      (1, 1, 111, 38000177, '2023-01-01'),
      (2, 1, 111, 38000180, '2023-01-02'),
      (3, 1, 222, 38000175, '2023-01-03');
  ")

  res <- vocabulary_preflight(con, required = "RxNorm", vocab_schema = "central_vocab")
  expect_true(res$passed)
  expect_true(res$vocabulary_reachable)
  expect_equal(res$concept_count, 2L)
  expect_equal(res$drug_types$prescribed, 1L)
  expect_equal(res$drug_types$administered, 1L)
  expect_equal(res$drug_types$dispensed, 1L)
  expect_equal(res$drug_types$total, 3L)
  expect_equal(round(res$drug_type_breakdown$prescribed_pct, 1), 33.3)

  prov <- provenance(con, vocab_schema = "central_vocab")
  expect_true(prov$vocabulary$reachable)
  expect_equal(prov$vocabulary$version_string, "RxNorm 2026-01-01")
})

test_that("Combination products audit and extract_medication_classes disaggregation work", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, concept_class_id VARCHAR);
    INSERT INTO concept VALUES
      (1001, 'Metformin', 'Ingredient'),
      (1002, 'Sitagliptin', 'Ingredient'),
      (2001, 'Metformin 500 MG Oral Tablet', 'Clinical Drug'),
      (2002, 'Metformin 500 MG / Sitagliptin 50 MG Oral Tablet', 'Clinical Drug');

    CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT);
    INSERT INTO concept_ancestor VALUES
      (1001, 1001),
      (1002, 1002),
      (1001, 2001),
      (1001, 2002),
      (1002, 2002);

    CREATE TABLE cohort (person_id BIGINT, cohort_start_date DATE, index_date DATE);
    INSERT INTO cohort VALUES
      (1, '2023-01-01', '2023-01-01'),
      (2, '2023-01-01', '2023-01-01'),
      (3, '2023-01-01', '2023-01-01'),
      (4, '2023-01-01', '2023-01-01');

    CREATE TABLE drug_exposure (person_id BIGINT, drug_concept_id BIGINT, drug_exposure_start_date DATE, drug_source_value VARCHAR);
    INSERT INTO drug_exposure VALUES
      (1, 2001, '2023-01-05', 'Metformin plain'),
      (2, 2002, '2023-01-05', 'Metformin combo'),
      (3, 2001, '2023-01-05', 'Metformin plain'),
      (3, 2002, '2023-01-06', 'Metformin combo');
  ")

  audit <- audit_combination_products(con, class_concept_id = 1001)
  expect_equal(audit$total_patients, 3L)
  expect_equal(audit$plain_only_patients, 1L)
  expect_equal(audit$combination_only_patients, 1L)
  expect_equal(audit$overlapping_patients, 1L)
  expect_equal(round(audit$combination_share_patients, 2), 0.67)

  specs <- list(metformin = c(1001))
  res <- extract_medication_classes(con, "cohort", specs, strategy = "standard", lookahead_days = 30, disaggregate_combinations = TRUE)
  expect_true("metformin_plain" %in% names(res))
  expect_true("metformin_combination" %in% names(res))
  expect_true("metformin" %in% names(res))

  p1 <- res[res$person_id == 1, ]
  expect_equal(p1$metformin_plain[1], 1L)
  expect_equal(p1$metformin_combination[1], 0L)
  expect_equal(p1$metformin[1], 1L)

  p2 <- res[res$person_id == 2, ]
  expect_equal(p2$metformin_plain[1], 0L)
  expect_equal(p2$metformin_combination[1], 1L)
  expect_equal(p2$metformin[1], 1L)

  p3 <- res[res$person_id == 3, ]
  expect_equal(p3$metformin_plain[1], 1L)
  expect_equal(p3$metformin_combination[1], 1L)
  expect_equal(p3$metformin[1], 1L)

  p4 <- res[res$person_id == 4, ]
  expect_equal(p4$metformin[1], 0L)
})

test_that("Dose-form filtering via RxNorm has dose form and source filtering work", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE concept (concept_id BIGINT, concept_name VARCHAR, concept_class_id VARCHAR);
    INSERT INTO concept VALUES
      (500, 'Hydrocortisone', 'Ingredient'),
      (501, 'Hydrocortisone Oral Tablet', 'Clinical Drug'),
      (502, 'Hydrocortisone 1% Topical Gel', 'Clinical Drug'),
      (901, 'Oral Tablet', 'Dose Form'),
      (902, 'Topical Gel', 'Dose Form');

    CREATE TABLE concept_relationship (concept_id_1 BIGINT, concept_id_2 BIGINT, relationship_id VARCHAR);
    INSERT INTO concept_relationship VALUES
      (501, 901, 'RxNorm has dose form'),
      (502, 902, 'RxNorm has dose form');

    CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT);
    INSERT INTO concept_ancestor VALUES
      (500, 500),
      (500, 501),
      (500, 502);

    CREATE TABLE cohort (person_id BIGINT, cohort_start_date DATE, index_date DATE);
    INSERT INTO cohort VALUES
      (1, '2023-01-01', '2023-01-01'),
      (2, '2023-01-01', '2023-01-01');

    CREATE TABLE drug_exposure (person_id BIGINT, drug_concept_id BIGINT, drug_exposure_start_date DATE, drug_source_value VARCHAR);
    INSERT INTO drug_exposure VALUES
      (1, 501, '2023-01-05', 'Hydrocortisone tab'),
      (2, 502, '2023-01-05', 'Hydrocortisone gel');
  ")

  specs <- list(steroid = c(500))
  res <- extract_medication_classes(con, "cohort", specs, strategy = "standard", lookahead_days = 30, exclude_dose_form_concept_ids = c(902))
  p1 <- res[res$person_id == 1, ]
  p2 <- res[res$person_id == 2, ]
  expect_equal(p1$steroid[1], 1L)
  expect_equal(p2$steroid[1], 0L)

  xw <- data.frame(
    source_value = c("HYDROCORTISONE GEL", "HYDROCORTISONE TAB"),
    drug_class = c("steroid", "steroid"),
    stringsAsFactors = FALSE
  )
  res_both <- extract_medication_classes(
    con, "cohort", specs, strategy = "both", lookahead_days = 30,
    crosswalk = xw, exclude_forms = c("gel"), apply_exclude_to_source = TRUE
  )
  pb1 <- res_both[res_both$person_id == 1, ]
  pb2 <- res_both[res_both$person_id == 2, ]
  expect_equal(pb1$steroid_source[1], 1L)
  expect_equal(pb2$steroid_source[1], 0L)
  expect_equal(pb2$steroid_standard[1], 0L)
})

test_that("export_companion_manifest preserves order and works with compare_feature_tables", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (person_id BIGINT, person_source_value VARCHAR);
    INSERT INTO person VALUES (10, 'MRN001'), (20, 'MRN002'), (30, 'MRN003');

    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_source_value VARCHAR, visit_start_date DATE);
    INSERT INTO visit_occurrence VALUES
      (1001, 10, 'ENC_A', '2023-01-01'),
      (1002, 20, 'ENC_B', '2023-02-01'),
      (1003, 30, 'ENC_C', '2023-03-01');
  ")

  cohort_df <- data.frame(
    person_id = c(30, 10, 20),
    visit_occurrence_id = c(1003, 1001, 1002),
    index_date = as.Date(c("2023-03-01", "2023-01-01", "2023-02-01"))
  )

  manifest <- export_companion_manifest(cohort_df, con = con)
  expect_equal(names(manifest), c("person_id", "person_source_value", "visit_occurrence_id", "visit_source_value", "index_date"))
  expect_equal(nrow(manifest), 3L)
  expect_equal(manifest$person_id, c(30, 10, 20))
  expect_equal(manifest$person_source_value, c("MRN003", "MRN001", "MRN002"))
  expect_equal(manifest$visit_source_value, c("ENC_C", "ENC_A", "ENC_B"))

  left_features <- data.frame(
    feat_age = c(65.0, 45.0, 55.0),
    feat_bmi = c(28.0, 24.0, 31.0)
  )
  right_features <- data.frame(
    feat_age = c(65.0, 45.0, 54.5),
    feat_bmi = c(28.2, 24.1, 30.9)
  )

  ledger <- compare_feature_tables(
    left_features, right_features,
    left_manifest = manifest, right_manifest = manifest,
    min_cell_count = 1
  )
  expect_true(nrow(ledger) > 0)
  expect_true("feat_age" %in% ledger$feature)
  expect_true("feat_bmi" %in% ledger$feature)
  expect_true(all(ledger$matched_n == 3))
})

test_that("cohort_from_index_stays reports encounter_match_rate and groups by site", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (person_id BIGINT, person_source_value VARCHAR);
    INSERT INTO person VALUES (1, 'P1'), (2, 'P2'), (3, 'P3');

    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_source_value VARCHAR, visit_start_date DATE);
    INSERT INTO visit_occurrence VALUES (101, 1, 'V1', '2023-01-01');
  ")

  stays <- data.frame(
    person_source_value = c("P1", "P2", "P3"),
    visit_source_value = c("V1", "V2_missing", "V3_missing"),
    index_date = as.Date(c("2023-01-01", "2023-01-02", "2023-01-03")),
    site = c("SiteA", "SiteA", "SiteB"),
    stringsAsFactors = FALSE
  )

  res <- cohort_from_index_stays(con, stays, group_by = "SITE")
  audit <- res$audit
  expect_true("encounter_match_rate" %in% names(audit))
  expect_equal(round(audit$encounter_match_rate, 2), 0.33)
  expect_true("by_site" %in% names(audit))
  expect_true("SiteA" %in% names(audit$by_site))
  expect_true("SiteB" %in% names(audit$by_site))
  expect_equal(audit$by_site$SiteA$matched_encounters, 1L)
  expect_equal(audit$by_site$SiteB$matched_encounters, 0L)
})

test_that("PCORnet MED_ADMIN and DISPENSING multi-vocab ETL routes NDC, HCPCS, and RxNorm in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  # Load mapping macros
  load_macros(con, skip_unresolved = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (person_id BIGINT, person_source_value VARCHAR);
    INSERT INTO person VALUES (1, 'PAT1'), (2, 'PAT2');

    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_source_value VARCHAR);
    INSERT INTO visit_occurrence VALUES (101, 1, 'ENC1'), (102, 2, 'ENC2');

    CREATE TABLE provider (provider_id BIGINT, provider_source_value VARCHAR);

    CREATE TABLE concept (
      concept_id BIGINT,
      concept_name VARCHAR,
      domain_id VARCHAR,
      vocabulary_id VARCHAR,
      concept_class_id VARCHAR,
      standard_concept VARCHAR,
      concept_code VARCHAR,
      valid_start_date DATE,
      valid_end_date DATE,
      invalid_reason VARCHAR
    );
    INSERT INTO concept VALUES
      (19000001, 'Target RxNorm Clinical Drug', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '12345', '1970-01-01', '2099-12-31', NULL),
      (20000001, 'Source NDC Concept', 'Drug', 'NDC', '11-digit NDC', NULL, '00002821501', '1970-01-01', '2099-12-31', NULL),
      (30000001, 'Source HCPCS Concept', 'Drug', 'HCPCS', 'HCPCS', NULL, 'J0135', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_relationship (
      concept_id_1 BIGINT,
      concept_id_2 BIGINT,
      relationship_id VARCHAR,
      valid_start_date DATE,
      valid_end_date DATE,
      invalid_reason VARCHAR
    );
    INSERT INTO concept_relationship VALUES
      (20000001, 19000001, 'Maps to', '1970-01-01', '2099-12-31', NULL),
      (30000001, 19000001, 'Maps to', '1970-01-01', '2099-12-31', NULL),
      (19000001, 19000001, 'Maps to', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT,
      person_id BIGINT,
      drug_concept_id BIGINT,
      drug_exposure_start_date DATE,
      drug_exposure_start_datetime TIMESTAMP,
      drug_exposure_end_date DATE,
      drug_exposure_end_datetime TIMESTAMP,
      verbatim_end_date DATE,
      drug_type_concept_id BIGINT,
      stop_reason VARCHAR,
      refills INTEGER,
      quantity DOUBLE,
      days_supply INTEGER,
      sig VARCHAR,
      route_concept_id BIGINT,
      lot_number VARCHAR,
      provider_id BIGINT,
      visit_occurrence_id BIGINT,
      visit_detail_id BIGINT,
      drug_source_value VARCHAR,
      drug_source_concept_id BIGINT,
      route_source_value VARCHAR,
      dose_unit_source_value VARCHAR
    );

    CREATE TABLE source_to_concept_map (
      source_code VARCHAR,
      source_concept_id BIGINT,
      source_vocabulary_id VARCHAR,
      source_code_description VARCHAR,
      target_concept_id BIGINT,
      target_vocabulary_id VARCHAR,
      valid_start_date DATE,
      valid_end_date DATE,
      invalid_reason VARCHAR
    );
  ")

  med_admin_df <- data.frame(
    medadminid = c("MA1", "MA2", "MA3"),
    patid = c("PAT1", "PAT1", "PAT2"),
    encounterid = c("ENC1", "ENC1", "ENC2"),
    medadmin_code = c("00002821501", "J0135", "12345"),
    medadmin_type = c("ND", "CH", "RX"),
    medadmin_start_date = c("2023-01-01", "2023-01-02", "2023-01-03"),
    stringsAsFactors = FALSE
  )

  dispensing_df <- data.frame(
    dispensingid = c("DISP1", "DISP2"),
    patid = c("PAT1", "PAT2"),
    raw_rx_ndc = c("00002821501", "12345"),
    dispensing_date = c("2023-01-10", "2023-01-11"),
    dispensing_sup = c(30, 30),
    dispensing_amt = c(30, 30),
    dispensing_type = c("ND", "RX"),
    stringsAsFactors = FALSE
  )

  tmpdir <- tempfile()
  dir.create(tmpdir)
  on.exit(unlink(tmpdir, recursive = TRUE), add = TRUE)
  utils::write.csv(med_admin_df, file.path(tmpdir, "med_admin.csv"), row.names = FALSE)
  utils::write.csv(dispensing_df, file.path(tmpdir, "dispensing.csv"), row.names = FALSE)

  load_drug_exposure(con, tmpdir)

  res <- DBI::dbGetQuery(con, "SELECT drug_exposure_id, person_id, drug_concept_id, drug_type_concept_id FROM drug_exposure ORDER BY drug_exposure_id")
  expect_equal(nrow(res), 5L)
  expect_true(all(res$drug_concept_id == 19000001))
  expect_equal(res$drug_type_concept_id[1:3], c(38000180, 38000180, 38000180))
  expect_equal(res$drug_type_concept_id[4:5], c(38000175, 38000175))
})
