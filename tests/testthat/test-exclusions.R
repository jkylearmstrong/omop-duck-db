library(omopduckdb)

test_that("find_metformin_only_prediabetes_pcos applies the four-part rule and the PCOS set", {
  con <- excl_con(); excl_populate(con)
  expect_equal(find_metformin_only_prediabetes_pcos(con)$person_id, c(1, 2, 9))
  expect_equal(find_metformin_only_prediabetes_pcos(con, "snomed")$person_id, c(1, 7, 9))
  expect_equal(find_metformin_only_prediabetes_pcos(con, "both")$person_id, c(1, 2, 7, 9))
  out <- find_metformin_only_prediabetes_pcos(con)
  expect_true(out$has_prediabetes[out$person_id == 1] && !out$has_pcos[out$person_id == 1])
  expect_true(out$has_pcos[out$person_id == 2] && !out$has_prediabetes[out$person_id == 2])
  expect_error(find_metformin_only_prediabetes_pcos(con, "nope"), "pcos must be")
})

test_that("without a vocabulary the sets fall back to ids and source values", {
  con <- excl_con(with_vocab = FALSE); excl_populate(con)
  expect_warning(out <- find_metformin_only_prediabetes_pcos(con), "concept_ancestor")
  expect_equal(out$person_id, c(2, 9))
})

excl_stays <- function(env = parent.frame()) {
  con <- excl_con(env = env); excl_populate(con)
  for (pid in 1:11) {
    DBI::dbExecute(con, sprintf("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) VALUES (%d, %d, DATE '2015-01-01', DATE '2022-01-01', 0)", pid, pid))
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%d, %d, 9201, DATE '2020-03-01', DATE '2020-03-05', 0)", pid, pid))
  }
  con
}
ederri <- function(con, ...) build_ederri_diabetes_cohort(con, transfer_discharge_concept_ids = integer(0), materialise = FALSE, ...)

test_that("the cohort drops metformin-only prediabetes / PCOS patients and records the step", {
  con <- excl_stays()
  DBI::dbExecute(con, "UPDATE drug_exposure SET drug_source_value = 'metformin' WHERE drug_concept_id IN (1000001, 1503297, 1000002)")
  base <- ederri(con)
  expect_true(all(c(1, 2, 5, 7, 8) %in% base$subject_id))
  out <- ederri(con, exclude_metformin_only_prediabetes_pcos = TRUE)
  expect_equal(sort(setdiff(base$subject_id, out$subject_id)), c(1, 2, 9))
  expect_true("Not metformin-only prediabetes / PCOS (patient-level)" %in% attr(out, "attrition")$step_name)
  both <- ederri(con, exclude_metformin_only_prediabetes_pcos = "both")
  expect_false(7 %in% both$subject_id)
  expect_true(7 %in% out$subject_id)
  expect_equal(DBI::dbGetQuery(con, "SELECT count(*) AS n FROM duckdb_tables() WHERE table_name = '_study_metformin_only'")$n, 0)
  expect_error(ederri(con, exclude_metformin_only_prediabetes_pcos = "maybe"), "exclude_metformin_only")
})

test_that("the diabetes medication rule can use ATC A10 or a CUI list", {
  con <- excl_stays()
  expect_equal(sort(ederri(con)$subject_id), c(9, 10, 11))
  atc <- ederri(con, diabetes_drug_rule = "atc_a10")$subject_id
  expect_true(all(c(1, 3, 5, 7, 8, 11) %in% atc) && !6 %in% atc)
  DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, concept_code, valid_start_date, valid_end_date) VALUES (1000004, 'x', 'Drug', 'RxNorm', 'Ingredient', '1000004', DATE '1970-01-01', DATE '2099-12-31')")
  excl_person(con, 12, SU)
  DBI::dbExecute(con, "INSERT INTO observation_period VALUES (12, 12, DATE '2015-01-01', DATE '2022-01-01', 0)")
  DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (12, 12, 9201, DATE '2020-03-01', DATE '2020-03-05', 0)")
  cuis_file <- tempfile(fileext = ".txt"); writeLines(c("'1000004'", "'999999'"), cuis_file)
  expect_warning(f <- ederri(con, diabetes_drug_rule = "rxnorm_cuis", diabetes_rxnorm_cuis = cuis_file), "not RxNorm concepts")
  expect_false(12 %in% f$subject_id)
  DBI::dbExecute(con, sprintf("INSERT INTO concept_ancestor VALUES (1000004, %.0f, 0, 0)", SU))
  g <- ederri(con, diabetes_drug_rule = "rxnorm_cuis", diabetes_rxnorm_cuis = 1000004)
  expect_true(12 %in% g$subject_id && !1 %in% g$subject_id)
  expect_error(ederri(con, diabetes_drug_rule = "x"), "diabetes_drug_rule must")
  expect_error(ederri(con, diabetes_drug_rule = "rxnorm_cuis"), "needs diabetes_rxnorm_cuis")
  expect_error(ederri(con, diabetes_rxnorm_cuis = 1), "only used with")
  empty <- excl_con(with_vocab = FALSE)
  expect_error(ederri(empty, diabetes_drug_rule = "atc_a10"), "concept_ancestor")
})
