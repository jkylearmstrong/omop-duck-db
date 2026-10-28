tensor_fixture <- function() {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  DBI::dbExecute(con, paste("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id,",
                            "ethnicity_concept_id) VALUES (1, 8507, 1960, 0, 0)"))
  # cohort anchored at ADMISSION: the index stay (visit 30) starts on cohort_start_date
  DBI::dbExecute(con, paste("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)",
                            "VALUES (1, 1, DATE '2021-12-28', DATE '2022-01-01')"))
  for (v in list(c(20, "2021-11-01", "2021-11-04"), c(30, "2021-12-28", "2022-01-01"))) {
    DBI::dbExecute(con, sprintf(paste("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id,",
      "visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%s, 1, 9201, DATE '%s', DATE '%s', 0)"),
      v[1], v[2], v[3]))
  }
  DBI::dbExecute(con, paste("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, visit_occurrence_id,",
    "condition_concept_id, condition_start_date, condition_type_concept_id) VALUES",
    "(1, 1, 20, 201820, DATE '2021-11-01', 0), (2, 1, 30, 316866, DATE '2021-12-28', 0)"))
  # same lab concept above and below the reference range during the index stay: two different tokens
  DBI::dbExecute(con, paste("INSERT INTO measurement (measurement_id, person_id, visit_occurrence_id,",
    "measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, range_low, range_high)",
    "VALUES (1, 1, 30, 3004249, DATE '2021-12-29', 0, 9.0, 1.0, 5.0),",
    "(2, 1, 30, 3004249, DATE '2021-12-30', 0, 0.2, 1.0, 5.0)"))
  list(con = con, db = db)
}

test_that("extract_longitudinal_tensors has the documented shape and ordering", {
  fx <- tensor_fixture()
  on.exit({ DBI::dbDisconnect(fx$con, shutdown = TRUE); unlink(fx$db) })
  base <- extract_longitudinal_tensors(fx$con, "cohort")
  expect_equal(dim(base), c(1L, MAX_LONGITUDINAL_VISITS, MAX_TOKENS_PER_VISIT))
  expect_true(base[1, 1, 1] > 0)
  expect_true(all(base[1, 2, ] == 0))          # admission-anchored: index stay dropped
  DBI::dbExecute(fx$con, "DELETE FROM cohort")
  expect_equal(dim(extract_longitudinal_tensors(fx$con, "cohort")), c(0L, 20L, 30L))
  expect_error(extract_longitudinal_tensors(fx$con, "cohort; DROP TABLE cohort"), "Invalid cohort_table")
})

test_that("include_index_stay and include_labs add the index stay and distinct lab tokens", {
  fx <- tensor_fixture()
  on.exit({ DBI::dbDisconnect(fx$con, shutdown = TRUE); unlink(fx$db) })
  base <- extract_longitudinal_tensors(fx$con, "cohort")
  idx <- extract_longitudinal_tensors(fx$con, "cohort", include_index_stay = TRUE)
  expect_true(idx[1, 1, 1] > 0 && idx[1, 2, 1] > 0)    # slot 1 = index stay, slot 2 = earlier visit
  expect_equal(idx[1, 2, 1], base[1, 1, 1])
  expect_false(idx[1, 1, 1] == idx[1, 2, 1])
  no_labs <- sum(idx[1, 1, ] > 0)
  labs <- extract_longitudinal_tensors(fx$con, "cohort", include_index_stay = TRUE, include_labs = TRUE)
  expect_equal(sum(labs[1, 1, ] > 0), no_labs + 2)     # above- and below-range are distinct tokens
  expect_identical(extract_longitudinal_tensors(fx$con, "cohort"), base)   # defaults unchanged
  # discharge-anchored cohort: the index stay started earlier, so the default window already has it
  DBI::dbExecute(fx$con, "UPDATE cohort SET cohort_start_date = DATE '2022-01-01'")
  expect_true(any(extract_longitudinal_tensors(fx$con, "cohort")[1, 2, ] > 0))
})
