# Runs CohortDiagnostics and PatientLevelPrediction on a synthetic DuckDB CDM, with a custom covariate written by
# write_custom_covariates(), and compares what the official tools report with the package's own numbers:
#   * CohortDiagnostics cohort counts   vs  execute_circe_cohort() / CohortGenerator counts
#   * PLP study population and outcomes vs  a direct SQL count of first inpatient visits in the risk window
#   * PLP covariate extraction          vs  the custom covariate values that were written
#   * runPlp (LASSO) completes on the DuckDB-backed data
#
# Needs: the vocabulary DuckDB (read-only; OMOP_VOCAB_DUCKDB), CirceR (Java), CohortGenerator, DatabaseConnector,
# CohortDiagnostics, PatientLevelPrediction, FeatureExtraction, omopduckdb.
#   Rscript scripts/ohdsi_parity_plp_cohortdiagnostics.R
suppressMessages(library(omopduckdb))
vocab <- Sys.getenv("OMOP_VOCAB_DUCKDB", "tasks/central_vocabulary.duckdb")
stopifnot(file.exists(vocab))
set.seed(20260428)

target_json_path <- if (file.exists("cohorts/diabetes_phenotype.circe.json")) "cohorts/diabetes_phenotype.circe.json" else system.file("cohorts", "diabetes_phenotype.circe.json", package = "omopduckdb")
target_json <- paste(readLines(target_json_path, warn = FALSE), collapse = "\n")
outcome <- list(ConceptSets = list(list(id = 0, name = "Inpatient visit", expression = list(items = list(list(
                  concept = list(CONCEPT_ID = 9201, CONCEPT_NAME = "Inpatient Visit", DOMAIN_ID = "Visit", VOCABULARY_ID = "Visit",
                                 CONCEPT_CLASS_ID = "Visit", STANDARD_CONCEPT = "S", CONCEPT_CODE = "IP"),
                  isExcluded = FALSE, includeDescendants = FALSE, includeMapped = FALSE))))),
                PrimaryCriteria = list(CriteriaList = list(list(VisitOccurrence = list(CodesetId = 0))), ObservationWindow = list(PriorDays = 0, PostDays = 0),
                                       PrimaryCriteriaLimit = list(Type = "All")),
                QualifiedLimit = list(Type = "All"), ExpressionLimit = list(Type = "All"), InclusionRules = list(),
                EndStrategy = list(DateOffset = list(DateField = "StartDate", Offset = 0)), CensoringCriteria = list(),
                CollapseSettings = list(CollapseType = "ERA", EraPad = 0), CensorWindow = structure(list(), names = character(0)))
outcome_json <- jsonlite::toJSON(outcome, auto_unbox = TRUE, null = "null", pretty = TRUE)

# --- fixture: concepts from the vocabulary, a random population, a custom covariate ----------------------------------------
db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
DBI::dbExecute(con, sprintf("ATTACH '%s' AS vocab (READ_ONLY)", normalizePath(vocab, winslash = "/")))
d <- jsonlite::fromJSON(target_json, simplifyVector = FALSE)
items <- unique(unlist(lapply(d$ConceptSets, function(s) vapply(s$expression$items, function(i) as.numeric(i$concept$CONCEPT_ID), numeric(1)))))
DBI::dbExecute(con, sprintf("CREATE TEMP TABLE _items AS SELECT unnest([%s]) AS id", paste(sprintf("%.0f", c(items, 9201)), collapse = ",")))
DBI::dbExecute(con, "INSERT INTO concept_ancestor SELECT ca.* FROM vocab.concept_ancestor ca WHERE ca.ancestor_concept_id IN (SELECT id FROM _items)")
DBI::dbExecute(con, "INSERT INTO concept SELECT c.* FROM vocab.concept c WHERE c.concept_id IN (SELECT descendant_concept_id FROM concept_ancestor UNION SELECT id FROM _items)")
n <- 1500L
duckdb::duckdb_register(con, "_p", data.frame(person_id = seq_len(n), gender_concept_id = sample(c(8507L, 8532L), n, TRUE),
                                              year_of_birth = sample(1935:2000, n, TRUE), race_concept_id = 0L, ethnicity_concept_id = 0L))
DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) SELECT * FROM _p")
DBI::dbExecute(con, "INSERT INTO observation_period SELECT person_id, person_id, DATE '2008-01-01', DATE '2023-12-31', 32817 FROM _p")
rand_date <- function(k) as.Date("2012-01-01") + sample(0:3000, k, TRUE)
pool <- function(dom) DBI::dbGetQuery(con, sprintf("SELECT concept_id FROM concept WHERE domain_id = '%s'", dom))$concept_id
ins <- function(df, sql) { nm <- paste0("_t", sample(1e6, 1)); duckdb::duckdb_register(con, nm, df); DBI::dbExecute(con, gsub("@t", nm, sql, fixed = TRUE)); duckdb::duckdb_unregister(con, nm) }
dm_people <- sample(n, 700)
cond <- data.frame(person_id = dm_people, concept_id = sample(pool("Condition"), length(dm_people), TRUE), d = rand_date(length(dm_people)))
meas <- data.frame(person_id = sample(n, 900, TRUE), concept_id = sample(pool("Measurement"), 900, TRUE), d = rand_date(900), v = round(runif(900, 4.5, 10), 1))
vis <- data.frame(person_id = sample(n, 5000, TRUE), concept_id = 9201L, d = rand_date(5000))
ins(cond, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_start_datetime, condition_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817 FROM @t")
ins(meas, "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, measurement_type_concept_id, value_as_number) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817, v FROM @t")
ins(vis, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_start_datetime, visit_end_date, visit_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), d, 32817 FROM @t")
# the custom covariate: an alternative definition of a feature, stored under its own concept id
feat <- data.frame(person_id = seq_len(n), date = as.Date("2012-01-01"), feature = "frailty_alt", value = round(runif(n, 0, 10), 1))
write_custom_covariates(con, feat, list(frailty_alt = c(2000200002, "Frailty score, alternative definition")))
DBI::dbExecute(con, "DETACH vocab")

native <- execute_circe_cohort(con, target_json, target_cohort_id = 1L, cohort_table = "native_cohort")
n_target_native <- length(unique(native$subject_id))
DBI::dbExecute(con, "CHECKPOINT")
DBI::dbDisconnect(con, shutdown = TRUE)

# --- official engine + CohortDiagnostics -----------------------------------------------------------------------------------
cd <- DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path)
defs <- data.frame(cohortId = c(1L, 2L), cohortName = c("target", "outcome"), json = c(target_json, as.character(outcome_json)), stringsAsFactors = FALSE)
defs$sql <- vapply(defs$json, function(j) CirceR::buildCohortQuery(CirceR::cohortExpressionFromJson(j), CirceR::createGenerateOptions(generateStats = TRUE)), character(1))
defs$cohortName <- as.character(defs$cohortName)
tables <- CohortGenerator::getCohortTableNames("cohort")
conn <- DatabaseConnector::connect(cd)
CohortGenerator::createCohortTables(connection = conn, cohortDatabaseSchema = "main", cohortTableNames = tables)
DatabaseConnector::disconnect(conn)
suppressMessages(CohortGenerator::generateCohortSet(connectionDetails = cd, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main",
                                                    cohortTableNames = tables, cohortDefinitionSet = defs))
conn <- DatabaseConnector::connect(cd)
cg <- DatabaseConnector::querySql(conn, "SELECT cohort_definition_id AS id, count(*) AS rows, count(DISTINCT subject_id) AS people FROM cohort GROUP BY 1 ORDER BY 1")
DatabaseConnector::disconnect(conn)
names(cg) <- tolower(names(cg))

export <- tempfile("cd_")
diag_ok <- tryCatch({
  suppressMessages(CohortDiagnostics::executeDiagnostics(
    cohortDefinitionSet = defs[, c("cohortId", "cohortName", "json", "sql")], exportFolder = export, databaseId = "synthetic",
    connectionDetails = cd, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main", cohortTableNames = tables,
    vocabularyDatabaseSchema = "main", minCellCount = 0, runInclusionStatistics = TRUE, runIncludedSourceConcepts = FALSE,
    runOrphanConcepts = FALSE, runTimeSeries = FALSE, runVisitContext = FALSE, runBreakdownIndexEvents = FALSE,
    runIncidenceRate = FALSE, runCohortRelationship = FALSE, runTemporalCohortCharacterization = FALSE, incremental = FALSE))
  TRUE
}, error = function(e) { cat("CohortDiagnostics::executeDiagnostics failed:", conditionMessage(e), "\n"); FALSE })
if (diag_ok) {
  cc <- read.csv(file.path(export, "cohort_count.csv"))
  names(cc) <- tolower(names(cc))
  cat("\nCohortDiagnostics cohort_count.csv:\n"); print(cc[, intersect(c("cohort_id", "cohort_entries", "cohort_subjects"), names(cc))], row.names = FALSE)
}
cat("\nCohortGenerator counts:\n"); print(cg, row.names = FALSE)
cat(sprintf("\nnative target cohort: %d people; CohortGenerator target: %d people -> %s\n", n_target_native, cg$people[cg$id == 1],
            if (n_target_native == cg$people[cg$id == 1]) "identical" else "DIFFERENT"))
