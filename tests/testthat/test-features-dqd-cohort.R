library(omopduckdb)

test_that("load_location ingests address history and links person.location_id", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)

  fixture_dir <- normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))
  omopduckdb:::.load_person(con, fixture_dir)
  load_location(con, fixture_dir)

  loc_df <- DBI::dbGetQuery(con, "SELECT location_id, city, state, zip FROM location ORDER BY location_id")
  expect_equal(nrow(loc_df), 3)
  expect_true(all(c("Philadelphia", "Pittsburgh", "Camden") %in% loc_df$city))

  linked_df <- DBI::dbGetQuery(con, "SELECT person_id, location_id FROM person WHERE location_id IS NOT NULL")
  expect_equal(nrow(linked_df), 3)
})

test_that("cohort helpers and concept hierarchy navigation work as expected", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  ensure_cohort_tables(con)

  # Mock concepts and ancestors
  DBI::dbExecute(con, "
    INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
    VALUES 
        (10, 'Type 2 Diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'T2D', DATE '2000-01-01', DATE '2099-12-31'),
        (11, 'T2D with neuropathy', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'T2DN', DATE '2000-01-01', DATE '2099-12-31'),
        (12, 'Secondary Diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'SEC_D', DATE '2000-01-01', DATE '2099-12-31');

    INSERT INTO concept_ancestor (ancestor_concept_id, descendant_concept_id, min_levels_of_separation, max_levels_of_separation)
    VALUES 
        (10, 10, 0, 0),
        (10, 11, 1, 1),
        (12, 12, 0, 0);
  ")

  # 1. get_concept_descendants
  desc <- get_concept_descendants(con, 10, include_self = TRUE)
  expect_equal(nrow(desc), 2)
  expect_equal(sort(desc$descendant_concept_id), c(10, 11))

  # 2. get_concept_ancestors
  anc <- get_concept_ancestors(con, 11, include_self = FALSE)
  expect_equal(nrow(anc), 1)
  expect_equal(anc$ancestor_concept_id[1], 10)

  # 3. resolve_concept_set
  cset <- resolve_concept_set(con, include_concepts = c(10, 12), exclude_concepts = 12)
  expect_equal(cset, c(10, 11))

  # 4. create_cohort
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
        (1001, 8507, 1980, 1, 1, 8527, 38003564),
        (1002, 8532, 1990, 5, 10, 8516, 38003564),
        (1003, 8507, 2010, 3, 15, 8527, 38003564);

    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id)
    VALUES 
        (1, 1001, 10, DATE '2020-01-15', 32020),
        (2, 1002, 11, DATE '2020-03-01', 32020),
        (3, 1003, 10, DATE '2020-06-20', 32020);
  ")

  n <- create_cohort(
    con,
    cohort_id = 1,
    cohort_name = "T2D Cohort",
    entry_sql = "SELECT person_id, condition_start_date AS start_date FROM condition_occurrence WHERE condition_concept_id IN (10, 11)",
    exit_rule = "fixed_days",
    exit_offset_days = 365
  )
  expect_equal(n, 3)

  # 5. compute_attrition
  attrition <- compute_attrition(
    con,
    cohort_id = 1,
    steps = list(
      list("Initial T2D diagnosis", "SELECT subject_id FROM cohort WHERE cohort_definition_id = 1"),
      list("Adults only (Age >= 18)", "SELECT c.subject_id FROM cohort c JOIN person p ON c.subject_id = p.person_id WHERE date_diff('year', make_date(p.year_of_birth, coalesce(p.month_of_birth, 1), coalesce(p.day_of_birth, 1)), c.cohort_start_date) >= 18"),
      list("Female patients", "SELECT c.subject_id FROM _step_current c JOIN person p ON c.subject_id = p.person_id WHERE p.gender_concept_id = 8532")
    )
  )
  expect_equal(nrow(attrition), 3)
  expect_equal(attrition$subjects_retained, c(3, 2, 1))

  # 6. combine_cohorts
  create_cohort(
    con,
    cohort_id = 2,
    cohort_name = "Single Female Cohort",
    entry_sql = "SELECT person_id, DATE '2020-03-01' AS start_date FROM person WHERE person_id = 1002",
    exit_rule = "fixed_days",
    exit_offset_days = 365
  )
  intersect_n <- combine_cohorts(con, new_cohort_id = 3, cohort_id_a = 1, cohort_id_b = 2, operation = "INTERSECT")
  expect_equal(intersect_n, 1)

  # 7. get_cohort_summary
  summary <- get_cohort_summary(con, cohort_id = 1)
  expect_equal(summary$total_subjects[1], 3)
  expect_equal(summary$mean_duration_days[1], 365)
})

test_that("native DQD engine executes checks and writes standard output", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)

  fixture_dir <- normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))
  omopduckdb:::.load_person(con, fixture_dir)

  tmp_json <- tempfile(fileext = ".json")
  on.exit(unlink(tmp_json), add = TRUE)

  res <- run_dqd(con, output_json = tmp_json, check_levels = c("TABLE", "FIELD", "TEMPORAL"))
  expect_true(!is.null(res$Metadata))
  expect_true(!is.null(res$Overview))
  expect_true(res$Overview$count_total > 0)
  expect_true(file.exists(tmp_json))

  # Test temporal violation detection
  DBI::dbExecute(con, "
    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)
    VALUES (999, 1, 9201, DATE '2022-05-10', DATE '2022-05-01', 32827);
  ")
  res_violation <- run_dqd(con, check_levels = "TEMPORAL")
  failed_checks <- Filter(function(r) isTRUE(r$failed), res_violation$CheckResults)
  expect_true(any(grepl("TEMPORAL_START_BEFORE_END_VISIT_OCCURRENCE", sapply(failed_checks, `[[`, "check_id"))))
})

