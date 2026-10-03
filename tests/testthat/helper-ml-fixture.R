# Shared fixture for the ML-feature / HADES-bridge tests. Mirrors tests/conftest.py (Python) record for
# record, so R and Python are checked against the same hand-derived expectations.
#
# Each record targets a failure mode: an event ON the index date, one AFTER it (leakage), one older than
# 365 days, concept 0 (collides with the PAD token downstream), a record with no visit id, a patient with
# two index rows, a patient with no events, and a cohort file deliberately not sorted by person.

make_ml_fixture <- function(env = parent.frame()) {
  db_path <- tempfile(fileext = ".duckdb")
  omopduckdb::build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  withr::defer({
    if (DBI::dbIsValid(con)) DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, envir = env)

  DBI::dbExecute(con, "
    INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, ethnicity_concept_id)
    VALUES (1, 8507, 1970, 8527, 38003564), (2, 8532, 1970, 8527, 38003564), (3, 8507, 1970, 8527, 38003564)")
  DBI::dbExecute(con, "
    INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
                         standard_concept, concept_code, valid_start_date, valid_end_date)
    VALUES (201, 'Type 2 diabetes', 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'X201', DATE '1970-01-01', DATE '2099-12-31'),
           (301, 'Metformin',       'Condition', 'SNOMED', 'Clinical Finding', 'S', 'X301', DATE '1970-01-01', DATE '2099-12-31')")
  DBI::dbExecute(con, "
    INSERT INTO visit_occurrence (visit_occurrence_id, person_id, visit_concept_id, visit_start_date,
                                  visit_end_date, visit_type_concept_id)
    VALUES (11, 1, 9202, DATE '2021-05-20', DATE '2021-05-20', 32827),
           (12, 1, 9202, DATE '2021-05-25', DATE '2021-05-25', 32827),
           (21, 2, 9202, DATE '2021-05-30', DATE '2021-05-30', 32827),
           (13, 1, 9202, DATE '2021-06-10', DATE '2021-06-10', 32827)")
  DBI::dbExecute(con, "
    INSERT INTO condition_occurrence (condition_occurrence_id, person_id, condition_concept_id,
                                      condition_start_date, condition_type_concept_id, visit_occurrence_id)
    VALUES (1, 1, 201, DATE '2021-05-20', 32020, 11),
           (2, 1, 201, DATE '2021-05-25', 32020, 12),
           (3, 1, 202, DATE '2021-06-01', 32020, 12),   -- ON index date of row 1
           (4, 1, 203, DATE '2021-06-10', 32020, 13),   -- AFTER index of row 1 (leakage)
           (5, 1, 204, DATE '2020-01-01', 32020, 11),   -- older than 365 days
           (6, 1, 0,   DATE '2021-05-01', 32020, 11),   -- concept 0
           (7, 2, 201, DATE '2021-05-30', 32020, 21),
           (8, 2, 205, DATE '2021-05-10', 32020, NULL)  -- no visit id")
  DBI::dbExecute(con, "
    INSERT INTO drug_exposure (drug_exposure_id, person_id, drug_concept_id, drug_exposure_start_date,
                               drug_exposure_end_date, drug_type_concept_id, visit_occurrence_id)
    VALUES (1, 1, 301, DATE '2021-05-25', DATE '2021-05-25', 32838, 12),
           (2, 2, 301, DATE '2021-05-30', DATE '2021-05-30', 32838, 21)")
  DBI::dbExecute(con, "
    INSERT INTO procedure_occurrence (procedure_occurrence_id, person_id, procedure_concept_id,
                                      procedure_date, procedure_type_concept_id, visit_occurrence_id)
    VALUES (1, 1, 401, DATE '2021-05-22', 32817, 11)")

  parquet <- tempfile(fileext = ".parquet")
  write_cohort_parquet(con, parquet, "
    SELECT * FROM (VALUES
      (2, DATE '2021-06-01', 1, 0.1), (1, DATE '2021-06-01', 0, 0.2),
      (3, DATE '2021-06-01', 0, 0.3), (1, DATE '2021-09-01', 1, 0.4)
    ) t(subject_id, cohort_start_date, outcome_flag, extra_feature)")
  withr::defer(unlink(parquet), envir = env)

  list(con = con, parquet = parquet, db_path = db_path)
}

write_cohort_parquet <- function(con, path, select_sql) {
  DBI::dbExecute(con, sprintf("COPY (%s) TO '%s' (FORMAT PARQUET)", select_sql, gsub("\\\\", "/", path)))
  invisible(path)
}

# What HADES needs on top of the clinical tables: observation periods and an OHDSI cohort table. The target
# cohort (id 1) mirrors the parquet rows [person 2 @ 6/1, person 1 @ 6/1, person 3 @ 6/1, person 1 @ 9/1];
# outcome cohort (id 2) holds the positive labels (persons 2 and 1) 19 days after their index.
seed_hades_tables <- function(con) {
  omopduckdb::ensure_cohort_tables(con)
  DBI::dbExecute(con, "
    INSERT INTO observation_period (observation_period_id, person_id, observation_period_start_date,
                                    observation_period_end_date, period_type_concept_id)
    VALUES (1, 1, DATE '2018-01-01', DATE '2022-12-31', 32817), (2, 2, DATE '2018-01-01', DATE '2022-12-31', 32817),
           (3, 3, DATE '2018-01-01', DATE '2022-12-31', 32817)")
  DBI::dbExecute(con, "
    INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    VALUES (1, 2, DATE '2021-06-01', DATE '2021-06-04'), (1, 1, DATE '2021-06-01', DATE '2021-06-05'),
           (1, 3, DATE '2021-06-01', DATE '2021-06-03'), (1, 1, DATE '2021-09-01', DATE '2021-09-03'),
           (2, 2, DATE '2021-06-20', DATE '2021-06-20'), (2, 1, DATE '2021-09-20', DATE '2021-09-20')")
  invisible(con)
}

# 400 patients, all indexed 2021-06-01. Concept 201 (a condition 30 days before index) drives the outcome; ten
# drugs (301..310) are noise.
synthetic_cdm <- function(env = parent.frame(), n = 400) {
  db_path <- tempfile(fileext = ".duckdb")
  omopduckdb::build_schema(db_path)
  con <- DBI::dbConnect(duckdb::duckdb(), dbdir = db_path)
  withr::defer({
    if (DBI::dbIsValid(con)) DBI::dbDisconnect(con, shutdown = TRUE)
    unlink(db_path)
  }, envir = env)
  set.seed(42)
  has201 <- runif(n) < 0.5
  y <- as.integer(runif(n) < ifelse(has201, 0.85, 0.12))

  person <- data.frame(person_id = seq_len(n), gender_concept_id = 8507L, year_of_birth = 1960L,
                       race_concept_id = 8527L, ethnicity_concept_id = 38003564L)
  obs <- data.frame(observation_period_id = seq_len(n), person_id = seq_len(n),
                    observation_period_start_date = as.Date("2018-01-01"),
                    observation_period_end_date = as.Date("2022-12-31"), period_type_concept_id = 32817L)
  cond <- data.frame(condition_occurrence_id = seq_len(sum(has201)), person_id = which(has201),
                     condition_concept_id = 201L, condition_start_date = as.Date("2021-05-02"),
                     condition_type_concept_id = 32020L)
  noise <- data.frame(person_id = rep(seq_len(n), each = 2), drug_concept_id = sample(301:310, 2 * n, replace = TRUE))
  noise$drug_exposure_id <- seq_len(nrow(noise))
  noise$drug_exposure_start_date <- as.Date("2021-05-10")
  noise$drug_exposure_end_date <- as.Date("2021-05-10")
  noise$drug_type_concept_id <- 32838L

  # a real CDM always has its vocabulary; hades_preflight() rightly rejects an empty `concept` table
  DBI::dbExecute(con, "
    INSERT INTO concept (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, standard_concept,
                         concept_code, valid_start_date, valid_end_date)
    SELECT i, 'concept ' || i, 'Condition', 'SNOMED', 'Clinical Finding', 'S', 'C' || i,
           DATE '1970-01-01', DATE '2099-12-31'
    FROM (SELECT unnest([201, 301, 302, 303, 304, 305, 306, 307, 308, 309, 310]) AS i)")
  for (spec in list(list("person", person), list("observation_period", obs), list("condition_occurrence", cond),
                    list("drug_exposure", noise))) {
    duckdb::duckdb_register(con, "_syn", spec[[2]])
    cols <- paste(names(spec[[2]]), collapse = ", ")
    DBI::dbExecute(con, sprintf("INSERT INTO %s (%s) SELECT %s FROM _syn", spec[[1]], cols, cols))
    duckdb::duckdb_unregister(con, "_syn")
  }
  parquet <- tempfile(fileext = ".parquet")
  withr::defer(unlink(parquet), envir = env)
  duckdb::duckdb_register(con, "_coh", data.frame(subject_id = seq_len(n), cohort_start_date = as.Date("2021-06-01"), y = y))
  DBI::dbExecute(con, sprintf("COPY (SELECT * FROM _coh) TO '%s' (FORMAT PARQUET)", gsub("\\\\", "/", parquet)))
  duckdb::duckdb_unregister(con, "_coh")

  # OHDSI cohort table for the native HADES path: target = everyone, outcome = positives 10 days after index
  omopduckdb::ensure_cohort_tables(con)
  src <- gsub("\\\\", "/", parquet)
  DBI::dbExecute(con, sprintf("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT 1, subject_id, cohort_start_date, cohort_start_date + 3 FROM read_parquet('%s')", src))
  DBI::dbExecute(con, sprintf("INSERT INTO cohort (cohort_definition_id, subject_id, cohort_start_date, cohort_end_date)
    SELECT 2, subject_id, cohort_start_date + 10, cohort_start_date + 10 FROM read_parquet('%s') WHERE y = 1", src))
  list(con = con, parquet = parquet, db_path = db_path, y = y, has201 = has201)
}
