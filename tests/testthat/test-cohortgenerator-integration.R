# Real HADES CohortGenerator execution of cohort_definition_set() on a tiny DuckDB CDM (v0.8.7).
# Optional: skipped unless OMOP_RUN_HADES_TESTS=true and CohortGenerator, DatabaseConnector (with its DuckDB driver) and Java are available.

test_that("CohortGenerator::generateCohortSet runs cohort_definition_set() on a DuckDB CDM", {
  skip_on_cran()
  if (!identical(Sys.getenv("OMOP_RUN_HADES_TESTS"), "true")) skip("set OMOP_RUN_HADES_TESTS=true (the HADES integration workflow does)")
  skip_if_not_installed("CohortGenerator")
  skip_if_not_installed("DatabaseConnector")
  skip_if_not_installed("duckdb")
  skip_if_not_installed("DBI")
  d <- testthat::test_path("..", "..", "cohorts")
  if (!file.exists(file.path(d, "target_cohort.sql"))) skip("cohorts/ is not part of the installed package")

  db <- tempfile(fileext = ".duckdb")
  con <- DBI::dbConnect(duckdb::duckdb(), db)
  DBI::dbExecute(con, "CREATE TABLE person (person_id BIGINT, year_of_birth INTEGER)")
  DBI::dbExecute(con, "CREATE TABLE observation_period (person_id BIGINT, observation_period_start_date DATE, observation_period_end_date DATE)")
  DBI::dbExecute(con, "CREATE TABLE visit_occurrence (visit_occurrence_id BIGINT, person_id BIGINT, visit_concept_id INTEGER, visit_start_date DATE, visit_end_date DATE, discharged_to_concept_id INTEGER)")
  for (t in c("death", "measurement", "condition_occurrence", "drug_exposure"))
    DBI::dbExecute(con, sprintf("CREATE TABLE %s (person_id BIGINT, %s DATE)", t,
                                c(death = "death_date", measurement = "measurement_date",
                                  condition_occurrence = "condition_start_date", drug_exposure = "drug_exposure_start_date")[[t]]))
  DBI::dbExecute(con, "INSERT INTO person VALUES (1,1980),(2,1980),(3,2015),(4,1980),(5,1980)")
  DBI::dbExecute(con, "INSERT INTO observation_period SELECT person_id, DATE '2015-01-01', CASE WHEN person_id = 4 THEN DATE '2020-02-01' ELSE DATE '2020-12-31' END FROM person")
  DBI::dbExecute(con, paste("INSERT INTO visit_occurrence VALUES",
    "(1,1,9201,DATE '2020-03-01',DATE '2020-03-05',NULL),(2,1,9201,DATE '2020-02-01',DATE '2020-02-04',NULL),",
    "(3,2,9201,DATE '2020-03-01',DATE '2020-03-05',4216643),(4,3,9201,DATE '2020-03-01',DATE '2020-03-05',NULL),",
    "(5,4,9201,DATE '2020-01-10',DATE '2020-01-20',NULL),(6,5,262,DATE '2020-03-01',DATE '2020-03-05',NULL),",
    "(7,1,9203,DATE '2020-01-01',DATE '2020-01-01',NULL)"))
  DBI::dbDisconnect(con, shutdown = TRUE)

  cd <- tryCatch(DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db), error = function(e) NULL)
  if (is.null(cd)) skip("DatabaseConnector DuckDB driver is not available")
  conn <- tryCatch(DatabaseConnector::connect(cd), error = function(e) skip(paste("cannot connect:", conditionMessage(e))))
  on.exit(DatabaseConnector::disconnect(conn), add = TRUE)

  cds <- cohort_definition_set(d)
  tables <- CohortGenerator::getCohortTableNames("cohort")
  CohortGenerator::createCohortTables(connection = conn, cohortDatabaseSchema = "main", cohortTableNames = tables)
  CohortGenerator::generateCohortSet(connection = conn, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main",
                                     cohortTableNames = tables, cohortDefinitionSet = cds,
                                     stopOnError = TRUE, incremental = FALSE)
  res <- DatabaseConnector::querySql(conn, "SELECT cohort_definition_id, subject_id, cohort_start_date FROM main.cohort ORDER BY 1, 2")
  names(res) <- tolower(names(res))
  tgt <- res[res$cohort_definition_id == 1, ]
  expect_equal(tgt$subject_id, c(1, 5))
  expect_equal(format(as.Date(tgt$cohort_start_date)), c("2020-02-04", "2020-03-05"))
  expect_equal(sum(res$cohort_definition_id == 2), 6L)   # same as the Python / PostgreSQL / SQL Server runs
})
