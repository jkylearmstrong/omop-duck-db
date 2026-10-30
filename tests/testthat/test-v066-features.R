test_that("1. Version is at least 0.6.6 and provenance reports parity in R", {
  expect_true(utils::packageVersion("omopduckdb") >= "0.6.6")
  prov <- provenance()
  expect_true(package_version(prov$package_version) >= "0.6.6")
})

test_that("1b. Drug type recognition detects modern EHR prescription 32838", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (person_id BIGINT, year_of_birth INT, gender_concept_id INT, race_concept_id INT, ethnicity_concept_id INT);
    INSERT INTO person VALUES (1, 1980, 8507, 0, 0);

    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT,
      person_id BIGINT,
      drug_concept_id BIGINT,
      drug_type_concept_id BIGINT,
      drug_exposure_start_date DATE
    );
    INSERT INTO drug_exposure VALUES
      (1, 1, 111, 38000177, '2023-01-01'), -- Legacy prescription written
      (2, 1, 111, 32838,    '2023-01-02'), -- Modern EHR prescription
      (3, 1, 222, 38000180, '2023-01-03'); -- Inpatient administration
  ")

  expect_true(32838L %in% DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS)
  expect_true(38000177L %in% DEFAULT_PRESCRIBED_TYPE_CONCEPT_IDS)

  res <- vocabulary_preflight(con, fail_fast = FALSE, required = character(0))
  expect_equal(res$drug_types$prescribed, 2L)
  expect_equal(res$drug_types$administered, 1L)
  expect_equal(res$drug_types$total, 3L)
  expect_equal(round(res$drug_type_breakdown$prescribed_pct, 1), 66.7)

  # Custom exposure types
  res_custom <- vocabulary_preflight(con, fail_fast = FALSE, required = character(0), prescribed_type_concept_ids = c(38000177))
  expect_equal(res_custom$drug_types$prescribed, 1L)
})

test_that("2. Multi-anchor windows support visit_start_date vs visit_end_date anchoring", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE cohort (
      person_id BIGINT,
      cohort_start_date DATE,
      cohort_end_date DATE,
      visit_start_date DATE,
      visit_end_date DATE
    );
    INSERT INTO cohort VALUES
      (1, '2023-05-10', '2023-05-20', '2023-05-10', '2023-05-20');

    CREATE TABLE measurement (
      measurement_id BIGINT,
      person_id BIGINT,
      measurement_concept_id BIGINT,
      measurement_date DATE,
      value_as_number DOUBLE
    );
    INSERT INTO measurement VALUES
      (1, 1, 3016723, '2023-05-09', 1.0), -- 1 day before visit start
      (2, 1, 3016723, '2023-05-19', 1.8); -- 1 day before visit end
  ")

  # Window [-7, 0] anchored to start date -> should capture measurement 1 (2023-05-09)
  res_start <- extract_patient_features(
    con,
    cohort_table = "cohort",
    measurement_concept_ids = c(3016723),
    lookback_days = 7,
    offset_anchor = "visit_start_date"
  )
  expect_true(nrow(res_start) > 0)
  expect_equal(res_start$mean_measurement_3016723, 1.0)

  # Window [-2, 0] anchored to end date -> should capture measurement 2 (2023-05-19)
  res_end <- extract_patient_features(
    con,
    cohort_table = "cohort",
    measurement_concept_ids = c(3016723),
    lookback_days = 2,
    offset_anchor = "visit_end_date"
  )
  expect_true(nrow(res_end) > 0)
  expect_equal(res_end$mean_measurement_3016723, 1.8)

  # extract_features alias test
  res_alias <- extract_features(
    con,
    cohort_table = "cohort",
    measurement_concept_ids = c(3016723),
    lookback_days = 2,
    anchor = "visit_end_date"
  )
  expect_equal(res_alias$mean_measurement_3016723, 1.8)
})

