test_that("plot_consort_flow and plot_multisite_attrition_heatmap render SVG and Plotly structures", {
  attrition_data <- data.frame(
    step_number = c(1, 2, 3, 4),
    step_name = c("Initial Inpatient Visits", "Age >= 18", "365-day Lookback", "Final Study Cohort"),
    subjects_retained = c(10000, 8500, 6200, 5800),
    subjects_dropped = c(0, 1500, 2300, 400),
    exclusion_reason = c("None", "Pediatric age < 18", "Insufficient prior observation", "In-hospital mortality"),
    stringsAsFactors = FALSE
  )

  svg <- plot_consort_flow(attrition_data, format = "svg", title = "Test CONSORT Flow")
  expect_true(grepl("<svg", svg))
  expect_true(grepl("Test CONSORT Flow", svg))
  expect_true(grepl("Initial Inpatient Visits", svg))
  expect_true(grepl("Excluded", svg))

  plotly_obj <- plot_consort_flow(attrition_data, format = "plotly")
  expect_true(is.list(plotly_obj))
  expect_true("data" %in% names(plotly_obj))
  expect_equal(plotly_obj$data[[1]]$type, "sankey")

  multisite_df <- data.frame(
    site = c("Site_A", "Site_A", "Site_A", "Site_B", "Site_B", "Site_B"),
    step_name = c("Step 1: Base", "Step 2: Adult", "Step 3: Follow-up", "Step 1: Base", "Step 2: Adult", "Step 3: Follow-up"),
    percent_retained = c(100.0, 92.5, 85.0, 100.0, 65.0, 35.0),
    stringsAsFactors = FALSE
  )
  heatmap_svg <- plot_multisite_attrition_heatmap(multisite_df, title = "Consortium Retention Heatmap")
  expect_true(grepl("<svg", heatmap_svg))
  expect_true(grepl("Site_A", heatmap_svg))
  expect_true(grepl("Site_B", heatmap_svg))
  expect_true(grepl("92.5%", heatmap_svg))
})

test_that("consortium_propensity_score and match_nearest_neighbor perform caliper matching", {
  set.seed(42)
  n <- 200
  treated <- rbinom(n, 1, 0.4)
  age <- rnorm(n, 60 + 5 * treated, 10)
  charlson <- rpois(n, 2 + 1 * treated)

  df <- data.frame(
    subject_id = seq_len(n),
    treatment = treated,
    age = age,
    charlson = charlson,
    stringsAsFactors = FALSE
  )

  res <- consortium_propensity_score(
    cohort_table = df,
    exposure_col = "treatment",
    covariate_cols = c("age", "charlson"),
    method = "matching",
    caliper = 0.25,
    ratio = 1L
  )

  expect_true(res$converged)
  expect_true(nrow(res$data) > 0)
  expect_true("matched" %in% names(res$data))
  expect_true(!is.null(res$balance))
  expect_true("post_smd" %in% names(res$balance))

  love_svg <- plot_love_plot(res$balance, threshold = 0.10)
  expect_true(grepl("<svg", love_svg))
  expect_true(grepl("Threshold (0.1)", love_svg, fixed = TRUE))
  expect_true(grepl("age", love_svg))
})

