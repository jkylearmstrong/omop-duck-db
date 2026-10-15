test_that("leakage_scan flags high AUC, zero variance, and pure separation", {
  n <- 100
  y <- c(rep(1, 50), rep(0, 50))

  set.seed(42)
  f_leak <- c(rep(10.0, 50), rep(0.0, 50))
  f_high <- c(rnorm(50, 5, 0.1), rnorm(50, 0, 0.1))
  f_norm <- rnorm(n, 0, 1)
  f_zero <- rep(0, n)

  X <- data.frame(
    f_leak = f_leak,
    f_high = f_high,
    f_norm = f_norm,
    f_zero = f_zero,
    stringsAsFactors = FALSE
  )

  audit <- leakage_scan(X, y, max_auc = 0.98)
  expect_true(nrow(audit) > 0)
  expect_true("pure_stratum_separation" %in% audit$check)
  expect_true("high_auc" %in% audit$check)
  expect_true("zero_variance" %in% audit$check)

  norm_rows <- audit[audit$feature == "f_norm", ]
  expect_false(any(norm_rows$flagged))
})

test_that("add_missing_indicators and missingness_by_outcome evaluate informative missingness", {
  df <- data.frame(
    feat_a = c(1.0, NA, 3.0, NA, 5.0, 6.0, NA, 8.0, 9.0, 10.0),
    feat_b = c(rep(NA, 5), rep(2.0, 5)),
    outcome = c(0, 0, 0, 0, 0, 1, 1, 1, 1, 1),
    site = c(rep("SiteA", 5), rep("SiteB", 5)),
    stringsAsFactors = FALSE
  )

  ind_df <- add_missing_indicators(df, cols = "feat_a")
  expect_true("miss_feat_a" %in% names(ind_df))
  expect_equal(sum(ind_df$miss_feat_a), 3)

  miss_table <- missingness_by_outcome(
    df,
    outcome_col = "outcome",
    cols = c("feat_a", "feat_b"),
    by = "site",
    fill_rule_notes = list(feat_b = "imputed 0")
  )
  expect_true(nrow(miss_table) > 0)
  b_all <- miss_table[miss_table$site == "ALL" & miss_table$feature == "feat_b", ]
  expect_true(b_all$flagged[1])
  expect_equal(b_all$fill_rule[1], "imputed 0")
})

