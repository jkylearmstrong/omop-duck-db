test_that("compare_cohort_definitions reports counts, overlap and outcome rates", {
  skip_if_not_installed("SqlRender")
  cohorts <- normalizePath(file.path(testthat::test_path(), "..", "..", "cohorts"), mustWork = FALSE)
  skip_if_not(file.exists(file.path(cohorts, "target_cohort.sql")))
  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db))
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf(paste("INSERT INTO person (person_id, gender_concept_id, year_of_birth,",
      "race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1980, 0, 0)"), pid))
  }
  # p1, p2: observation covers follow-up; p3: only a later visit evidences it (counts under definition B only)
  ends <- c("2020-12-31", "2020-12-31", "2020-02-05")
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf(paste("INSERT INTO observation_period (observation_period_id, person_id,",
      "observation_period_start_date, observation_period_end_date, period_type_concept_id)",
      "VALUES (%d, %d, DATE '2015-01-01', DATE '%s', 0)"), pid, pid, ends[pid]))
  }
  visits <- data.frame(id = 1:5, pid = c(1, 2, 3, 3, 1), c = c(9201, 9201, 9201, 9202, 9201),
                       s = c("2020-02-01", "2020-02-01", "2020-02-01", "2020-04-05", "2020-02-20"),
                       e = c("2020-02-04", "2020-02-04", "2020-02-04", "2020-04-05", "2020-02-22"))
  for (i in seq_len(nrow(visits))) {
    v <- visits[i, ]
    DBI::dbExecute(con, sprintf(paste("INSERT INTO visit_occurrence (visit_occurrence_id, person_id,",
      "visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)",
      "VALUES (%d, %d, %d, DATE '%s', DATE '%s', 0)"), v$id, v$pid, v$c, v$s, v$e))
  }
  tgt <- file.path(cohorts, "target_cohort.sql")
  res <- compare_cohort_definitions(con, tgt, tgt, outcome_sql = file.path(cohorts, "outcome_cohort.sql"),
                                    params_b = list(followup_evidence = "observation_period_or_event"))
  expect_equal(res$summary$n, c(2L, 3L))
  expect_equal(res$summary$events[1], 1L)
  expect_equal(res$summary$outcome_rate[1], 0.5)
  o <- res$overlap
  expect_equal(c(o$both_count, o$a_only_count, o$b_only_count), c(2, 0, 1))
  expect_equal(o$jaccard_index, round(2 / 3, 4))
  no_outcome <- compare_cohort_definitions(con, tgt, tgt)
  expect_true(all(is.na(no_outcome$summary$events)))
})

test_that("compare_cohort_definitions and run_cohort_sql work on self-contained stub cohorts", {
  skip_if_not_installed("SqlRender")
  dir <- tempfile(); dir.create(dir)
  writeLines(c(
    "{DEFAULT @followup_evidence = 'a'}",
    "DELETE FROM @target_database_schema.@target_cohort_table WHERE cohort_definition_id = @target_cohort_id;",
    "INSERT INTO @target_database_schema.@target_cohort_table (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)",
    "SELECT @target_cohort_id, person_id, visit_start_date, visit_end_date FROM @cdm_database_schema.visit_occurrence",
    "WHERE visit_concept_id = 9201 {@followup_evidence == 'b'}?{OR visit_concept_id = 9202}:{};"),
    file.path(dir, "target.sql"))
  writeLines(c(
    "DELETE FROM @target_database_schema.@target_cohort_table WHERE cohort_definition_id = @target_cohort_id;",
    "INSERT INTO @target_database_schema.@target_cohort_table (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)",
    "SELECT @target_cohort_id, person_id, visit_start_date, visit_end_date FROM @cdm_database_schema.visit_occurrence",
    "WHERE visit_concept_id = 9203;"),
    file.path(dir, "outcome.sql"))

  db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(db))
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE, after = FALSE)
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf(paste("INSERT INTO person (person_id, gender_concept_id, year_of_birth,",
      "race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1980, 0, 0)"), pid))
  }
  # p1, p2: inpatient (A and B); p3: outpatient only (B only). p1 has an event 10 days after, p3 none.
  visits <- data.frame(id = 1:4, pid = c(1, 2, 3, 1), c = c(9201, 9201, 9202, 9203),
                       s = c("2020-02-01", "2020-02-01", "2020-02-01", "2020-02-11"))
  for (i in seq_len(nrow(visits))) {
    v <- visits[i, ]
    DBI::dbExecute(con, sprintf(paste("INSERT INTO visit_occurrence (visit_occurrence_id, person_id,",
      "visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id)",
      "VALUES (%d, %d, %d, DATE '%s', DATE '%s', 0)"), v$id, v$pid, v$c, v$s, v$s))
  }
  # run_cohort_sql also accepts SQL text, and drops parameters the file does not use
  DBI::dbExecute(con, "CREATE TEMP TABLE t (cohort_definition_id INTEGER, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE)")
  run_cohort_sql(con, readLines(file.path(dir, "outcome.sql")) |> paste(collapse = "\n"),
                 cdm_database_schema = "main", target_database_schema = "temp.main", target_cohort_table = "t",
                 target_cohort_id = 9L, unused_param = 1L)
  expect_equal(DBI::dbGetQuery(con, "SELECT COUNT(*) AS n FROM t")$n, 1)

  res <- compare_cohort_definitions(con, file.path(dir, "target.sql"), file.path(dir, "target.sql"),
                                    outcome_sql = file.path(dir, "outcome.sql"),
                                    params_b = list(followup_evidence = "b"), common_params = list(min_age = 21L))
  expect_equal(res$summary$n, c(2L, 3L))
  expect_equal(res$summary$events, c(1L, 1L))
  expect_equal(res$summary$outcome_rate, c(0.5, 1 / 3))
  expect_equal(c(res$overlap$both_count, res$overlap$a_only_count, res$overlap$b_only_count), c(2, 0, 1))
})
