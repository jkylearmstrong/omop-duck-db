# A small CDM with the awkward index-stay cases (children, deaths around discharge, short observation, two stays), shared by the
# Circe target-cohort and cohort-reconciliation tests.
target_cdm <- function(env = parent.frame()) {
  db <- tempfile(fileext = ".duckdb")
  build_schema(db)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
  withr::defer({ DBI::dbDisconnect(con, shutdown = TRUE); unlink(db) }, envir = env)
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
  con
}

# The index-stay cohort (inst/cohorts/target_cohort.circe.json, built in Python by target_cohort_circe()) as an R list. Built here, not read
# from the file, so the tests do not depend on where they run from; a test checks that it matches the shipped file.
target_definition <- function(min_age = 18, followup_days = 30) {
  empty <- structure(list(), names = character(0))
  item <- function(id, name, code) list(concept = list(CONCEPT_ID = id, CONCEPT_NAME = name, DOMAIN_ID = "Visit", VOCABULARY_ID = "Visit",
                                                       CONCEPT_CLASS_ID = "Visit", STANDARD_CONCEPT = "S", CONCEPT_CODE = code),
                                        isExcluded = FALSE, includeDescendants = FALSE, includeMapped = FALSE)
  stay <- list(CodesetId = 0, Age = list(Value = min_age, Op = "gte"),
               DateAdjustment = list(StartWith = "END_DATE", StartOffset = 0, EndWith = "END_DATE", EndOffset = 0),
               CorrelatedCriteria = list(Type = "ALL", DemographicCriteriaList = list(), Groups = list(), CriteriaList = list(list(
                 Criteria = list(Death = empty), StartWindow = list(Start = list(Coeff = -1), End = list(Days = 0, Coeff = 1)),
                 Occurrence = list(Type = 0, Count = 0)))))
  list(ConceptSets = list(list(id = 0, name = "Index stay visit types",
                               expression = list(items = list(item(9201, "Inpatient Visit", "IP"), item(262, "Emergency Room and Inpatient Visit", "ERIP"))))),
       PrimaryCriteria = list(CriteriaList = list(list(VisitOccurrence = stay)), ObservationWindow = list(PriorDays = 0, PostDays = followup_days),
                              PrimaryCriteriaLimit = list(Type = "First")),
       QualifiedLimit = list(Type = "First"), ExpressionLimit = list(Type = "First"), InclusionRules = list(),
       EndStrategy = list(DateOffset = list(DateField = "StartDate", Offset = 0)), CensoringCriteria = list(),
       CollapseSettings = list(CollapseType = "ERA", EraPad = 0), CensorWindow = empty, cdmVersionRange = ">=5.3.0")
}
