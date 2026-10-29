# The R reconciliation gives the same reasons and counts as omop_etl.cohort_reconcile (tests/test_cohort_reconcile.py).
recon_cdm <- function(env = parent.frame()) {
  con <- target_cdm(env = env)
  DBI::dbExecute(con, "UPDATE person SET person_source_value = 'P' || person_id")
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) VALUES (10, 8507, 1980, 0, 0, 'P10')")
  con
}

recon_def <- function() target_definition()

recon_gaps <- function(res) {
  g <- res$gaps
  stats::setNames(g$n, paste(g$reason, g$detail, sep = "|"))
}

test_that("every reference patient gets the first step that excludes them", {
  con <- recon_cdm()
  ref <- data.frame(person_source_value = c(paste0("P", 1:10), "P99"))
  res <- explain_cohort_gaps(con, ref, definition = recon_def(), include_person_rows = TRUE)
  why <- stats::setNames(res$persons$reason, res$persons$ref_id)
  expect_equal(why[c(paste0("P", 1:10), "P99")], c(
    P1 = "in_cohort", P2 = "in_cohort", P3 = "no_entry_event", P4 = "entry_outside_observation_window", P5 = "in_cohort", P6 = "in_cohort",
    P7 = "entry_outside_observation_window", P8 = "failed_entry_correlated_criteria", P9 = "failed_entry_correlated_criteria",
    P10 = "no_observation_period", P99 = "not_in_cdm"))
  expect_equal(res$n_reference, 11)
  expect_equal(sum(res$gaps$n), 11)
  expect_null(explain_cohort_gaps(con, ref, definition = recon_def())$persons)
})

test_that("an inclusion rule is named when it is the reason", {
  con <- recon_cdm()
  d <- recon_def()
  d$InclusionRules <- list(list(name = "Age 40 or older", expression = list(Type = "ALL", CriteriaList = list(), Groups = list(),
                                                                            DemographicCriteriaList = list(list(Age = list(Value = 40, Op = "gte"))))))
  res <- explain_cohort_gaps(con, data.frame(person_source_value = c("P1", "P6")), definition = d)
  expect_equal(recon_gaps(res), c("in_cohort|" = 1, "failed_inclusion_rule|Age 40 or older" = 1))
})

test_that("reconcile_cohort counts the overlap and the date agreement", {
  con <- recon_cdm()
  ref <- data.frame(person_source_value = c("P1", "P5", "P6", "P7", "P99"),
                    index_date = c("2020-02-04", "2020-03-10", "2020-01-03", "2020-03-05", "2020-01-01"))
  out <- reconcile_cohort(con, ref, definition = recon_def(), index_date_col = "index_date")
  expect_equal(out$counts, list(n_reference = 5, n_mapped_to_cdm = 4, n_cohort = 4, both = 3, reference_only = 2, cohort_only = 1, jaccard = 0.5))
  expect_equal(out$dates, list(n_compared = 3, n_same_date = 2, n_different_date = 1, median_abs_days = 5, max_abs_days = 5))
  expect_equal(recon_gaps(out), c("in_cohort|" = 3, "not_in_cdm|" = 1, "entry_outside_observation_window|" = 1))
  expect_null(out$persons)
})

test_that("a cohort table without a definition still reports the basics, and input forms and errors are handled", {
  con <- recon_cdm()
  cohort <- data.frame(subject_id = 1, cohort_start_date = "2020-02-04")
  res <- explain_cohort_gaps(con, data.frame(person_source_value = c("P1", "P5", "P99", "P10")), cohort = cohort)
  expect_equal(recon_gaps(res), c("in_cohort|" = 1, "not_in_cdm|" = 1, "no_observation_period|" = 1, "not_in_cohort_no_definition_given|" = 1))
  res <- explain_cohort_gaps(con, data.frame(person_source_value = c("P1", "P5", "P99", "P10")), definition = recon_def(), cohort = cohort)
  expect_true("in_definition_not_in_cohort|" %in% names(recon_gaps(res)))
  path <- tempfile(fileext = ".csv")
  utils::write.csv(data.frame(pid = c(1, 3, 99)), path, row.names = FALSE)
  res <- explain_cohort_gaps(con, path, definition = recon_def(), id_col = "pid", id_type = "person_id")
  expect_equal(recon_gaps(res), c("in_cohort|" = 1, "not_in_cdm|" = 1, "no_entry_event|" = 1))
  DBI::dbExecute(con, "CREATE TABLE legacy_ids AS SELECT 'p1' AS person_source_value")
  expect_equal(recon_gaps(explain_cohort_gaps(con, "legacy_ids", definition = recon_def())), c("in_cohort|" = 1))
  expect_error(explain_cohort_gaps(con, data.frame(x = 1), definition = recon_def()), "no column")
  expect_error(explain_cohort_gaps(con, data.frame(person_source_value = "P1")), "definition, a cohort")
  expect_error(explain_cohort_gaps(con, data.frame(person_source_value = "P1"), definition = recon_def(), id_type = "mrn"), "id_type")
})

test_that("small cells are suppressed for sharing", {
  con <- recon_cdm()
  res <- explain_cohort_gaps(con, data.frame(person_source_value = c("P1", "P2", "P5", "P6", "P7")), definition = recon_def(), min_cell = 3)
  g <- res$gaps
  expect_equal(g$n[g$reason == "in_cohort"], 4)
  expect_true(is.na(g$n[g$reason == "entry_outside_observation_window"]))
  expect_true(g$suppressed[g$reason == "entry_outside_observation_window"])
  out <- suppress_small_cells(data.frame(n = c(0, 1, 5), pct = c(0, 10, 50)), "n", 3, also = "pct")
  expect_equal(out$suppressed, c(FALSE, TRUE, FALSE))
  expect_true(is.na(out$pct[2]))
})

test_that("AdditionalCriteria is named when it is the reason", {
  con <- recon_cdm()
  d <- recon_def()
  d$AdditionalCriteria <- list(Type = "ALL", Groups = list(), CriteriaList = list(), DemographicCriteriaList = list(list(Age = list(Value = 40, Op = "gte"))))
  res <- explain_cohort_gaps(con, data.frame(person_source_value = c("P1", "P6")), definition = d)
  expect_equal(recon_gaps(res), c("in_cohort|" = 1, "failed_additional_criteria|" = 1))
})
