library(omopduckdb)

MET <- 1503297; MET_BRAND <- 1000001; GLIPIZIDE_MET <- 1000002; INSULIN <- 1000003; SU <- 1000004
A10 <- 21600712; A10A <- 21600713; A10BA <- 21600745; A10BD <- 21600765
PREDIAB <- 37018196; PCOS_ICD <- 40443308; PCOS_SNOMED <- 36683296; DM <- 201820; DM_CHILD <- 1000005

excl_con <- function(with_vocab = TRUE, env = parent.frame()) {
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  withr::defer({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db_path) }, envir = env)
  if (with_vocab) {
    anc <- list(c(MET, MET), c(MET, MET_BRAND), c(MET, GLIPIZIDE_MET), c(A10, A10), c(A10, A10A), c(A10, A10BA), c(A10, A10BD),
                c(A10, MET_BRAND), c(A10, GLIPIZIDE_MET), c(A10, INSULIN), c(A10, SU), c(A10A, A10A), c(A10A, INSULIN),
                c(A10BA, A10BA), c(A10BA, MET_BRAND), c(A10BD, A10BD), c(A10BD, GLIPIZIDE_MET), c(DM, DM), c(DM, DM_CHILD),
                c(PREDIAB, PREDIAB), c(PCOS_ICD, PCOS_ICD), c(PCOS_SNOMED, PCOS_SNOMED))
    for (a in anc) DBI::dbExecute(con, sprintf("INSERT INTO concept_ancestor VALUES (%.0f, %.0f, 0, 0)", a[1], a[2]))
  }
  con
}

excl_person <- function(con, pid, drugs = numeric(0), conds = list(), drug_src = NA_character_) {
  DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, 1970, 0, 0)", pid))
  for (i in seq_along(drugs)) {
    DBI::dbExecute(con, "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id, drug_source_value) VALUES (?, ?, ?, DATE '2019-01-01', DATE '2019-01-31', 0, ?)",
                   params = list(pid * 100L + i, pid, drugs[i], drug_src))
  }
  for (i in seq_along(conds)) {
    DBI::dbExecute(con, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id, condition_source_value) VALUES (?, ?, ?, DATE '2019-02-01', 0, ?)",
                   params = list(pid * 100L + i, pid, conds[[i]][[1]], conds[[i]][[2]]))
  }
}

excl_populate <- function(con) {
  excl_person(con, 1, MET_BRAND, list(list(PREDIAB, NA_character_)))
  excl_person(con, 2, MET, list(list(0, "E28.2")))
  excl_person(con, 3, c(MET_BRAND, INSULIN), list(list(PREDIAB, NA_character_)))
  excl_person(con, 4, MET_BRAND, list(list(PREDIAB, NA_character_), list(DM_CHILD, NA_character_)))
  excl_person(con, 5, MET_BRAND)
  excl_person(con, 6, numeric(0), list(list(PREDIAB, NA_character_)))
  excl_person(con, 7, MET_BRAND, list(list(PCOS_SNOMED, NA_character_)))
  excl_person(con, 8, GLIPIZIDE_MET, list(list(PREDIAB, NA_character_)))
  excl_person(con, 9, 0, list(list(0, "R73.03")), drug_src = "Metformin HCl 500 mg")
  excl_person(con, 10, MET_BRAND, list(list(0, "E11.9")))
  excl_person(con, 11, c(MET_BRAND, 0), list(list(PREDIAB, NA_character_)))
  DBI::dbExecute(con, "UPDATE drug_exposure SET drug_source_value = 'insulin glargine' WHERE drug_exposure_id = 1102")
}

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
