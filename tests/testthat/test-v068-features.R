test_that("1. Version is 0.6.8", {
  desc_ver <- as.character(read.dcf(system.file("DESCRIPTION", package = "omopduckdb"))[1, "Version"])
  if (is.na(desc_ver) || !nzchar(desc_ver)) {
    desc_ver <- as.character(read.dcf("DESCRIPTION")[1, "Version"])
  }
  expect_equal(desc_ver, "0.6.8")
})

test_that("2. HierarchicalRollup from CSV file", {
  csv_path <- file.path("..", "..", "inst", "extdata", "code_lists", "ederri_medications.csv")
  if (!file.exists(csv_path)) {
    csv_path <- system.file("extdata", "code_lists", "ederri_medications.csv", package = "omopduckdb")
  }
  skip_if_not(file.exists(csv_path), "Bundled CSV not found")

  rollup <- hierarchical_rollup_from_csv(
    path = csv_path,
    name = "Biguanides",
    code_col = "RXNORM_CUI",
    class_col = "CLASS",
    vocabulary_col = "VOCABULARY",
    expand = "descendants",
    include_retired_maps_to = TRUE
  )

  expect_equal(rollup$name, "Biguanides")
  expect_false(is.null(rollup$source_file))
  expect_false(is.null(rollup$source_file_hash))
  expect_true(length(rollup$rules) >= 2L)
  codes <- sapply(rollup$rules, function(r) r$code_pattern)
  expect_true("6809" %in% codes)
})

test_that("3. HierarchicalRollup from headerless legacy CSV with column index mapping", {
  csv_path <- file.path("..", "..", "inst", "extdata", "code_lists", "rxnorm_categories_CUI_map_brief.csv")
  if (!file.exists(csv_path)) {
    csv_path <- system.file("extdata", "code_lists", "rxnorm_categories_CUI_map_brief.csv", package = "omopduckdb")
  }
  skip_if_not(file.exists(csv_path), "Headerless CSV not found")

  rollup <- hierarchical_rollup_from_csv(
    path = csv_path,
    name = "Sulfonylureas",
    class_col = 1,
    code_col = 2,
    header = FALSE,
    vocabulary_id = "RxNorm",
    expand = "descendants"
  )

  expect_equal(rollup$name, "Sulfonylureas")
  expect_true(length(rollup$rules) >= 4L)
  codes <- sapply(rollup$rules, function(r) r$code_pattern)
  expect_true("4815" %in% codes)
  expect_true("4821" %in% codes)
})

test_that("4. HierarchicalRollup from Excel workbook", {
  skip_if_not_installed("readxl")
  excel_path <- file.path("..", "..", "inst", "extdata", "code_lists", "eDERRI_Clinical_Review_Package.xlsx")
  if (!file.exists(excel_path)) {
    excel_path <- system.file("extdata", "code_lists", "eDERRI_Clinical_Review_Package.xlsx", package = "omopduckdb")
  }
  skip_if_not(file.exists(excel_path), "Excel workbook not found")

  rollup <- hierarchical_rollup_from_excel(
    path = excel_path,
    sheet = "Diabetes_Medications",
    name = "DPP-4 Inhibitors",
    class_col = "CLASS",
    code_col = "RXNORM_CUI",
    vocabulary_col = "VOCABULARY",
    expand = "descendants",
    exclude_dose_forms = c("Topical")
  )

  expect_equal(rollup$name, "DPP-4 Inhibitors")
  expect_true(length(rollup$rules) >= 4L)
  codes <- sapply(rollup$rules, function(r) r$code_pattern)
  expect_true("593411" %in% codes)
  expect_true("616440" %in% codes)
})

test_that("5. HierarchicalRollup from bundled clinical code lists", {
  rollup <- hierarchical_rollup_from_bundled(
    name = "GLP-1 Receptor Agonists",
    bundle_name = "ederri_medications",
    class_col = "CLASS",
    code_col = "RXNORM_CUI",
    vocabulary_col = "VOCABULARY"
  )

  expect_equal(rollup$name, "GLP-1 Receptor Agonists")
  expect_true(length(rollup$rules) >= 4L)
  codes <- sapply(rollup$rules, function(r) r$code_pattern)
  expect_true("1991302" %in% codes)
  expect_true("60548" %in% codes)
})

