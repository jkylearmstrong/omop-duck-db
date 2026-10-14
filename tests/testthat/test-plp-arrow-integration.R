# as_plp_arrow() -> plp_arrow_to_plp_data() -> PatientLevelPrediction::runPlp(): the Arrow hand-off must yield a
# plpData that a real PLP model can fit and from which it recovers a planted signal.

skip_if_no_plp_stack <- function() {
  for (pkg in c("arrow", "Andromeda", "PatientLevelPrediction", "FeatureExtraction")) {
    testthat::skip_if_not_installed(pkg)
  }
}

test_that("as_plp_arrow output drives a real PLP model and recovers the planted lab", {
  skip_if_no_plp_stack()
  fx <- plp_arrow_fixture()
  con <- fx$con; arr <- fx$arr; plp <- fx$plp; y <- fx$y; n <- length(y)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  expect_equal(sort(names(arr)), c("cohorts", "covariate_ref", "covariates", "outcomes"))
  expect_s3_class(plp, "plpData")
  expect_true(FeatureExtraction::isCovariateData(plp$covariateData))
  expect_equal(nrow(plp$cohorts), n)
  expect_equal(nrow(plp$outcomes), sum(y))

  fit <- suppressMessages(PatientLevelPrediction::runPlp(
    plpData = plp, outcomeId = 2, analysisId = "arrow_smoke", analysisName = "arrow smoke",
    populationSettings = PatientLevelPrediction::createStudyPopulationSettings(
      requireTimeAtRisk = FALSE, riskWindowStart = 1, riskWindowEnd = 365, firstExposureOnly = FALSE,
      washoutPeriod = 0, removeSubjectsWithPriorOutcome = FALSE),
    splitSettings = PatientLevelPrediction::createDefaultSplitSetting(
      testFraction = 0.25, trainFraction = 0.75, splitSeed = 1, nfold = 3, type = "stratified"),
    sampleSettings = PatientLevelPrediction::createSampleSettings(),
    featureEngineeringSettings = PatientLevelPrediction::createFeatureEngineeringSettings(),
    preprocessSettings = PatientLevelPrediction::createPreprocessSettings(minFraction = 0, normalize = TRUE,
                                                                          removeRedundancy = FALSE),
    modelSettings = PatientLevelPrediction::setLassoLogisticRegression(seed = 1),
    logSettings = PatientLevelPrediction::createLogSettings(verbosity = "ERROR", logName = "arrow_smoke"),
    executeSettings = PatientLevelPrediction::createExecuteSettings(
      runSplitData = TRUE, runSampleData = FALSE, runFeatureEngineering = FALSE, runPreprocessData = TRUE,
      runModelDevelopment = TRUE, runCovariateSummary = FALSE),
    saveDirectory = tempfile("plp_arrow_")))
  stats_df <- fit$performanceEvaluation$evaluationStatistics
  auc <- as.numeric(stats_df$value[stats_df$evaluation == "Test" & stats_df$metric == "AUROC"])
  expect_gt(auc, 0.65)
  imp <- fit$model$covariateImportance
  imp <- imp[order(-abs(imp$covariateValue)), ]
  ref <- as.data.frame(arr$covariate_ref)
  expect_equal(imp$covariateId[1], ref$covariateId[ref$covariateName == "creatinine"])
})

test_that("plp_arrow_to_plp_data validates its inputs", {
  skip_if_no_plp_stack()
  con <- DBI::dbConnect(duckdb::duckdb())
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  arr <- as_plp_arrow(con, data.frame(subject_id = 1:3, cohort_start_date = as.Date("2020-01-01"), a = c(1, 2, 3)))
  expect_error(plp_arrow_to_plp_data(arr, con), "outcome_col")
})
