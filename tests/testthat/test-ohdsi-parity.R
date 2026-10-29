library(omopduckdb)

# Parity with the official OHDSI tools on a shared fixture. Each test skips when the tool (or Java) is not available.

test_that("prior utilization counts equal FeatureExtraction's visit-concept counts", {
  skip_if_not_installed("FeatureExtraction")
  skip_if_not_installed("DatabaseConnector")
  db_path <- tempfile(fileext = ".duckdb")
  build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) VALUES (1, 8507, 1960, 0, 0)")
  DBI::dbExecute(con, "INSERT INTO observation_period VALUES (1,1,DATE '2015-01-01',DATE '2022-01-01',0)")
  DBI::dbExecute(con, "DROP TABLE cohort")
  DBI::dbExecute(con, "CREATE TABLE cohort (cohort_definition_id INT, subject_id BIGINT, cohort_start_date DATE, cohort_end_date DATE)")
  DBI::dbExecute(con, "INSERT INTO cohort VALUES (1, 1, DATE '2020-06-01', DATE '2020-06-05')")
  # one visit of each kind in the year before admission, one older, one on the admission day
  v <- list(c(1, 262, "2020-01-10"), c(2, 9203, "2020-02-10"), c(3, 581385, "2020-03-10"), c(4, 9202, "2020-04-10"),
            c(5, 9201, "2020-05-10"), c(6, 9201, "2019-01-10"), c(7, 9201, "2020-06-01"))
  for (x in v) {
    DBI::dbExecute(con, sprintf("INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_end_date, visit_type_concept_id) VALUES (%s, 1, %s, DATE '%s', DATE '%s', 0)", x[1], x[2], x[3], x[3]))
  }
  for (id in c(262, 9203, 581385, 9202, 9201)) {
    DBI::dbExecute(con, sprintf("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES (%d, 'v%d', 'Visit', 'Visit', 'Visit', 'S', 'v%d', DATE '1970-01-01', DATE '2099-12-31')", id, id, id))
  }
  ours <- extract_prior_utilization(con)
  DBI::dbDisconnect(con, shutdown = TRUE)
  on.exit(unlink(db_path), add = TRUE)

  conn <- tryCatch(DatabaseConnector::connect(DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path)),
                   error = function(e) skip(paste("cannot connect:", conditionMessage(e))))
  on.exit(DatabaseConnector::disconnect(conn), add = TRUE, after = FALSE)
  cs <- FeatureExtraction::createCovariateSettings(useVisitConceptCountLongTerm = TRUE, longTermStartDays = -365, endDays = -1)
  cov <- tryCatch(FeatureExtraction::getDbCovariateData(connection = conn, cdmDatabaseSchema = "main", cohortTable = "cohort",
                    cohortDatabaseSchema = "main", cohortIds = 1, rowIdField = "subject_id", covariateSettings = cs, aggregated = FALSE),
                  error = function(e) skip(paste("FeatureExtraction cannot run here:", conditionMessage(e))))
  got <- as.data.frame(cov$covariates)
  n <- function(concept) { v <- got$covariateValue[got$covariateId %/% 1000 == concept]; if (length(v)) v else 0 }
  # window: the 365 days before admission, the admission day excluded (endDays = -1), as extract_prior_utilization
  expect_equal(c(ours$prior_ip_count, ours$prior_ed_count, ours$prior_os_count),
               c(n(9201) + n(262), n(9203), n(9202) + n(581385)))
  expect_equal(ours$prior_ip_count, 2)
})
