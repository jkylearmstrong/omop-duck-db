test_that("Phenotype bundles are correctly exposed", {
  phenos <- list_available_phenotypes()
  expect_true("heart_failure" %in% phenos)
  expect_true("type_2_diabetes" %in% phenos)
  expect_true("sepsis" %in% phenos)
  expect_true("acute_kidney_injury" %in% phenos)

  hf <- get_phenotype_concept_set("heart_failure")
  expect_true(316139 %in% hf$standard_concept_ids)
  expect_true("I50" %in% hf$icd10_prefixes)
  expect_equal(hf$domain_id, "Condition")

  expect_error(get_phenotype_concept_set("non_existent_phenotype"))
})

test_that("Bedside clinical risk scores compute correctly", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  }, add = TRUE)
  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
      (1, 8507, 1950, 1, 1, 8527, 38003564),
      (2, 8532, 1940, 5, 1, 8527, 38003564),
      (3, 8507, 1980, 10, 1, 8527, 38003564);
  ")

  DBI::dbExecute(con, "
    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
    VALUES 
      (101, 1, 9201, DATE '2020-05-01', DATE '2020-05-05', 32817),
      (102, 2, 9201, DATE '2020-06-01', DATE '2020-06-10', 32817),
      (103, 3, 9201, DATE '2020-07-01', DATE '2020-07-02', 32817);
  ")

  DBI::dbExecute(con, "
    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT 1, person_id, visit_start_date, visit_end_date FROM visit_occurrence;
  ")

  DBI::dbExecute(con, "
    INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
    VALUES 
      (316139, 316139, 0, 0),
      (316866, 316866, 0, 0),
      (201826, 201826, 0, 0),
      (443454, 443454, 0, 0);
  ")

  DBI::dbExecute(con, "
    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value)
    VALUES 
      (1, 1, 316139, DATE '2020-01-15', 32817, 'I50.9'),
      (2, 2, 316866, DATE '2020-02-20', 32817, 'I10'),
      (3, 2, 201826, DATE '2020-03-10', 32817, 'E11.9'),
      (4, 2, 443454, DATE '2020-04-01', 32817, 'I63.9');
  ")

  scores_df <- suppressWarnings(calculate_bedside_scores(con, cohort_table = "cohort"))
  expect_equal(nrow(scores_df), 3)
  expect_true("lace_score" %in% names(scores_df))
  expect_true("hospital_score" %in% names(scores_df))
  expect_true("chads_vasc_score" %in% names(scores_df))
  expect_true("sofa_score" %in% names(scores_df))

  p2 <- scores_df[scores_df$person_id == 2, ]
  expect_true(p2$chads_vasc_score >= 6)
})

test_that("Core lab panel harmonizer extracts and winsorizes values", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  }, add = TRUE)
  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES (1, 8507, 1970, 8527, 38003564), (2, 8507, 1980, 8527, 38003564);
  ")

  DBI::dbExecute(con, "
    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    VALUES 
      (1, 1, DATE '2020-01-01', DATE '2020-01-10'),
      (1, 2, DATE '2020-01-01', DATE '2020-01-10');
  ")

  DBI::dbExecute(con, "
    INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, measurement_source_value)
    VALUES 
      (1, 1, 3016723, DATE '2020-01-02', 32817, 1.2, '2160-0'),
      (2, 2, 3016723, DATE '2020-01-02', 32817, 999.0, '2160-0');
  ")

  labs_df <- extract_standard_labs(con, cohort_table = "cohort", winsorize = TRUE)
  expect_equal(nrow(labs_df), 2)
  expect_true("creatinine" %in% names(labs_df))

  p2_creat <- labs_df$creatinine[labs_df$subject_id == 2]
  expect_true(p2_creat <= 30.0)
})

test_that("CONSORT attrition generation and exporters work", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  }, add = TRUE)
  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
      (1, 8507, 1950, 8527, 38003564),
      (2, 8507, 1980, 8527, 38003564),
      (3, 8507, 2010, 8527, 38003564);
  ")

  steps <- list(
    list(name = "All Patients", query = "SELECT person_id AS subject_id FROM person"),
    list(name = "Adults >= 18", query = "SELECT person_id AS subject_id FROM person WHERE year_of_birth <= 2005")
  )

  attrition <- generate_consort_attrition(con, steps = steps)
  df <- as.data.frame(attrition)
  expect_equal(nrow(df), 2)
  expect_equal(df$subjects_retained[1], 3)
  expect_equal(df$subjects_retained[2], 2)

  mermaid <- to_mermaid(attrition)
  expect_true(grepl("flowchart TD", mermaid))
  expect_true(grepl("All Patients", mermaid))

  latex <- to_latex(attrition)
  expect_true(grepl("\\\\begin\\{tabular\\}", latex))

  md <- to_markdown(attrition)
  expect_true(grepl("step_name", md))
  expect_true(grepl("All Patients", md))
})

