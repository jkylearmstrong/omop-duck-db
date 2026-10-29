library(omopduckdb)

circe_file <- function(name) {
  candidates <- c(file.path(testthat::test_path(), "..", "..", "cohorts", name),
                  file.path(testthat::test_path(), "..", "..", "..", "cohorts", name),
                  system.file("cohorts", name, package = "omopduckdb"))
  hit <- candidates[nzchar(candidates) & file.exists(candidates)]
  if (!length(hit)) testthat::skip(paste("cohorts/", name, "not available in this test environment"))
  hit[[1]]
}

# every person observed 2015-2022, which the official engine (and now the native compiler) requires of an index event
circe_observed <- function(con) {
  DBI::dbExecute(con, "DELETE FROM observation_period")
  DBI::dbExecute(con, "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) SELECT person_id, person_id, DATE '2015-01-01', DATE '2022-01-01', 0 FROM person")
  con
}

circe_mapped_fixture <- function(env = parent.frame()) {
  con <- excl_con(env = env); excl_populate(con)
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 2", PCOS_ICD))
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 9", PREDIAB))
  DBI::dbExecute(con, sprintf("UPDATE drug_exposure SET drug_concept_id = %.0f WHERE person_id = 9", MET))
  DBI::dbExecute(con, sprintf("UPDATE condition_occurrence SET condition_concept_id = %.0f WHERE person_id = 10", DM))
  DBI::dbExecute(con, sprintf("UPDATE drug_exposure SET drug_concept_id = %.0f WHERE person_id = 11 AND drug_concept_id = 0", INSULIN))
  circe_observed(con)
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
  DBI::dbExecute(con, "CREATE TABLE observation_period (person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE)")
  DBI::dbExecute(con, "INSERT INTO observation_period SELECT unnest([1,2,3,4,5]), DATE '2015-01-01', DATE '2022-01-01'")
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
  circe_observed(con)
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


# --- ATLAS semantics the native compiler must follow (found by running PhenotypeLibrary definitions through CirceR) -------

atlas_con <- function(env = parent.frame()) {
  con <- DBI::dbConnect(duckdb::duckdb())
  withr::defer(DBI::dbDisconnect(con, shutdown = TRUE), envir = env)
  DBI::dbExecute(con, "CREATE TABLE concept_ancestor (ancestor_concept_id BIGINT, descendant_concept_id BIGINT)")
  DBI::dbExecute(con, "CREATE TABLE condition_occurrence (person_id BIGINT, condition_concept_id BIGINT, condition_start_date DATE, condition_end_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE observation (person_id BIGINT, observation_concept_id BIGINT, observation_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE observation_period (person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE person (person_id BIGINT, gender_concept_id INT, year_of_birth INT, month_of_birth INT, day_of_birth INT)")
  for (pid in 1:3) {
    DBI::dbExecute(con, sprintf("INSERT INTO person VALUES (%d, 8507, 1960, 1, 1)", pid))
    DBI::dbExecute(con, sprintf("INSERT INTO observation_period VALUES (%d, DATE '2010-01-01', DATE '2023-01-01')", pid))
  }
  con
}

atlas_def <- function(rules = list(), primary_limit = "All", expression_limit = "All", end = list(DateOffset = list(DateField = "StartDate", Offset = 0)),
                      pad = 0, primary = list(list(ConditionOccurrence = list(CodesetId = 0)))) {
  list(ConceptSets = list(list(id = 0, name = "a", expression = list(items = list(list(concept = list(CONCEPT_ID = 100))))),
                          list(id = 1, name = "b", expression = list(items = list(list(concept = list(CONCEPT_ID = 200)))))),
       PrimaryCriteria = list(CriteriaList = primary, ObservationWindow = list(PriorDays = 0, PostDays = 0),
                              PrimaryCriteriaLimit = list(Type = primary_limit)),
       ExpressionLimit = list(Type = expression_limit), InclusionRules = rules, EndStrategy = end,
       CollapseSettings = list(CollapseType = "ERA", EraPad = pad))
}

atlas_b_within_year <- list(name = "b within 365 days", expression = list(Type = "ALL", Groups = list(), CriteriaList = list(list(
  Criteria = list(ConditionOccurrence = list(CodesetId = 1)),
  StartWindow = list(Start = list(Days = 0, Coeff = -1), End = list(Days = 365, Coeff = 1)),
  Occurrence = list(Type = 2, Count = 1)))))

atlas_run <- function(con, d) DBI::dbGetQuery(con, compile_circe_to_duckdb(d, cdm_schema = "main"))

test_that("the primary limit comes before the inclusion rules, as in ATLAS", {
  con <- atlas_con()
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (1, 100, DATE '2015-01-01', NULL), (1, 100, DATE '2016-01-01', NULL), (1, 200, DATE '2016-06-01', NULL), (2, 100, DATE '2015-01-01', NULL), (2, 200, DATE '2015-03-01', NULL)")
  expect_equal(sort(atlas_run(con, atlas_def(list(atlas_b_within_year), primary_limit = "First"))$subject_id), 2)
  expect_equal(sort(atlas_run(con, atlas_def(list(atlas_b_within_year), primary_limit = "All"))$subject_id), c(1, 2))
})

test_that("end strategy, era collapse and expression limit follow ATLAS", {
  con <- atlas_con()
  DBI::dbExecute(con, "INSERT INTO condition_occurrence VALUES (1, 100, DATE '2015-01-01', NULL), (1, 100, DATE '2015-01-20', NULL), (1, 100, DATE '2016-01-01', NULL)")
  plus30 <- list(DateOffset = list(DateField = "EndDate", Offset = 30))
  out <- atlas_run(con, atlas_def(end = plus30))
  expect_equal(format(as.Date(out$cohort_start_date)), c("2015-01-01", "2016-01-01"))
  expect_equal(format(as.Date(out$cohort_end_date)), c("2015-02-20", "2016-02-01"))
  expect_equal(nrow(atlas_run(con, atlas_def(end = plus30, expression_limit = "First"))), 1)
  expect_equal(nrow(atlas_run(con, atlas_def(end = plus30, pad = 400))), 1)
  default_end <- atlas_run(con, atlas_def(end = list()))
  expect_equal(nrow(default_end), 1)
  expect_equal(format(as.Date(default_end$cohort_end_date)), "2023-01-01")
})

test_that("the Observation domain is supported and events need an observation period", {
  con <- atlas_con()
  DBI::dbExecute(con, "INSERT INTO observation VALUES (1, 100, DATE '2015-01-01'), (3, 100, DATE '2005-01-01')")
  expect_equal(sort(atlas_run(con, atlas_def(primary = list(list(Observation = list(CodesetId = 0)))))$subject_id), 1)
})

test_that("constructs that would change the cohort are refused, not ignored", {
  d <- atlas_def(); d$AdditionalCriteria <- list(Type = "ALL", CriteriaList = list(list(a = 1)))
  expect_error(compile_circe_to_duckdb(d), "AdditionalCriteria")
  d <- atlas_def(primary = list(list(ConditionOccurrence = list(CodesetId = 0, First = TRUE))))
  expect_error(compile_circe_to_duckdb(d), "First")
  d <- atlas_def(primary = list(list(ConditionOccurrence = list(CodesetId = 0, Unit = list(list(CONCEPT_ID = 1))))))
  expect_error(compile_circe_to_duckdb(d), "Unit")
  d <- atlas_def(primary = list(list(Specimen = list())))
  expect_error(compile_circe_to_duckdb(d), "Specimen")
  d <- atlas_def(); d$ConceptSets[[1]]$expression$items[[1]]$includeMapped <- TRUE
  expect_error(compile_circe_to_duckdb(d), "includeMapped")
  expect_warning(compile_circe_to_duckdb(atlas_def(end = list(CustomEra = list(DrugCodesetId = 1, GapDays = 0, Offset = 0)))), "CustomEra")
})

test_that("between operators are supported and unknown operators are refused", {
  con <- atlas_con()
  DBI::dbExecute(con, "CREATE TABLE measurement (person_id BIGINT, measurement_concept_id BIGINT, measurement_date DATE, value_as_number DOUBLE)")
  DBI::dbExecute(con, "INSERT INTO measurement VALUES (1, 100, DATE '2015-01-01', 5.0), (2, 100, DATE '2015-01-01', 9.0)")
  mdef <- function(op) atlas_def(primary = list(list(Measurement = list(CodesetId = 0, ValueAsNumber = list(Value = 4, Extent = 6, Op = op)))))
  expect_equal(sort(atlas_run(con, mdef("bt"))$subject_id), 1)
  expect_equal(sort(atlas_run(con, mdef("!bt"))$subject_id), 2)
  expect_error(compile_circe_to_duckdb(mdef("between-ish")), "operator")
})

# ---- the index-stay (target) cohort as Circe JSON -------------------------------------------------------------------------------
target_pairs <- function(con, d) {
  out <- DBI::dbGetQuery(con, compile_circe_to_duckdb(d, cdm_schema = "main"))
  sort(sprintf("%d@%s", out$subject_id, format(as.Date(out$cohort_start_date))))
}

target_json <- function() jsonlite::fromJSON(circe_file("target_cohort.circe.json"), simplifyVector = FALSE)

# what CirceR + CohortGenerator return for this definition on this CDM (scripts/ohdsi_parity_target_cohort.R repeats the run)
target_official <- c("1@2020-02-04", "2@2020-03-05", "5@2020-03-05", "6@2020-01-03")

test_that("the native compiler gives the target cohort the official engine returns", {
  con <- target_cdm()
  expect_equal(target_pairs(con, target_json()), target_official)
})

test_that("the target cohort differs from the authoritative SQL only in the two documented cases", {
  con <- target_cdm()
  cohort_sql <- readLines(circe_file("target_cohort.sql"), warn = FALSE)
  expect_true(any(grepl("discharged_to_concept_id", cohort_sql)))
  # the SQL (run in test_target_cohort_circe.py) returns 1, 5, 6 and 7: person 7 died within 30 days of discharge and the
  # observation period ends at death; person 2 is discharged "dead" without a death record, which Circe cannot see
  got <- target_pairs(con, target_json())
  expect_true("2@2020-03-05" %in% got)
  expect_false(any(startsWith(got, "7@")))
})

test_that("date adjustment, age at the adjusted date and correlated criteria on an entry event are evaluated", {
  con <- target_cdm()
  d <- target_json()
  d$PrimaryCriteria$ObservationWindow <- list(PriorDays = 0, PostDays = 0)
  v <- d$PrimaryCriteria$CriteriaList[[1]]$VisitOccurrence
  d$PrimaryCriteria$CriteriaList[[1]]$VisitOccurrence$DateAdjustment <- list(StartWith = "END_DATE", StartOffset = 2, EndWith = "END_DATE", EndOffset = 2)
  expect_true("1@2020-02-06" %in% target_pairs(con, d))
  d$PrimaryCriteria$CriteriaList[[1]]$VisitOccurrence$DateAdjustment <- NULL
  expect_true("1@2020-02-01" %in% target_pairs(con, d))
  any_death <- list(Type = "ANY", DemographicCriteriaList = list(), Groups = list(), CriteriaList = list(list(
    Criteria = list(Death = structure(list(), names = character(0))),
    StartWindow = list(Start = list(Days = 0, Coeff = 1), End = list(Days = 30, Coeff = 1)), Occurrence = list(Type = 2, Count = 1))))
  d$PrimaryCriteria$CriteriaList <- list(list(VisitOccurrence = list(CodesetId = 0, CorrelatedCriteria = any_death)))
  expect_equal(sort(unique(as.integer(sub("@.*", "", target_pairs(con, d))))), c(7L, 8L, 9L))
  nested <- any_death; nested$Groups <- list(list(Type = "ALL", CriteriaList = list()))
  d$PrimaryCriteria$CriteriaList <- list(list(VisitOccurrence = list(CodesetId = 0, CorrelatedCriteria = nested)))
  expect_error(compile_circe_to_duckdb(d), "nested criteria groups")
  d$PrimaryCriteria$CriteriaList <- list(list(VisitOccurrence = list(CodesetId = 0, DateAdjustment = list(StartWith = "CODE_DATE"))))
  expect_error(compile_circe_to_duckdb(d), "DateAdjustment")
})

test_that("Death is a domain for entry events", {
  con <- target_cdm()
  d <- target_json()
  d$PrimaryCriteria$ObservationWindow <- list(PriorDays = 0, PostDays = 0)
  d$PrimaryCriteria$CriteriaList <- list(list(Death = structure(list(), names = character(0))))
  expect_equal(sort(as.integer(sub("@.*", "", target_pairs(con, d)))), c(7L, 8L, 9L))
})
