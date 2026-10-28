# NOTE: if you use CohortGenerator::generateCohortSet() instead of run_sql(), pass generateStats = FALSE (no #InclusionRules tables).
# Example: run the portable eDERRI cohorts and a PatientLevelPrediction readmission model (FR-D1).
# Writes the cohort table into the CDM schema, so the database must be writable. For a read-only CDM use
# run_plp_readonly_example.R. Works on any OMOP CDM that DatabaseConnector can reach; DuckDB is only the example.
library(DatabaseConnector)
library(SqlRender)
library(PatientLevelPrediction)

# 63-bit visit ids exceed 2^53: keep them exact (FR-C9). omopduckdb::hades_set_integer64() does the same.
options(databaseConnectorInteger64AsNumeric = FALSE)

connectionDetails <- createConnectionDetails(dbms = "duckdb", server = "site.duckdb")
connection <- connect(connectionDetails)
on.exit(disconnect(connection), add = TRUE)

run_sql <- function(file, ...) {
  sql <- readChar(file, file.info(file)$size)
  sql <- render(sql, ...)
  sql <- translate(sql, targetDialect = connectionDetails$dbms)
  executeSql(connection, sql)
}

run_sql("cohorts/target_cohort.sql", cdm_database_schema = "main", target_database_schema = "main",
        target_cohort_table = "cohort", target_cohort_id = 1, followup_days = 30, min_age = 18)
run_sql("cohorts/outcome_cohort.sql", cdm_database_schema = "main", target_database_schema = "main",
        target_cohort_table = "cohort", target_cohort_id = 2)

databaseDetails <- createDatabaseDetails(
  connectionDetails = connectionDetails, cdmDatabaseSchema = "main", cdmDatabaseName = "site",
  cohortDatabaseSchema = "main", cohortTable = "cohort", targetId = 1, outcomeIds = 2,
  outcomeDatabaseSchema = "main", outcomeTable = "cohort")

plpData <- getPlpData(
  databaseDetails = databaseDetails,
  covariateSettings = FeatureExtraction::createCovariateSettings(useDemographicsGender = TRUE, useDemographicsAgeGroup = TRUE,
                                                                  useConditionOccurrenceLongTerm = TRUE, longTermStartDays = -365),
  restrictPlpDataSettings = createRestrictPlpDataSettings())

populationSettings <- createStudyPopulationSettings(
  requireTimeAtRisk = FALSE, riskWindowStart = 1, startAnchor = "cohort start",
  riskWindowEnd = 30, endAnchor = "cohort start", firstExposureOnly = TRUE, removeSubjectsWithPriorOutcome = FALSE)

runPlp(plpData = plpData, outcomeId = 2, populationSettings = populationSettings,
       modelSettings = setLassoLogisticRegression(), saveDirectory = "plp_results")
