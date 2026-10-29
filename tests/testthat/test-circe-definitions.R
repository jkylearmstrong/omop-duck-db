library(omopduckdb)

circe_file <- function(name) {
  candidates <- c(file.path(testthat::test_path(), "..", "..", "cohorts", name),
                  file.path(testthat::test_path(), "..", "..", "..", "cohorts", name),
                  system.file("cohorts", name, package = "omopduckdb"))
  hit <- candidates[nzchar(candidates) & file.exists(candidates)]
  if (!length(hit)) testthat::skip(paste("cohorts/", name, "not available in this test environment"))
  hit[[1]]
}

circe_mapped_fixture <- function(env = parent.frame()) {
  con <- excl_con(env = env); excl_populate(con)
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 2", PCOS_ICD))
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 9", PREDIAB))
  DBI::dbExecute(con, sprintf("UPDATE drug_exposure SET drug_concept_id = %.0f WHERE person_id = 9", MET))
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 10", DM))
  DBI::dbExecute(con, sprintf("UPDATE drug_exposure SET drug_concept_id = %.0f WHERE person_id = 11 AND drug_concept_id = 0", INSULIN))
  con
}

test_that("excluded concept-set items are subtracted by the compiler", {
  d <- list(ConceptSets = list(list(id = 0, name = "a", expression = list(items = list(
    list(concept = list(CONCEPT_ID = 10), includeDescendants = TRUE, isExcluded = FALSE),
    list(concept = list(CONCEPT_ID = 11), includeDescendants = TRUE, isExcluded = TRUE))))),
    PrimaryCriteria = list(CriteriaList = list(list(DrugExposure = list(CodesetId = 0))),
                           ObservationWindow = list(PriorDays = 0, PostDays = 0)))
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE))
  DBI::dbExecute(con, "CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT)")
  DBI::dbExecute(con, "INSERT INTO concept_ancestor VALUES (10,10),(10,11),(10,12),(11,11),(11,13),(10,13)")
  DBI::dbExecute(con, "CREATE TABLE drug_exposure (person_id BIGINT, drug_concept_id BIGINT, drug_exposure_start_date DATE, drug_exposure_end_date DATE)")
  for (x in list(c(1, 10), c(2, 11), c(3, 12), c(4, 13), c(5, 99))) {
    DBI::dbExecute(con, sprintf("INSERT INTO drug_exposure VALUES (%d, %d, DATE '2020-01-01', DATE '2020-01-02')", x[1], x[2]))
  }
  out <- DBI::dbGetQuery(con, compile_circe_to_duckdb(d, cdm_schema = "main"))
  expect_equal(sort(out$subject_id), c(1, 3))
})

test_that("the committed metformin-only Circe cohorts equal the finder on mapped data", {
  con <- circe_mapped_fixture()
  expected <- list(icd10 = c(1, 2, 9), snomed = c(1, 7, 9), both = c(1, 2, 7, 9))
  for (p in names(expected)) {
    cohort <- execute_circe_cohort(con, circe_file(sprintf("metformin_only_prediabetes_pcos_%s.circe.json", p)))
    expect_equal(sort(cohort$subject_id), expected[[p]])
    expect_equal(find_metformin_only_prediabetes_pcos(con, p)$person_id, expected[[p]])
  }
})

test_that("the diabetes phenotype Circe cohort covers diagnosis, drug and HbA1c >= 6.5", {
  con <- excl_con()
  for (x in list(c(1, 1970), c(2, 1970), c(3, 1970), c(4, 1970), c(5, 1970), c(6, 2015), c(7, 1970))) {
    DBI::dbExecute(con, sprintf("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (%d, 8507, %d, 0, 0)", x[1], x[2]))
  }
  DBI::dbExecute(con, sprintf("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES (1, 1, %.0f, DATE '2019-01-01', 0)", DM_CHILD))
  DBI::dbExecute(con, sprintf("INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_end_date, drug_type_concept_id) VALUES (2, 2, %.0f, DATE '2019-01-01', DATE '2019-01-31', 0)", INSULIN))
  for (x in list(c(3, 3, 3004410, 7.2), c(4, 4, 3004410, 5.4), c(5, 5, 40758583, 6.5), c(6, 6, 3004410, 9))) {
    DBI::dbExecute(con, sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number) VALUES (%d, %d, %.0f, DATE '2019-01-01', 0, %s)", x[1], x[2], x[3], format(x[4])))
  }
  out <- execute_circe_cohort(con, circe_file("diabetes_phenotype.circe.json"))
  expect_equal(sort(out$subject_id), c(1, 2, 3, 5))
})

