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

test_that("cohort_definition_set renders parameters and validates inputs on stub files (no repo cohorts/ needed)", {
  d <- tempfile("cohort_stubs_")
  dir.create(d)
  on.exit(unlink(d, recursive = TRUE), add = TRUE)
  writeLines(c("-- target",
               "INSERT INTO @target_database_schema.@target_cohort_table SELECT @target_cohort_id, DATEADD(day, @followup_days, d) FROM @cdm_database_schema.t WHERE age >= @min_age"),
             file.path(d, "target_cohort.sql"))
  writeLines("INSERT INTO @target_database_schema.@target_cohort_table SELECT @target_cohort_id FROM @cdm_database_schema.t",
             file.path(d, "outcome_cohort.sql"))
  writeLines('{"ConceptSets": []}', file.path(d, "target_cohort.circe.json"))
  writeLines('{"ConceptSets": []}', file.path(d, "outcome_cohort.circe.json"))

  cds <- cohort_definition_set(d, target_cohort_id = 5L, outcome_cohort_id = 6L, followup_days = 45L, min_age = 21L)
  expect_named(cds, c("cohortId", "cohortName", "sql", "json"))
  expect_equal(cds$cohortId, c(5L, 6L))
  expect_match(cds$sql[1], "DATEADD(day, 45, d)", fixed = TRUE)
  expect_match(cds$sql[1], "age >= 21", fixed = TRUE)
  expect_false(grepl("@followup_days|@min_age", paste(cds$sql, collapse = " ")))
  expect_match(cds$sql[2], "@target_cohort_id", fixed = TRUE)   # left for CohortGenerator
  expect_equal(cds$json[1], '{"ConceptSets": []}')

  expect_error(cohort_definition_set(d, target_cohort_id = 6L, outcome_cohort_id = 6L), "differ")
  expect_error(cohort_definition_set(d, followup_days = -1), "followup_days")
  expect_error(cohort_definition_set(d, min_age = 1.5), "min_age")
  expect_error(cohort_definition_set(file.path(d, "missing")), "not found")
})

test_that("readonly_attach_sql builds READ_ONLY ATTACH statements and validates inputs (FR-D4)", {
  cdm <- tempfile(fileext = ".duckdb"); vocab <- tempfile(fileext = ".duckdb")
  file.create(cdm, vocab)
  on.exit(unlink(c(cdm, vocab)), add = TRUE)
  expect_length(readonly_attach_sql(cdm), 1L)
  sql <- readonly_attach_sql(cdm, vocab)
  expect_length(sql, 2L)
  expect_true(all(grepl("^ATTACH '.*' AS .* [(]READ_ONLY[)]$", sql)))
  expect_match(sql[1], " AS cdm ", fixed = TRUE)
  expect_match(sql[2], " AS central_vocab ", fixed = TRUE)
  expect_error(readonly_attach_sql(cdm, vocab, cdm_alias = "bad alias"), "alias")
  expect_error(readonly_attach_sql(file.path(tempdir(), "nope.duckdb")), "not found")
  expect_error(readonly_attach_sql(cdm, file.path(tempdir(), "nope.duckdb")), "not found")
})

test_that("followup_evidence switches the later-event follow-up rule on and leaves no SqlRender syntax behind (FR-D7)", {
  d <- cohorts_dir()
  base <- cohort_definition_set(d)
  ext <- cohort_definition_set(d, followup_evidence = "observation_period_or_event")
  expect_false(grepl("m.measurement_date", base$sql[1], fixed = TRUE))
  expect_true(grepl("m.measurement_date", ext$sql[1], fixed = TRUE))
  for (sql in c(base$sql, ext$sql)) {
    expect_false(grepl("{DEFAULT", sql, fixed = TRUE))
    expect_false(grepl("@followup_evidence", sql, fixed = TRUE))
    expect_false(grepl("}?{", sql, fixed = TRUE))
  }
  expect_error(cohort_definition_set(d, followup_evidence = "nope"), "arg")
})
