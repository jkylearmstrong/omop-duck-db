# Runs real OHDSI PhenotypeLibrary cohort definitions on a synthetic DuckDB CDM through the official engine (CirceR +
# CohortGenerator) and through the package's native Circe compiler, and compares the cohorts person by person and date by date.
#
# Needs: the vocabulary DuckDB (read-only; set OMOP_VOCAB_DUCKDB), CirceR (Java), CohortGenerator, DatabaseConnector,
# PhenotypeLibrary, omopduckdb. The 7 GB vocabulary is not in the repository, so this is a script and not a CI test.
#
#   Rscript scripts/ohdsi_parity_phenotypelibrary.R [cohortId ...]
#
# Output: one row per definition with the people and rows each engine returned and whether they are identical.
suppressMessages({library(omopduckdb); library(PhenotypeLibrary)})
vocab <- Sys.getenv("OMOP_VOCAB_DUCKDB", "tasks/central_vocabulary.duckdb")
stopifnot(file.exists(vocab))
args <- as.integer(commandArgs(trailingOnly = TRUE))
set.seed(20260428)

# 1. which library definitions can the native compiler evaluate at all? (it refuses the rest instead of returning a wrong cohort)
log <- PhenotypeLibrary::getPhenotypeLog()
all_ids <- if (length(args)) args else as.integer(log$cohortId)
all_defs <- PhenotypeLibrary::getPlCohortDefinitionSet(cohortIds = all_ids)
verdict <- vapply(seq_len(nrow(all_defs)), function(i) {
  tryCatch({ suppressWarnings(compile_circe_to_duckdb(all_defs$json[[i]])); "supported" },
           error = function(e) {
             m <- conditionMessage(e)
             if (grepl("AdditionalCriteria", m)) "AdditionalCriteria"
             else if (grepl("includeMapped", m)) "concept set includeMapped"
             else if (grepl("nested criteria groups", m)) "nested criteria groups"
             else if (grepl("attribute(s)", m, fixed = TRUE)) paste("criterion attribute:", sub(" are not supported.*", "", strsplit(m, "attribute(s) ", fixed = TRUE)[[1]][2]))
             else substr(m, 1, 70)
           })
}, character(1))
cat(sprintf("
%d of %d library definitions are evaluated by the native compiler; the others are refused:
", sum(verdict == "supported"), length(verdict)))
print(sort(table(verdict[verdict != "supported"]), decreasing = TRUE))
max_run <- as.integer(Sys.getenv("PARITY_MAX", "40"))
defs <- all_defs[verdict == "supported", ]
if (nrow(defs) > max_run) defs <- defs[sample(nrow(defs), max_run), ]
if (!nrow(defs)) stop("no supported definition to run")
db_path <- tempfile(fileext = ".duckdb")
build_schema(db_path)
con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
DBI::dbExecute(con, sprintf("ATTACH '%s' AS vocab (READ_ONLY)", normalizePath(vocab, winslash = "/")))

# every concept a definition names, with its descendants
items <- unique(unlist(lapply(defs$json, function(j) {
  d <- jsonlite::fromJSON(j, simplifyVector = FALSE)
  unlist(lapply(d$ConceptSets, function(s) vapply(s$expression$items, function(i) as.numeric(i$concept$CONCEPT_ID), numeric(1))))
})))
DBI::dbExecute(con, sprintf("CREATE TEMP TABLE _items AS SELECT unnest([%s]) AS id", paste(sprintf("%.0f", items), collapse = ",")))
DBI::dbExecute(con, "INSERT INTO concept_ancestor SELECT ca.* FROM vocab.concept_ancestor ca WHERE ca.ancestor_concept_id IN (SELECT id FROM _items)")
DBI::dbExecute(con, "INSERT INTO concept SELECT c.* FROM vocab.concept c WHERE c.concept_id IN (SELECT descendant_concept_id FROM concept_ancestor UNION SELECT id FROM _items)")

# a reproducible population: ages, observation periods, visits, and a random subset of each definition's concepts as events
n <- 2000L
person <- data.frame(person_id = seq_len(n), gender_concept_id = sample(c(8507L, 8532L), n, TRUE),
                     year_of_birth = sample(1935:2005, n, TRUE), race_concept_id = 0L, ethnicity_concept_id = 0L)
duckdb::duckdb_register(con, "_p", person)
DBI::dbExecute(con, "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id) SELECT * FROM _p")
DBI::dbExecute(con, "INSERT INTO observation_period SELECT person_id, person_id, DATE '2008-01-01', DATE '2023-12-31', 32817 FROM _p")
rand_date <- function(k) as.Date("2009-01-01") + sample(0:5000, k, TRUE)
pick <- function(domain, k) {
  pool <- DBI::dbGetQuery(con, sprintf("SELECT concept_id FROM concept WHERE domain_id = '%s'", domain))$concept_id
  if (!length(pool)) return(NULL)
  data.frame(person_id = sample(n, k, TRUE), concept_id = sample(pool, k, TRUE), d = rand_date(k))
}
cond <- pick("Condition", 30000); drug <- pick("Drug", 30000); meas <- pick("Measurement", 30000)
if (!is.null(meas)) meas$v <- round(runif(nrow(meas), 4, 11), 1)
vis <- data.frame(person_id = sample(n, 6000, TRUE), concept_id = sample(c(9201L, 9202L, 9203L), 6000, TRUE), d = rand_date(6000))
ins <- function(df, sql) { if (is.null(df)) return(invisible()); nm <- paste0("_", sample(letters, 1), sample(1e6, 1)); duckdb::duckdb_register(con, nm, df); DBI::dbExecute(con, gsub("@t", nm, sql, fixed = TRUE)); duckdb::duckdb_unregister(con, nm) }
obsv <- pick("Observation", 30000); dev <- pick("Device", 20000)
ins(obsv, "INSERT INTO observation (observation_id, person_id, observation_concept_id, observation_date, observation_datetime, observation_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817 FROM @t")
ins(dev, "INSERT INTO device_exposure (device_exposure_id, person_id, device_concept_id, device_exposure_start_date, device_exposure_start_datetime, device_exposure_end_date, device_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), d + 5, 32817 FROM @t")
ins(cond, "INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id, condition_start_date, condition_start_datetime, condition_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817 FROM @t")
ins(drug, "INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date, drug_exposure_start_datetime, drug_exposure_end_date, drug_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), d + 30, 32817 FROM @t")
ins(meas, "INSERT INTO measurement (measurement_id, person_id, measurement_concept_id, measurement_date, measurement_datetime, measurement_type_concept_id, value_as_number) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), 32817, v FROM @t")
ins(vis, "INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date, visit_start_datetime, visit_end_date, visit_type_concept_id) SELECT row_number() OVER (), person_id, concept_id, d, CAST(d AS TIMESTAMP), d, 32817 FROM @t")

native <- lapply(seq_len(nrow(defs)), function(i) {
  r <- tryCatch(execute_circe_cohort(con, defs$json[[i]], target_cohort_id = defs$cohortId[[i]], cohort_table = "native_cohort"), error = function(e) conditionMessage(e))
  r
})
DBI::dbExecute(con, "CHECKPOINT")
DBI::dbDisconnect(con, shutdown = TRUE)

cd <- DatabaseConnector::createConnectionDetails(dbms = "duckdb", server = db_path)
tables <- CohortGenerator::getCohortTableNames("cohort_cg")
conn <- DatabaseConnector::connect(cd)
CohortGenerator::createCohortTables(connection = conn, cohortDatabaseSchema = "main", cohortTableNames = tables)
DatabaseConnector::disconnect(conn)
suppressMessages(CohortGenerator::generateCohortSet(connectionDetails = cd, cdmDatabaseSchema = "main", cohortDatabaseSchema = "main",
                                                    cohortTableNames = tables, cohortDefinitionSet = defs))
conn <- DatabaseConnector::connect(cd)
official <- DatabaseConnector::querySql(conn, "SELECT cohort_definition_id, subject_id, cohort_start_date, cohort_end_date FROM cohort_cg")
DatabaseConnector::disconnect(conn)
names(official) <- tolower(names(official))

rows <- lapply(seq_len(nrow(defs)), function(i) {
  o <- official[official$cohort_definition_id == defs$cohortId[[i]], ]
  nat <- native[[i]]
  nm <- substr(defs$cohortName[[i]], 1, 40)
  if (is.character(nat)) {
    return(data.frame(cohortId = defs$cohortId[[i]], name = nm, official_people = length(unique(o$subject_id)), native_people = NA,
                      same_people = NA, same_start_dates = NA, native_says = substr(nat, 1, 60)))
  }
  # the native compiler returns the entry event's end date and does not collapse eras, so compare people and start dates
  key <- function(x) sort(unique(paste(x$subject_id, as.Date(x$cohort_start_date))))
  data.frame(cohortId = defs$cohortId[[i]], name = nm, official_people = length(unique(o$subject_id)),
             native_people = length(unique(nat$subject_id)), same_people = setequal(o$subject_id, nat$subject_id),
             same_start_dates = identical(key(o), key(nat)), native_says = "")
})
parity_result <- do.call(rbind, rows)
saveRDS(list(result = parity_result, verdict = setNames(verdict, all_defs$cohortId)), Sys.getenv("PARITY_OUT", tempfile(fileext = ".rds")))
print(parity_result[!parity_result$same_people | !parity_result$same_start_dates, ], row.names = FALSE)
cat(sprintf("
Ran %d supported definitions: same people in %d, same people and start dates in %d.
", nrow(parity_result),
            sum(parity_result$same_people), sum(parity_result$same_people & parity_result$same_start_dates)))
unlink(db_path)
