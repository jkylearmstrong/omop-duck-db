library(omopduckdb)

test_that("build_end_of_life_cohort handles in-hospital mortality and dual death sources", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)
  ensure_cohort_tables(con)

  # Patients:
  # 101: Adult, died in hospital via discharged_to_concept_id = 4216643
  # 102: Adult, died in hospital via death table during stay
  # 103: Adult, survived stay (discharged home 8536)
  # 104: Pediatric, died in hospital (should be excluded due to min_age=18)
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
        (101, 8507, 1970, 1, 1, 8527, 38003564),
        (102, 8532, 1965, 5, 10, 8516, 38003564),
        (103, 8507, 1980, 3, 15, 8527, 38003564),
        (104, 8532, 2015, 6, 20, 8527, 38003564);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (1001, 101, 9201, DATE '2021-05-01', DATE '2021-05-05', 4216643, 32827),
        (2001, 102, 9201, DATE '2021-05-01', DATE '2021-05-06', 8536, 32827),
        (3001, 103, 9201, DATE '2021-05-01', DATE '2021-05-04', 8536, 32827),
        (4001, 104, 9201, DATE '2021-05-01', DATE '2021-05-03', 4216643, 32827);

    INSERT INTO death (person_id, death_date, death_type_concept_id)
    VALUES (102, DATE '2021-05-06', 32815);
  ")

  df <- build_end_of_life_cohort(
    con,
    cohort_id = 1,
    outcome_cohort_id = 2,
    mortality_type = "in_hospital",
    washin_days = 0
  )

  expect_setequal(df$subject_id, c(101, 102, 103))

  p101 <- df[df$subject_id == 101, ]
  expect_equal(p101$outcome_flag, 1)
  expect_equal(as.character(p101$outcome_date), "2021-05-05")

  p102 <- df[df$subject_id == 102, ]
  expect_equal(p102$outcome_flag, 1)
  expect_equal(as.character(p102$outcome_date), "2021-05-06")

  p103 <- df[df$subject_id == 103, ]
  expect_equal(p103$outcome_flag, 0)
  expect_true(is.na(p103$outcome_date))

  # Check outcome cohort table population
  c_out <- DBI::dbGetQuery(con, "SELECT subject_id FROM cohort WHERE cohort_definition_id = 2 ORDER BY subject_id;")$subject_id
  expect_equal(c_out, c(101, 102))
})

