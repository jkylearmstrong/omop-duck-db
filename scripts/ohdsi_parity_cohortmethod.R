# Runs OHDSI CohortMethod on a synthetic DuckDB CDM built by omop-duck-db and compares what the official tool reports with
# direct SQL counts and a hand-computed odds ratio:
#   * getDbCohortMethodData()        -> the target / comparator rows it reads equal the cohort table
#   * createStudyPopulation()        -> per-arm subjects and outcomes equal a direct SQL count in the risk window
#   * fitOutcomeModel(logistic)      -> the unadjusted odds ratio equals the 2x2 odds ratio (log-OR within 1e-4, the Cyclops convergence tolerance)
#   * the DuckDB connection comes from cm_database_details(), the package's HADES bridge
#
# No Athena vocabulary is needed: the fixture carries its own handful of concepts.
# Needs: CohortMethod, FeatureExtraction, DatabaseConnector, omopduckdb.
#   Rscript scripts/ohdsi_parity_cohortmethod.R        (exit status 1 on any mismatch)
suppressMessages({library(omopduckdb); library(CohortMethod)})
set.seed(20261113)

db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES
  (8507, 'MALE', 'Gender', 'Gender', 'Gender', 'S', 'M', DATE '1970-01-01', DATE '2099-12-31'),
  (8532, 'FEMALE', 'Gender', 'Gender', 'Gender', 'S', 'F', DATE '1970-01-01', DATE '2099-12-31'),
  (9201, 'Inpatient Visit', 'Visit', 'Visit', 'Visit', 'S', 'IP', DATE '1970-01-01', DATE '2099-12-31'),
  (1000001, 'Drug A', 'Drug', 'RxNorm', 'Ingredient', 'S', 'A', DATE '1970-01-01', DATE '2099-12-31'),
  (1000002, 'Drug B', 'Drug', 'RxNorm', 'Ingredient', 'S', 'B', DATE '1970-01-01', DATE '2099-12-31'),
  (2000001, 'Outcome', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'O', DATE '1970-01-01', DATE '2099-12-31')")

n <- 3000L
sex <- sample(c(8507L, 8532L), n, TRUE)
yob <- sample(1940:1990, n, TRUE)
arm <- rbinom(n, 1, plogis(-0.3 + 0.02 * (yob - 1965)))          # 1 = target (Drug A), 0 = comparator (Drug B)
start <- as.Date("2015-01-01") + sample(0:1500, n, TRUE)
p_out <- plogis(-2.2 + 0.7 * arm + 0.3 * (sex == 8507L))          # true log-OR 0.7 for the target
outc <- rbinom(n, 1, p_out)
out_date <- start + sample(5:200, n, TRUE)                         # inside a 1-365 day risk window

duckdb::duckdb_register(con, "_p", data.frame(person_id = seq_len(n), gender_concept_id = sex, year_of_birth = yob,
                                              race_concept_id = 0L, ethnicity_concept_id = 0L))
DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) SELECT * FROM _p")
DBI::dbExecute(con, "INSERT INTO observation_period SELECT person_id, person_id, DATE '2010-01-01', DATE '2023-12-31', 32817 FROM _p")

# exposures and outcomes live in an OHDSI-format cohort table: 1 = target, 2 = comparator, 3 = outcome
exp_df <- data.frame(cohort_definition_id = ifelse(arm == 1L, 1L, 2L), subject_id = seq_len(n),
                     cohort_start_date = start, cohort_end_date = start + 30L)
out_df <- data.frame(cohort_definition_id = 3L, subject_id = which(outc == 1L),
                     cohort_start_date = out_date[outc == 1L], cohort_end_date = out_date[outc == 1L])
duckdb::duckdb_register(con, "_c", rbind(exp_df, out_df))
DBI::dbExecute(con, "INSERT INTO cohort SELECT * FROM _c")
DBI::dbDisconnect(con, shutdown = TRUE)

# ---- direct SQL / R truth --------------------------------------------------------------------------------------------
truth <- data.frame(arm = arm, outc = as.integer(outc == 1L))
a <- sum(truth$arm == 1 & truth$outc == 1); b <- sum(truth$arm == 1 & truth$outc == 0)
c0 <- sum(truth$arm == 0 & truth$outc == 1); d <- sum(truth$arm == 0 & truth$outc == 0)
or_truth <- (a * d) / (b * c0)

# ---- CohortMethod -----------------------------------------------------------------------------------------------------
det <- cm_database_details(db_path, cohort_table = "cohort")
cov <- FeatureExtraction::createCovariateSettings(useDemographicsGender = TRUE, useDemographicsAgeGroup = TRUE)
cmd <- getDbCohortMethodData(
  connectionDetails = det$connectionDetails, cdmDatabaseSchema = det$cdmDatabaseSchema,
  targetId = 1, comparatorId = 2, outcomeIds = 3,
  exposureDatabaseSchema = det$cohortDatabaseSchema, exposureTable = det$cohortTable,
  outcomeDatabaseSchema = det$cohortDatabaseSchema, outcomeTable = det$cohortTable,
  getDbCohortMethodDataArgs = createGetDbCohortMethodDataArgs(
    removeDuplicateSubjects = "keep all", firstExposureOnly = FALSE, washoutPeriod = 0,
    restrictToCommonPeriod = FALSE, covariateSettings = cov))
sp <- createStudyPopulation(
  cmd, outcomeId = 3,
  createStudyPopulationArgs = createCreateStudyPopulationArgs(
    removeSubjectsWithPriorOutcome = FALSE, minDaysAtRisk = 0, riskWindowStart = 1, startAnchor = "cohort start",
    riskWindowEnd = 365, endAnchor = "cohort start"))
fit <- fitOutcomeModel(sp, fitOutcomeModelArgs = createFitOutcomeModelArgs(
  modelType = "logistic", stratified = FALSE, useCovariates = FALSE))
or_cm <- exp(coef(fit))

checks <- c(
  `study population: target subjects` = sum(sp$treatment == 1) == sum(arm == 1L),
  `study population: comparator subjects` = sum(sp$treatment == 0) == sum(arm == 0L),
  `study population: target outcomes` = sum(sp$outcomeCount[sp$treatment == 1] > 0) == a,
  `study population: comparator outcomes` = sum(sp$outcomeCount[sp$treatment == 0] > 0) == c0,
  `logistic odds ratio equals the 2x2 odds ratio` = abs(log(or_cm) - log(or_truth)) < 1e-4
)
cat(sprintf("CohortMethod odds ratio %.6f, 2x2 odds ratio %.6f\n", or_cm, or_truth))
print(data.frame(check = names(checks), ok = unname(checks)), row.names = FALSE)
if (!all(checks)) quit(status = 1)
cat("All CohortMethod parity checks passed.\n")
