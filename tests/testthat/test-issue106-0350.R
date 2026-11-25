# 0.35.0 (issue 106): data-quality and clinical-fidelity follow-ups of runs C10 to C17. Synthetic data only.

.i106_drug_vocab <- function(con) {
  DBI::dbExecute(con, "CREATE TABLE concept AS SELECT * FROM (VALUES
    (21600001, 'ACE inhibitors, plain', 'ATC', 'ATC 4th', 'C', 'C09AA', NULL),
    (21600002, 'Thiazides, plain', 'ATC', 'ATC 4th', 'C', 'C03AA', NULL),
    (1308216, 'lisinopril', 'RxNorm', 'Ingredient', 'S', '29046', NULL),
    (974166, 'hydrochlorothiazide', 'RxNorm', 'Ingredient', 'S', '5487', NULL),
    (1308217, 'lisinopril 10 MG Oral Tablet', 'RxNorm', 'Clinical Drug', 'S', '314076', NULL),
    (40000010, 'old lisinopril / HCTZ tablet', 'RxNorm', 'Clinical Drug', NULL, '1', 'D'),
    (40000011, 'Zestoretic', 'RxNorm', 'Brand Name', NULL, '2', NULL),
    (40000012, 'old plain lisinopril product', 'RxNorm', 'Branded Drug', NULL, '3', 'D'))
    t(concept_id, concept_name, vocabulary_id, concept_class_id, standard_concept, concept_code, invalid_reason)")
  DBI::dbExecute(con, "CREATE TABLE concept_ancestor AS SELECT * FROM (VALUES
    (21600001, 1308216), (21600001, 1308217), (21600002, 974166), (1308216, 1308216), (1308216, 1308217), (974166, 974166))
    t(ancestor_concept_id, descendant_concept_id)")
  DBI::dbExecute(con, "CREATE TABLE concept_relationship AS SELECT * FROM (VALUES
    (40000010, 1308216, 'RxNorm has ing', 'D'), (40000010, 974166, 'RxNorm has ing', 'D'),
    (40000011, 1308216, 'Brand name of', NULL), (40000011, 974166, 'Brand name of', NULL),
    (40000012, 1308217, 'Tradename of', 'D'))
    t(concept_id_1, concept_id_2, relationship_id, invalid_reason)")
}

test_that("combination products count for every class of their ingredients", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  .i106_drug_vocab(con)
  DBI::dbExecute(con, "CREATE TABLE coh AS SELECT * FROM (VALUES
    (1, DATE '2020-06-01', DATE '2020-06-05'), (2, DATE '2020-06-01', DATE '2020-06-05'), (3, DATE '2020-06-01', DATE '2020-06-05'),
    (4, DATE '2020-06-01', DATE '2020-06-05')) t(subject_id, cohort_start_date, cohort_end_date)")
  DBI::dbExecute(con, "CREATE TABLE drug_exposure AS SELECT * FROM (VALUES
    (1, 1, 40000010, DATE '2020-03-01', 0), (2, 2, 40000011, DATE '2020-03-01', 0),
    (3, 3, 40000012, DATE '2020-03-01', 0), (4, 4, 1308217, DATE '2020-03-01', 0))
    t(drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_type_concept_id)")
  specs <- list(ace = 21600001L, thiazide = 21600002L)
  got <- extract_medication_classes(con, "coh", specs, strategy = "standard", person_col = "subject_id", window = "pre_admission")
  expect_equal(got$ace, c(1L, 1L, 1L, 1L))
  expect_equal(got$thiazide, c(1L, 1L, 0L, 0L))
  expect_equal(attr(got, "combination_flags"), 5L)
  off <- extract_medication_classes(con, "coh", specs, strategy = "standard", person_col = "subject_id", window = "pre_admission",
                                    combination_ingredients = FALSE)
  expect_equal(off$ace, c(0L, 0L, 0L, 1L))
  expect_equal(off$thiazide, c(0L, 0L, 0L, 0L))
})

