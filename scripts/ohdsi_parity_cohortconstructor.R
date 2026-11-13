# Runs OHDSI CohortConstructor (through CDMConnector / omopgenerics) on a synthetic DuckDB CDM built by omop-duck-db and
# compares what it builds with the package's own cohort code and a direct SQL count:
#   * conceptCohort()      -> entries / subjects equal  execute_circe_cohort() on the equivalent Circe definition
#   * demographicsCohort()   -> subjects equal a direct SQL count of adults
#
# No Athena vocabulary is needed: the fixture carries its own concepts and a self-row in concept_ancestor.
# Needs: CohortConstructor, CDMConnector, omopgenerics, CirceR (Java), omopduckdb.
#   Rscript scripts/ohdsi_parity_cohortconstructor.R        (exit status 1 on any mismatch)
suppressMessages({library(omopduckdb); library(CohortConstructor); library(CDMConnector); library(dplyr)})
set.seed(20261113)

db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
DBI::dbExecute(con, "INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES
  (8507, 'MALE', 'Gender', 'Gender', 'Gender', 'S', 'M', DATE '1970-01-01', DATE '2099-12-31'),
  (8532, 'FEMALE', 'Gender', 'Gender', 'Gender', 'S', 'F', DATE '1970-01-01', DATE '2099-12-31'),
  (201826, 'Type 2 diabetes mellitus', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '44054006', DATE '1970-01-01', DATE '2099-12-31'),
  (320128, 'Essential hypertension', 'Condition', 'SNOMED', 'Clinical Finding', 'S', '59621000', DATE '1970-01-01', DATE '2099-12-31')")
DBI::dbExecute(con, "INSERT INTO concept_ancestor VALUES (201826, 201826, 0, 0), (320128, 320128, 0, 0)")
DBI::dbExecute(con, "INSERT INTO vocabulary (vocabulary_id, vocabulary_name, vocabulary_concept_id) VALUES ('None', 'None', 0)")
DBI::dbExecute(con, "INSERT INTO cdm_source (cdm_source_name, cdm_source_abbreviation, cdm_holder, source_description, cdm_etl_reference, source_release_date, cdm_release_date, cdm_version, cdm_version_concept_id, vocabulary_version) VALUES ('synthetic', 'syn', 'x', 'x', 'x', DATE '2024-01-01', DATE '2024-01-01', 'v5.4', 756265, 'v5.0')")

n <- 1500L
yob <- sample(1950:2012, n, TRUE); mob <- sample(1:12, n, TRUE); dob <- sample(1:28, n, TRUE)
duckdb::duckdb_register(con, "_p", data.frame(person_id = seq_len(n), gender_concept_id = sample(c(8507L, 8532L), n, TRUE),
                                              year_of_birth = yob, month_of_birth = mob, day_of_birth = dob,
                                              race_concept_id = 0L, ethnicity_concept_id = 0L))
DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, month_of_birth, day_of_birth, race_concept_id, ethnicity_concept_id) SELECT * FROM _p")
DBI::dbExecute(con, "INSERT INTO observation_period SELECT person_id, person_id, DATE '2005-01-01', DATE '2023-12-31', 32817 FROM _p")
k <- 1200L
cond <- data.frame(person_id = sample(n, k, TRUE), concept_id = sample(c(201826L, 320128L), k, TRUE),
                   d = as.Date("2010-01-01") + sample(0:4000, k, TRUE))
duckdb::duckdb_register(con, "_c", cond)
DBI::dbExecute(con, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_start_datetime, condition_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817 FROM _c")
DBI::dbDisconnect(con, shutdown = TRUE)

con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
cdm <- CDMConnector::cdmFromCon(con, cdmSchema = "main", writeSchema = "main", cdmName = "synthetic")

# ---- CohortConstructor ------------------------------------------------------------------------------------------------
cdm$t2dm <- conceptCohort(cdm, name = "t2dm", conceptSet = list(t2dm = 201826L), exit = "event_end_date")
cc_t2dm <- cdm$t2dm |> collect()
cdm$adults <- demographicsCohort(cdm, name = "adults", ageRange = list(c(18, 150)))
cc_adults_subjects <- cdm$adults |> collect() |> pull(subject_id) |> unique() |> length()

# ---- package / direct SQL truth ---------------------------------------------------------------------------------------
circe <- list(ConceptSets = list(list(id = 0, name = "T2DM", expression = list(items = list(list(
                concept = list(CONCEPT_ID = 201826, CONCEPT_NAME = "T2DM", DOMAIN_ID = "Condition", VOCABULARY_ID = "SNOMED",
                               CONCEPT_CLASS_ID = "Clinical Finding", STANDARD_CONCEPT = "S", CONCEPT_CODE = "44054006"),
                isExcluded = FALSE, includeDescendants = FALSE, includeMapped = FALSE))))),
              PrimaryCriteria = list(CriteriaList = list(list(ConditionOccurrence = list(CodesetId = 0))),
                                     ObservationWindow = list(PriorDays = 0, PostDays = 0), PrimaryCriteriaLimit = list(Type = "All")),
              QualifiedLimit = list(Type = "All"), ExpressionLimit = list(Type = "All"), InclusionRules = list(),
              EndStrategy = list(DateOffset = list(DateField = "StartDate", Offset = 0)), CensoringCriteria = list(),
              CollapseSettings = list(CollapseType = "ERA", EraPad = 0), CensorWindow = structure(list(), names = character(0)))
native <- execute_circe_cohort(con, jsonlite::toJSON(circe, auto_unbox = TRUE, null = "null"), target_cohort_id = 1L,
                               cohort_table = "native_cohort")
sql_n <- function(q) as.integer(DBI::dbGetQuery(con, q)$n)
# a person is 18+ on the first day of observation if they are at least 18 completed years old that day
adults_truth <- sql_n("SELECT COUNT(DISTINCT p.person_id) AS n FROM person p JOIN observation_period o USING (person_id)
  WHERE date_sub('year', make_date(p.year_of_birth, p.month_of_birth, p.day_of_birth), DATE '2023-12-31') >= 18")

checks <- c(
  `conceptCohort subjects == Circe subjects` = length(unique(cc_t2dm$subject_id)) == length(unique(native$subject_id)),
  `conceptCohort distinct subject/start pairs == Circe` =
    nrow(distinct(cc_t2dm, subject_id, cohort_start_date)) == nrow(distinct(native, subject_id, cohort_start_date)),
  `demographicsCohort(18+) subjects == direct SQL (completed years, last observed day)` = cc_adults_subjects == adults_truth
)
cat(sprintf("conceptCohort: %d subjects, Circe: %d; demographicsCohort: %d, SQL: %d\n", length(unique(cc_t2dm$subject_id)),
            length(unique(native$subject_id)), cc_adults_subjects, adults_truth))
print(data.frame(check = names(checks), ok = unname(checks)), row.names = FALSE)
DBI::dbDisconnect(con, shutdown = TRUE)
if (!all(checks)) quit(status = 1)
cat("All CohortConstructor parity checks passed.\n")