test_that("CirceR + CohortGenerator give the same people as the native compiler (the official Circe engine)", {
  skip_if_not_installed("CirceR")
  skip_if_not_installed("CohortGenerator")
  skip_if_not_installed("DatabaseConnector")
  files <- c("metformin_only_prediabetes_pcos_icd10", "metformin_only_prediabetes_pcos_snomed",
             "metformin_only_prediabetes_pcos_both", "diabetes_phenotype")
  paths <- vapply(files, function(f) circe_file(paste0(f, ".circe.json")), character(1))

  con <- excl_con(env = new.env())          # the cleanup is done below, before CohortGenerator opens the file
  db_path <- DBI::dbGetInfo(con)$dbname
  excl_populate(con)
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 2", PCOS_ICD))
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 9", PREDIAB))
  DBI::dbExecute(con, sprintf("UPDATE drug_exposure SET drug_concept_id = %.0f WHERE person_id = 9", MET))
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 10", DM))
  DBI::dbExecute(con, sprintf("UPDATE drug_exposure SET drug_concept_id = %.0f WHERE person_id = 11 AND drug_concept_id = 0", INSULIN))
  for (pid in 1:11) DBI::dbExecute(con, sprintf("INSERT INTO observation_period VALUES (%d,%d,DATE '2015-01-01',DATE '2022-01-01',0)", pid, pid))
  # the official engine resolves concept sets through the concept table
  ids <- unique(c(DBI::dbGetQuery(con, "SELECT ancestor_concept_id AS i FROM concept_ancestor UNION SELECT descendant_concept_id FROM concept_ancestor")$i,
                  DBI::dbGetQuery(con, "SELECT DISTINCT drug_concept_id AS i FROM drug_exposure")$i,
                  DBI::dbGetQuery(con, "SELECT DISTINCT condition_concept_id AS i FROM condition_occurrence")$i))
  for (i in ids[ids != 0]) {
    DBI::dbExecute(con, sprintf("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES (%.0f, 'c%.0f', 'Drug', 'RxNorm', 'Ingredient', 'S', 'c%.0f', DATE '1970-01-01', DATE '2099-12-31')", i, i, i))
  }
  native <- lapply(seq_along(paths), function(i) sort(execute_circe_cohort(con, paths[[i]], target_cohort_id = 100L + i, cohort_table = "native_cohort")$subject_id))
  DBI::dbExecute(con, "CHECKPOINT")
  DBI::dbDisconnect(con, shutdown = TRUE)
  on.exit(unlink(db_path), add = TRUE)

  defs <- tryCatch(do.call(rbind, lapply(seq_along(paths), function(i) {
    json <- paste(readLines(paths[[i]], warn = FALSE), collapse = "\n")
    sql <- CirceR::buildCohortQuery(CirceR::cohortExpressionFromJson(json), CirceR::createGenerateOptions(generateStats = FALSE))
    data.frame(cohortId = i, cohortName = files[[i]], json = json, sql = sql, stringsAsFactors = FALSE)
  })), error = function(e) skip(paste("CirceR cannot run here (Java?):", conditionMessage(e))))

  cd <- DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path)
  tables <- CohortGenerator::getCohortTableNames("cohort_cg")
  conn <- tryCatch(DatabaseConnector::connect(cd), error = function(e) skip(paste("cannot connect:", conditionMessage(e))))
  CohortGenerator::createCohortTables(connection = conn, cohortDatabaseSchema = "main", cohortTableNames = tables)
  DatabaseConnector::disconnect(conn)
  suppressMessages(CohortGenerator::generateCohortSet(connectionDetails = cd, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main",
                                                      cohortTableNames = tables, cohortDefinitionSet = defs))
  conn <- DatabaseConnector::connect(cd)
  on.exit(DatabaseConnector::disconnect(conn), add = TRUE, after = FALSE)
  official <- DatabaseConnector::querySql(conn, "SELECT cohort_definition_id, subject_id FROM cohort_cg")
  names(official) <- tolower(names(official))
  for (i in seq_along(paths)) {
    expect_equal(sort(official$subject_id[official$cohort_definition_id == i]), native[[i]], info = files[[i]])
  }
  expect_equal(sort(official$subject_id[official$cohort_definition_id == 3]), c(1, 2, 7, 9))
})
