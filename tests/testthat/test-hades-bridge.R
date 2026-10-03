# Tests for the HADES bridge (PatientLevelPrediction / FeatureExtraction / DeepPatientLevelPrediction input).
# The strongest checks use the real HADES packages as an oracle on the same CDM; they skip if those packages
# are not installed. DuckDB files cannot be opened for writing twice, so tests seed with `con`, then
# disconnect before handing the file to DatabaseConnector.

skip_if_no_hades <- function() {
  for (pkg in c("Matrix", "Andromeda", "DatabaseConnector", "PatientLevelPrediction", "FeatureExtraction")) {
    testthat::skip_if_not_installed(pkg)
  }
}

# Fixture seeded for HADES, with the sparse concept matrix computed while `con` is still open.
hades_fixture <- function(env = parent.frame(), ...) {
  fx <- make_ml_fixture(env)
  seed_hades_tables(fx$con)
  fx
}

disconnect <- function(fx) if (DBI::dbIsValid(fx$con)) DBI::dbDisconnect(fx$con, shutdown = TRUE)

# "subject|date|covariateId" keys of a plpData, to compare covariate content independent of rowId order
covariate_keys <- function(plp) {
  cov <- as.data.frame(plp$covariateData$covariates)
  key <- plp$cohorts[match(cov$rowId, plp$cohorts$rowId), ]
  sort(sprintf("%s|%s|%.0f", key$subjectId, key$cohortStartDate, cov$covariateId))
}

test_that("hades_preflight passes on a HADES-ready file and prints a readable summary", {
  fx <- hades_fixture()
  disconnect(fx)
  pf <- hades_preflight(fx$db_path)
  expect_true(attr(pf, "all_ok"))
  expect_s3_class(pf, "hades_preflight")
  expect_match(paste(capture.output(print(pf)), collapse = "\n"), "all checks passed")
})

test_that("hades_preflight flags a missing/empty cohort table", {
  fx <- make_ml_fixture()                         # no observation_period, no cohort rows
  omopduckdb::ensure_cohort_tables(fx$con)
  disconnect(fx)
  pf <- hades_preflight(fx$db_path)
  expect_false(attr(pf, "all_ok"))
  expect_false(pf$ok[pf$check == "observation_period populated"])
  expect_false(pf$ok[pf$check == "cohort table cohort"])
})

test_that("hades_preflight catches a vocabulary that only exists through an attached central_vocab", {
  fx <- hades_fixture()
  vocab_db <- tempfile(fileext = ".duckdb")
  on.exit(unlink(vocab_db), add = TRUE)
  DBI::dbExecute(fx$con, sprintf("ATTACH '%s' AS central_vocab", gsub("\\\\", "/", vocab_db)))
  DBI::dbExecute(fx$con, "CREATE TABLE central_vocab.concept AS SELECT * FROM concept")
  DBI::dbExecute(fx$con, "DROP TABLE concept")
  DBI::dbExecute(fx$con, "CREATE VIEW concept AS SELECT * FROM central_vocab.concept")
  # works in this session (that is exactly the trap) ...
  expect_gt(DBI::dbGetQuery(fx$con, "SELECT COUNT(*) AS n FROM concept")$n, 0)
  DBI::dbExecute(fx$con, "DETACH central_vocab")
  disconnect(fx)
  # ... but not for a connection that cannot ATTACH, which is what DatabaseConnector opens
  pf <- hades_preflight(fx$db_path)
  expect_false(attr(pf, "all_ok"))
  detail <- pf$detail[pf$check == "vocabulary readable inside the file"]
  expect_match(detail, "central_vocab")
})

test_that("hades_preflight gives an actionable error for a bad path", {
  expect_error(hades_preflight(tempfile(fileext = ".duckdb")), "not found")
})

test_that("hades_preflight reports a file another process holds, e.g. a Python session (verified DuckDB behaviour)", {
  # DatabaseConnector opens the file read-write; any OTHER process holding it, even read-only, blocks that.
  # (Connections inside this R session never conflict, so this needs a second process.)
  py <- Sys.which("python")
  testthat::skip_if(py == "", "python not available")
  testthat::skip_if(system2(py, c("-c", shQuote("import duckdb")), stdout = FALSE, stderr = FALSE) != 0,
                    "python duckdb not available")
  fx <- hades_fixture()
  disconnect(fx)

  ready <- tempfile()
  release <- tempfile()
  script <- tempfile(fileext = ".py")
  # The holder exits when `release` appears, or after 90 s on its own, so nothing ever needs to be killed.
  writeLines(c(
    "import duckdb, os, time",
    sprintf("con = duckdb.connect(r'%s', read_only=True)", fx$db_path),
    sprintf("open(r'%s', 'w').write('ready')", ready),
    "t0 = time.time()",
    sprintf("while not os.path.exists(r'%s') and time.time() - t0 < 90: time.sleep(0.2)", release)
  ), script)
  system2(py, shQuote(script), wait = FALSE, stdout = FALSE, stderr = FALSE)
  on.exit(file.create(release), add = TRUE)
  waited <- 0
  while (!file.exists(ready) && waited < 40) { Sys.sleep(0.5); waited <- waited + 0.5 }
  testthat::skip_if_not(file.exists(ready), "python holder did not start")

  rw_check <- "file can be opened read-write (as DatabaseConnector does)"
  pf <- hades_preflight(fx$db_path)
  expect_false(attr(pf, "all_ok"))
  expect_false(pf$ok[pf$check == rw_check])
  expect_match(pf$detail[pf$check == rw_check], "another process")

  # once the other process lets go, the same file passes
  file.create(release)
  passed <- FALSE
  for (i in 1:60) {
    Sys.sleep(0.5)
    pf <- tryCatch(hades_preflight(fx$db_path), error = function(e) NULL)
    if (!is.null(pf) && isTRUE(attr(pf, "all_ok"))) { passed <- TRUE; break }
  }
  expect_true(passed)
})

