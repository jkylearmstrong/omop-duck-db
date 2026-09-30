library(omopduckdb)

test_that("build_readmission_cohort enforces eligibility, censoring, and sampling rules", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)
  ensure_cohort_tables(con)

  # 1. Seed Person
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
        (101, 8507, 1980, 1, 1, 8527, 38003564),  -- Adult, Readmitted within 30d
        (102, 8532, 1985, 5, 10, 8516, 38003564), -- Adult, Follow-up encounter >= 30d, No readmission
        (103, 8507, 1990, 3, 15, 8527, 38003564), -- Adult, Censored (no events >= 30d)
        (104, 8532, 2015, 6, 20, 8527, 38003564), -- Pediatric (Age < 18)
        (105, 8507, 1970, 8, 12, 8527, 38003564), -- Adult, Same-day stay (LOS = 0)
        (106, 8532, 1975, 4, 5, 8516, 38003564),  -- Adult, Died in hospital (4216643)
        (107, 8507, 1965, 9, 30, 8527, 38003564); -- Adult, In death table during stay
  ")

  # 2. Seed Visits
  DBI::dbExecute(con, "
    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        -- 101: Eligible index stay + 30d readmission
        (1001, 101, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (1002, 101, 9201, DATE '2021-05-15', DATE '2021-05-18', 8536, 32827),
        -- 102: Eligible index stay + OP visit at day 45 (verified follow-up >= 30d)
        (2001, 102, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
        (2002, 102, 9202, DATE '2021-06-18', DATE '2021-06-18', 8536, 32827),
        -- 103: Eligible index stay but lost to follow-up (no encounters >= 30d post-discharge)
        (3001, 103, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
        -- 104: Pediatric stay (Age < 18)
        (4001, 104, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
        -- 105: Same-day stay (LOS = 0)
        (5001, 105, 9201, DATE '2021-05-01', DATE '2021-05-01', 8536, 32827),
        -- 106: Died in hospital
        (6001, 106, 9201, DATE '2021-05-01', DATE '2021-05-04', 4216643, 32827),
        -- 107: Died during stay
        (7001, 107, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827);

    INSERT INTO death (person_id, death_date, death_type_concept_id)
    VALUES (107, DATE '2021-05-03', 32815);
  ")

  # Run cohort generator with require_verified_followup=TRUE
  cohort_df <- build_readmission_cohort(
    con,
    cohort_id = 1,
    outcome_cohort_id = 2,
    washin_days = 0,
    require_verified_followup = TRUE
  )

  expect_equal(nrow(cohort_df), 2)
  expect_equal(sort(cohort_df$subject_id), c(101, 102))

  p101 <- cohort_df[cohort_df$subject_id == 101, ]
  expect_equal(p101$outcome_flag[1], 1)
  expect_equal(p101$los_days[1], 4)

  p102 <- cohort_df[cohort_df$subject_id == 102, ]
  expect_equal(p102$outcome_flag[1], 0)
  expect_equal(p102$los_days[1], 3)

  # Check cohort table population
  c_rows <- DBI::dbGetQuery(con, "SELECT cohort_definition_id, subject_id FROM cohort ORDER BY cohort_definition_id, subject_id")
  expect_true(any(c_rows$cohort_definition_id == 1 & c_rows$subject_id == 101))
  expect_true(any(c_rows$cohort_definition_id == 1 & c_rows$subject_id == 102))
  expect_true(any(c_rows$cohort_definition_id == 2 & c_rows$subject_id == 101))
  expect_false(any(c_rows$cohort_definition_id == 2 & c_rows$subject_id == 102))

  # Test index sampling rules with multiple stays
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES (201, 8507, 1980, 1, 1, 8527, 38003564);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (2001, 201, 9201, DATE '2021-01-10', DATE '2021-01-15', 8536, 32827),
        (2002, 201, 9201, DATE '2021-04-10', DATE '2021-04-14', 8536, 32827),
        (2003, 201, 9201, DATE '2021-08-10', DATE '2021-08-16', 8536, 32827),
        (2004, 201, 9202, DATE '2021-10-01', DATE '2021-10-01', 8536, 32827);
  ")

  first_df <- build_readmission_cohort(con, cohort_id = 3, index_selection_rule = "first", washin_days = 0)
  p201_first <- first_df[first_df$subject_id == 201, ]
  expect_equal(p201_first$visit_occurrence_id[1], 2001)

  last_df <- build_readmission_cohort(con, cohort_id = 4, index_selection_rule = "last", washin_days = 0)
  p201_last <- last_df[last_df$subject_id == 201, ]
  expect_equal(p201_last$visit_occurrence_id[1], 2003)

  rand_df <- build_readmission_cohort(con, cohort_id = 5, index_selection_rule = "random", random_state = 42, washin_days = 0)
  rand_df2 <- build_readmission_cohort(con, cohort_id = 6, index_selection_rule = "random", random_state = 42, washin_days = 0)
  expect_equal(rand_df[rand_df$subject_id == 201, "visit_occurrence_id"], rand_df2[rand_df2$subject_id == 201, "visit_occurrence_id"])
})

test_that("extract_temporal_features computes lookback utilization, recency, and stay metrics", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)
  ensure_cohort_tables(con)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
        (301, 8507, 1970, 6, 15, 8527, 38003564),
        (302, 8532, 1950, 2, 20, 8516, 38003563);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (3001, 301, 9201, DATE '2020-10-01', DATE '2020-10-05', 8536, 32827),
        (3002, 301, 9201, DATE '2020-10-20', DATE '2020-10-23', 8536, 32827),
        (3003, 301, 9203, DATE '2021-06-01', DATE '2021-06-01', 8536, 32827),
        (3004, 301, 9202, DATE '2021-06-15', DATE '2021-06-15', 8536, 32827),
        (3005, 301, 9201, DATE '2021-07-01', DATE '2021-07-06', 8536, 32827),
        (3006, 302, 9201, DATE '2021-08-01', DATE '2021-08-11', 38004284, 32827);

    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    VALUES 
        (10, 301, DATE '2021-07-01', DATE '2021-07-06'),
        (10, 302, DATE '2021-08-01', DATE '2021-08-11');
  ")

  t_df <- extract_temporal_features(con, cohort_table = "cohort", cohort_id = 10, lookback_days = 365)
  expect_equal(nrow(t_df), 2)

  p301 <- t_df[t_df$subject_id == 301, ]
  expect_equal(p301$index_los[1], 5)
  expect_equal(p301$discharge_category[1], "Home")
  expect_equal(p301$prior_ip_count[1], 2)
  expect_equal(p301$prior_ed_count[1], 1)
  expect_equal(p301$prior_op_count[1], 1)
  expect_equal(p301$prior_visit_count[1], 4)
  expect_equal(p301$prior_readmissions_30d[1], 1)
  expect_equal(p301$days_since_prior_encounter[1], 16)
  expect_equal(p301$days_since_prior_ip_ed[1], 30)
  expect_equal(p301$gender_name[1], "Male")

  p302 <- t_df[t_df$subject_id == 302, ]
  expect_equal(p302$index_los[1], 10)
  expect_equal(p302$discharge_category[1], "SNF/Rehab")
  expect_equal(p302$prior_visit_count[1], 0)
  expect_equal(p302$age_at_admission[1], 71)
  expect_equal(p302$age_group[1], "65-74")
  expect_equal(p302$gender_name[1], "Female")
  expect_equal(p302$ethnicity_name[1], "Hispanic")
})

test_that("aggregate_concept_sets and extract_measurements rollup hierarchies and strategies", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)
  ensure_cohort_tables(con)

  DBI::dbExecute(con, "
    INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
    VALUES 
        (1503297, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', DATE '2000-01-01', DATE '2099-12-31'),
        (19001409, 'Metformin 500 MG Oral Tablet', 'Drug', 'RxNorm', 'Clinical Drug', 'S', '860975', DATE '2000-01-01', DATE '2099-12-31'),
        (3094, 'Blood urea nitrogen', 'Measurement', 'LOINC', 'Lab Test', 'S', '3094-0', DATE '2000-01-01', DATE '2099-12-31'),
        (6299, 'BUN alternate', 'Measurement', 'LOINC', 'Lab Test', 'S', '6299-2', DATE '2000-01-01', DATE '2099-12-31');

    INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
    VALUES 
        (1503297, 1503297, 0, 0),
        (1503297, 19001409, 1, 1);

    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    VALUES 
        (10, 401, DATE '2021-05-10', DATE '2021-05-15'),
        (10, 402, DATE '2021-06-01', DATE '2021-06-05');

    INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
    VALUES (1, 401, 19001409, DATE '2021-04-01', DATE '2021-04-30', 32838);

    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, value_as_number, measurement_type_concept_id)
    VALUES 
        (10, 401, 3094, DATE '2021-05-10', TIMESTAMP '2021-05-10 08:00:00', 25.0, 32817),
        (11, 401, 6299, DATE '2021-05-14', TIMESTAMP '2021-05-14 16:00:00', 18.0, 32817);
  ")

  drug_df <- aggregate_concept_sets(
    con,
    cohort_table = "cohort",
    cohort_id = 10,
    concept_sets = list(metformin = 1503297L),
    domain = "drug",
    lookback_days = 365,
    as_flag = TRUE
  )
  expect_equal(nrow(drug_df), 2)
  expect_equal(drug_df$metformin[drug_df$subject_id == 401], 1)
  expect_equal(drug_df$metformin[drug_df$subject_id == 402], 0)

  meas_df <- extract_measurements(
    con,
    cohort_table = "cohort",
    cohort_id = 10,
    loinc_map = list(bun = c("3094-0", "6299-2")),
    strategy = "last_before_discharge",
    window = "stay"
  )
  expect_equal(nrow(meas_df), 2)
  expect_equal(meas_df$bun[meas_df$subject_id == 401], 18.0)
  expect_true(is.na(meas_df$bun[meas_df$subject_id == 402]))

  first_df <- extract_measurements(
    con,
    cohort_table = "cohort",
    cohort_id = 10,
    loinc_map = list(bun = c("3094-0", "6299-2")),
    strategy = "first_on_admission",
    window = "stay"
  )
  expect_equal(first_df$bun[first_df$subject_id == 401], 25.0)
})

