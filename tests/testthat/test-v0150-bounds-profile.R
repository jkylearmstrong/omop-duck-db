# v0.15.0 R side: physiologic bounds, WBC rescale, alternative LOINC codes, measured-first anion gap, hemoglobin from
# hematocrit, the "ederri" / "both" profile, the protocol's Elixhauser code table and the protocol history columns.
# Mirror of tests/test_benchmark_bounds_profile.py, tests/test_elixhauser_ederri.py and tests/test_protocol_history_and_both.py.
# Synthetic data only.

bp_codes <- list(bmi = c("39156-5", 1L), sbp = c("8480-6", 2L), wbc = c("6690-2", 3L), alt = c("1743-4", 4L), hct = c("4544-3", 5L),
                 na = c("2951-2", 6L), cl = c("2075-0", 7L), hco3 = c("1963-8", 8L), ag = c("33037-3", 9L), hgb = c("718-7", 10L))

bp_db <- function() {
  con <- DBI::dbConnect(duckdb::duckdb())
  DBI::dbExecute(con, "CREATE TABLE person (person_id BIGINT, gender_concept_id INT, year_of_birth INT, month_of_birth INT, day_of_birth INT,
                       race_concept_id INT, ethnicity_concept_id INT)")
  DBI::dbExecute(con, "INSERT INTO person VALUES (1, 8532, 1960, 1, 1, 8527, 38003564), (2, 8507, 1950, 1, 1, 8527, 38003564),
                       (3, 8532, 1970, 1, 1, 8527, 38003564)")
  DBI::dbExecute(con, "CREATE TABLE concept (concept_id INT, concept_code VARCHAR, vocabulary_id VARCHAR)")
  DBI::dbExecute(con, paste("INSERT INTO concept VALUES", paste(vapply(bp_codes, function(z) sprintf("(%s, '%s', 'LOINC')", z[[2]], z[[1]]), ""), collapse = ", ")))
  DBI::dbExecute(con, "CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE,
                       measurement_datetime TIMESTAMP, value_as_number DOUBLE)")
  mid <- 0L
  add <- function(person, key, value, hour = 8L) {
    mid <<- mid + 1L
    DBI::dbExecute(con, sprintf("INSERT INTO measurement VALUES (%d, %d, %s, DATE '2020-01-12', TIMESTAMP '2020-01-12 %02d:00:00', %s)",
                                mid, person, bp_codes[[key]][[2]], hour, format(value, scientific = FALSE)))
  }
  add(1, "bmi", 30); add(1, "bmi", 562000, 9L); add(1, "sbp", 120); add(1, "sbp", 999, 9L)
  add(1, "wbc", 7500); add(1, "wbc", 9, 9L); add(1, "alt", 40)
  add(1, "na", 140); add(1, "cl", 100); add(1, "hco3", 24); add(1, "hct", 36)
  add(2, "na", 140); add(2, "cl", 100); add(2, "hco3", 24); add(2, "ag", 12)
  add(3, "hct", 30); add(3, "hgb", 13); add(3, "bmi", 5)
  DBI::dbExecute(con, "CREATE TABLE cohort_t AS SELECT * FROM (VALUES (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0),
                       (2, DATE '2020-01-10', DATE '2020-01-15', 102, 1), (3, DATE '2020-01-10', DATE '2020-01-15', 103, 0))
                       t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)")
  con
}

bp_kw <- list(include_elixhauser = FALSE, include_medications = FALSE, include_utilization = FALSE, include_disposition = FALSE)
bp_run <- function(con, ...) suppressWarnings(do.call(extract_benchmark_features, c(list(con, "cohort_t"), bp_kw, list(...))))
bp_row <- function(df, person) df[df$person_id == person, , drop = FALSE]

test_that("bounds drop artifacts, can be overridden and disabled", {
  con <- bp_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  df <- bp_run(con)
  expect_equal(bp_row(df, 1)$vital_bmi, 30)
  expect_equal(bp_row(df, 1)$vital_systolic_bp, 120)
  expect_true(is.na(bp_row(df, 3)$vital_bmi))
  raw <- bp_run(con, bounds = NULL)
  expect_equal(bp_row(raw, 1)$vital_bmi, (30 + 562000) / 2)
  expect_equal(bp_row(raw, 3)$vital_bmi, 5)
  loose <- bp_run(con, bounds = list(bmi = c(1, NA)))
  expect_equal(bp_row(loose, 3)$vital_bmi, 5)
  expect_equal(bp_row(loose, 1)$vital_bmi, (30 + 562000) / 2)
  expect_equal(bp_row(loose, 1)$vital_systolic_bp, 120)
  expect_error(bp_run(con, bounds = "strict"), "bounds must be")
})