test_that("3. Automated unit discordance detection & bimodal normalization", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE measurement (
      measurement_id BIGINT,
      person_id BIGINT,
      measurement_concept_id BIGINT,
      measurement_date DATE,
      value_as_number DOUBLE,
      unit_concept_id BIGINT
    );
    -- 50 rows in mg/dL (~1.0)
    INSERT INTO measurement
    SELECT
      range AS measurement_id,
      range AS person_id,
      3016723 AS measurement_concept_id,
      DATE '2023-01-01' AS measurement_date,
      1.0 + (range % 5) * 0.05 AS value_as_number,
      8840 AS unit_concept_id
    FROM range(1, 51);

    -- 50 rows in umol/L (~88.42)
    INSERT INTO measurement
    SELECT
      range AS measurement_id,
      range AS person_id,
      3016723 AS measurement_concept_id,
      DATE '2023-01-01' AS measurement_date,
      88.42 * (1.0 + (range % 5) * 0.1) AS value_as_number,
      0 AS unit_concept_id
    FROM range(51, 101);
  ")

  audit <- detect_unit_discordance(con, measurement_concept_id = 3016723, target_unit = "mg/dL")
  expect_true(nrow(audit) > 0)
  row <- audit[audit$measurement_concept_id == 3016723, ]
  expect_true(any(row$is_discordant))
  expect_equal(row$detected_ratio[1], 88.42, tolerance = 0.1)

  # Test normalize_units
  norm_df <- normalize_units(
    con,
    measurement_concept_id = 3016723,
    target_unit = "mg/dL",
    auto_convert_ratio = 88.42,
    bimodal_threshold = 30.0
  )
  scaled_vals <- norm_df$value_as_number[norm_df$measurement_id >= 51]
  expect_true(all(scaled_vals <= 3.0))
})

test_that("4. Flexible outlier strategies (winsorize, flag_indicator)", {
  df <- data.frame(
    heart_rate = c(20, 60, 80, 100, 250),
    creatinine = c(0.1, 1.0, 1.2, 2.0, 35.0)
  )

  bounds <- list(
    heart_rate = c(min = 30, max = 220),
    creatinine = c(min = 0.2, max = 25.0)
  )

  # 1. Winsorize
  w_df <- filter_bounds(df, bounds = bounds, action = "winsorize")
  expect_equal(w_df$heart_rate[1], 30)
  expect_equal(w_df$heart_rate[5], 220)
  expect_equal(w_df$creatinine[1], 0.2)
  expect_equal(w_df$creatinine[5], 25.0)

  # 2. Flag indicator
  f_df <- filter_bounds(df, bounds = bounds, action = "flag_indicator")
  expect_true("is_outlier" %in% names(f_df))
  expect_true("is_outlier_heart_rate" %in% names(f_df))
  expect_equal(f_df$heart_rate[1], 30)
  expect_equal(f_df$is_outlier_heart_rate[1], 1L)
  expect_equal(f_df$is_outlier_heart_rate[2], 0L)
  expect_equal(f_df$is_outlier[1], 1L)
  expect_equal(f_df$is_outlier[2], 0L)

  # Test in sanitize_measurements
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  DBI::dbExecute(con, "
    CREATE TABLE raw_labs (
      person_id BIGINT,
      measurement_id BIGINT,
      measurement_concept_id BIGINT,
      unit_concept_id BIGINT,
      measurement_date DATE,
      value_as_number DOUBLE
    );
    INSERT INTO raw_labs VALUES
      (1, 1, 3016723, 8840, '2023-01-01', 1.0),
      (2, 2, 3016723, 8840, '2023-01-01', 50.0);
  ")

  # Drop
  dropped <- sanitize_measurements(con, measurement_table = "raw_labs", outlier_action = "drop")
  expect_equal(nrow(dropped), 1L)
  expect_equal(dropped$person_id[1], 1L)

  # Winsorize
  winsorized <- sanitize_measurements(con, measurement_table = "raw_labs", outlier_action = "winsorize")
  expect_equal(nrow(winsorized), 2L)
  expect_equal(winsorized$value_as_number[winsorized$person_id == 2], 30.0)

  # Flag indicator
  flagged <- sanitize_measurements(con, measurement_table = "raw_labs", outlier_action = "flag_indicator")
  expect_true("is_outlier" %in% names(flagged))
  expect_equal(flagged$is_outlier[flagged$person_id == 1], 0L)
  expect_equal(flagged$is_outlier[flagged$person_id == 2], 1L)
  expect_equal(flagged$value_as_number[flagged$person_id == 2], 30.0)
})

test_that("5. Vectorized drug eras and adherence PDC computation", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE cohort (person_id BIGINT, cohort_start_date DATE, cohort_end_date DATE, visit_start_date DATE);
    INSERT INTO cohort VALUES (1, '2023-01-01', '2023-06-30', '2023-01-01');

    CREATE TABLE drug_exposure (
      drug_exposure_id BIGINT,
      person_id BIGINT,
      drug_concept_id BIGINT,
      drug_exposure_start_date DATE,
      drug_exposure_end_date DATE,
      days_supply INT
    );
    -- 3 fills: Jan 1 - Jan 30, Feb 5 - Mar 6 (gap 6 <= 30), May 10 - Jun 8 (gap > 30)
    INSERT INTO drug_exposure VALUES
      (1, 1, 1112807, '2023-01-01', '2023-01-30', 30),
      (2, 1, 1112807, '2023-02-05', '2023-03-06', 30),
      (3, 1, 1112807, '2023-05-10', '2023-06-08', 30);
  ")

  eras <- compute_drug_eras(con, persistence_window_days = 30)
  expect_equal(nrow(eras), 2L)
  expect_equal(eras$drug_exposure_count[1], 2L)
  expect_equal(eras$drug_exposure_count[2], 1L)

  pdc_df <- compute_adherence_pdc(
    con,
    cohort_table = "cohort",
    lookback_days = 0,
    lookahead_days = 180,
    offset_anchor = "visit_start_date"
  )
  expect_true(nrow(pdc_df) > 0)
  expect_true("pdc" %in% names(pdc_df))
  expect_true("mpr" %in% names(pdc_df))
  expect_true("is_adherent_80" %in% names(pdc_df))
  expect_true(pdc_df$pdc[1] >= 0.0 && pdc_df$pdc[1] <= 1.0)
})