test_that("UCUM unit conversions run before the bounds", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE measurement (measurement_id BIGINT, person_id BIGINT, measurement_concept_id INT, measurement_date DATE,
                       measurement_datetime TIMESTAMP, value_as_number DOUBLE, unit_concept_id INT, unit_source_value VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO measurement VALUES
    (1, 1, 3004501, DATE '2020-06-01', NULL, 5.55, 8753, 'mmol/L'),
    (2, 1, 3004501, DATE '2020-06-02', NULL, 120, 8840, 'mg/dL'),
    (3, 1, 3016723, DATE '2020-06-01', NULL, 88.42, 0, 'umol/L'),
    (4, 1, 3000963, DATE '2020-06-01', NULL, 120, NULL, 'g/L')")
  cohort <- data.frame(row_id = 0L, subject_id = 1L, cohort_start_date = as.Date("2020-06-01"), cohort_end_date = as.Date("2020-06-03"))
  cmap <- data.frame(name = c("glucose", "creatinine", "hemoglobin"), concept_id = c(3004501, 3016723, 3000963))
  b <- list(glucose = c(10, 1500), creatinine = c(0.1, 30), hemoglobin = c(1, 25))
  st <- .bf_measurement_stats(con, cohort, cmap, 0, b, NULL, DEFAULT_UNIT_CONVERSIONS)
  expect_equal(st$mean[st$name == "glucose"], (5.55 * 18.016 + 120) / 2, tolerance = 1e-6)
  expect_equal(as.integer(st$n_converted[st$name == "glucose"]), 1L)
  expect_equal(st$mean[st$name == "creatinine"], 1, tolerance = 1e-6)
  expect_equal(st$mean[st$name == "hemoglobin"], 12, tolerance = 1e-6)
  off <- .bf_measurement_stats(con, cohort, cmap, 0, b, NULL, NULL)
  expect_false("hemoglobin" %in% off$name)
})

test_that("merge_episodes_days joins a stay that starts on the discharge day", {
  con <- DBI::dbConnect(duckdb::duckdb(), ":memory:")
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE person (person_id BIGINT, gender_concept_id INT, year_of_birth INT, month_of_birth INT, day_of_birth INT,
                       birth_datetime TIMESTAMP, race_concept_id INT, ethnicity_concept_id INT)")
  DBI::dbExecute(con, "INSERT INTO person VALUES (1, 8507, 1950, 1, 1, NULL, 0, 0), (2, 8532, 1950, 1, 1, NULL, 0, 0)")
  DBI::dbExecute(con, "CREATE TABLE observation_period (observation_period_id BIGINT, person_id BIGINT, observation_period_start_date DATE,
                       observation_period_end_date DATE, period_type_concept_id INT)")
  DBI::dbExecute(con, "INSERT INTO observation_period VALUES (1, 1, DATE '2015-01-01', DATE '2023-01-01', 0), (2, 2, DATE '2015-01-01', DATE '2023-01-01', 0)")
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INT, visit_start_date DATE,
                       visit_end_date DATE, discharged_to_concept_id INT, discharged_to_source_value VARCHAR)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence VALUES
    (10, 1, 9201, DATE '2020-01-01', DATE '2020-01-05', 0, 'A'), (11, 1, 9201, DATE '2020-01-05', DATE '2020-01-09', 8536, 'HO'),
    (12, 1, 9202, DATE '2020-03-01', DATE '2020-03-01', 0, NULL), (20, 2, 9201, DATE '2020-01-01', DATE '2020-01-05', 0, 'A'),
    (21, 2, 9201, DATE '2020-01-20', DATE '2020-01-25', 0, 'A'), (22, 2, 9202, DATE '2020-03-01', DATE '2020-03-01', 0, NULL)")
  DBI::dbExecute(con, "CREATE TABLE death (person_id BIGINT, death_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id BIGINT, condition_start_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE drug_exposure (person_id BIGINT, drug_exposure_start_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE measurement (person_id BIGINT, measurement_date DATE)")
  base <- define_study_cohort(con, materialise = FALSE, sample_stay_rule = "all", washin_days = 0,
                              min_admit_date = "2019-01-01", max_admit_date = "2020-12-31")
  expect_equal(sort(as.numeric(base$visit_occurrence_id)), c(10, 11, 20, 21))
  merged <- define_study_cohort(con, materialise = FALSE, sample_stay_rule = "all", washin_days = 0,
                                min_admit_date = "2019-01-01", max_admit_date = "2020-12-31", merge_episodes_days = 0)
  expect_equal(sort(as.numeric(merged$visit_occurrence_id)), c(10, 20, 21))
  expect_equal(as.character(as.Date(merged$cohort_end_date[merged$visit_occurrence_id == 10])), "2020-01-09")
  expect_equal(attr(merged, "definition")$merge_episodes_days, 0)
})