test_that("WBC per uL is rescaled and ALT is found under an alternative LOINC", {
  con <- bp_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  df <- bp_run(con)
  expect_equal(bp_row(df, 1)$lab_wbc, (7.5 + 9) / 2)
  expect_equal(bp_row(df, 1)$lab_wbc_last, 9)
  expect_equal(bp_row(df, 1)$lab_alt, 40)
  expect_equal(bp_row(bp_run(con, bounds = NULL), 1)$lab_wbc, (7500 + 9) / 2)
  expect_true(all(unlist(CORE_14_LAB_PANEL) %in% unlist(BENCHMARK_LAB_LOINCS)))
  expect_true(all(c(names(BENCHMARK_LAB_LOINCS), names(BENCHMARK_VITAL_LOINCS)) %in% names(DEFAULT_BOUNDS)))
})

test_that("a measured anion gap wins and hemoglobin is filled from hematocrit only when asked", {
  con <- bp_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  df <- bp_run(con)
  expect_equal(bp_row(df, 2)$lab_anion_gap, 12)
  expect_equal(bp_row(df, 1)$lab_anion_gap, 16)
  expect_equal(sum(names(df) == "lab_anion_gap"), 1L)
  expect_true(is.na(bp_row(df, 1)$lab_hemoglobin))
  on <- bp_run(con, hematocrit_fallback = TRUE)
  expect_equal(bp_row(on, 1)$lab_hemoglobin, 12)
  expect_equal(bp_row(on, 3)$lab_hemoglobin, 13)
  expect_equal(bp_row(bp_run(con, profile = "ederri", include_protocol_history = FALSE), 1)$lab_hemoglobin, 12)
  expect_true(is.na(bp_row(bp_run(con, profile = "ederri", hematocrit_fallback = FALSE, include_protocol_history = FALSE), 1)$lab_hemoglobin))
})

test_that("Elixhauser code_table = 'ederri' follows the protocol's prefix lists", {
  cases <- list(c("G40.9", "elix_neuro_other", "elix_paralysis"), c("G81.9", "elix_paralysis", "elix_neuro_other"),
                c("342.9", "elix_paralysis", "elix_neuro_other"), c("C50.9", "elix_solid_tumor", "elix_mets"),
                c("C78.0", "elix_mets", "elix_solid_tumor"), c("C7A.1", "elix_solid_tumor", "elix_mets"),
                c("D50.0", "elix_blood_loss_anemia", "elix_deficiency_anemia"), c("D50.9", "elix_deficiency_anemia", "elix_blood_loss_anemia"),
                c("E11.9", "elix_dm_uncomp", "elix_dm_comp"), c("E11.65", "elix_dm_comp", "elix_dm_uncomp"),
                c("250.00", "elix_dm_uncomp", "elix_dm_comp"), c("250.40", "elix_dm_comp", "elix_dm_uncomp"), c("I50.9", "elix_chf", "elix_copd"))
  for (cs in cases) {
    con <- DBI::dbConnect(duckdb::duckdb())
    DBI::dbExecute(con, sprintf("CREATE TABLE condition_occurrence AS SELECT 1 AS person_id, 0 AS condition_concept_id,
                                 DATE '2020-05-01' AS condition_start_date, '%s' AS condition_source_value, CAST(NULL AS BIGINT) AS visit_occurrence_id", cs[[1]]))
    DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT 1 AS subject_id, DATE '2020-06-01' AS cohort_start_date")
    df <- extract_elixhauser_comorbidities(con, source_and_standard = FALSE, code_table = "ederri", lookback_days = NULL)
    expect_equal(df[[cs[[2]]]], 1L)
    expect_equal(df[[cs[[3]]]], 0L)
    DBI::dbDisconnect(con, shutdown = TRUE)
  }
  con <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS SELECT 1 AS person_id, 0 AS condition_concept_id, DATE '2020-05-01' AS condition_start_date,
                       'G40.9' AS condition_source_value, CAST(NULL AS BIGINT) AS visit_occurrence_id")
  DBI::dbExecute(con, "CREATE TABLE cohort AS SELECT 1 AS subject_id, DATE '2020-06-01' AS cohort_start_date")
  expect_equal(extract_elixhauser_comorbidities(con, source_and_standard = FALSE)$elix_neuro_other, 1L)
  expect_error(extract_elixhauser_comorbidities(con, code_table = "icd11"), "code_table must be")
})

bp_history_db <- function() {
  con <- DBI::dbConnect(duckdb::duckdb())
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,
                       visit_end_date DATE, discharged_to_concept_id INT)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence VALUES
    (1, 1, 9201, DATE '2019-12-01', DATE '2019-12-04', 8536), (2, 1, 9203, DATE '2019-06-01', DATE '2019-06-01', 8536),
    (3, 1, 9202, DATE '2019-11-01', DATE '2019-11-01', 8536), (4, 1, 9201, DATE '2018-12-01', DATE '2018-12-05', 8536),
    (101, 1, 9201, DATE '2020-01-10', DATE '2020-01-15', 8536), (102, 2, 9201, DATE '2020-01-10', DATE '2020-01-15', 8536),
    (103, 3, 9201, DATE '2020-01-10', DATE '2020-01-15', 8536)")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id INT, condition_start_date DATE,
                       condition_source_value VARCHAR, visit_occurrence_id BIGINT)")
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (1, 11, DATE '2019-05-01', 'I50.9', NULL), (1, 12, DATE '2020-01-12', 'F17.210', NULL),
                       (1, 13, DATE '2020-02-01', 'G40.9', NULL), (2, 21, DATE '2015-05-01', 'Z87.891', NULL)")
  DBI::dbExecute(con, "CREATE TABLE observation (person_id BIGINT, observation_concept_id INT, observation_date DATE)")
  DBI::dbExecute(con, "INSERT INTO observation VALUES (3, 4052039, DATE '2019-01-01')")
  DBI::dbExecute(con, "CREATE TABLE cohort_t AS SELECT * FROM (VALUES (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0),
                       (2, DATE '2020-01-10', DATE '2020-01-15', 102, 1), (3, DATE '2020-01-10', DATE '2020-01-15', 103, 0))
                       t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)")
  con
}