test_that("generate_table1 and validate_table1_reconciliation produce valid summaries and drift checks", {
  data <- data.frame(
    subject_id = 1:8,
    outcome_flag = c(0, 0, 0, 0, 1, 1, 1, 1),
    age = c(50.0, 55.0, 60.0, 65.0, 70.0, 72.0, 75.0, 80.0),
    los = c(2.0, 3.0, 4.0, 2.0, 5.0, 6.0, 7.0, 8.0),
    gender = c("Female", "Male", "Female", "Male", "Female", "Female", "Male", "Male"),
    bun = c(15.0, 18.0, 16.0, NA, 28.0, 32.0, NA, 35.0),
    stringsAsFactors = FALSE
  )

  res <- generate_table1(
    data,
    strata_col = "outcome_flag",
    continuous_vars = c("age", "los", "bun"),
    categorical_vars = c("gender"),
    strata_labels = list("0" = "Non-readmitted", "1" = "Readmitted"),
    compute_smd = TRUE
  )

  t1 <- res$table1
  t1b <- res$table1b

  expect_true("Overall (N=8)" %in% names(t1))
  expect_true("Non-readmitted (N=4)" %in% names(t1))
  expect_true("Readmitted (N=4)" %in% names(t1))
  expect_true("SMD" %in% names(t1))
  expect_true("p_value" %in% names(t1))

  bun_miss <- t1b[t1b$Variable == "bun", ]
  expect_equal(bun_miss$overall_missing_n[1], 2)
  expect_equal(bun_miss$overall_missing_pct[1], 25.0)

  # Self-reconciliation is 100% concordant
  rec_pass <- validate_table1_reconciliation(t1, t1, tolerance = 0.05)
  expect_true(rec_pass$is_concordant)
  expect_equal(rec_pass$status, "PASS")

  # Simulated drift
  drifted <- data.frame(
    Variable = c("age", "los"),
    Overall = c("85.0 (5.0)", "12.0 (3.0)"),
    stringsAsFactors = FALSE
  )
  rec_drift <- validate_table1_reconciliation(t1, drifted, tolerance = 0.05)
  expect_false(rec_drift$is_concordant)
  expect_equal(rec_drift$status, "DRIFT_DETECTED")
})