test_that("6. Competing risks CIF and Fine-Gray subdistribution hazard", {
  site_a <- data.frame(
    time = c(5, 10, 15, 20, 25, 30),
    event = c(1, 2, 0, 1, 2, 0), # 1 = primary, 2 = competing
    group = c(1, 1, 1, 0, 0, 0)
  )

  site_b <- data.frame(
    time = c(4, 9, 14, 18, 24, 28),
    event = c(1, 0, 2, 1, 0, 2),
    group = c(1, 1, 1, 0, 0, 0)
  )

  result <- consortium_survival_meta_analysis(
    list(SiteA = site_a, SiteB = site_b),
    time_col = "time",
    event_col = "event",
    group_col = "group",
    competing_event = 2L
  )
  expect_false(is.null(result$pooled_cif))
  expect_false(is.null(result$cause_specific_hr))
  expect_false(is.null(result$fine_gray_subdistribution_hr))
  expect_true("SiteA" %in% names(result$site_cifs))
})

test_that("7. Out-of-core Parquet streaming from extract_patient_features", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE cohort (
      person_id BIGINT,
      cohort_start_date DATE,
      cohort_end_date DATE,
      site VARCHAR
    );
    INSERT INTO cohort VALUES
      (1, '2023-01-01', '2023-01-10', 'Site1'),
      (2, '2023-02-01', '2023-02-10', 'Site2');

    CREATE TABLE measurement (
      measurement_id BIGINT,
      person_id BIGINT,
      measurement_concept_id BIGINT,
      measurement_date DATE,
      value_as_number DOUBLE
    );
    INSERT INTO measurement VALUES
      (1, 1, 3016723, '2023-01-05', 1.1),
      (2, 2, 3016723, '2023-02-05', 1.4);
  ")

  pq_file <- tempfile(fileext = ".parquet")
  on.exit(unlink(pq_file), add = TRUE)

  res <- extract_patient_features(
    con,
    cohort_table = "cohort",
    measurement_concept_ids = c(3016723),
    stream_to_parquet = pq_file
  )
  expect_true(file.exists(pq_file))

  # Read back via DuckDB
  read_back <- DBI::dbGetQuery(con, sprintf("SELECT * FROM read_parquet('%s')", gsub("\\\\", "/", pq_file)))
  expect_equal(nrow(read_back), 2L)
})

test_that("8. Dual-path comorbidity scoring with source-code fallback", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE cohort (
      subject_id BIGINT,
      cohort_start_date DATE
    );
    INSERT INTO cohort VALUES
      (10, '2023-01-01'),
      (20, '2023-01-01');

    CREATE TABLE condition_occurrence (
      condition_occurrence_id BIGINT,
      person_id BIGINT,
      condition_concept_id BIGINT,
      condition_start_date DATE,
      condition_source_value VARCHAR
    );
    INSERT INTO condition_occurrence VALUES
      (1, 10, 0, '2022-12-15', 'I21.9'),  -- Unmapped standard concept (0), valid ICD-10 for MI
      (2, 20, 4329847, '2022-12-15', 'I21.9'); -- Standard concept mapped
  ")

  charlson <- suppressWarnings(extract_charlson_index(
    con,
    cohort_table = "cohort",
    fallback_on_unmapped = TRUE,
    return_mapping_fidelity = TRUE
  ))
  expect_true(nrow(charlson) > 0)
  expect_true("charlson_index" %in% names(charlson))
  expect_true("mapping_fidelity_score" %in% names(charlson))

  p10 <- charlson[charlson$subject_id == 10, ]
  expect_true(p10$charlson_index >= 1)
})

