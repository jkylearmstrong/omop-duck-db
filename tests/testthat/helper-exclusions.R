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