if (diag_ok) cat(sprintf("CohortDiagnostics target subjects: %d -> %s\n", cc$cohort_subjects[cc$cohort_id == 1],
                         if (cc$cohort_subjects[cc$cohort_id == 1] == cg$people[cg$id == 1]) "identical" else "DIFFERENT"))

# --- PatientLevelPrediction -------------------------------------------------------------------------------------------------
plp_ok <- tryCatch({
  dd <- plp_database_details(db_path, target_id = 1, outcome_ids = 2, cohort_table = "cohort", integer64 = "never")
  cs <- list(FeatureExtraction::createCovariateSettings(useDemographicsGender = TRUE, useDemographicsAgeGroup = TRUE),
             custom_covariate_settings(2000200002, long_term_start_days = -3650))
  plp_data <- PatientLevelPrediction::getPlpData(databaseDetails = dd, covariateSettings = cs,
                                                 restrictPlpDataSettings = PatientLevelPrediction::createRestrictPlpDataSettings())
  pop <- PatientLevelPrediction::createStudyPopulation(plpData = plp_data, outcomeId = 2, populationSettings = PatientLevelPrediction::createStudyPopulationSettings(
    riskWindowStart = 1, riskWindowEnd = 365, requireTimeAtRisk = FALSE, removeSubjectsWithPriorOutcome = FALSE, firstExposureOnly = TRUE, washoutPeriod = 0))
  # the same count in SQL: first inpatient visit 1-365 days after the target entry, per person (first target entry per person)
  conn <- DatabaseConnector::connect(cd)
  direct <- DatabaseConnector::querySql(conn, "
    WITH t AS (SELECT subject_id, min(cohort_start_date) AS s FROM cohort WHERE cohort_definition_id = 1 GROUP BY 1)
    SELECT count(DISTINCT t.subject_id) AS n FROM t JOIN cohort o ON o.subject_id = t.subject_id AND o.cohort_definition_id = 2
     AND o.cohort_start_date >= t.s + 1 AND o.cohort_start_date <= t.s + 365")
  DatabaseConnector::disconnect(conn)
  cov <- as.data.frame(plp_data$covariateData$covariateRef)
  got_custom <- any(cov$conceptId == 2000200002 | grepl("Frailty", cov$covariateName))
  cat(sprintf("\nPLP study population: %d people, %d with the outcome; direct SQL: %d -> %s\n", nrow(pop), sum(pop$outcomeCount > 0), direct[[1]],
              if (sum(pop$outcomeCount > 0) == direct[[1]]) "identical" else "DIFFERENT"))
  cat(sprintf("PLP extracted the custom covariate: %s\n", got_custom))
  res <- tryCatch(suppressMessages(PatientLevelPrediction::runPlp(plpData = plp_data, outcomeId = 2,
        populationSettings = PatientLevelPrediction::createStudyPopulationSettings(riskWindowStart = 1, riskWindowEnd = 365, requireTimeAtRisk = FALSE, firstExposureOnly = TRUE),
        splitSettings = PatientLevelPrediction::createDefaultSplitSetting(testFraction = 0.25, nfold = 2, splitSeed = 1),
        modelSettings = PatientLevelPrediction::setLassoLogisticRegression(seed = 1), saveDirectory = tempfile("plp_"), logSettings = PatientLevelPrediction::createLogSettings(verbosity = "FATAL"))),
        error = function(e) { cat("runPlp failed:", conditionMessage(e), "\n"); NULL })
  if (!is.null(res)) cat(sprintf("runPlp (LASSO) finished; test AUC %.3f\n", res$performanceEvaluation$evaluationStatistics$value[res$performanceEvaluation$evaluationStatistics$metric == "AUROC" & res$performanceEvaluation$evaluationStatistics$evaluation == "Test"][1]))
  TRUE
}, error = function(e) { cat("PLP failed:", conditionMessage(e), "\n"); FALSE })
unlink(db_path)