test_that("plp_database_details builds PLP databaseDetails and refuses non-HADES-ready files", {
  skip_if_no_hades()
  fx <- hades_fixture()
  disconnect(fx)
  dbd <- plp_database_details(fx$db_path, target_id = 1, outcome_ids = 2)
  expect_s3_class(dbd, "databaseDetails")
  expect_equal(dbd$connectionDetails$dbms, "duckdb")
  expect_equal(dbd$cohortTable, "cohort")
  expect_equal(dbd$targetId, 1)
  expect_equal(dbd$outcomeIds, 2)

  bad <- make_ml_fixture()
  disconnect(bad)
  expect_error(plp_database_details(bad$db_path, 1, 2), "not ready for HADES")
})

test_that("conformance: our concept windows equal FeatureExtraction's long-term analyses (independent oracle)", {
  skip_if_no_hades()
  fx <- hades_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet, domains = c("condition", "drug", "procedure"),
                                       value = "binary")
  ours <- as_plp_data(res, fx$con, target_id = 1, outcome_id = 2)
  disconnect(fx)

  dbd <- plp_database_details(fx$db_path, target_id = 1, outcome_ids = 2)
  cs <- FeatureExtraction::createCovariateSettings(
    useConditionOccurrenceLongTerm = TRUE, useDrugExposureLongTerm = TRUE, useProcedureOccurrenceLongTerm = TRUE,
    longTermStartDays = -365, endDays = -1)
  native <- suppressMessages(PatientLevelPrediction::getPlpData(
    databaseDetails = dbd, covariateSettings = cs,
    restrictPlpDataSettings = PatientLevelPrediction::createRestrictPlpDataSettings()))

  # FeatureExtraction decides which concepts fall in [index-365, index-1] with its own SQL; we must agree,
  # including covariate ids (concept_id * 1000 + analysis id).
  expect_gt(length(covariate_keys(native)), 0)
  expect_equal(covariate_keys(ours), covariate_keys(native))
  # same cohort rows and observation-period arithmetic
  key <- function(p) sort(sprintf("%s|%s|%s|%s", p$cohorts$subjectId, p$cohorts$cohortStartDate,
                                  p$cohorts$daysFromObsStart, p$cohorts$daysToObsEnd))
  expect_equal(key(ours), key(native))
})

test_that("as_plp_data returns a plpData that PatientLevelPrediction accepts", {
  skip_if_no_hades()
  fx <- hades_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet, domains = c("condition", "drug"), value = "binary")
  plp <- as_plp_data(res, fx$con, target_id = 1, outcome_id = 2)
  disconnect(fx)

  expect_s3_class(plp, "plpData")
  expect_true(FeatureExtraction::isCovariateData(plp$covariateData))   # Andromeda objects are S4
  expect_equal(nrow(plp$cohorts), 4)
  expect_equal(plp$outcomes$rowId, c(1, 4))                 # parquet rows with y = 1 (persons 2 and 1)
  expect_equal(unique(plp$outcomes$outcomeId), 2)
  cov <- as.data.frame(plp$covariateData$covariates)
  ref <- as.data.frame(plp$covariateData$covariateRef)
  expect_true(all(cov$covariateId %in% ref$covariateId))
  expect_true(201102 %in% ref$covariateId)                   # 201 * 1000 + 102 (condition, long term)
  expect_true(301302 %in% ref$covariateId)                   # 301 * 1000 + 302 (drug, long term)
  expect_equal(ref$conceptId[ref$covariateId == 201102], 201)
  expect_match(ref$covariateName[ref$covariateId == 201102], "Type 2 diabetes")

  pop <- PatientLevelPrediction::createStudyPopulation(
    plpData = plp, outcomeId = 2,
    populationSettings = PatientLevelPrediction::createStudyPopulationSettings(
      riskWindowStart = 1, riskWindowEnd = 365, requireTimeAtRisk = FALSE, firstExposureOnly = FALSE,
      washoutPeriod = 0, removeSubjectsWithPriorOutcome = FALSE))
  expect_equal(sum(pop$outcomeCount), 2)
})

