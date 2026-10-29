# Runs small Circe definitions through the official engine (CirceR + CohortGenerator) and the package's native compiler on one fixture and
# prints who is in each cohort, so that every construct the compiler evaluates (First, AdditionalCriteria, Unit and source-concept
# attributes, AT_LEAST / AT_MOST and nested groups) is checked against the engine it must agree with. tests/test_circe_constructs.py and
# tests/testthat/test-circe-definitions.R pin the answers this script printed.
#
#   Rscript scripts/ohdsi_parity_constructs.R
#
# Needs CirceR (Java), CohortGenerator, DatabaseConnector and omopduckdb; no vocabulary file.
# Runs small Circe definitions through the official engine (CirceR + CohortGenerator) and the native compiler on one fixture and prints
# who is in each cohort. Used to settle the semantics of constructs before implementing them natively.
suppressMessages(library(omopduckdb))
db <- tempfile(fileext = ".duckdb")
build_schema(db)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
x <- function(sql) DBI::dbExecute(con, sql)
x("INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) SELECT i, 8507, 1970, 0, 0 FROM range(1, 11) t(i)")
x("INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date, observation_period_end_date, period_type_concept_id) SELECT person_id, person_id, DATE '2015-01-01', DATE '2022-12-31', 0 FROM person")
cond <- function(pid, cid, d) x(sprintf("INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_type_concept_id) VALUES ((SELECT coalesce(max(condition_occurrence_id),0)+1 FROM condition_occurrence), %d, %d, DATE '%s', 0)", pid, cid, d))
proc <- function(pid, cid, src, d) x(sprintf("INSERT INTO procedure_occurrence (procedure_occurrence_id, person_id, procedure_concept_id, procedure_date, procedure_type_concept_id, procedure_source_concept_id) VALUES ((SELECT coalesce(max(procedure_occurrence_id),0)+1 FROM procedure_occurrence), %d, %d, DATE '%s', 0, %d)", pid, cid, d, src))
meas <- function(pid, unit, d) x(sprintf("INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_type_concept_id, value_as_number, unit_concept_id) VALUES ((SELECT coalesce(max(measurement_id),0)+1 FROM measurement), %d, 3004410, DATE '%s', 0, 7.0, %d)", pid, d, unit))
# 201820 = diabetes (A), 4024659 = gestational (B), procedure 4000001 with source 4000002 (S) or 0
cond(1, 201820, "2019-01-01"); cond(1, 201820, "2020-01-01"); proc(1, 4000001, 4000002, "2019-06-01")
cond(2, 201820, "2020-01-01"); meas(2, 8554, "2019-12-01")
cond(3, 4024659, "2019-01-01"); cond(3, 201820, "2020-06-01")
cond(4, 201820, "2018-01-01"); cond(4, 201820, "2018-02-01"); cond(4, 201820, "2021-01-01"); meas(4, 8753, "2018-01-15")
proc(5, 4000001, 0, "2019-01-01")
cond(6, 201820, "2019-03-01"); cond(6, 4024659, "2019-03-10"); proc(6, 4000001, 4000002, "2019-03-05")
cond(7, 201820, "2019-05-01"); proc(7, 4000001, 0, "2019-05-20"); meas(7, 8554, "2019-05-02")
cond(8, 4024659, "2019-02-01"); proc(8, 4000001, 4000002, "2019-02-10")
cond(9, 201820, "2018-01-01"); cond(9, 201820, "2019-08-05"); cond(9, 4024659, "2019-08-01"); proc(9, 4000001, 0, "2019-08-20")
meas(10, 8753, "2019-01-01"); meas(10, 8554, "2019-02-01")
for (r in list(c(201820, "Diabetes", "Condition"), c(4024659, "Gestational", "Condition"), c(4000001, "Proc", "Procedure"), c(4000002, "Proc src", "Procedure"),
               c(3004410, "HbA1c", "Measurement"), c(8554, "percent", "Unit"), c(8753, "mmol/mol", "Unit")))
  x(sprintf("INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept, concept_code, valid_start_date, valid_end_date) VALUES (%s,'%s','%s','V','C','S','c%s',DATE '1970-01-01',DATE '2099-12-31')", r[1], r[2], r[3], r[1]))
