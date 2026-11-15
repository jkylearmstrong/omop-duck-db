# Tests for v0.6.4b Medication Classification Guards and Data Quality Audits

.get_mock_cdm <- function() {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")

  # Build CDM schema from inst or system.file
  ddl_path <- file.path("inst", "extdata", "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql")
  if (!file.exists(ddl_path)) {
    ddl_path <- system.file("extdata", "5.4", "duckdb", "OMOPCDM_duckdb_5.4_ddl.sql", package = "omopduckdb")
  }
  if (file.exists(ddl_path)) {
    ddl_sql <- readLines(ddl_path, warn = FALSE)
    ddl_string <- paste(ddl_sql, collapse = "\n")
    ddl_string <- gsub("@cdmDatabaseSchema.", "", ddl_string, fixed = TRUE)
    ddl_string <- gsub(" NUMERIC ", " DOUBLE ", ddl_string, fixed = TRUE)
    DBI::dbExecute(con, ddl_string)
  } else {
    # Minimal fallback DDL
    DBI::dbExecute(con, "CREATE TABLE concept (concept_id INTEGER, concept_name VARCHAR, domain_id VARCHAR, vocabulary_id VARCHAR, concept_class_id VARCHAR, standard_concept VARCHAR, concept_code VARCHAR, valid_start_date DATE, valid_end_date DATE);")
    DBI::dbExecute(con, "CREATE TABLE concept_ancestor (ancestor_concept_id INTEGER, descendant_concept_id INTEGER, min_levels_of_separation INTEGER, max_levels_of_separation INTEGER);")
    DBI::dbExecute(con, "CREATE TABLE concept_relationship (concept_id_1 INTEGER, concept_id_2 INTEGER, relationship_id VARCHAR, valid_start_date DATE, valid_end_date DATE);")
    DBI::dbExecute(con, "CREATE TABLE person (person_id BIGINT, gender_concept_id INTEGER, year_of_birth INTEGER, race_concept_id INTEGER, ethnicity_concept_id INTEGER, person_source_value VARCHAR);")
    DBI::dbExecute(con, "CREATE TABLE cohort (cohort_definition_id BIGINT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE);")
    DBI::dbExecute(con, "CREATE TABLE drug_exposure (drug_exposure_id BIGINT, person_id BIGINT, drug_concept_id INTEGER, drug_exposure_start_date DATE, drug_exposure_end_date DATE, drug_type_concept_id INTEGER, drug_source_value VARCHAR, route_concept_id INTEGER);")
  }

  DBI::dbExecute(con, "
    INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
    VALUES 
      (21601664, 'Beta blocking agents, non-selective', 'Drug', 'ATC', 'ATC 4th', 'C', 'C07AA', DATE '1970-01-01', DATE '2099-12-31'),
      (1347384, 'Timolol 10 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '855332', DATE '1970-01-01', DATE '2099-12-31'),
      (1347390, 'Timolol 0.5 % Ophthalmic Solution', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '313299', DATE '1970-01-01', DATE '2099-12-31'),
      (9999001, 'Old Timolol Oral Form', 'Drug', 'RxNorm', 'Clinical Drug', NULL, 'OLD-855', DATE '1970-01-01', DATE '2099-12-31'),
      (1713332, 'Amoxicillin 500 MG Oral Capsule', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '308182', DATE '1970-01-01', DATE '2099-12-31');
  ")

  DBI::dbExecute(con, "
    INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
    VALUES 
      (21601664, 21601664, 0, 0),
      (21601664, 1347384, 1, 1),
      (21601664, 1347390, 1, 1);
  ")

  DBI::dbExecute(con, "
    INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, valid_start_date, valid_end_date)
    VALUES (9999001, 1347384, 'Maps to', DATE '1970-01-01', DATE '2099-12-31');
  ")

  # 20 patients across SiteA and SiteB
  for (pid in 1:20) {
    site <- if (pid <= 10) "SiteA" else "SiteB"
    DBI::dbExecute(con, sprintf("
      INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value)
      VALUES (%d, 8507, 1960, 8527, 38003563, 'PAT-%s-%d');
    ", pid, site, pid))
    DBI::dbExecute(con, sprintf("
      INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
      VALUES (1, %d, DATE '2023-01-05', DATE '2023-01-10');
    ", pid))
  }

  # Drug exposures: SiteA
  for (pid in 1:5) {
    DBI::dbExecute(con, sprintf("
      INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
      VALUES (%d, %d, 1347384, DATE '2023-01-02', DATE '2023-01-05', 38000177, '855332', 4132161);
    ", pid, pid))
  }
  for (pid in 6:7) {
    DBI::dbExecute(con, sprintf("
      INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
      VALUES (%d, %d, 1347390, DATE '2023-01-02', DATE '2023-01-05', 38000177, '313299', 4184451);
    ", pid, pid))
  }
  for (pid in 8:10) {
    DBI::dbExecute(con, sprintf("
      INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
      VALUES (%d, %d, 1713332, DATE '2023-01-02', DATE '2023-01-05', 38000177, '308182', 4132161);
    ", pid, pid))
  }
  # SiteB (silent zero for beta blockers)
  for (pid in 11:20) {
    DBI::dbExecute(con, sprintf("
      INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value, route_concept_id)
      VALUES (%d, %d, 1713332, DATE '2023-01-02', DATE '2023-01-05', 38000177, '308182', 4132161);
    ", pid, pid))
  }

  DBI::dbExecute(con, "
    CREATE OR REPLACE VIEW v_cohort AS 
    SELECT c.subject_id, c.cohort_start_date, c.cohort_end_date,
           CASE WHEN c.subject_id <= 10 THEN 'SiteA' ELSE 'SiteB' END AS site
    FROM cohort c;
  ")
  DBI::dbExecute(con, "
    CREATE OR REPLACE VIEW v_drug_exposure AS 
    SELECT de.*,
           CASE WHEN de.person_id <= 10 THEN 'SiteA' ELSE 'SiteB' END AS site
    FROM drug_exposure de;
  ")

  con
}

testthat::test_that("extract_medication_classes unresolved guard raises or warns", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  bad_specs <- list(missing_class = list(atc_codes = c("NONEXISTENT")))
  testthat::expect_error(
    extract_medication_classes(con, "v_cohort", bad_specs, strategy = "standard", on_unresolved = "raise"),
    "resolved to no concept in vocabulary"
  )

  testthat::expect_warning(
    extract_medication_classes(con, "v_cohort", bad_specs, strategy = "standard", on_unresolved = "warn"),
    "resolved to no concept"
  )
})

testthat::test_that("extract_medication_classes dosage form exclusions filter ophthalmic routes", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  specs <- list(beta_blocker = list(atc_codes = c("C07AA")))
  all_forms <- extract_medication_classes(con, "v_cohort", specs, strategy = "standard")
  testthat::expect_equal(sum(all_forms$beta_blocker), 7L)

  ex_forms <- extract_medication_classes(con, "v_cohort", specs, strategy = "standard", exclude_forms = c("Ophthalmic"))
  testthat::expect_equal(sum(ex_forms$beta_blocker), 5L)
})

testthat::test_that("extract_medication_classes route and by_type extraction", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  specs <- list(beta_blocker = list(atc_codes = c("C07AA")))
  res <- extract_medication_classes(con, "v_cohort", specs, strategy = "standard", route_concept_ids = c(4132161), by_type = TRUE)
  testthat::expect_equal(sum(res$beta_blocker), 5L)
  testthat::expect_true("beta_blocker_type_38000177" %in% names(res))
})

testthat::test_that("assert_exposure_coverage catches silent zero sites", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  specs <- list(beta_blocker = list(atc_codes = c("C07AA")))
  flags <- extract_medication_classes(con, "v_cohort", specs, strategy = "standard")

  # SiteB has 0 exposures (silent zero); with min_n = 5 it should fail
  testthat::expect_error(
    assert_exposure_coverage(flags, classes = c("beta_blocker"), min_n = 5),
    "assert_exposure_coverage failed for medication class 'beta_blocker' at site 'SiteB'"
  )

  # With min_n = 100 it skips SiteB (n=10) and passes
  testthat::expect_silent(
    assert_exposure_coverage(flags, classes = c("beta_blocker"), min_n = 100)
  )
})

testthat::test_that("audit_medication_strategies flags both_blind and supports explain", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  specs <- list(beta_blocker = list(atc_codes = c("C07AA")))
  xw <- data.frame(source_value = c("855332"), drug_class = c("beta_blocker"), stringsAsFactors = FALSE)
  flags <- extract_medication_classes(con, "v_cohort", specs, strategy = "both", crosswalk = xw)

  audit <- audit_medication_strategies(flags, classes = c("beta_blocker"), min_n = 5)
  testthat::expect_true("site" %in% names(audit))
  site_b_row <- audit[audit$site == "SiteB", ]
  testthat::expect_true(grepl("both_blind", site_b_row$reason))

  audit_expl <- audit_medication_strategies(flags, classes = c("beta_blocker"), min_n = 5,
                                            explain = TRUE, con = con, medication_specs = specs)
  testthat::expect_true(is.list(audit_expl))
  testthat::expect_true("explanation" %in% names(audit_expl))
})

testthat::test_that("explain_medication_disagreement masks small cell counts", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  specs <- list(beta_blocker = list(atc_codes = c("C07AA")))
  xw <- data.frame(source_value = c("855332"), drug_class = c("beta_blocker"), stringsAsFactors = FALSE)
  flags <- extract_medication_classes(con, "v_cohort", specs, strategy = "both", crosswalk = xw)

  expl <- explain_medication_disagreement(con, flags, specs, min_cell_count = 5L, mask_value = "<5")
  testthat::expect_true(nrow(expl) > 0)
  std_only <- expl[expl$disagreement_type == "standard_only", ]
  testthat::expect_true(nrow(std_only) > 0)
  testthat::expect_true(1347390 %in% std_only$drug_concept_id)
  testthat::expect_true(std_only$is_suppressed[1])
  testthat::expect_equal(std_only$patient_count[1], "<5")
})

testthat::test_that("export_medication_crosswalk exports concepts and validates required codes", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  specs <- list(beta_blocker = list(atc_codes = c("C07AA")))
  tmp_csv <- tempfile(fileext = ".csv")

  xw <- export_medication_crosswalk(con, specs, output_path = tmp_csv, include_retired = TRUE,
                                    must_contain = c("855332", "OLD-855"))
  testthat::expect_true(file.exists(tmp_csv))
  testthat::expect_true("OLD-855" %in% xw$source_value)
  testthat::expect_true("855332" %in% xw$source_value)

  testthat::expect_error(
    export_medication_crosswalk(con, specs, must_contain = c("NONEXISTENT")),
    "missing required codes"
  )
})

testthat::test_that("audit_code_list_coverage detects omitted high-frequency codes", {
  con <- .get_mock_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  rep <- audit_code_list_coverage(
    con, domain = "drug", supplied_codes = c("855332"),
    table_name = "v_drug_exposure", by_site = TRUE, min_share = 0.10
  )
  testthat::expect_true(nrow(rep) > 0)
  site_a <- rep[rep$site == "SiteA", ]
  testthat::expect_equal(site_a$covered_rows[1], 5L)
  testthat::expect_equal(site_a$coverage_share[1], 0.50)
  testthat::expect_true(any(site_a$flagged))
})

testthat::test_that("panel_missingness_audit flags discordant analyte missingness", {
  n <- 100
  df <- data.frame(
    site = c(rep("SiteA", n), rep("SiteB", n)),
    sodium = c(stats::rnorm(n), stats::rnorm(n)),
    potassium = c(stats::rnorm(n), stats::rnorm(n)),
    chloride = c(stats::rnorm(n), stats::rnorm(n)),
    bicarbonate = c(stats::rnorm(n), stats::rnorm(n)),
    bun = c(rep(NA, 90), stats::rnorm(10), stats::rnorm(n)), # 90% missing at SiteA
    creatinine = c(stats::rnorm(95), rep(NA, 5), stats::rnorm(n)), # 5% missing at SiteA
    glucose = c(stats::rnorm(n), stats::rnorm(n)),
    stringsAsFactors = FALSE
  )

  audit <- panel_missingness_audit(df, by = "site", threshold = 0.20)
  testthat::expect_true(nrow(audit) > 0)
  bun_site_a <- audit[audit$site == "SiteA" & audit$analyte == "bun", ]
  testthat::expect_true(bun_site_a$flagged[1])
  testthat::expect_true(grepl("panel_missingness_gap", bun_site_a$reason[1]))
})

testthat::test_that("plausibility_report verifies bounds and CI gate", {
  # Raw features with negative height and out-of-bounds potassium
  bad_df <- data.frame(
    potassium = c(12.5, 14.0, 15.0), # Extreme hyperkalemia
    sodium = c(140, 142, 138),
    height = c(-10.0, 170.0, 165.0), # Negative height
    stringsAsFactors = FALSE
  )

  rep <- plausibility_report(bad_df, ci_gate = FALSE)
  testthat::expect_true(nrow(rep) > 0)
  testthat::expect_true(any(rep$reason == "mean_outside_range"))
  testthat::expect_true(any(rep$reason == "negative_clinical_value"))

  testthat::expect_error(
    plausibility_report(bad_df, ci_gate = TRUE),
    "Plausibility CI gate failed"
  )
})

testthat::test_that("detect_redundant_features detects 3x ratio and correlation", {
  set.seed(42)
  n <- 100
  hgb <- stats::rnorm(n, 14, 2)
  hct <- 3.0 * hgb # Constant ratio 3.0x
  wbc <- stats::rnorm(n, 7, 1)

  df <- data.frame(hct = hct, hgb = hgb, wbc = wbc)
  red <- detect_redundant_features(df, tol = 1e-4)
  testthat::expect_true(nrow(red) > 0)
  ratio_hit <- red[red$redundancy_type == "constant_ratio", ]
  testthat::expect_true(nrow(ratio_hit) > 0)
  testthat::expect_equal(round(ratio_hit$metric_value[1], 1), 3.0)
})

testthat::test_that("dqd_summary checks mathematical consistency", {
  dqd_data <- data.frame(
    site = c("SiteA", "SiteA", "SiteA", "SiteB", "SiteB"),
    execution_status = c("PASSED", "PASSED", "FAILED", "PASSED", "PASSED"),
    pass_rate = c(0.70, 0.70, 0.70, 1.00, 1.00), # 0.70 is inconsistent with 2/3 (0.6667)
    stringsAsFactors = FALSE
  )

  summ <- dqd_summary(dqd_data, by_site = TRUE)
  testthat::expect_equal(nrow(summ), 2L)
  site_a <- summ[summ$site == "SiteA", ]
  testthat::expect_equal(site_a$passed[1], 2L)
  testthat::expect_equal(site_a$failed[1], 1L)
  testthat::expect_true(site_a$flagged[1])
  testthat::expect_true(grepl("reported_rate_inconsistent", site_a$reason[1]))
})
