# Runs the index-stay (target) cohort three ways on one small CDM and prints the people and index dates each returns:
#   1. inst/cohorts/target_cohort.sql  (the authoritative SQL) through CohortGenerator,
#   2. inst/cohorts/target_cohort.circe.json through CirceR + CohortGenerator (the official Circe engine),
#   3. the same JSON through the package's native compiler.
# The official engine and the native compiler must agree; the SQL differs in two documented cases (discharge disposition, and a
# death within the follow-up window when the observation period ends at death). tests/test_target_cohort_circe.py and
# tests/testthat/test-circe-definitions.R pin the numbers this script prints.
#
#   Rscript scripts/ohdsi_parity_target_cohort.R
#
# Needs CirceR (Java), CohortGenerator, DatabaseConnector and omopduckdb; no vocabulary file.
suppressMessages(library(omopduckdb))
db <- tempfile(fileext = ".duckdb")
build_schema(db)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES
  (1,8507,1980,0,0),(2,8507,1980,0,0),(3,8507,2015,0,0),(4,8507,1980,0,0),(5,8507,1980,0,0),(6,8507,2002,0,0),(7,8507,1980,0,0),(8,8507,1980,0,0),(9,8507,1980,0,0)")
DBI::dbExecute(con, "INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id)
  SELECT person_id, person_id, DATE '2015-01-01', CASE WHEN person_id = 4 THEN DATE '2020-02-01' WHEN person_id = 7 THEN DATE '2020-03-20' ELSE DATE '2020-12-31' END, 0 FROM person")
DBI::dbExecute(con, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id, discharged_to_concept_id) VALUES
  (1,1,9201,DATE '2020-03-01',DATE '2020-03-05',0,NULL),(2,1,9201,DATE '2020-02-01',DATE '2020-02-04',0,NULL),
  (3,2,9201,DATE '2020-03-01',DATE '2020-03-05',0,4216643),(4,3,9201,DATE '2020-03-01',DATE '2020-03-05',0,NULL),
  (5,4,9201,DATE '2020-01-10',DATE '2020-01-20',0,NULL),(6,5,262,DATE '2020-03-01',DATE '2020-03-05',0,NULL),
  (7,1,9203,DATE '2020-01-01',DATE '2020-01-01',0,NULL),(8,6,9201,DATE '2019-12-28',DATE '2020-01-03',0,NULL),
  (9,7,9201,DATE '2020-03-01',DATE '2020-03-05',0,NULL),(10,8,9201,DATE '2020-03-01',DATE '2020-03-05',0,NULL),
  (11,9,9201,DATE '2020-01-10',DATE '2020-01-20',0,NULL),(12,9,9201,DATE '2020-05-01',DATE '2020-05-05',0,NULL)")
DBI::dbExecute(con, "INSERT INTO death (person_id, death_date, death_type_concept_id) VALUES (7, DATE '2020-03-20', 0), (8, DATE '2020-03-03', 0), (9, DATE '2020-01-20', 0)")
# Circe resolves concept sets through the concept table
DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date)
  VALUES (9201,'Inpatient Visit','Visit','Visit','Visit','S','IP',DATE '1970-01-01',DATE '2099-12-31'),(262,'Emergency Room and Inpatient Visit','Visit','Visit','Visit','S','ERIP',DATE '1970-01-01',DATE '2099-12-31')")
DBI::dbDisconnect(con, shutdown = TRUE)

cohort_dir <- omopduckdb::cohort_definitions_dir()
json <- paste(readLines(file.path(cohort_dir, "target_cohort.circe.json"), warn = FALSE), collapse = "\n")
native <- {
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
  execute_circe_cohort(con, json, target_cohort_id = 22, cohort_table = "native_cohort")
}

set <- cohort_definition_set(cohort_dir)
sql_row <- set[set$cohortId == 1, ]
sql <- CirceR::buildCohortQuery(CirceR::cohortExpressionFromJson(json), CirceR::createGenerateOptions(generateStats = FALSE))
circe_row <- sql_row
circe_row$cohortId <- 21L; circe_row$cohortName <- "target cohort, Circe"; circe_row$json <- json; circe_row$sql <- sql
defs <- rbind(sql_row, circe_row)

cd <- DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db)
tables <- CohortGenerator::getCohortTableNames("cohort_cg")
conn <- DatabaseConnector::connect(cd)
CohortGenerator::createCohortTables(connection = conn, cohortDatabaseSchema = "main", cohortTableNames = tables)
DatabaseConnector::disconnect(conn)
suppressMessages(CohortGenerator::generateCohortSet(connectionDetails = cd, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main",
                                                    cohortTableNames = tables, cohortDefinitionSet = defs, stopOnError = TRUE, incremental = FALSE))
conn <- DatabaseConnector::connect(cd)
res <- DatabaseConnector::querySql(conn, "SELECT cohort_definition_id, subject_id, cohort_start_date FROM cohort_cg ORDER BY 1, 2, 3")
DatabaseConnector::disconnect(conn)
names(res) <- tolower(names(res))
show <- function(x) paste(sprintf("%d@%s", x$subject_id, format(as.Date(x$cohort_start_date))), collapse = "  ")
cat("\nauthoritative SQL    :", show(res[res$cohort_definition_id == 1, ]), "\n")
cat("official Circe engine:", show(res[res$cohort_definition_id == 21, ]), "\n")
cat("native compiler      :", show(native), "\n")
same <- identical(sort(sprintf("%d@%s", res$subject_id[res$cohort_definition_id == 21], format(as.Date(res$cohort_start_date[res$cohort_definition_id == 21])))),
                  sort(sprintf("%d@%s", native$subject_id, format(as.Date(native$cohort_start_date)))))
cat(sprintf("\nnative compiler == official Circe engine: %s\n", same))
unlink(db)
if (!same) quit(status = 1)