DBI::dbDisconnect(con, shutdown = TRUE)

cs <- function(id, ...) list(id = id, name = paste0("cs", id), expression = list(items = lapply(c(...), function(cid) list(
  concept = list(CONCEPT_ID = cid, CONCEPT_NAME = "c", DOMAIN_ID = "X", VOCABULARY_ID = "V", CONCEPT_CLASS_ID = "C", STANDARD_CONCEPT = "S", CONCEPT_CODE = "c"),
  isExcluded = FALSE, includeDescendants = FALSE, includeMapped = FALSE))))
sets <- list(cs(0, 201820), cs(1, 4024659), cs(2, 4000001), cs(3, 4000002), cs(4, 3004410))
win <- function(s, e) list(Start = list(Days = abs(s), Coeff = if (s < 0) -1 else 1), End = list(Days = abs(e), Coeff = if (e < 0) -1 else 1))
wrap <- function(crit, s, e, type = 2, count = 1) list(Criteria = crit, StartWindow = win(s, e), Occurrence = list(Type = type, Count = count))
rule <- function(...) list(name = "r", expression = list(Type = "ALL", CriteriaList = list(), Groups = list(), DemographicCriteriaList = list(), ...))
base <- function(primary, rules = list(), limit = "All", extra = list()) c(list(
  ConceptSets = sets, PrimaryCriteria = list(CriteriaList = list(primary), ObservationWindow = list(PriorDays = 0, PostDays = 0), PrimaryCriteriaLimit = list(Type = limit)),
  QualifiedLimit = list(Type = "First"), ExpressionLimit = list(Type = "All"), InclusionRules = rules,
  EndStrategy = list(DateOffset = list(DateField = "StartDate", Offset = 0)), CensoringCriteria = list(),
  CollapseSettings = list(CollapseType = "ERA", EraPad = 0), CensorWindow = structure(list(), names = character(0)), cdmVersionRange = ">=5.3.0"), extra)