test_that("extract_protocol_history_features follows the protocol's windows", {
  con <- bp_history_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  cohort <- data.frame(row_id = 0:2, subject_id = 1:3, cohort_start_date = as.Date(rep("2020-01-10", 3)), cohort_end_date = as.Date(rep("2020-01-15", 3)))
  h <- extract_protocol_history_features(con, cohort)
  expect_equal(h$prior_encounters_all_count[[1]], 3)
  expect_equal(h$prior_ip_os_ed_count[[1]], 2)
  expect_equal(c(h$days_since_prior_encounter[[1]], h$days_since_prior_ip_ed[[1]]), c(40, 40))
  expect_equal(h$readmission_count_twelve_months[[1]], 1)
  expect_equal(c(h$days_since_prior_encounter[[2]], h$prior_encounters_all_count[[2]]), c(365, 0))
  expect_equal(c(h$n_secondary_dx_ccs[[1]], h$elix_pre_index_conditions_count[[1]]), c(1, 1))
  expect_equal(h$tobacco, c("Current user", "Former user", "Never user"))
  expect_equal(extract_protocol_history_features(con, cohort, lookback_days = 30)$days_since_prior_encounter[[2]], 30)
  expect_error(extract_protocol_history_features(con, cohort, lookback_days = -1), "non-negative")
})

test_that("profile 'both' keeps the generic columns and adds the protocol's under ederri_", {
  con <- bp_history_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  kw <- list(include_medications = FALSE, include_utilization = FALSE, include_vitals = FALSE, include_labs = FALSE,
             include_disposition = FALSE, include_demographics = FALSE)
  run <- function(...) suppressWarnings(do.call(extract_benchmark_features, c(list(con, "cohort_t"), kw, list(...))))
  generic <- run(); both <- run(profile = "both"); only <- run(profile = "ederri")
  expect_false("tobacco" %in% names(generic))
  expect_true("tobacco" %in% names(both))
  elix <- grep("^elix_", names(generic), value = TRUE)
  expect_true(all(elix %in% names(both)))
  expect_true(all(paste0("ederri_", elix) %in% names(both)))
  for (cc in elix) expect_equal(both[[cc]], generic[[cc]])
  expect_false("ederri_elix_chf" %in% names(only))
  expect_true("elix_chf" %in% names(only))
  a <- run(elixhauser_code_table = "both")
  expect_true("ederri_elix_chf" %in% names(a))
  expect_false("tobacco" %in% names(a))
  expect_false("tobacco" %in% names(run(profile = "both", include_protocol_history = FALSE)))
  expect_error(run(profile = "x"), "profile must be")
  expect_error(run(medication_set = "x"), "medication_set must be")
})