test_that("provenance and vocabulary_preflight audit CDM versions", {
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE concept (
      concept_id INTEGER PRIMARY KEY,
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
    CREATE TABLE concept_ancestor (
      ancestor_concept_id INTEGER,
      descendant_concept_id INTEGER,
      min_levels_of_separation INTEGER,
      max_levels_of_separation INTEGER
    );
    CREATE TABLE vocabulary (
      vocabulary_id VARCHAR PRIMARY KEY,
      vocabulary_name VARCHAR,
      vocabulary_reference VARCHAR,
      vocabulary_version VARCHAR,
      vocabulary_concept_id INTEGER
    );
    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT PRIMARY KEY,
      person_id BIGINT,
      drug_concept_id INTEGER
    );
  ")

  DBI::dbExecute(con, "
    INSERT INTO vocabulary VALUES
    ('None', 'OMOP Standard', '', 'v5.0 2023-10-01', 0),
    ('RxNorm', 'RxNorm', '', 'RxNorm 2023-11-06', 0),
    ('SNOMED', 'SNOMED CT', '', 'SNOMED 2023-09-01', 0),
    ('LOINC', 'LOINC', '', 'LOINC 2.76', 0),
    ('ATC', 'WHO ATC', '', 'ATC 2023-01-01', 0);

    INSERT INTO concept VALUES
    (21602722, 'Metformin ATC', 'Drug', 'ATC', 'ATC 4th', 'C', 'A10BA', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (6809, 'Metformin Ingredient', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (4048386, 'Chemotherapy procedure', 'Procedure', 'SNOMED', 'Procedure', 'S', '12345', DATE '1970-01-01', DATE '2099-12-31', NULL),
    (3004410, 'Glucose measurement', 'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7', DATE '1970-01-01', DATE '2099-12-31', NULL);

    INSERT INTO concept_ancestor VALUES
    (21602722, 6809, 1, 1);
  ")

  prov <- provenance(con, seed = 123)
  expect_true(prov$package_version >= "0.6.4")
  expect_true("RxNorm" %in% names(prov$vocabularies))

  report <- vocabulary_preflight(con, required = c("RxNorm", "ATC", "LOINC", "SNOMED"), fail_fast = TRUE)
  expect_true(report$passed)
  expect_true(report$vocabularies$RxNorm > 0)
  expect_true(report$atc_counts$total_descendants > 0)
})

test_that("numbers_of_record and scan_document_numbers identify verified and stale counts", {
  c1 <- data.frame(outcome = c(1, 0, 1, 0, 0), site = c(rep("SiteA", 3), rep("SiteB", 2)), stringsAsFactors = FALSE)
  c2 <- data.frame(outcome = c(1, 1, 0), site = rep("SiteA", 3), stringsAsFactors = FALSE)

  cohorts <- list(Cohort1 = c1, Cohort2 = c2)
  manifest <- numbers_of_record(cohorts)

  expect_equal(manifest$cohorts$Cohort1$total_n, 5)
  expect_equal(manifest$cohorts$Cohort1$events, 2)

  prev_manifest <- list(cohorts = list(Cohort1 = list(total_n = 999, events = 888)))
  text <- "We studied a total of 5 patients in Cohort1 and observed 2 events. In an older version we had 999 patients."
  scan <- scan_document_numbers(text, manifest, previous_manifest = prev_manifest)

  expect_true(nrow(scan) > 0)
  expect_true(5 %in% scan$number)
  expect_true(999 %in% scan$number)
  expect_equal(scan$status[scan$number == 5][1], "verified")
  expect_equal(scan$status[scan$number == 999][1], "stale")
})

test_that("generate_site_patient_tokens and consortium_patient_overlap compute salted overlap", {
  salt <- "consortium_secret_salt_2026"
  pids_a <- 1:12
  pids_b <- 8:16

  tokens_a <- generate_site_patient_tokens(pids_a, salt)
  tokens_b <- generate_site_patient_tokens(pids_b, salt)

  expect_equal(length(tokens_a), 12)
  expect_equal(length(tokens_b), 9)

  overlap <- consortium_patient_overlap(list(SiteA = tokens_a, SiteB = tokens_b), min_cell_count = 11)
  expect_true(nrow(overlap) > 0)

  pair <- overlap[overlap$site_1 == "SiteA" & overlap$site_2 == "SiteB", ]
  expect_true(pair$is_suppressed[1])
  expect_equal(pair$shared_patients[1], "<11")
})

test_that("planned_readmission_filter and observation_period_diagnostics run in DuckDB", {
  con <- duckdb::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(duckdb::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (person_id BIGINT PRIMARY KEY);
    CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT PRIMARY KEY, person_id BIGINT, visit_start_date DATE, visit_end_date DATE);
    CREATE TABLE procedure_occurrence (procedure_occurrence_id BIGINT PRIMARY KEY, person_id BIGINT, procedure_concept_id INTEGER, procedure_date DATE);
    CREATE TABLE observation_period (observation_period_id BIGINT PRIMARY KEY, person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE);
    CREATE TABLE condition_occurrence (condition_occurrence_id BIGINT PRIMARY KEY, person_id BIGINT, condition_start_date DATE);
    CREATE TABLE drug_exposure (drug_exposure_id BIGINT PRIMARY KEY, person_id BIGINT, drug_exposure_start_date DATE);
    CREATE TABLE measurement (measurement_id BIGINT PRIMARY KEY, person_id BIGINT, measurement_date DATE);
  ")

  for (pid in 1:10) {
    DBI::dbExecute(con, sprintf("INSERT INTO person VALUES (%d);", pid))
    DBI::dbExecute(con, sprintf("INSERT INTO observation_period VALUES (%d, %d, DATE '2021-01-01', DATE '2024-01-01');", pid, pid))
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence VALUES (%d, %d, DATE '2023-05-01', DATE '2023-05-05');", 100 + pid, pid))
    DBI::dbExecute(con, sprintf("INSERT INTO condition_occurrence VALUES (%d, %d, DATE '2021-06-01');", 200 + pid, pid))
    DBI::dbExecute(con, sprintf("INSERT INTO drug_exposure VALUES (%d, %d, DATE '2022-01-01');", 300 + pid, pid))
    DBI::dbExecute(con, sprintf("INSERT INTO measurement VALUES (%d, %d, DATE '2022-01-01');", 400 + pid, pid))
  }

  # Patient 1 has planned chemo
  DBI::dbExecute(con, "INSERT INTO procedure_occurrence VALUES (501, 1, 4048386, DATE '2023-05-15');")

  DBI::dbExecute(con, "
    CREATE TABLE cohort AS
    SELECT
      person_id,
      visit_start_date AS cohort_start_date,
      visit_end_date AS cohort_end_date,
      'SiteA' AS site
    FROM visit_occurrence;
  ")

  res <- planned_readmission_filter(con, cohort_table = "cohort", by = "site", target_table = "clean_cohort")
  audit <- res$audit
  clean_cohort <- res$clean_cohort

  expect_equal(audit$planned_readmissions_removed[audit$site == "ALL"], 1)
  expect_equal(nrow(clean_cohort), 9)

  obs_diag <- observation_period_diagnostics(con, cohort_table = "cohort", prior_days = 365, by = "site")
  expect_true(nrow(obs_diag) > 0)
  expect_equal(obs_diag$total_patients[1], 10)
  expect_equal(obs_diag$rate_obs_period[1], 1.0)
})
