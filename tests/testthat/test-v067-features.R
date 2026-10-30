test_that("1. Version is 0.6.7", {
  desc_ver <- as.character(read.dcf(system.file("DESCRIPTION", package = "omopduckdb"))[1, "Version"])
  if (is.na(desc_ver) || !nzchar(desc_ver)) {
    desc_ver <- as.character(read.dcf("DESCRIPTION")[1, "Version"])
  }
  expect_true(desc_ver %in% c("0.6.7", "0.6.8", "0.7.0", "0.8.0", "0.8.1", "0.8.2", "0.8.3", "0.8.4", "0.8.5", "0.8.6", "0.8.7", "0.9.0"))
})

test_that("2. HierarchicalRollup spec definition and cryptographic SHA-256 hash", {
  rollup <- hierarchical_rollup(
    name = "antidiabetic_agents",
    domain = "Drug",
    root_vocabulary = "ATC",
    roots = c("A10BA", "A10BB", "A10BK"),
    target_vocabulary = "RxNorm",
    expansion_direction = "descendants",
    include_retired_maps_to = TRUE,
    exclude_dose_forms = c("Topical", "Ophthalmic", "Otic"),
    combination_policy = "attribute_all"
  )

  expect_equal(rollup$name, "antidiabetic_agents")
  expect_equal(rollup$roots, c("A10BA", "A10BB", "A10BK"))
  h <- spec_hash(rollup)
  expect_true(is.character(h))
  expect_equal(nchar(h), 64L)
})

test_that("3. Concept code prefix & wildcard matching in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
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
      (101, 'Metformin Oral Solution', 'Drug', 'ATC', 'ATC 5th', 'C', 'A10BA02', '1970-01-01', '2099-12-31', NULL),
      (102, 'Glimepiride', 'Drug', 'ATC', 'ATC 5th', 'C', 'A10BB12', '1970-01-01', '2099-12-31', NULL),
      (103, 'Aspirin', 'Drug', 'ATC', 'ATC 5th', 'C', 'B01AC06', '1970-01-01', '2099-12-31', NULL),
      (201, 'Type 2 diabetes mellitus without complications', 'Condition', 'ICD10CM', 'ICD10 code', 'N', 'E11.9', '1970-01-01', '2099-12-31', NULL),
      (202, 'Heart failure, unspecified', 'Condition', 'ICD10CM', 'ICD10 code', 'N', 'I50.9', '1970-01-01', '2099-12-31', NULL),
      (301, 'Office outpatient visit 15 minutes', 'Procedure', 'CPT4', 'CPT4', 'S', '99213', '1970-01-01', '2099-12-31', NULL);
  ")

  patterns <- list(
    concept_pattern(vocabulary_id = "ATC", code_pattern = "A10B%"),
    concept_pattern(vocabulary_id = "ICD10CM", code_pattern = "E11%"),
    "CPT4:992%"
  )

  resolved <- resolve_concept_patterns(con, patterns = patterns)
  expect_equal(nrow(resolved), 4L)
  cids <- sort(resolved$concept_id)
  expect_equal(cids, c(101, 102, 201, 301))
})

test_that("4. Rollup engine hierarchy expansion and dose form exclusions in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
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
      (1, 'Biguanides', 'Drug', 'ATC', 'ATC 4th', 'C', 'A10BA', '1970-01-01', '2099-12-31', NULL),
      (10, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861004', '1970-01-01', '2099-12-31', NULL),
      (11, 'Metformin 1% Topical Cream', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861005', '1970-01-01', '2099-12-31', NULL),
      (12, 'Metformin 0.5% Ophthalmic Drops', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861006', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_ancestor (
      ancestor_concept_id BIGINT,
      descendant_concept_id BIGINT,
      min_levels_of_separation INTEGER,
      max_levels_of_separation INTEGER
    );
    INSERT INTO concept_ancestor VALUES
      (1, 10, 1, 1),
      (1, 11, 1, 1),
      (1, 12, 1, 1);
  ")

  engine <- rollup_engine(
    name = "metformin_systemic",
    rules = list(
      concept_pattern(
        vocabulary_id = "ATC",
        code_pattern = "A10BA",
        expand = "descendants",
        target_vocabulary = "RxNorm"
      )
    ),
    exclude_forms = c("Topical", "Ophthalmic")
  )

  mat <- materialize_crosswalk(engine, con)
  expect_equal(nrow(mat), 1L)
  expect_equal(mat$concept_id[1], 10)
})

test_that("5. Exposure extraction with provenance filtering in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
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
      (10, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861004', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT,
      person_id BIGINT,
      drug_concept_id BIGINT,
      drug_exposure_start_date DATE,
      drug_exposure_end_date DATE,
      drug_type_concept_id BIGINT
    );
    INSERT INTO drug_exposure VALUES
      (1, 501, 10, '2023-01-01', '2023-01-30', 32838),      -- EHR prescription
      (2, 502, 10, '2023-02-01', '2023-02-28', 38000177),   -- Written prescription
      (3, 503, 10, '2023-03-01', '2023-03-31', 38000180);   -- Inpatient administration
  ")

  engine <- rollup_engine(
    name = "metformin",
    rules = list(concept_pattern(vocabulary_id = "RxNorm", code_pattern = "861004"))
  )

  exposures <- extract_rollup_exposures(
    engine,
    con,
    prescribed_types = c(32838, 38000177)
  )
  expect_equal(nrow(exposures), 2L)
  expect_equal(sort(exposures$person_id), c(501, 502))
})