test_that("6. RollupEngine from Excel workbook", {
  skip_if_not_installed("readxl")
  excel_path <- file.path("..", "..", "inst", "extdata", "code_lists", "eDERRI_Clinical_Review_Package.xlsx")
  if (!file.exists(excel_path)) {
    excel_path <- system.file("extdata", "code_lists", "eDERRI_Clinical_Review_Package.xlsx", package = "omopduckdb")
  }
  skip_if_not(file.exists(excel_path), "Excel workbook not found")

  engine <- rollup_engine_from_excel(
    path = excel_path,
    sheet = "Medication_Classes",
    class_col = "CLASS",
    code_col = "CODE",
    vocabulary_col = "VOCABULARY",
    expand = "descendants",
    include_retired_maps_to = TRUE
  )

  expect_true(length(engine$rollups) >= 7L)
  expected_classes <- c("Biguanides", "Sulfonylureas", "Thiazolidinediones", "Insulins")
  for (ec in expected_classes) {
    expect_true(ec %in% names(engine$rollups))
    r <- engine[[ec]]
    expect_true(length(r$rules) > 0L)
  }
})

test_that("7. RollupEngine from multiple CSV files", {
  csv1 <- file.path("..", "..", "inst", "extdata", "code_lists", "diabetes_drug_categories.csv")
  if (!file.exists(csv1)) {
    csv1 <- system.file("extdata", "code_lists", "diabetes_drug_categories.csv", package = "omopduckdb")
  }
  csv2 <- file.path("..", "..", "inst", "extdata", "code_lists", "ederri_medications.csv")
  if (!file.exists(csv2)) {
    csv2 <- system.file("extdata", "code_lists", "ederri_medications.csv", package = "omopduckdb")
  }
  skip_if_not(file.exists(csv1) && file.exists(csv2), "CSV files not found")

  engine <- rollup_engine_from_csvs(
    paths = c(csv1, csv2),
    class_col = "CLASS",
    code_col = "RXNORM_CUI",
    vocabulary_col = "VOCABULARY",
    name = "multi_csv_diabetes_battery"
  )

  expect_equal(engine$name, "multi_csv_diabetes_battery")
  expect_true(length(engine$rollups) >= 7L)
  expect_true("Biguanides" %in% names(engine$rollups))
  expect_true("Insulins" %in% names(engine$rollups))
})

test_that("8. RollupEngine from folder scanning", {
  folder <- file.path("..", "..", "inst", "extdata", "code_lists")
  if (!dir.exists(folder)) {
    folder <- system.file("extdata", "code_lists", package = "omopduckdb")
  }
  skip_if_not(dir.exists(folder), "Code lists folder not found")

  engine <- rollup_engine_from_folder(
    folder = folder,
    glob = "*medications.csv",
    class_col = "CLASS",
    code_col = "RXNORM_CUI",
    vocabulary_col = "VOCABULARY"
  )

  expect_true(length(engine$rollups) >= 7L)
  expect_true("Insulins" %in% names(engine$rollups))
})

test_that("9. RollupEngine from bundled clinical code lists", {
  engine <- rollup_engine_from_bundled("ederri_medications")
  expect_true(length(engine$rollups) >= 7L)
  expect_true("Biguanides" %in% names(engine$rollups))
  expect_true("Thiazolidinediones" %in% names(engine$rollups))
  biguanides <- engine[["Biguanides"]]
  expect_true(length(biguanides$rules) >= 2L)
  codes <- sapply(biguanides$rules, function(r) r$code_pattern)
  expect_true("6809" %in% codes)
})

