# Integrations with external OHDSI / ML stacks: DeepPatientLevelPrediction (torch, via reticulate) and
# OmopOnDuckDB. Both are GitHub-only (not on CRAN, so deliberately NOT in DESCRIPTION Suggests); each test skips
# cleanly when the package (or torch) is not installed, and calls it through getExportedValue() so R CMD check
# does not complain about an undeclared dependency.
deep <- function(f) getExportedValue("DeepPatientLevelPrediction", f)
omop_on_duckdb <- function(f) getExportedValue("OmopOnDuckDB", f)

test_that("DeepPatientLevelPrediction fits an MLP on plpData built from as_plp_arrow and finds the signal", {
  skip_on_cran()
  for (pkg in c("DeepPatientLevelPrediction", "PatientLevelPrediction", "Andromeda", "arrow", "reticulate")) {
    testthat::skip_if_not_installed(pkg)
  }
  testthat::skip_if_not(reticulate::py_available(initialize = TRUE) && reticulate::py_module_available("torch"),
                        "python torch is not available to reticulate (set RETICULATE_PYTHON)")
  ok <- tryCatch({ reticulate::import("pyarrow.lib"); TRUE }, error = function(e) FALSE)
  testthat::skip_if_not(ok, "pyarrow cannot be imported next to R's arrow in this process (Windows DLL clash)")
  fx <- plp_arrow_fixture(n = 600L)
  on.exit(DBI::dbDisconnect(fx$con, shutdown = TRUE), add = TRUE)
  s <- plp_fixture_settings()
  fit <- suppressMessages(PatientLevelPrediction::runPlp(
    plpData = fx$plp, outcomeId = 2, analysisId = "deep_smoke", analysisName = "deep smoke",
    populationSettings = s$population, splitSettings = s$split,
    sampleSettings = PatientLevelPrediction::createSampleSettings(),
    featureEngineeringSettings = PatientLevelPrediction::createFeatureEngineeringSettings(),
    preprocessSettings = PatientLevelPrediction::createPreprocessSettings(minFraction = 0, normalize = TRUE,
                                                                          removeRedundancy = FALSE),
    modelSettings = deep("setMultiLayerPerceptron")(
      numLayers = 1, sizeHidden = 16, dropout = 0.1, sizeEmbedding = 16,
      estimatorSettings = deep("setEstimator")(learningRate = 0.01, batchSize = 64, epochs = 15,
                                                                   device = "cpu", seed = 1),
      hyperParamSearch = "random", randomSample = 1, randomSampleSeed = 1),
    logSettings = PatientLevelPrediction::createLogSettings(verbosity = "ERROR", logName = "deep_smoke"),
    executeSettings = s$execute, saveDirectory = tempfile("deep_arrow_")))
  st <- fit$performanceEvaluation$evaluationStatistics
  auc <- as.numeric(st$value[st$evaluation == "Test" & st$metric == "AUROC"])
  expect_length(auc, 1)                                     # a failed python fit leaves no metrics at all
  expect_gt(auc, 0.6)
})

test_that("an omop-duck-db file opens through OmopOnDuckDB::duckdbSource and our functions run on its connection", {
  testthat::skip_if_not_installed("OmopOnDuckDB")
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  src <- suppressMessages(omop_on_duckdb("duckdbSource")(con = db))
  expect_s3_class(src, "duckdb_cdm")
  expect_true(inherits(src$con, "duckdb_connection"))
  on.exit(DBI::dbDisconnect(src$con, shutdown = TRUE), add = TRUE)
  expect_true(all(c("person", "measurement", "visit_occurrence") %in% DBI::dbListTables(src$con)))
  DBI::dbExecute(src$con, paste0("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ",
                                 "ethnicity_concept_id, person_source_value) VALUES (1, 8507, 1950, 8527, 38003563, 'P1')"))
  DBI::dbExecute(src$con, paste0("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, ",
                                 "measurement_type_concept_id, value_as_number, unit_source_value) ",
                                 "VALUES (1, 1, 0, DATE '2020-01-01', 32817, 7500, '/uL')"))
  rep <- harmonize_measurement_units(src$con, list(wbc = "6690-2"))
  expect_s3_class(rep, "data.frame")
  out <- filter_bounds(data.frame(wbc = c(7.5, 226750000)))
  expect_equal(is.na(out$wbc), c(FALSE, TRUE))
})
