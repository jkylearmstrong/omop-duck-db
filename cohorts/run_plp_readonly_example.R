# Example: eDERRI cohorts + PatientLevelPrediction on a READ-ONLY OMOP CDM DuckDB file (N4 / FR-D3).
#
# The CDM file is attached read-only and never written. Cohort rows go into a separate scratch DuckDB file.
# DatabaseConnector cannot ATTACH on connect, and HADES opens several connections per run, so every connection is
# wrapped to run the ATTACH. That wrapper patches DatabaseConnector::connect for this R session only; it is the
# same pattern the consortium runner uses. It has NOT been run against a real read-only file by the package tests.
#
# Vocabulary: site files often expose `concept` as views into a catalog named `central_vocab`. Set `vocab_file` to
# the shared vocabulary file and it is attached READ_ONLY under that name on every connection, so nothing has to be
# copied or materialized. Leave it NULL when the CDM file holds real vocabulary tables.
library(DatabaseConnector)
library(SqlRender)
library(PatientLevelPrediction)

cdm_file <- "site.duckdb"          # read-only CDM
vocab_file <- NULL                 # e.g. "central_vocab.duckdb" when the CDM's concept tables are views into it
scratch_file <- "cohorts_scratch.duckdb"  # writable; holds only the cohort table

# 63-bit ids are exact only as integer64. omopduckdb::plp_database_details() sets this itself when needed
# (integer64 = "auto"); when you build databaseDetails by hand, set it if omopduckdb::hades_needs_integer64(cdm_file).
if (requireNamespace("omopduckdb", quietly = TRUE) && omopduckdb::hades_needs_integer64(cdm_file)) {
  options(databaseConnectorInteger64AsNumeric = FALSE)
}

# --- attach the CDM read-only on every connection -------------------------------------------------------
ns <- asNamespace("DatabaseConnector")
original_connect <- get("connect", envir = ns)
attach_sql <- omopduckdb::readonly_attach_sql(cdm_file, vocab_file)   # one ATTACH ... (READ_ONLY) per file
unlockBinding("connect", ns)
assign("connect", function(...) {
  conn <- original_connect(...)
  for (stmt in attach_sql) executeSql(conn, stmt, progressBar = FALSE, reportOverallTime = FALSE)
  conn
}, envir = ns)
lockBinding("connect", ns)
on.exit({
  unlockBinding("connect", ns); assign("connect", original_connect, envir = ns); lockBinding("connect", ns)
}, add = TRUE)

connectionDetails <- createConnectionDetails(dbms = "duckdb", server = scratch_file)

# --- cohorts: CDM schema is cdm.main (read-only), cohort table lives in the scratch file's main schema -----
connection <- connect(connectionDetails)
on.exit(disconnect(connection), add = TRUE)
executeSql(connection, "CREATE TABLE IF NOT EXISTS main.cohort (cohort_definition_id INTEGER, subject_id BIGINT,
                        cohort_start_date DATE, cohort_end_date DATE)")

run_sql <- function(file, ...) {
  sql <- translate(render(readChar(file, file.info(file)$size), ...), targetDialect = "duckdb")
  executeSql(connection, sql)
}
run_sql("cohorts/target_cohort.sql", cdm_database_schema = "cdm.main", target_database_schema = "main",
        target_cohort_table = "cohort", target_cohort_id = 1, followup_days = 30, min_age = 18)
run_sql("cohorts/outcome_cohort.sql", cdm_database_schema = "cdm.main", target_database_schema = "main",
        target_cohort_table = "cohort", target_cohort_id = 2)

# --- prediction -------------------------------------------------------------------------------------------
databaseDetails <- createDatabaseDetails(
  connectionDetails = connectionDetails, cdmDatabaseSchema = "cdm.main", cdmDatabaseName = "site",
  cohortDatabaseSchema = "main", cohortTable = "cohort", targetId = 1, outcomeIds = 2,
  outcomeDatabaseSchema = "main", outcomeTable = "cohort")

plpData <- getPlpData(
  databaseDetails = databaseDetails,
  covariateSettings = FeatureExtraction::createCovariateSettings(useDemographicsGender = TRUE, useDemographicsAgeGroup = TRUE,
                                                                  useConditionOccurrenceLongTerm = TRUE, longTermStartDays = -365),
  restrictPlpDataSettings = createRestrictPlpDataSettings())

runPlp(plpData = plpData, outcomeId = 2,
       populationSettings = createStudyPopulationSettings(requireTimeAtRisk = FALSE, riskWindowStart = 1, startAnchor = "cohort start",
                                                          riskWindowEnd = 30, endAnchor = "cohort start", firstExposureOnly = TRUE,
                                                          removeSubjectsWithPriorOutcome = FALSE),
       modelSettings = setLassoLogisticRegression(), saveDirectory = "plp_results")

# --- CohortGenerator alternative to run_sql() above ----------------------------------------------------------
# cds <- omopduckdb::cohort_definition_set("cohorts")
# CohortGenerator::createCohortTables(connectionDetails = connectionDetails, cohortDatabaseSchema = "main",
#                                     cohortTableNames = CohortGenerator::getCohortTableNames("cohort"))
# CohortGenerator::generateCohortSet(connectionDetails = connectionDetails, cdmDatabaseSchema = "cdm.main",
#                                    cohortDatabaseSchema = "main", cohortTableNames = CohortGenerator::getCohortTableNames("cohort"),
#                                    cohortDefinitionSet = cds, generateStats = FALSE)  # shipped SQL has no #InclusionRules tables