test_that("9. Acute in-hospital trajectories (KDIGO AKI, shock index, glucose volatility)", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE cohort (
      person_id BIGINT,
      visit_occurrence_id BIGINT,
      cohort_start_date DATE,
      cohort_end_date DATE
    );
    INSERT INTO cohort VALUES
      (1, 101, '2023-01-01', '2023-01-10');

    CREATE TABLE measurement (
      measurement_id BIGINT,
      person_id BIGINT,
      visit_occurrence_id BIGINT,
      measurement_concept_id BIGINT,
      measurement_datetime TIMESTAMP,
      value_as_number DOUBLE
    );
    -- Admission Creatinine = 1.0, peak Creatinine = 3.2 (Stage 3 AKI)
    -- Glucose admission = 120, max = 280, min = 100
    -- Heart rate = 110, SBP = 90 (Shock Index = 110 / 90 = 1.22)
    INSERT INTO measurement VALUES
      (1, 1, 101, 3016723, '2023-01-01 08:00:00', 1.0),
      (2, 1, 101, 3016723, '2023-01-04 12:00:00', 3.2),
      (3, 1, 101, 3016723, '2023-01-09 10:00:00', 1.4),
      (4, 1, 101, 3004501, '2023-01-01 09:00:00', 120.0),
      (5, 1, 101, 3004501, '2023-01-03 09:00:00', 280.0),
      (6, 1, 101, 3004501, '2023-01-05 09:00:00', 100.0),
      (7, 1, 101, 3027018, '2023-01-02 10:00:00', 110.0),
      (8, 1, 101, 3004249, '2023-01-02 10:00:00', 90.0);
  ")

  acute <- extract_acute_trajectories(con, cohort_table = "cohort")
  expect_true(nrow(acute) > 0)
  row <- acute[1, ]
  expect_equal(row$creatinine_admission, 1.0)
  expect_equal(row$creatinine_peak, 3.2)
  expect_equal(round(row$creatinine_delta, 1), 2.2)
  expect_equal(row$kdigo_aki_stage, 3L)
  expect_equal(row$glucose_volatility, 180.0)
  expect_equal(round(row$shock_index, 2), 1.22)
})

test_that("10. In-encounter LOCF and leakage-safe imputation", {
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE encounter_obs (
      person_id BIGINT,
      visit_occurrence_id BIGINT,
      obs_hour INT,
      heart_rate DOUBLE,
      creatinine DOUBLE
    );
    INSERT INTO encounter_obs VALUES
      (1, 101, 1, 72.0, 1.0),
      (1, 101, 2, NULL, NULL),  -- Should LOCF heart_rate to 72.0, creatinine to 1.0
      (1, 101, 3, 80.0, NULL),  -- Heart rate updated to 80.0, creatinine LOCF 1.0
      (2, 102, 1, NULL, 1.2);   -- Heart rate never observed in encounter: imputed by median
  ")

  training_medians <- list(heart_rate = 75.0, creatinine = 1.1)
  imputed <- impute_encounter_observations(
    con,
    feature_table = "encounter_obs",
    time_col = "obs_hour",
    median_values = training_medians,
    add_indicators = TRUE
  )
  expect_true(nrow(imputed) > 0)
  expect_true("miss_heart_rate" %in% names(imputed))
  expect_true("miss_creatinine" %in% names(imputed))

  # Check LOCF on encounter 101
  enc101 <- imputed[imputed$visit_occurrence_id == 101, ]
  enc101 <- enc101[order(enc101$obs_hour), ]
  expect_equal(enc101$heart_rate, c(72.0, 72.0, 80.0))
  expect_equal(enc101$creatinine, c(1.0, 1.0, 1.0))

  # Check median fallback on encounter 102
  enc102 <- imputed[imputed$visit_occurrence_id == 102, ]
  expect_equal(enc102$heart_rate[1], 75.0)
  expect_equal(enc102$miss_heart_rate[1], 1L)
})
