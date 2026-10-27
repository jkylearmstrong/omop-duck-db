cohorts_dir <- function() {
  d <- testthat::test_path("..", "..", "cohorts")
  if (!file.exists(file.path(d, "target_cohort.sql"))) testthat::skip("cohorts/ is not part of the installed package")
  d
}

test_that("cohort_definition_set returns CohortGenerator's columns with only its own parameters left (FR-D3)", {
  cds <- cohort_definition_set(cohorts_dir(), followup_days = 45L, min_age = 21L)
  expect_named(cds, c("cohortId", "cohortName", "sql", "json"))
  expect_equal(cds$cohortId, c(1L, 2L))
  left <- unique(unlist(regmatches(cds$sql, gregexpr("@[a-z_]+", cds$sql))))
  expect_setequal(left, c("@cdm_database_schema", "@target_database_schema", "@target_cohort_table", "@target_cohort_id"))
  expect_match(cds$sql[1], "DATEADD(day, 45,", fixed = TRUE)
  expect_match(cds$sql[1], ">= 21", fixed = TRUE)
  expect_true(all(vapply(cds$json, function(j) is.list(jsonlite::fromJSON(j)), logical(1))))
})

test_that("cohort_definition_set validates its inputs", {
  d <- cohorts_dir()
  expect_error(cohort_definition_set(d, target_cohort_id = 2L, outcome_cohort_id = 2L), "differ")
  expect_error(cohort_definition_set(d, followup_days = -1), "followup_days")
  expect_error(cohort_definition_set(file.path(d, "nope")), "not found")
})