A <- list(ConditionOccurrence = list(CodesetId = 0)); B <- list(ConditionOccurrence = list(CodesetId = 1)); P <- list(ProcedureOccurrence = list(CodesetId = 2))
grp <- function(type, crit, groups = list(), count = NULL) { g <- list(Type = type, CriteriaList = crit, Groups = groups, DemographicCriteriaList = list()); if (!is.null(count)) g$Count <- count; g }
defs <- list(
  first_primary = base(list(ConditionOccurrence = list(CodesetId = 0, First = TRUE))),
  first_in_rule = base(A, list(list(name = "r", expression = grp("ALL", list(wrap(list(ConditionOccurrence = list(CodesetId = 1, First = TRUE)), -365, 365)))))),
  first_in_rule_a = base(B, list(list(name = "r", expression = grp("ALL", list(wrap(list(ConditionOccurrence = list(CodesetId = 0, First = TRUE)), -30, 30)))))),
  additional = base(A, extra = list(AdditionalCriteria = grp("ALL", list(wrap(P, 0, 60))))),
  additional_limit = base(A, limit = "First", extra = list(AdditionalCriteria = grp("ALL", list(wrap(P, 0, 60))))),
  src_concept = base(list(ProcedureOccurrence = list(CodesetId = 2, ProcedureSourceConcept = 3))),
  src_concept_only = base(list(ProcedureOccurrence = list(ProcedureSourceConcept = 3))),
  first_window = base(B, list(list(name = "r", expression = grp("ALL", list(wrap(list(ConditionOccurrence = list(CodesetId = 0, First = TRUE)), -30, 30)))))),
  first_attr = base(list(Measurement = list(CodesetId = 4, Unit = list(list(CONCEPT_ID = 8554, CONCEPT_NAME = "percent")), First = TRUE))),
  unit = base(list(Measurement = list(CodesetId = 4, Unit = list(list(CONCEPT_ID = 8554, CONCEPT_NAME = "percent"))))),
  at_least_2 = base(A, list(list(name = "r", expression = grp("AT_LEAST", list(wrap(B, -30, 30), wrap(P, -30, 30), wrap(list(Measurement = list(CodesetId = 4)), -30, 30)), count = 2)))),
  at_most_1 = base(A, list(list(name = "r", expression = grp("AT_MOST", list(wrap(B, -30, 30), wrap(P, -30, 30), wrap(list(Measurement = list(CodesetId = 4)), -30, 30)), count = 1)))),
  any_group = base(A, list(list(name = "r", expression = grp("ANY", list(wrap(B, -30, 30), wrap(P, -30, 30)))))),
  nested = base(A, list(list(name = "r", expression = grp("ALL", list(wrap(P, -60, 60)), groups = list(grp("ANY", list(wrap(B, -30, 30), wrap(list(Measurement = list(CodesetId = 4)), -30, 30)))))))),
  nested_at_least = base(A, list(list(name = "r", expression = grp("ANY", list(wrap(B, -30, 30)), groups = list(grp("AT_LEAST", list(wrap(P, -60, 60), wrap(list(Measurement = list(CodesetId = 4)), -60, 60)), count = 2))))))
)
cd <- DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db)
set1 <- cohort_definition_set(cohort_definitions_dir())[1, ]
rows <- list(); i <- 100L
for (nm in names(defs)) {
  cj <- as.character(jsonlite::toJSON(defs[[nm]], auto_unbox = TRUE, null = "null"))
  csql <- tryCatch(CirceR::buildCohortQuery(CirceR::cohortExpressionFromJson(cj), CirceR::createGenerateOptions(generateStats = FALSE)), error = function(e) NA_character_)
  if (is.na(csql)) { cat(nm, ": CirceR could not build\n"); next }
  r <- set1; r$cohortId <- i; r$cohortName <- nm; r$json <- cj; r$sql <- csql
  rows[[nm]] <- r; i <- i + 1L
}
cdefs <- do.call(rbind, rows)
tables <- CohortGenerator::getCohortTableNames("cohort_cg")
conn <- DatabaseConnector::connect(cd)
CohortGenerator::createCohortTables(connection = conn, cohortDatabaseSchema = "main", cohortTableNames = tables)
DatabaseConnector::disconnect(conn)
suppressMessages(CohortGenerator::generateCohortSet(connectionDetails = cd, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main",
                                                    cohortTableNames = tables, cohortDefinitionSet = cdefs, stopOnError = FALSE, incremental = FALSE))
conn <- DatabaseConnector::connect(cd)
off <- DatabaseConnector::querySql(conn, "SELECT cohort_definition_id, subject_id, cohort_start_date FROM cohort_cg ORDER BY 1, 2, 3")
DatabaseConnector::disconnect(conn)
names(off) <- tolower(names(off))
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db)
fmt <- function(df) paste(sprintf("%d@%s", df$subject_id, substr(format(as.Date(df$cohort_start_date)), 3, 10)), collapse = " ")
for (k in seq_len(nrow(cdefs))) {
  nm <- cdefs$cohortName[[k]]
  o <- off[off$cohort_definition_id == cdefs$cohortId[[k]], ]
  n <- tryCatch(DBI::dbGetQuery(con, suppressWarnings(compile_circe_to_duckdb(defs[[nm]], cdm_schema = "main"))), error = function(e) paste("refused:", substr(conditionMessage(e), 1, 60)))
  nat <- if (is.character(n)) n else fmt(n)
  cat(sprintf("%-16s official: %s\n%-16s native:   %s %s\n", nm, fmt(o), "", nat, if (!is.character(n)) { if (identical(sort(sprintf("%d@%s", o$subject_id, o$cohort_start_date)), sort(sprintf("%d@%s", n$subject_id, n$cohort_start_date)))) "[same]" else "[DIFFERENT]" } else ""))
}
DBI::dbDisconnect(con, shutdown = TRUE)