test_that("the ederri profile reads all Elixhauser history up to discharge, the default uses the lookback", {
  con <- DBI::dbConnect(duckdb::duckdb()); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence AS SELECT * FROM (VALUES (1, 0, DATE '2012-03-01', 'I50.9', CAST(NULL AS BIGINT)),
                       (1, 0, DATE '2020-01-14', 'G40.9', CAST(NULL AS BIGINT))) t(person_id, condition_concept_id, condition_start_date,
                       condition_source_value, visit_occurrence_id)")
  DBI::dbExecute(con, "CREATE TABLE cohort_t AS SELECT 1 AS subject_id, DATE '2020-01-10' AS cohort_start_date, DATE '2020-01-15' AS cohort_end_date,
                       101 AS visit_occurrence_id, 0 AS outcome_flag")
  kw <- list(include_medications = FALSE, include_utilization = FALSE, include_vitals = FALSE, include_labs = FALSE,
             include_disposition = FALSE, include_demographics = FALSE, include_protocol_history = FALSE)
  run <- function(...) suppressWarnings(do.call(extract_benchmark_features, c(list(con, "cohort_t"), kw, list(...))))
  d <- run(); e <- run(profile = "ederri")
  expect_equal(c(d$elix_chf, e$elix_chf), c(0L, 1L))
  expect_equal(c(d$elix_neuro_other, e$elix_neuro_other), c(1L, 1L))
  expect_equal(run(profile = "ederri", elixhauser_lookback_days = 365)$elix_chf, 0L)
  expect_error(run(elixhauser_lookback_days = -1), "non-negative")
})

# ---- SDI / RUCA through the benchmark table: a study's own address table, deterministic ties, the no-ZIP warning ---------------
sd_db <- function() {
  con <- DBI::dbConnect(duckdb::duckdb())
  DBI::dbExecute(con, "CREATE TABLE person (person_id BIGINT, location_id BIGINT)")
  DBI::dbExecute(con, "INSERT INTO person VALUES (1, NULL), (2, NULL), (3, NULL)")
  DBI::dbExecute(con, "CREATE TABLE location (location_id BIGINT, zip VARCHAR)")
  DBI::dbExecute(con, "CREATE TABLE ederri_address_history (person_id BIGINT, location_id BIGINT, zip5 VARCHAR, address_start DATE, address_end DATE, preferred VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO ederri_address_history VALUES
    (1, 1, '17001', DATE '2015-01-01', DATE '2019-12-31', 'N'), (1, 2, '19101', DATE '2020-01-01', NULL, 'N'),
    (2, 3, '19102', DATE '2020-01-10', DATE '2020-01-20', 'N'), (2, 4, '17001', DATE '2020-01-10', DATE '2020-01-20', 'N'),
    (3, 5, NULL, DATE '2019-01-01', NULL, 'Y')")
  DBI::dbExecute(con, "CREATE TABLE cohort_t AS SELECT * FROM (VALUES (1, DATE '2020-01-10', DATE '2020-01-15', 101, 0),
    (2, DATE '2020-01-12', DATE '2020-01-15', 102, 1), (3, DATE '2020-01-10', DATE '2020-01-15', 103, 0))
    t(subject_id, cohort_start_date, cohort_end_date, visit_occurrence_id, outcome_flag)")
  con
}
sd_ruca <- data.frame(zipcode = c("19101", "19102", "17001", "99999"), ruca_code = c(1, 2, 4.2, 10))
sd_sdi <- data.frame(zcta5_fips = c("19101", "19102", "17001"), sdi_score = c(90, 50, 10))

test_that("SDI / RUCA use the study's address table, break ties deterministically and warn when no ZIP is found", {
  con <- sd_db(); on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  kw <- list(include_elixhauser = FALSE, include_medications = FALSE, include_utilization = FALSE, include_vitals = FALSE,
             include_labs = FALSE, include_disposition = FALSE, include_demographics = FALSE, include_sdoh = TRUE,
             ruca_crosswalk = sd_ruca, sdi_table = sd_sdi, sdi_cut_points = c(20, 40, 60, 80))
  df <- suppressWarnings(do.call(extract_benchmark_features, c(list(con, "cohort_t", address_table = "ederri_address_history"), kw)))
  expect_equal(df$ruca_category[df$person_id == 1], "RUCA: Metropolitan (1.0)")
  expect_equal(df$sdi_quintile[df$person_id == 1], "SDI Quintile 5 (Most Deprived)")
  expect_equal(df$sdoh_missing, c(0L, 0L, 1L))
  stays <- data.frame(person_id = 2, stay_id = 102, admit_date = as.Date("2020-01-12"))
  for (i in 1:3) expect_equal(derive_geography_features(con, sd_ruca, sd_sdi, c(20, 40, 60, 80), stays = stays,
                                                        address_table = "ederri_address_history")$zip5, "17001")
  expect_warning(w <- do.call(extract_benchmark_features, c(list(con, "cohort_t"), kw)), "address_table")
  expect_true(all(w$sdoh_missing == 1))
})