test_that("Treatment episodes collapse longitudinal exposures", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  }, add = TRUE)
  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES (1, 8507, 1970, 8527, 38003564);
  ")

  DBI::dbExecute(con, "
    INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
    VALUES 
      (1, 1, 1125315, DATE '2020-01-01', DATE '2020-01-30', 32817),
      (2, 1, 1125315, DATE '2020-02-10', DATE '2020-03-10', 32817),
      (3, 1, 1125315, DATE '2020-06-01', DATE '2020-06-30', 32817);
  ")

  episodes <- build_treatment_episodes(con, max_gap_days = 30)
  expect_equal(nrow(episodes), 2)
  expect_equal(episodes$episode_number, c(1L, 2L))
  expect_equal(episodes$exposure_count[1], 2L)
})

test_that("Table 1 export formatting works across formats", {
  t1_obj <- list(
    table1 = data.frame(
      Variable = c("N", "Age, mean (SD)", "Female, n (%)"),
      Overall = c("100", "65.2 (12.1)", "45 (45.0%)"),
      Strata_0 = c("60", "63.1 (11.0)", "25 (41.7%)"),
      Strata_1 = c("40", "68.3 (13.0)", "20 (50.0%)"),
      SMD = c("-", "0.43", "0.17"),
      p_value = c("-", "0.035", "0.412"),
      stringsAsFactors = FALSE
    )
  )

  md <- export_table1(t1_obj, format = "markdown")
  expect_true(grepl("\\| Variable", md))

  csv_out <- export_table1(t1_obj, format = "csv")
  expect_true(grepl("Variable", csv_out))
  expect_true(grepl("Overall", csv_out))

  latex <- export_table1(t1_obj, format = "latex")
  expect_true(grepl("\\\\begin\\{table\\}", latex))

  quarto <- export_table1(t1_obj, format = "quarto")
  expect_true(grepl("Variable", quarto))
})

test_that("Cell suppression and cross-database discrepancy check work", {
  db1 <- tempfile(fileext = ".duckdb")
  db2 <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db1)
    unlink(db2)
    gc()
  })

  build_schema(db_path = db1)
  c1 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db1)
  DBI::dbExecute(c1, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES (1, 1, 316139, DATE '2020-01-01', 32817);")
  DBI::dbDisconnect(c1, shutdown = TRUE)

  build_schema(db_path = db2)
  c2 <- DBI::dbConnect(duckdb::duckdb(), dbdir = db2)
  DBI::dbExecute(c2, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES (2, 2, 316139, DATE '2020-01-01', 32817);")
  DBI::dbDisconnect(c2, shutdown = TRUE)

  con_fed <- create_federated_consortium(list("Site A" = db1, "Site B" = db2))
  on.exit(DBI::dbDisconnect(con_fed, shutdown = TRUE), add = TRUE)

  disc <- check_cross_database_discrepancy(con_fed, table_name = "condition_occurrence")
  expect_true(disc$evaluated_concepts >= 1)

  DBI::dbExecute(con_fed, "CREATE TABLE agg_counts AS SELECT 316139 AS concept_id, 3 AS n_patients;")
  safe_view <- with_cell_suppression(con_fed, "agg_counts", min_cell_size = 5)
  res <- DBI::dbGetQuery(con_fed, sprintf("SELECT * FROM %s", safe_view))
  expect_equal(res$n_patients[1], "<10")
})

test_that("Native CIRCE compiler compiles and executes cohort JSON", {
  db_path <- tempfile(fileext = ".duckdb")
  on.exit({
    unlink(db_path)
    gc()
  }, add = TRUE)
  build_schema(db_path = db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES (1, 8507, 1970, 8527, 38003564);
  ")

  DBI::dbExecute(con, "
    INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
    VALUES (316139, 316139, 0, 0);
  ")

  DBI::dbExecute(con, "
    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id)
    VALUES (1, 1, 316139, DATE '2020-01-01', 32817);
  ")

  circe_json <- '{
    "ConceptSets": [
      {
        "id": 0,
        "name": "Heart Failure",
        "expression": {
          "items": [
            {"concept": {"CONCEPT_ID": 316139}, "includeDescendants": true, "isExcluded": false}
          ]
        }
      }
    ],
    "PrimaryCriteria": {
      "CriteriaList": [
        {
          "ConditionOccurrence": {
            "CodesetId": 0
          }
        }
      ],
      "ObservationWindow": {"PriorDays": 0, "PostDays": 0},
      "PrimaryCriteriaLimit": {"Type": "All"}
    }
  }'

  sql <- compile_circe_to_duckdb(circe_json, target_cohort_id = 10)
  expect_true(grepl("WITH _cs_resolved AS", sql))
  expect_true(grepl("10 AS cohort_definition_id", sql))

  cohort_res <- execute_circe_cohort(con, circe_json, target_cohort_id = 10)
  expect_equal(nrow(cohort_res), 1)
  expect_equal(cohort_res$cohort_definition_id[1], 10)
})
