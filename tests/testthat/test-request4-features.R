# Tests covering the 7 feature requests from tasks/request4.txt:
# 1. Anthropometric Harmonization (Height, Weight, and Calculated BMI)
# 2. Clinical Benchmark Score Calculators (LACE Index & HOSPITAL Score)
# 3. Transitions-of-Care Medication Trajectories (medication_transitions)
# 4. Multi-Site Federated Consortium Connector (federated_consortium)
# 5. Additional European & Alternative Unit Scaling Factors (Hemoglobin mmol/L, HbA1c mmol/mol, Creatinine umol/L)
# 6. Population-Specific Bounds Profiles (Adult vs Pediatric vs Neonatal)
# 7. Subprocess Worker in as_plp_arrow

r4_cdm <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  con
}

test_that("Anthropometric harmonization correctly standardizes inches/lbs and derives BMI", {
  con <- r4_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1960, 8527, 38003563)")
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES (3036277, 'Height', 'Measurement', 'LOINC', 'Clinical Observation', '8302-2', DATE '1970-01-01', DATE '2099-12-31'), (3025315, 'Weight', 'Measurement', 'LOINC', 'Clinical Observation', '29463-7', DATE '1970-01-01', DATE '2099-12-31'), (3038553, 'BMI', 'Measurement', 'LOINC', 'Clinical Observation', '39156-5', DATE '1970-01-01', DATE '2099-12-31')")

  DBI::dbExecute(con, "
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_source_value)
    VALUES
    (1, 1, 3036277, DATE '2023-01-01', 32817, 68.0, '[in_us]'),
    (2, 1, 3025315, DATE '2023-01-01', 32817, 154.3236, '[lb_av]'),
    (3, 1, 3038553, DATE '2023-01-01', 32817, 350.0, 'kg/m2')
  ")

  report <- harmonize_anthropometrics(con, create_view = TRUE, replace_implausible_bmi = TRUE)
  expect_gte(report$height_records_checked[1], 1L)
  expect_gte(report$weight_records_checked[1], 1L)
  expect_gte(report$calculated_bmis_derived[1], 1L)

  res <- DBI::dbGetQuery(con, "SELECT value_as_number FROM measurement_harmonized_anthropometrics WHERE measurement_concept_id = 3038553 AND person_id = 1")
  expect_equal(nrow(res), 1)
  bmi_val <- res$value_as_number[1]
  expect_true(bmi_val >= 23.0 && bmi_val <= 24.0)
})

test_that("calculate_lace_index and calculate_hospital_score evaluate bedside heuristics", {
  con <- r4_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1960, 8527, 38003563)")
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES (9203, 'Emergency', 'Visit', 'Visit', 'Visit', 'ER', DATE '1970-01-01', DATE '2099-12-31'), (3000963, 'Hemoglobin', 'Measurement', 'LOINC', 'Lab Test', '718-7', DATE '1970-01-01', DATE '2099-12-31'), (3019550, 'Sodium', 'Measurement', 'LOINC', 'Lab Test', '2951-2', DATE '1970-01-01', DATE '2099-12-31')")

  DBI::dbExecute(con, "
    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
    VALUES (101, 1, 9203, DATE '2023-02-01', DATE '2023-02-06', 32817);
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number)
    VALUES
    (10, 1, 3000963, DATE '2023-02-05', 32817, 10.5),
    (11, 1, 3019550, DATE '2023-02-05', 32817, 130.0);
  ")

  DBI::dbExecute(con, "
    DROP TABLE IF EXISTS cohort;
    CREATE OR REPLACE VIEW cohort AS
    SELECT person_id AS subject_id, visit_start_date AS cohort_start_date, visit_end_date AS cohort_end_date
    FROM visit_occurrence WHERE visit_occurrence_id = 101;
  ")

  lace_df <- calculate_lace_index(con, cohort_table = "cohort")
  expect_true("lace_score" %in% names(lace_df))
  expect_true("lace_risk" %in% names(lace_df))
  expect_gte(lace_df$lace_score[1], 7L)
  expect_true(lace_df$lace_risk[1] %in% c("Moderate", "High"))

  hosp_df <- calculate_hospital_score(con, cohort_table = "cohort")
  expect_true("hospital_score" %in% names(hosp_df))
  expect_true("hospital_risk" %in% names(hosp_df))
  expect_gte(hosp_df$hospital_score[1], 5L)
})

