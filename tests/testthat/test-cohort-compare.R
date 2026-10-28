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