test_that("10. Seamless Parquet crosswalk caching and cryptographic cache invalidation", {
  tmpdir <- tempfile("test_cache_")
  dir.create(tmpdir, recursive = TRUE)
  on.exit(unlink(tmpdir, recursive = TRUE), add = TRUE)

  csv_file <- file.path(tmpdir, "med_list.csv")
  writeLines(c("CLASS,CODE,VOCABULARY", "Biguanides,6809,RxNorm"), csv_file)

  cache_parquet <- file.path(tmpdir, "crosswalk_cache.parquet")

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
      (6809, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
      (1001, 'Metformin hydrochloride 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860975', '1970-01-01', '2099-12-31', NULL),
      (1002, 'Metformin Oral Solution', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860976', '1970-01-01', '2099-12-31', NULL),
      (4815, 'Glipizide', 'Drug', 'RxNorm', 'Ingredient', 'S', '4815', '1970-01-01', '2099-12-31', NULL),
      (2001, 'Glipizide 5 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '310488', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_ancestor (
      ancestor_concept_id BIGINT,
      descendant_concept_id BIGINT,
      min_levels_of_separation INTEGER,
      max_levels_of_separation INTEGER
    );
    INSERT INTO concept_ancestor VALUES
      (6809, 6809, 0, 0),
      (6809, 1001, 1, 1),
      (6809, 1002, 1, 1),
      (4815, 4815, 0, 0),
      (4815, 2001, 1, 1);

    CREATE TABLE concept_relationship (
      concept_id_1 BIGINT,
      concept_id_2 BIGINT,
      relationship_id VARCHAR,
      valid_start_date DATE,
      valid_end_date DATE,
      invalid_reason VARCHAR
    );
  ")

  engine <- rollup_engine_from_csvs(
    paths = c(csv_file),
    class_col = "CLASS",
    code_col = "CODE",
    vocabulary_col = "VOCABULARY",
    expand = "descendants"
  )

  initial_hash <- spec_hash(engine)

  # 1. First Materialization: creates Parquet cache
  df1 <- materialize_crosswalk(engine, con, output_path = cache_parquet)
  expect_true(file.exists(cache_parquet))
  expect_equal(nrow(df1), 3L)
  expect_true("spec_hash" %in% names(df1))
  expect_equal(df1$spec_hash[1], initial_hash)

  # 2. Second Materialization: loaded directly from cache
  con2 <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con2, shutdown = TRUE), add = TRUE)
  df2 <- materialize_crosswalk(engine, con2, output_path = cache_parquet)
  expect_equal(nrow(df2), 3L)
  expect_equal(df2$spec_hash[1], initial_hash)

  # 3. Modify underlying CSV file
  writeLines(c("CLASS,CODE,VOCABULARY", "Biguanides,6809,RxNorm", "Sulfonylureas,4815,RxNorm"), csv_file)

  engine_updated <- rollup_engine_from_csvs(
    paths = c(csv_file),
    class_col = "CLASS",
    code_col = "CODE",
    vocabulary_col = "VOCABULARY",
    expand = "descendants"
  )
  updated_hash <- spec_hash(engine_updated)
  expect_false(identical(initial_hash, updated_hash))

  # 4. Invalidation: should re-materialize with 5 concepts (3 metformin + 2 glipizide)
  df3 <- materialize_crosswalk(engine_updated, con, output_path = cache_parquet)
  expect_equal(nrow(df3), 5L)
  expect_equal(df3$spec_hash[1], updated_hash)
  expect_true("Biguanides" %in% df3$rollup_name)
  expect_true("Sulfonylureas" %in% df3$rollup_name)
})

test_that("11. Multi-class battery exposure extraction attributes correct rollup_name", {
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
      (6809, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
      (1001, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860975', '1970-01-01', '2099-12-31', NULL),
      (4815, 'Glipizide', 'Drug', 'RxNorm', 'Ingredient', 'S', '4815', '1970-01-01', '2099-12-31', NULL),
      (2001, 'Glipizide 5 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '310488', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_ancestor (
      ancestor_concept_id BIGINT,
      descendant_concept_id BIGINT,
      min_levels_of_separation INTEGER,
      max_levels_of_separation INTEGER
    );
    INSERT INTO concept_ancestor VALUES
      (6809, 6809, 0, 0),
      (6809, 1001, 1, 1),
      (4815, 4815, 0, 0),
      (4815, 2001, 1, 1);

    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT,
      person_id BIGINT,
      drug_concept_id BIGINT,
      drug_exposure_start_date DATE,
      drug_exposure_end_date DATE,
      drug_type_concept_id BIGINT
    );
    INSERT INTO drug_exposure VALUES
      (1, 101, 1001, '2023-01-01', '2023-01-30', 38000177),
      (2, 102, 2001, '2023-02-01', '2023-02-28', 38000177),
      (3, 103, 9999, '2023-03-01', '2023-03-31', 38000177);
  ")

  r1 <- hierarchical_rollup(
    name = "Biguanides",
    rules = list(concept_pattern(vocabulary_id = "RxNorm", code_pattern = "6809")),
    expansion_direction = "descendants"
  )
  r2 <- hierarchical_rollup(
    name = "Sulfonylureas",
    rules = list(concept_pattern(vocabulary_id = "RxNorm", code_pattern = "4815")),
    expansion_direction = "descendants"
  )
  engine <- rollup_engine(
    name = "diabetes_engine",
    rollups = list(r1, r2)
  )

  exposures <- extract_rollup_exposures(engine, con)
  expect_equal(nrow(exposures), 2L)
  expect_true("rollup_name" %in% names(exposures))

  row_big <- exposures[exposures$person_id == 101, ]
  expect_equal(row_big$rollup_name[1], "Biguanides")

  row_sulf <- exposures[exposures$person_id == 102, ]
  expect_equal(row_sulf$rollup_name[1], "Sulfonylureas")
})
