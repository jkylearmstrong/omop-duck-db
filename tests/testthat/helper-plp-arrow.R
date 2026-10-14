# On Windows, once R's arrow DLL is loaded, python's pyarrow cannot be imported in the same process ("DLL load
# failed while importing lib"), and DeepPatientLevelPrediction needs pyarrow. Importing pyarrow FIRST avoids it.
if (nzchar(Sys.getenv("RETICULATE_PYTHON")) && requireNamespace("reticulate", quietly = TRUE)) {
  try(reticulate::import("pyarrow.lib"), silent = TRUE)
}

# Shared fixture: n synthetic people with an observation period, creatinine / wbc labs and an outcome driven by
# creatinine, pushed through filter_bounds() -> as_plp_arrow() -> plp_arrow_to_plp_data().
plp_arrow_fixture <- function(n = 400L, seed = 7) {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db_path = db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  set.seed(seed)
  DBI::dbExecute(con, sprintf(paste0(
    "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id, person_source_value) ",
    "SELECT i, 8507, 1950, 8527, 38003563, 'P' || i FROM range(1, %d) t(i)"), n + 1L))
  DBI::dbExecute(con, sprintf(paste0(
    "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, ",
    "observation_period_end_date, period_type_concept_id) SELECT i, i, DATE '2015-01-01', DATE '2022-01-01', 32817 ",
    "FROM range(1, %d) t(i)"), n + 1L))
  creat <- round(stats::rlnorm(n, log(1.1), 0.5), 2)
  wbc <- round(stats::rlnorm(n, log(7), 0.3), 1)
  y <- stats::rbinom(n, 1, stats::plogis(2.2 * (creat - 1.2)))
  feats <- filter_bounds(data.frame(subject_id = seq_len(n), cohort_start_date = as.Date("2020-06-01"),
                                    creatinine = creat, wbc = wbc, y = y))
  arr <- as_plp_arrow(con, feats, outcome_col = "y")
  plp <- plp_arrow_to_plp_data(arr, con, target_id = 1, outcome_id = 2)
  list(con = con, arr = arr, plp = plp, y = y)
}

plp_fixture_settings <- function() {
  list(population = PatientLevelPrediction::createStudyPopulationSettings(
         requireTimeAtRisk = FALSE, riskWindowStart = 1, riskWindowEnd = 365, firstExposureOnly = FALSE,
         washoutPeriod = 0, removeSubjectsWithPriorOutcome = FALSE),
       split = PatientLevelPrediction::createDefaultSplitSetting(
         testFraction = 0.25, trainFraction = 0.75, splitSeed = 1, nfold = 3, type = "stratified"),
       execute = PatientLevelPrediction::createExecuteSettings(
         runSplitData = TRUE, runSampleData = FALSE, runFeatureEngineering = FALSE, runPreprocessData = TRUE,
         runModelDevelopment = TRUE, runCovariateSummary = FALSE))
}