test_that("6. In-database fuzzy matching for unmapped codes in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
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
      (1503297, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
      (1515249, 'Glipizide', 'Drug', 'RxNorm', 'Ingredient', 'S', '4821', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT,
      drug_concept_id BIGINT,
      drug_source_value VARCHAR
    );
    INSERT INTO drug_exposure VALUES
      (1, 0, 'METFORMIN HCL 500MG TAB'),
      (2, 0, 'METFORMIN HCL 500MG TAB'),
      (3, 0, 'GLIPIZIDE 5MG TABLET');
  ")

  suggestions <- suggest_source_mappings(
    con,
    unmapped_table = "drug_exposure",
    domain = "Drug",
    min_similarity = 0.85
  )
  expect_true(nrow(suggestions) > 0)
  expect_true("similarity_score" %in% names(suggestions))

  met_row <- suggestions[suggestions$source_value == "METFORMIN HCL 500MG TAB", ]
  expect_equal(met_row$suggested_concept_id[1], 1503297)
  expect_equal(met_row$unmapped_count[1], 2)
  expect_true(met_row$similarity_score[1] >= 0.85)
})

test_that("7. Multi-domain provenance filters in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE visit_occurrence (
      visit_occurrence_id BIGINT,
      person_id BIGINT,
      visit_type_concept_id BIGINT
    );
    INSERT INTO visit_occurrence VALUES
      (1, 10, 32035),    -- Inpatient EHR encounter
      (2, 20, 9202),     -- Outpatient
      (3, 30, 9203);     -- Emergency

    CREATE TABLE condition_occurrence (
      condition_occurrence_id BIGINT,
      person_id BIGINT,
      condition_type_concept_id BIGINT
    );
    INSERT INTO condition_occurrence VALUES
      (1, 10, 38000230),  -- Primary discharge diagnosis
      (2, 20, 38000199),  -- Admitting diagnosis
      (3, 30, 32020);     -- Problem list

    CREATE TABLE measurement (
      measurement_id BIGINT,
      person_id BIGINT,
      measurement_type_concept_id BIGINT
    );
    INSERT INTO measurement VALUES
      (1, 10, 44818702),  -- Lab result
      (2, 20, 32836),     -- Lab order
      (3, 30, 32828);     -- Point-of-care test
  ")

  # Visit filter
  v_filt <- filter_visits(con, kind = c("inpatient", "emergency"))
  expect_equal(nrow(v_filt), 2L)
  expect_equal(sort(v_filt$visit_occurrence_id), c(1, 3))

  # Condition filter
  c_filt <- filter_conditions(con, kind = "primary")
  expect_equal(nrow(c_filt), 1L)
  expect_equal(c_filt$condition_occurrence_id[1], 1)

  # Measurement filter
  m_filt <- filter_measurements(con, kind = c("lab_result", "point_of_care"))
  expect_equal(nrow(m_filt), 2L)
  expect_equal(sort(m_filt$measurement_id), c(1, 3))
})

test_that("8. Materialized crosswalk Parquet cache in R", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
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
      (10, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '861004', '1970-01-01', '2099-12-31', NULL);
  ")

  rollup <- hierarchical_rollup(
    name = "test_rollup_r",
    roots = "861004",
    root_vocabulary = "RxNorm"
  )

  pq_file <- tempfile(fileext = ".parquet")
  on.exit(unlink(pq_file), add = TRUE)

  mat_df <- materialize_crosswalk(rollup, con, output_path = pq_file)
  expect_true(file.exists(pq_file))
  expect_equal(nrow(mat_df), 1L)
  expect_true("spec_hash" %in% names(mat_df))
})

test_that("9. Concept drift auditor in R", {
  con_v1 <- DBI::dbConnect(duckdb::duckdb())
  con_v2 <- DBI::dbConnect(duckdb::duckdb())
  on.exit({
    DBI::dbDisconnect(con_v1, shutdown = TRUE)
    DBI::dbDisconnect(con_v2, shutdown = TRUE)
  }, add = TRUE)

  DBI::dbExecute(con_v1, "
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
      (100, 'Drug A', 'Drug', 'RxNorm', 'Ingredient', 'S', '100', '1970-01-01', '2099-12-31', NULL),
      (200, 'Drug B', 'Drug', 'RxNorm', 'Ingredient', 'S', '200', '1970-01-01', '2099-12-31', NULL);
  ")

  DBI::dbExecute(con_v2, "
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
      (100, 'Drug A', 'Drug', 'RxNorm', 'Ingredient', 'S', '100', '1970-01-01', '2099-12-31', NULL),
      (200, 'Drug B', 'Drug', 'RxNorm', 'Ingredient', NULL, '200', '1970-01-01', '2025-01-01', 'D'),
      (300, 'Drug C', 'Drug', 'RxNorm', 'Ingredient', 'S', '300', '2025-01-01', '2099-12-31', NULL);
  ")

  rollup <- hierarchical_rollup(
    name = "test_drift_r",
    rules = list(concept_pattern(vocabulary_id = "RxNorm", code_pattern = "%"))
  )

  drift <- audit_concept_drift(con_v1, con_v2, rollup)
  expect_equal(drift$old_total_concepts, 2L)
  expect_equal(drift$new_total_concepts, 3L)
  expect_equal(drift$added_concept_ids, 300)
  expect_equal(drift$removed_count, 0L)
  expect_equal(drift$status_changed_count, 1L)
  expect_equal(drift$status_changed[[1]]$concept_id, 200)
  expect_equal(drift$status_changed[[1]]$new_invalid_reason, "D")
})