test_that("as_plp_data validates its inputs and reports rows outside observation", {
  skip_if_no_hades()
  fx <- hades_fixture()
  res <- extract_sparse_concept_matrix(fx$con, fx$parquet)
  expect_error(as_plp_data(res, "not a connection"), "DuckDB connection")
  no_y <- res
  no_y$y <- NULL
  expect_error(as_plp_data(no_y, fx$con), "outcome")
  # a person with no observation period around the index date is dropped, loudly
  DBI::dbExecute(fx$con, "DELETE FROM observation_period WHERE person_id = 3")
  expect_warning(plp <- as_plp_data(res, fx$con, target_id = 1, outcome_id = 2), "no observation period")
  expect_equal(nrow(plp$cohorts), 3)
})

# ---------------------------------------------------------------------------------------------------------
# End-to-end: a model is actually fitted by PatientLevelPrediction on data bridged from the sparse matrix, and
# DeepPLP's temporal input contract is satisfied by FeatureExtraction on an omop-duck-db file.
# ---------------------------------------------------------------------------------------------------------

test_that("PatientLevelPrediction fits a model on data bridged from the sparse matrix and finds the signal", {
  skip_if_no_hades()
  syn <- synthetic_cdm()
  res <- extract_sparse_concept_matrix(syn$con, syn$parquet, domains = c("condition", "drug"), value = "binary")
  plp <- as_plp_data(res, syn$con, target_id = 1, outcome_id = 2)
  expect_equal(nrow(plp$cohorts), 400)
  expect_equal(nrow(plp$outcomes), sum(syn$y))

  fit <- suppressMessages(PatientLevelPrediction::runPlp(
    plpData = plp, outcomeId = 2, analysisId = "bridge_smoke", analysisName = "bridge smoke",
    populationSettings = PatientLevelPrediction::createStudyPopulationSettings(
      requireTimeAtRisk = FALSE, riskWindowStart = 1, riskWindowEnd = 365, firstExposureOnly = FALSE,
      washoutPeriod = 0, removeSubjectsWithPriorOutcome = FALSE),
    splitSettings = PatientLevelPrediction::createDefaultSplitSetting(
      testFraction = 0.25, trainFraction = 0.75, splitSeed = 1, nfold = 3, type = "stratified"),
    sampleSettings = PatientLevelPrediction::createSampleSettings(),
    featureEngineeringSettings = PatientLevelPrediction::createFeatureEngineeringSettings(),
    preprocessSettings = PatientLevelPrediction::createPreprocessSettings(minFraction = 0, normalize = FALSE,
                                                                          removeRedundancy = FALSE),
    modelSettings = PatientLevelPrediction::setLassoLogisticRegression(seed = 1),
    logSettings = PatientLevelPrediction::createLogSettings(verbosity = "ERROR", logName = "bridge_smoke"),
    executeSettings = PatientLevelPrediction::createExecuteSettings(
      runSplitData = TRUE, runSampleData = FALSE, runFeatureEngineering = FALSE, runPreprocessData = TRUE,
      runModelDevelopment = TRUE, runCovariateSummary = FALSE),
    saveDirectory = tempfile("plp_bridge_")))

  stats <- fit$performanceEvaluation$evaluationStatistics
  auc <- as.numeric(stats$value[stats$evaluation == "Test" & stats$metric == "AUROC"])
  expect_gt(auc, 0.65)                                       # signal recovered (true AUROC is about 0.75)
  imp <- fit$model$covariateImportance
  imp <- imp[order(-abs(imp$covariateValue)), ]
  expect_equal(imp$covariateId[1], 201102)                   # the planted condition is the top coefficient
})

test_that("FeatureExtraction temporal sequences (DeepPLP's input) extract from an omop-duck-db file with timeId", {
  skip_if_no_hades()
  syn <- synthetic_cdm()
  DBI::dbDisconnect(syn$con, shutdown = TRUE)
  dbd <- plp_database_details(syn$db_path, target_id = 1, outcome_ids = 2)
  temporal <- FeatureExtraction::createTemporalSequenceCovariateSettings(
    useDemographicsAge = TRUE, useDemographicsGender = TRUE, useConditionOccurrence = TRUE, useDrugExposure = TRUE,
    sequenceStartDay = -365, sequenceEndDay = -1)
  plp <- suppressMessages(PatientLevelPrediction::getPlpData(
    databaseDetails = dbd, covariateSettings = temporal,
    restrictPlpDataSettings = PatientLevelPrediction::createRestrictPlpDataSettings()))
  cov <- as.data.frame(plp$covariateData$covariates)
  # DeepPatientLevelPrediction switches to its temporal path when covariates carry a timeId column
  expect_true("timeId" %in% names(cov))
  expect_gt(nrow(cov), 0)
  expect_true(FeatureExtraction::isCovariateData(plp$covariateData))
})