test_that("extract_patient_features produces ML-ready matrices", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
        (501, 8507, 1975, 6, 15, 8527, 38003564),
        (502, 8532, 1985, 10, 20, 8516, 38003564);

    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id)
    VALUES 
        (1, 501, 201826, DATE '2021-05-01', 32020),
        (2, 501, 201826, DATE '2021-05-20', 32020);

    INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id)
    VALUES (1, 501, 1503297, DATE '2021-05-15', DATE '2021-06-15', 32838);
  ")

  ensure_cohort_tables(con)
  create_cohort(
    con,
    cohort_id = 10,
    cohort_name = "Index Cohort",
    entry_sql = "SELECT person_id, DATE '2021-06-01' AS start_date FROM person",
    exit_rule = "fixed_days",
    exit_offset_days = 180
  )
  create_cohort(
    con,
    cohort_id = 20,
    cohort_name = "Outcome Cohort",
    entry_sql = "SELECT 501 AS person_id, DATE '2021-07-01' AS start_date",
    exit_rule = "fixed_days",
    exit_offset_days = 30
  )

  # Dense format
  df_dense <- extract_patient_features(
    con,
    cohort_id = 10,
    outcome_cohort_id = 20,
    lookback_days = c(30, 90, 365),
    format = "df"
  )
  expect_equal(nrow(df_dense), 2)
  expect_true(all(c("y", "age_at_index", "condition_count_30d", "drug_count_30d") %in% names(df_dense)))

  p501 <- df_dense[df_dense$subject_id == 501, ]
  expect_equal(p501$y[1], 1)
  expect_equal(p501$condition_count_30d[1], 1)
  expect_equal(p501$condition_count_90d[1], 2)
  expect_equal(p501$drug_count_30d[1], 1)

  # Sparse format
  sparse_df <- extract_patient_features(con, cohort_id = 10, format = "sparse")
  expect_true(all(c("row_id", "covariate_id", "covariate_value") %in% names(sparse_df)))
})

test_that("export_cdm exports to Parquet, CSV, and Oracle formats", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)

  fixture_dir <- normalizePath(file.path(testthat::test_path(), "..", "fixtures", "pcornet_sample"))
  omopduckdb:::.load_person(con, fixture_dir)

  tmp_dir <- tempfile("export_test_")
  dir.create(tmp_dir)
  on.exit(unlink(tmp_dir, recursive = TRUE), add = TRUE)

  # Parquet
  pq_dir <- file.path(tmp_dir, "parquet")
  res_pq <- export_cdm(con, target_type = "parquet", output_path = pq_dir)
  expect_equal(res_pq$target_type, "parquet")
  expect_true(file.exists(file.path(pq_dir, "person.parquet")))

  # CSV
  csv_dir <- file.path(tmp_dir, "csv")
  res_csv <- export_cdm(con, target_type = "csv", output_path = csv_dir)
  expect_true(file.exists(file.path(csv_dir, "person.csv")))

  # Oracle
  ora_dir <- file.path(tmp_dir, "oracle")
  res_ora <- export_cdm(con, target_type = "oracle", output_path = ora_dir)
  expect_true(file.exists(file.path(ora_dir, "person.csv")))
  expect_true(file.exists(file.path(ora_dir, "person.ctl")))
  expect_true(file.exists(file.path(ora_dir, "omop_oracle_ddl.sql")))
  expect_true(file.exists(file.path(ora_dir, "load_oracle.sh")))
})