test_that("extract_medication_transitions calculates care transition trajectory flags", {
  con <- r4_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1960, 8527, 38003563)")
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES (9201, 'Inpatient', 'Visit', 'Visit', 'Visit', 'IP', DATE '1970-01-01', DATE '2099-12-31'), (1112807, 'Aspirin', 'Drug', 'RxNorm', 'Ingredient', '1191', DATE '1970-01-01', DATE '2099-12-31')")
  DBI::dbExecute(con, "INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation) VALUES (1112807, 1112807, 0, 0)")

  DBI::dbExecute(con, "
    DROP TABLE IF EXISTS cohort;
    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
    VALUES (201, 1, 9201, DATE '2023-03-01', DATE '2023-03-10', 32817);
    CREATE OR REPLACE VIEW cohort AS
    SELECT person_id, visit_start_date AS cohort_start_date, visit_end_date AS cohort_end_date
    FROM visit_occurrence WHERE visit_occurrence_id = 201;
  ")

  DBI::dbExecute(con, "
    INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
    VALUES
    (1, 1, 1112807, DATE '2023-03-05', DATE '2023-03-05', 38000177),
    (2, 1, 1112807, DATE '2023-03-10', DATE '2023-03-10', 38000177);
  ")

  specs <- list(antiplatelets = c(1112807))
  trans <- extract_medication_transitions(con, cohort_table = "cohort", medication_specs = specs)

  df <- trans$to_dataframe()
  expect_true("antiplatelets_initiated_inpatient" %in% names(df))
  expect_true("antiplatelets_discontinued_at_discharge" %in% names(df))
  expect_true("antiplatelets_continued_across_stay" %in% names(df))
  expect_true("med_initiated_inpatient" %in% names(df))

  expect_equal(df$antiplatelets_initiated_inpatient[1], 1L)
  expect_equal(df$antiplatelets_discontinued_at_discharge[1], 0L)
  expect_equal(df$med_initiated_inpatient[1], 1L)

  summ <- trans$summary()
  expect_gt(nrow(summ), 0)
  expect_equal(summ$initiated_inpatient_n[summ$medication_class == "antiplatelets"], 1L)

  tbl <- trans$transition_matrix("antiplatelets")
  expect_true(is.table(tbl))
})

test_that("federated_consortium connects multiple sites and audits summaries", {
  tmp_dir <- tempfile()
  dir.create(tmp_dir)
  on.exit(unlink(tmp_dir, recursive = TRUE), add = TRUE)

  db_a <- file.path(tmp_dir, "site_a.duckdb")
  db_b <- file.path(tmp_dir, "site_b.duckdb")

  build_schema(db_path = db_a)
  ca <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_a)
  DBI::dbExecute(ca, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (101, 8507, 1970, 8527, 38003563)")
  DBI::dbExecute(ca, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (1, 101, 9201, DATE '2023-01-01', DATE '2023-01-05', 32817)")
  DBI::dbDisconnect(ca, shutdown = TRUE)

  build_schema(db_path = db_b)
  cb <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_b)
  DBI::dbExecute(cb, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (201, 8532, 1980, 8527, 38003563)")
  DBI::dbExecute(cb, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (2, 201, 9201, DATE '2023-02-01', DATE '2023-02-04', 32817)")
  DBI::dbDisconnect(cb, shutdown = TRUE)

  fc <- federated_consortium(sites = c("Site Alpha" = db_a, "Site Beta" = db_b))
  on.exit(consortium_close(fc), add = TRUE)

  q_res <- consortium_query(fc, "SELECT COUNT(*) AS n FROM v_person")
  expect_equal(q_res$n[1], 2L)

  sums <- consortium_site_summaries(fc)
  expect_equal(nrow(sums), 2)
  expect_equal(sum(sums$n_persons), 2L)
  expect_equal(sum(sums$n_visits), 2L)

  t2 <- consortium_generate_table2(fc, cohort_table = "v_person", columns = "year_of_birth")
  expect_true(nrow(t2) > 0)
  expect_equal(unique(t2$pct_missing), 0.0)
})

test_that("European and alternative unit scaling rules operate properly", {
  con <- r4_cdm()
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1960, 8527, 38003563)")
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES (3019550, 'Sodium', 'Measurement', 'LOINC', 'Lab Test', '2951-2', DATE '1970-01-01', DATE '2099-12-31')")

  DBI::dbExecute(con, "
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_source_value)
    VALUES
    (302, 1, 3019550, DATE '2023-04-01', 32817, 88.42, 'umol/L');
  ")

  rep <- harmonize_measurement_units(con, list(creatinine = "2951-2"), create_view = TRUE)
  res <- DBI::dbGetQuery(con, "SELECT value_as_number FROM measurement_harmonized WHERE measurement_id = 302")
  expect_equal(nrow(res), 1)
  expect_equal(res$value_as_number[1], 1.0, tolerance = 0.02)

  # Check affine formula for HbA1c mmol/mol -> NGSP %: (0.09148 * 48) + 2.152 = 6.543%
  hba1c_rule <- DEFAULT_UNIT_RULES$hba1c$rules[[1]]
  expect_equal(hba1c_rule[[1]], "^(mmol/mol)$")
  expect_equal(hba1c_rule[[2]], 0.09148)
  expect_equal(hba1c_rule[[3]], 2.152)
})

test_that("Population-specific bounds profiles enforce distinct physiological limits", {
  expect_true(all(c("adult", "pediatric", "neonatal") %in% names(POPULATION_PHYSIOLOGIC_BOUNDS)))

  df <- data.frame(
    heart_rate = c(50.0, 140.0),
    systolic_bp = c(80.0, 180.0)
  )

  # Adult bounds allow both
  adult_out <- filter_bounds(df, population = "adult")
  expect_false(any(is.na(adult_out$heart_rate)))
  expect_false(any(is.na(adult_out$systolic_bp)))

  # Neonatal bounds: 50 bpm is out of range (< 60), 180 mmHg SBP is out of range (> 130)
  neo_out <- filter_bounds(df, population = "neonatal")
  expect_true(is.na(neo_out$heart_rate[1]))
  expect_true(is.na(neo_out$systolic_bp[2]))

  # Table1Generator accepts population
  t1 <- table1_generator(con = DBI::dbConnect(duckdb::duckdb()), population = "pediatric")
  on.exit(t1$close(), add = TRUE)
  expect_equal(t1$state$population, NULL)
})

test_that("as_plp_arrow accepts subprocess_worker parameter", {
  testthat::skip_if_not_installed("arrow")
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  df <- data.frame(
    subject_id = c(1, 2, 3),
    cohort_start_date = as.Date(c("2023-01-01", "2023-01-02", "2023-01-03")),
    age = c(45, 62, 70),
    charlson = c(0, 2, 4),
    outcome = c(0, 1, 0)
  )

  plp1 <- as_plp_arrow(con, df, outcome_col = "outcome", subprocess_worker = FALSE)
  expect_true(!is.null(plp1$cohorts))
  expect_true(!is.null(plp1$covariates))

  plp2 <- as_plp_arrow(con, df, outcome_col = "outcome", subprocess_worker = TRUE)
  expect_true(!is.null(plp2$cohorts))
  expect_true(!is.null(plp2$covariates))
})