test_that("generate_negative_controls and calibrate_estimates function as expected", {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE concept (
        concept_id INTEGER,
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
        (1001, 'Type 2 Diabetes Mellitus', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', '1970-01-01', '2099-12-31', NULL),
        (2001, 'Metformin', 'Drug', 'RxNorm', 'Ingredient', 'S', '6809', '1970-01-01', '2099-12-31', NULL),
        (4001, 'Inguinal Hernia', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '396232000', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE concept_ancestor (
        ancestor_concept_id INTEGER,
        descendant_concept_id INTEGER,
        min_levels_of_separation INTEGER,
        max_levels_of_separation INTEGER
    );
    INSERT INTO concept_ancestor VALUES
        (1001, 1001, 0, 0),
        (2001, 2001, 0, 0),
        (4001, 4001, 0, 0);

    CREATE TABLE concept_relationship (
        concept_id_1 INTEGER,
        concept_id_2 INTEGER,
        relationship_id VARCHAR,
        valid_start_date DATE,
        valid_end_date DATE,
        invalid_reason VARCHAR
    );
    INSERT INTO concept_relationship VALUES
        (2001, 1001, 'Has indication', '1970-01-01', '2099-12-31', NULL);

    CREATE TABLE condition_occurrence (
        condition_occurrence_id BIGINT,
        person_id BIGINT,
        condition_concept_id INTEGER,
        condition_start_date DATE,
        condition_end_date DATE,
        condition_type_concept_id INTEGER
    );
    INSERT INTO condition_occurrence VALUES
        (1, 1, 1001, '2021-12-01', '2021-12-01', 38000280),
        (2, 2, 4001, '2021-05-01', '2021-05-01', 38000280);
  ")

  neg_controls <- generate_negative_controls(con, exposure_concept_id = 2001, target_domain = "Condition")
  expect_true(nrow(neg_controls) > 0)
  expect_false(1001 %in% neg_controls$concept_id) # T2DM is an indication, excluded!
  expect_true(4001 %in% neg_controls$concept_id)  # Inguinal Hernia qualifies!

  set.seed(123)
  null_effects <- rnorm(25, 0.05, 0.15)
  null_ses <- runif(25, 0.08, 0.12)
  null_df <- data.frame(log_rr = null_effects, se_log_rr = null_ses)

  cal_res <- calibrate_estimates(null_df, observed_effect = 0.35, observed_se = 0.10)
  expect_equal(cal_res$n_negative_controls, 25)
  expect_true(cal_res$calibrated_p >= 0.0 && cal_res$calibrated_p <= 1.0)
  expect_true(cal_res$null_sd >= 0.0)
})

test_that("optimize_concept_set suggests missing related codes", {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE concept (
        concept_id INTEGER,
        concept_name VARCHAR,
        domain_id VARCHAR,
        vocabulary_id VARCHAR,
        standard_concept VARCHAR
    );
    INSERT INTO concept VALUES
        (1001, 'Type 2 Diabetes Mellitus', 'Condition', 'SNOMED', 'S'),
        (1002, 'Diabetes Mellitus with Renal Manifestation', 'Condition', 'SNOMED', 'S'),
        (1003, 'Essential Hypertension', 'Condition', 'SNOMED', 'S');

    CREATE TABLE concept_ancestor (
        ancestor_concept_id INTEGER,
        descendant_concept_id INTEGER,
        min_levels_of_separation INTEGER,
        max_levels_of_separation INTEGER
    );
    INSERT INTO concept_ancestor VALUES
        (1000, 1001, 1, 1),
        (1000, 1002, 1, 1);

    CREATE TABLE condition_occurrence (
        condition_occurrence_id BIGINT,
        person_id BIGINT,
        condition_concept_id INTEGER,
        condition_source_value VARCHAR
    );
    INSERT INTO condition_occurrence VALUES
        (1, 1, 1001, 'T2DM'),
        (2, 1, 1003, 'HTN'),
        (3, 2, 1001, 'T2DM'),
        (4, 2, 1003, 'HTN'),
        (5, 1, 0, 'ICD10:E11.9_UNMAPPED'),
        (6, 2, 0, 'ICD10:E11.9_UNMAPPED');
  ")

  suggestions <- optimize_concept_set(con, concept_ids = 1001, threshold_frequency = 1)
  expect_true(nrow(suggestions) > 0)
  sug_types <- suggestions$suggestion_type
  expect_true("sibling_hierarchy" %in% sug_types || "clinical_cooccurrence" %in% sug_types)
})

test_that("extract_temporal_trajectories calculates slope, r2, and volatility", {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE cohort (
        subject_id INTEGER,
        cohort_start_date DATE
    );
    INSERT INTO cohort VALUES
        (1, '2022-01-01'),
        (2, '2022-01-15');

    CREATE TABLE measurement (
        measurement_id BIGINT,
        person_id BIGINT,
        measurement_concept_id INTEGER,
        measurement_date DATE,
        value_as_number DOUBLE
    );
    INSERT INTO measurement VALUES
        (1, 1, 3001, '2021-06-01', 8.5),
        (2, 1, 3001, '2021-09-01', 8.0),
        (3, 1, 3001, '2021-12-01', 7.4),
        (4, 2, 3001, '2021-07-01', 9.0),
        (5, 2, 3001, '2021-11-01', 7.8);
  ")

  trajs <- extract_temporal_trajectories(
    con,
    cohort_table = "cohort",
    measurement_concept_ids = 3001,
    window_days = 365,
    id_col = "subject_id",
    index_date_col = "cohort_start_date"
  )

  expect_true(nrow(trajs) > 0)
  expect_true("subject_id" %in% names(trajs))
  expect_true("traj_3001_slope" %in% names(trajs))
  expect_true("traj_3001_r2" %in% names(trajs))
  expect_true("traj_3001_volatility" %in% names(trajs))
  expect_true("traj_3001_count" %in% names(trajs))

  p1 <- trajs[trajs$subject_id == 1, ]
  expect_equal(p1$traj_3001_count, 3L)
  expect_false(is.na(p1$traj_3001_slope))
})

test_that("consortium_survival_meta_analysis pools hazard ratios and generates KM curves", {
  site_dict <- list(
    Site_Alpha = list(log_hr = 0.25, se = 0.08, n_patients = 500L, n_events = 45L),
    Site_Beta = list(log_hr = 0.30, se = 0.10, n_patients = 400L, n_events = 35L),
    Site_Gamma = list(log_hr = 0.20, se = 0.09, n_patients = 600L, n_events = 50L)
  )

  res <- consortium_survival_meta_analysis(site_dict)
  expect_true(res$pooled_hr > 1.0)
  expect_true(res$pooled_hr_ci[1] > 0)
  expect_true(res$pooled_p < 0.05)
  expect_true(res$i_squared >= 0.0)
  expect_equal(nrow(res$site_estimates), 3L)
})

test_that("generate_synthetic_omop_twin creates a structurally valid DuckDB database", {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    CREATE TABLE person (
        person_id INTEGER,
        gender_concept_id INTEGER,
        year_of_birth INTEGER
    );
    INSERT INTO person VALUES
        (1, 8507, 1965),
        (2, 8532, 1975),
        (3, 8507, 1985);

    CREATE TABLE cohort (
        cohort_definition_id INTEGER,
        subject_id INTEGER,
        cohort_start_date DATE,
        cohort_end_date DATE
    );
    INSERT INTO cohort VALUES
        (1, 1, '2022-01-01', '2022-06-30'),
        (1, 2, '2022-02-01', '2022-07-31'),
        (1, 3, '2022-03-01', '2022-08-31');

    CREATE TABLE condition_occurrence (
        condition_occurrence_id BIGINT,
        person_id BIGINT,
        condition_concept_id INTEGER
    );
    INSERT INTO condition_occurrence VALUES (1, 1, 316866), (2, 2, 201820);
  ")

  tmp_file <- tempfile(fileext = ".duckdb")
  on.exit(unlink(tmp_file), add = TRUE)

  summary <- generate_synthetic_omop_twin(
    con,
    cohort_table = "cohort",
    output_db_path = tmp_file,
    noise_level = 0.15,
    n_patients = 20L,
    seed = 42L
  )

  expect_equal(summary$status, "success")
  expect_equal(summary$n_patients, 20L)
  expect_true(file.exists(tmp_file))

  syn_con <- DBI::dbConnect(duckdb::duckdb(), dbdir = tmp_file)
  on.exit(DBI::dbDisconnect(syn_con, shutdown = TRUE), add = TRUE)

  n_p <- DBI::dbGetQuery(syn_con, "SELECT COUNT(*) AS cnt FROM person")$cnt
  n_c <- DBI::dbGetQuery(syn_con, "SELECT COUNT(*) AS cnt FROM cohort")$cnt
  n_m <- DBI::dbGetQuery(syn_con, "SELECT COUNT(*) AS cnt FROM measurement")$cnt

  expect_equal(n_p, 20L)
  expect_equal(n_c, 20L)
  expect_true(n_m > 0)
})