test_that("build_end_of_life_cohort handles post-discharge mortality and right-censoring", {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  on.exit({
    DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, add = TRUE)

  omopduckdb:::load_mapping_macros(con)
  ensure_cohort_tables(con)

  # Patients:
  # 201: Adult, discharged alive day 5, dies at day 20 (within 30d) -> outcome_flag = 1
  # 202: Adult, discharged alive day 5, confirmed OP encounter at day 40, dies at day 50 -> outcome_flag = 0
  # 203: Adult, discharged alive day 5, lost to follow-up (no records >= 30d) -> dropped by censoring
  # 204: Adult, dies in hospital day 5 -> excluded from post-discharge index stays
  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES 
        (201, 8507, 1975, 1, 1, 8527, 38003564),
        (202, 8532, 1980, 2, 2, 8516, 38003564),
        (203, 8507, 1985, 3, 3, 8527, 38003564),
        (204, 8532, 1970, 4, 4, 8516, 38003564);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (2001, 201, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (2002, 202, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (2003, 202, 9202, DATE '2021-06-15', DATE '2021-06-15', 8536, 32827),
        (2004, 203, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (2005, 204, 9201, DATE '2021-05-01', DATE '2021-05-05', 4216643, 32827);

    INSERT INTO death (person_id, death_date, death_type_concept_id)
    VALUES 
        (201, DATE '2021-05-20', 32815),
        (202, DATE '2021-06-25', 32815);
  ")

  df <- build_end_of_life_cohort(
    con,
    cohort_id = 1,
    outcome_cohort_id = 2,
    mortality_type = "post_discharge",
    mortality_window_days = 30,
    washin_days = 0,
    require_verified_followup = TRUE
  )

  # 204 excluded (died in hospital); 203 dropped by right-censoring
  expect_setequal(df$subject_id, c(201, 202))

  p201 <- df[df$subject_id == 201, ]
  expect_equal(p201$outcome_flag, 1)
  expect_equal(as.character(p201$outcome_date), "2021-05-20")

  p202 <- df[df$subject_id == 202, ]
  expect_equal(p202$outcome_flag, 0)
})

test_that("build_end_of_life_cohort handles fixed-window EOL with gap_days (SARD style)", {
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
        (301, 8507, 1960, 1, 1, 8527, 38003564),
        (302, 8532, 1962, 2, 2, 8516, 38003564),
        (303, 8507, 1964, 3, 3, 8527, 38003564);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (3001, 301, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827),
        (3002, 302, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827),
        (3003, 303, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827);

    INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
    VALUES 
        (1, 301, DATE '2020-01-01', DATE '2021-12-31', 32817),
        (2, 302, DATE '2020-01-01', DATE '2021-12-31', 32817),
        (3, 303, DATE '2020-01-01', DATE '2021-12-31', 32817);

    INSERT INTO death (person_id, death_date, death_type_concept_id)
    VALUES 
        (301, DATE '2021-01-16', 32815),
        (302, DATE '2021-04-01', 32815);
  ")

  df <- build_end_of_life_cohort(
    con,
    mortality_type = "fixed_window",
    mortality_window_days = 180,
    gap_days = 30,
    washin_days = 0
  )

  expect_setequal(df$subject_id, c(301, 302, 303))

  p301 <- df[df$subject_id == 301, ]
  expect_equal(p301$outcome_flag, 0) # died during gap_days

  p302 <- df[df$subject_id == 302, ]
  expect_equal(p302$outcome_flag, 1)
  expect_equal(as.character(p302$outcome_date), "2021-04-01")

  p303 <- df[df$subject_id == 303, ]
  expect_equal(p303$outcome_flag, 0)
})

test_that("build_end_of_life_cohort handles composite readmission or death", {
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
        (401, 8507, 1970, 1, 1, 8527, 38003564),
        (402, 8532, 1972, 2, 2, 8516, 38003564),
        (403, 8507, 1974, 3, 3, 8527, 38003564),
        (404, 8532, 1976, 4, 4, 8516, 38003564);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (4001, 401, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (4002, 401, 9201, DATE '2021-05-15', DATE '2021-05-18', 8536, 32827),
        (4003, 402, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (4004, 403, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (4005, 403, 9201, DATE '2021-05-15', DATE '2021-05-18', 8536, 32827),
        (4006, 404, 9201, DATE '2021-05-01', DATE '2021-05-05', 8536, 32827),
        (4007, 404, 9202, DATE '2021-06-20', DATE '2021-06-20', 8536, 32827);

    INSERT INTO death (person_id, death_date, death_type_concept_id)
    VALUES 
        (402, DATE '2021-05-17', 32815),
        (403, DATE '2021-05-25', 32815);
  ")

  df <- build_end_of_life_cohort(
    con,
    mortality_type = "composite_readmit_or_death",
    mortality_window_days = 30,
    washin_days = 0
  )

  expect_setequal(df$subject_id, c(401, 402, 403, 404))

  p401 <- df[df$subject_id == 401, ]
  expect_equal(p401$outcome_flag, 1)
  expect_equal(as.character(p401$outcome_date), "2021-05-15")

  p402 <- df[df$subject_id == 402, ]
  expect_equal(p402$outcome_flag, 1)
  expect_equal(as.character(p402$outcome_date), "2021-05-17")

  p403 <- df[df$subject_id == 403, ]
  expect_equal(p403$outcome_flag, 1)
  expect_equal(as.character(p403$outcome_date), "2021-05-15")

  p404 <- df[df$subject_id == 404, ]
  expect_equal(p404$outcome_flag, 0)
})

test_that("build_mortality_cohort alias matches and index selection rules work", {
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
    VALUES (501, 8507, 1970, 1, 1, 8527, 38003564);

    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, discharged_to_concept_id, visit_type_concept_id)
    VALUES 
        (5001, 501, 9201, DATE '2021-01-01', DATE '2021-01-05', 8536, 32827),
        (5002, 501, 9201, DATE '2021-06-01', DATE '2021-06-05', 8536, 32827);

    INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
    VALUES (1, 501, DATE '2020-01-01', DATE '2022-01-01', 32817);
  ")

  df_eol <- build_end_of_life_cohort(con, mortality_type = "in_hospital", index_selection_rule = "first", washin_days = 0)
  df_mort <- build_mortality_cohort(con, mortality_type = "in_hospital", index_selection_rule = "first", washin_days = 0)

  expect_equal(nrow(df_eol), 1)
  expect_equal(df_eol$visit_occurrence_id, 5001)
  expect_equal(df_eol, df_mort)

  df_last <- build_end_of_life_cohort(con, mortality_type = "in_hospital", index_selection_rule = "last", washin_days = 0)
  expect_equal(df_last$visit_occurrence_id, 5002)
})
